# herdr-bridge architecture

How the bridge is built and why. For installing and running it, see
[USER_GUIDE.md](USER_GUIDE.md). For the exact messages on the wire, see
[PROTOCOL.md](../PROTOCOL.md); this document does not repeat them.

## 1. Overview

The bridge is one Python process built on `asyncio` and `aiohttp`. It has three faces:

```
                         ┌──────────────────────── herdr-bridge ────────────────────────┐
 Herdr App  ◀─WebSocket─▶│ server.py        state.py            herdr_client.py         │◀─socket──▶ herdr
 (one or more)           │ ClientSession ◀─ StateTracker ─────▶ HerdrClient             │   (named pipe or
                         │      │                                                       │    Unix socket)
                         │      └─────────▶ terminal.py                                 │
                         │                  TerminalStream ──── subprocess ─────────────│──▶ herdr terminal
                         │                                                              │    session observe
                         └──────────────────────────────────────────────────────────────┘    or control
```

- **Towards the app:** one WebSocket per client, JSON text messages.
- **Towards herdr, requests and events:** herdr's local socket API, one JSON object per line.
- **Towards herdr, pane screens:** one `herdr terminal session` subprocess per open pane stream.

Everything runs on one event loop. There are no threads and no locks other than `asyncio` ones.

## 2. Modules

| Module | Responsibility |
|---|---|
| `cli.py` | Command line: parses arguments, sets up logging, wires the parts together. |
| `config.py` | The configuration file, token generation, finding the address to listen on. |
| `pairing.py` | Builds the pairing link and renders it as a QR code. |
| `listener.py` | Keeps the listening socket bound to an address that can disappear and return. |
| `server.py` | The WebSocket endpoint: authentication, one `ClientSession` per client, every request handler. |
| `state.py` | `StateTracker`: the cached copy of herdr's state and the logic that keeps it fresh. |
| `herdr_client.py` | `HerdrClient`: requests and event subscriptions on herdr's socket. |
| `terminal.py` | `TerminalStream`: one observe or control subprocess for one pane. |
| `gitdiff.py` | Runs `git` in a pane's folder. |
| `autostart.py` | Windows scheduled task for starting at logon. |

## 3. Start-up

`herdr-bridge serve` does this, in order:

1. `Config.load_or_init()` reads the configuration and creates the token if there is none.
2. `resolve_bind()` turns `bind` into an address. `"tailscale"` means the first local IPv4 address
   in `100.64.0.0/10`; if there is none, start-up fails with a message.
3. The herdr address is found: `herdr_address` from the configuration if set, otherwise the
   `HERDR_SOCKET_PATH` environment variable, otherwise the `socket:` line printed by
   `herdr status server`, otherwise the default location under herdr's configuration folder. On
   Windows the result is used as a named pipe, elsewhere as a Unix socket.
4. A `Bridge` object is created. It holds the `HerdrClient`, the `StateTracker`, the token, the
   name and the set of connected clients.
5. `listener.serve()` starts the `aiohttp` application. Its start-up hook starts the
   `StateTracker`.

## 4. Configuration and the token

`config.py` holds one `Config` dataclass. Loading reads the TOML file and copies every key it
knows; unknown keys are ignored. Saving writes all keys back and, outside Windows, sets the file
to mode `0600`.

The token comes from `secrets.token_urlsafe(32)`: 32 bytes from the operating system's random
source, written as about 43 URL-safe characters. It is generated in exactly two places:
`load_or_init()` when the file has no token, and the `rotate-token` command. It is derived from
nothing and stored only in the configuration file.

`rotate-token` only rewrites the file. A running bridge keeps the token it loaded at start, so the
old one stays valid until the bridge is restarted.

## 5. Listening on an address that comes and goes

A laptop's Wi-Fi address disappears when Wi-Fi is off. A plain bind would either fail at start or,
on Windows, succeed on a stale address of a disconnected adapter.

`listener.serve()` therefore polls `address_is_up()` every 5 seconds. That function asks `psutil`
whether the address is assigned to an interface that is currently up. Loopback, wildcard addresses
and host names always count as up.

- Address up and not listening: start a `TCPSite`.
- Address down and listening: stop the site and wait.

The application object, and with it the herdr connection and the cached state, lives for the whole
run. Only the listening socket follows the address.

## 6. Keeping herdr's state: `StateTracker`

herdr's API offers a full snapshot (`session.snapshot`) and a stream of events. The bridge does
not try to apply events one by one. Every event is treated as a sign that something changed, and
the bridge reads the whole snapshot again. This is simpler than mirroring herdr's rules, and it
cannot drift.

Three tasks run for the lifetime of the bridge:

