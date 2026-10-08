# herdr-bridge

A small PC-side daemon that lets the **Herdr App** (Android) drive your
[herdr](https://herdr.dev) agents from your phone.

```
phone (Herdr App) ──WebSocket + token──▶ herdr-bridge ──named pipe / CLI──▶ herdr
                       over Tailscale        (this repo, on your PC)
```

The bridge:

- keeps a live cache of herdr's state (workspaces, tabs, panes, agents and their
  `idle/working/blocked/done` status) by subscribing to herdr's socket API, and pushes
  snapshots, status transitions and last-line previews to the app;
- streams any pane as ANSI frames by running `herdr terminal session observe|control`, so the
  app can render it in a real terminal emulator. Panes open read-only, and **take control**
  switches to input mode;
- passes a fixed set of herdr API calls through (send text and keys, rename, close and create
  panes, tabs and workspaces)
  and serves `git diff` for a pane's working directory.

Using it day to day is covered in the [user guide](docs/USER_GUIDE.md), how it works inside in
[ARCHITECTURE.md](docs/ARCHITECTURE.md), and the wire protocol in [PROTOCOL.md](PROTOCOL.md).

## Requirements

- herdr **0.8.0-preview or newer** running natively on Windows (socket protocol 19 tested).
  Linux and macOS should also work (Unix socket), but are untested.
- Python 3.12+ and [uv](https://docs.astral.sh/uv/) (`pip install uv`, then use `uv` or `python -m uv`).
- [Tailscale](https://tailscale.com) on both the PC and the phone, **or** a LAN address the phone
  can reach (e.g. a static Wi-Fi IP, possibly via a Tailscale subnet router elsewhere on that LAN).
- `git` on PATH for the diff feature.

## Setup

```powershell
git clone https://github.com/<you>/herdr-bridge
cd herdr-bridge
uv sync

uv run herdr-bridge check      # verifies herdr is reachable and prints its version
uv run herdr-bridge pair       # creates the config + token and prints the pairing QR code
uv run herdr-bridge serve      # run in the foreground (Ctrl+C to stop)
```

Open the Herdr App, tap **Add host** and scan the QR code.

### Run hidden at logon (Windows)

```powershell
uv run herdr-bridge install-task    # Scheduled Task, runs pythonw (no console window); no admin needed
uv run herdr-bridge task-status
uv run herdr-bridge uninstall-task
```

Logs are written to `%APPDATA%\herdr-bridge\bridge.log`.

## Configuration

`%APPDATA%\herdr-bridge\config.toml` (override the path with `HERDR_BRIDGE_CONFIG`).
`uv run herdr-bridge config` shows the current values.

| key | default | meaning |
|---|---|---|
| `token` | generated | bearer token the app must send |
| `bind` | `"tailscale"` | `"tailscale"` = this PC's Tailscale IPv4 only. Or an explicit address such as a static LAN IP (`192.168.1.20`) or `127.0.0.1`. If the address isn't up (e.g. Wi-Fi disconnected), the bridge waits for it and re-listens whenever it drops and comes back |
| `port` | `8787` | WebSocket port |
| `name` | hostname | name shown in the app |
| `herdr_cmd` | `["herdr"]` | how to run the herdr CLI (use the full path to `herdr.exe` if it isn't on PATH for the task) |
| `herdr_session` | `""` | named herdr session (`herdr --session NAME`); empty = default |
| `herdr_address` | `""` | override the socket: `pipe:<path>`, `unix:<path>` or `tcp:host:port` |
| `advertise_host` | `""` | host put into the QR code, e.g. a MagicDNS name |

`serve` flags override the file: `--bind`, `--port`, `--session`, `-v`. `--qr` also prints the
pairing code on start.
`uv run herdr-bridge rotate-token` invalidates every paired phone.

## Security

herdr's local socket has **no authentication**: anything that can talk to it can type into
your agents' terminals. The bridge is therefore the only gate:

- By default it binds **only** to the Tailscale interface, so it is unreachable from your LAN or
  the internet. Don't bind `0.0.0.0`.
- If you bind a LAN address instead, every device on that LAN can reach the port, and the
  connection is plain `ws://`: the token crosses the LAN unencrypted. Only do this on a network
  you trust. Windows Firewall must allow inbound TCP for the bridge's `python.exe`/`pythonw.exe`
  (Windows asks the first time it listens on a non-loopback address).
- Every WebSocket must present the token (`Authorization: Bearer …` or `?token=`), which is
  compared in constant time.
- Treat the pairing QR code like a password, and rotate the token if it leaks.
- Consider Tailscale ACLs so that only your phone can reach port 8787 on the PC.

## Notes on herdr on Windows

- The socket is a named pipe, `\\.\pipe\` + the path in `HERDR_SOCKET_PATH`. That path is
  only a marker file.
- herdr answers one request per connection; subscriptions stay open.
- `terminal session control` resizes the real PTY, so the PC view reflows while the phone
  controls a pane. Release control to restore it.

## Development

```powershell
uv sync
uv run pytest            # unit + integration tests against a fake herdr (pipe/TCP server + fake CLI)
uv run ruff check src tests
```

## License

Apache License 2.0. See [LICENSE](LICENSE). Not affiliated with the herdr project.