| Task | What it does |
|---|---|
| Subscription loop | Pings herdr, takes a snapshot, subscribes to events, and marks the state dirty on every event. |
| Refresh loop | Waits for the dirty mark, waits 150 ms more so that a burst of events becomes one refresh, then refreshes. |
| Poll loop | Marks the state dirty every 5 seconds, as a safety net for missed events. |

### Subscriptions

The bridge subscribes to a fixed list of global events (workspaces, worktrees, tabs, panes,
layout) and to `pane.agent_status_changed` for every known pane. That last event is per pane, so
whenever the set of pane ids changes, the tracker asks the subscription loop to subscribe again
with the new list.

If herdr reports `events_lost`, the loop resynchronises by starting over. If the subscription
fails because herdr is down, the tracker reports herdr as unavailable and retries with a delay
that doubles from 1 second up to 15.

### A refresh

1. Read `session.snapshot`. On failure, report herdr unavailable.
2. Compare each agent's status with the previous snapshot to find transitions.
3. Update previews (below).
4. If the set of pane ids changed, ask for a new subscription.
5. Build the `snapshot` message. Send it to clients only if it differs from the last one sent.
6. Send one `agent_status` message per transition.

A transition is reported when an agent's status changed, and either the agent was already known or
its new status is `blocked`. Nothing is reported for the very first snapshot.

### Previews

A preview is the last three non-empty lines of an agent's pane, each cut to 200 characters. It is
read with `pane.read` (`recent_unwrapped`, 15 lines, without colour codes). A preview is read
again when the agent's status changed, when there is none yet, or when the agent is working and
the preview is more than 8 seconds old. At most four reads run at once.

### Pane sizes

`pane_sizes` in the snapshot message is taken from the `layouts` part of herdr's snapshot: each
pane's rectangle gives its real width and height in cells on the PC. Panes in tabs that are not
shown may be missing.

## 7. Talking to herdr: `HerdrClient`

herdr answers one request per connection and then closes it. `request()` therefore opens a
connection, writes one JSON line, reads one line and closes. On Windows the named pipe can be
busy; opening is retried up to 50 times, 20 ms apart.

`subscribe()` keeps its connection open and yields events until herdr closes it.

Errors from herdr become `HerdrError` with herdr's code and message. Two special cases exist:
`HerdrUnavailable` when herdr cannot be reached, and `EventsLost`.

## 8. Clients: `ClientSession`

### Authentication

`GET /ws` reads the token from the `Authorization: Bearer` header or, failing that, from the
`token` query parameter. It is compared with `hmac.compare_digest`, so the time taken does not
reveal how many characters matched. A mismatch returns HTTP 401 before the WebSocket is opened. An
empty configured token never matches.

`GET /health` needs no token and returns the protocol version and the bridge's name.

### Life of a connection

1. The session is created and a writer task is started.
2. `hello` is queued, then the current `snapshot` if there is one.
3. The session is added to the bridge's client set, so it receives broadcasts.
4. Incoming messages are handled until the socket closes.
5. On close, the session is removed, every stream it opened is stopped, and the writer ends.

### Sending

Each session has an unbounded queue and one writer task. Handlers and broadcasts only put messages
on the queue, so a slow phone never blocks the rest of the bridge. A connected client never misses
a message. If more than 5000 messages are waiting, the client is disconnected instead: terminal
frames build on each other, so a gap would corrupt the screen, while a reconnect starts from a
full frame. The waiting messages are thrown away at that point and the socket is closed at once,
without first sending what the client was too slow to read.

### Receiving

Each message is a JSON object with a `type`. The handler is the method named `op_<type>`.

- `input` and `scroll` are handled inline, one after the other, so keystrokes keep their order.
- Every other message is started as its own task, so a slow herdr call does not hold up typing.

If the message has an `id`, exactly one `result` is sent back. Failures map to an error code:
herdr's own code, or `bad_request`, `unknown_type`, `not_controlling`, `not_allowed`, `stream_failed`,
`git_failed`, `internal`.

### Passing calls through

`call` forwards a herdr method with its parameters and returns herdr's result unchanged. After
every call the state is marked dirty, so the effect shows up in the next snapshot.

Only the methods in `ALLOWED_CALLS` (`server.py`) are forwarded: the ones the Herdr App uses to
read a pane, send text and keys, and rename, close and create panes, tabs and workspaces. Any
other method is refused with `not_allowed`. herdr's API is much wider, and it can start programs
and stop the server, so a leaked token should not reach all of it.

## 9. Pane streams: `TerminalStream`

herdr shows a pane's screen through a command, not through the socket:

```
herdr terminal session observe <pane> --cols N --rows M
herdr terminal session control <pane> --cols N --rows M [--takeover]
```

The command prints JSON lines: `terminal.frame` with the screen content, and `terminal.closed`.
In control mode it reads commands on standard input: `terminal.input`, `terminal.resize`,
`terminal.scroll` and `terminal.release`.

One `TerminalStream` wraps one such process.

- **Start:** the process is spawned without a console window. Start waits up to 8 seconds for the
  first frame. If the process exits first or the time runs out, it is stopped and the last lines
  of its error output become the failure reason.
- **Frames:** each frame is passed to the session, which forwards it as a `frame` message with
  herdr's `seq`, `full`, `width`, `height` and `bytes` untouched.
- **End:** if the process exits by itself, the session removes the stream and tells the client
  the stream is closed, with the reason.
- **Stop:** in control mode a `terminal.release` is sent first and the process is given 2 seconds
  to exit; then it is killed.

A session holds at most one stream per pane. Changing mode means stopping the process and starting
another. A lock per pane keeps open, close, take control, release and resize from overlapping.

### Observe uses the PC's size

herdr crops observe frames to the size asked for; it does not rewrap them. A request at the
phone's size would cut off the right side of the pane. The bridge therefore observes at the pane's
real size from `pane_sizes`, and uses the client's size only when the real one is unknown. The app
scales the result to fit its screen.

After every snapshot broadcast, each session checks its observe streams against the current pane
sizes and restarts any whose pane was resized on the PC. If the restart fails, the pane has no
stream left and the client is told that the stream is closed, with the reason.

### Control uses the client's size

Control mode resizes the real terminal to the size the client asks for, because the client is now
the one typing. A `resize` in control mode is sent to herdr; in observe mode it only matters when
the PC size is unknown.

If taking control fails, the bridge falls back to an observe stream, so the user still sees the
pane, and then reports the failure. If the fallback fails too, the stream is reported as closed.

## 10. Git diff

`diff` finds the pane's folder (`foreground_cwd`, else `cwd`) in the cached snapshot, or asks
herdr if the pane is not cached. It then runs, with a 30 second limit each: `git rev-parse` for
the repository root and the branch, `git diff --stat`, `git diff --no-color`, and, unless staged
changes were asked for, `git ls-files --others --exclude-standard`. The diff text is cut at 2 MiB
and the list of untracked files at 500 entries.

## 11. Limits and constants

| Constant | Value | Where |
|---|---|---|
| Protocol version | 1 | `server.py` |
| Default port | 8787 | `config.py` |
| WebSocket heartbeat | 20 s | `server.py` |
| Largest incoming message | 8 MiB | `server.py` |
| Queue length that disconnects a client | 5000 messages | `server.py` |
| Default observe size | 80 × 40 | `server.py` |
| Stream size limits | 2 to 1000 cells each way | `terminal.py` |
| Wait for a stream's first frame | 8 s | `terminal.py` |
| Scroll per request | 1 to 1000 lines | `server.py` |
| herdr request timeout | 15 s | `herdr_client.py` |
| Refresh debounce | 150 ms | `state.py` |
| Safety poll | 5 s | `state.py` |
| Address check interval | 5 s | `listener.py` |

## 12. Security model

- **Trust boundary:** the WebSocket. herdr's socket has no authentication, so the bridge's token
  check is the only control.
- **One level of access:** a valid token grants everything the bridge offers. There are no read-only clients and no
  per-pane permissions.
- **No transport encryption:** the bridge serves plain `ws://`. Confidentiality comes from the
  network: Tailscale encrypts, a LAN does not.
- **Default exposure:** listening only on the Tailscale address keeps the port off the LAN and the
  internet.
- **The token on disk:** in the configuration file, readable by the user account. On Windows no
  extra file permissions are set.
- **Logging:** the token is never logged, and `config` prints it as `<hidden>`. The `pair` command
  prints it, as part of the pairing link, on purpose.

## 13. Tests

`uv run pytest` runs everything against a fake herdr: a small server that speaks the socket API
over a pipe or TCP (`tests/fake_herdr.py`) and a fake `herdr` command for streams
(`tests/fake_herdr_cli.py`). No real herdr is needed.

| File | Covers |
|---|---|
| `test_client.py` | Requests, errors and subscriptions in `HerdrClient`. |
| `test_state.py` | Snapshots, transitions, previews and resubscription in `StateTracker`. |
| `test_server.py` | Authentication, every request type, streams and control over a real WebSocket. |
| `test_listener.py` | Rebinding when the address goes away and returns. |
| `test_config_pairing.py` | Configuration, the token and the pairing link. |

## 14. Known gaps

- No TLS. A leaked LAN capture reveals the token.
- No per-client identity: clients cannot be told apart or revoked one by one.
- `rotate-token` needs a restart to take effect.
- After control is released, the PC pane keeps the phone's size until something on the PC redraws
  it.
- Only Windows is tested.
