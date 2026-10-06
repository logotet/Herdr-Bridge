# herdr-bridge WebSocket protocol (v1)

The bridge runs on the PC next to herdr and exposes one WebSocket. Clients (the Herdr App)
connect over Tailscale.

## Transport & auth

- HTTP `GET /health` — no auth. Returns `{"ok": true, "protocol": 1, "name": "<host name>"}`.
- WebSocket `GET /ws` — auth with header `Authorization: Bearer <token>` **or** query
  `?token=<token>`. A wrong or missing token gets HTTP 401 before the upgrade.
- All messages are JSON **text** frames. Every message has a `"type"` string.
- Unknown fields **and unknown message types** must be ignored by both sides (forward compatibility).

## Pairing

The bridge prints/shows a QR code with this URI:

```
herdr-bridge://pair?host=<ip-or-hostname>&port=<port>&token=<token>&name=<display-name>
```

The app stores `{name, host, port, token}` and connects to `ws://host:port/ws`.

## Requests & results

Client → server messages may carry an `"id"` (string). When an `id` is present, the server
always answers with exactly one `result`:

```json
{"type": "result", "id": "r1", "ok": true,  "data": { ... }}
{"type": "result", "id": "r1", "ok": false, "error": {"code": "pane_not_found", "message": "..."}}
```

Error codes are herdr's error codes passed through, plus bridge codes: `bad_request`,
`unknown_type`, `not_controlling`, `stream_failed`, `herdr_unavailable`, `git_failed`.

## Server → client messages

### `hello` (first message after connect)
```json
{"type": "hello", "protocol": 1, "bridge_version": "0.1.0", "name": "WORK-PC",
 "herdr": {"version": "0.8.0-preview...", "protocol": 19, "available": true}}
```

### `snapshot` (sent after `hello` and again whenever herdr state changes, debounced)
The full current state. The client should **replace** its state with it.
```json
{"type": "snapshot",
 "workspaces": [WorkspaceInfo...],
 "tabs": [TabInfo...],
 "panes": [PaneInfo...],
 "agents": [AgentInfo...],
 "focused_workspace_id": "w1", "focused_tab_id": "w1:t1", "focused_pane_id": "w1:p1",
 "previews": {"w1:p1": "last few lines of plain text"}}
```
Object shapes are herdr's (`session.snapshot`). Fields used by the app:
- WorkspaceInfo: `workspace_id, number, label, agent_status, focused, pane_count, tab_count, active_tab_id`
- TabInfo: `tab_id, workspace_id, number, label, agent_status, focused, pane_count`
- PaneInfo: `pane_id, workspace_id, tab_id, terminal_id, cwd, agent?, agent_status, focused,
  label?, terminal_title?, terminal_title_stripped?, scroll?{offset_from_bottom, max_offset_from_bottom, viewport_rows}`
- AgentInfo: same as PaneInfo plus `state_change_seq`. Only panes running a detected agent.
- `agent_status` ∈ `idle | working | blocked | done | unknown` (`done` = finished, not yet seen).
- `previews`: the last ~3 non-empty lines of each **agent** pane (plain text). May be missing for some panes.

### `agent_status` (a transition, used for notifications)
Sent when an agent pane's status changes between two snapshots.
```json
{"type": "agent_status", "pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude",
 "from": "working", "to": "blocked", "title": "Refactor auth", "workspace_label": "api"}
```

### `frame` (terminal output for a stream the client opened)
```json
{"type": "frame", "pane_id": "w1:p1", "seq": 12, "full": false, "width": 80, "height": 40,
 "bytes": "<base64 ANSI/VT bytes>"}
```
Feed `bytes` (decoded) straight into a VT emulator. A `full: true` frame repaints the whole
screen (it starts with clear + home). The emulator size should match `width`×`height`.

### `stream` (stream state change)
```json
{"type": "stream", "pane_id": "w1:p1", "mode": "observe" | "control" | "closed", "reason": "detached"}
```

### `herdr_status` (herdr server became available/unavailable)
```json
{"type": "herdr_status", "version": "0.8.0-preview...", "protocol": 19, "available": false}
```
While `available` is false, the bridge is connected but herdr isn't running. Show a banner.
A fresh `snapshot` follows when herdr comes back.

### `pong`
`{"type": "pong", "id": "..."}` (answer to `ping`; this is the `result` replacement for ping).

## Client → server messages

| type | fields | notes |
|---|---|---|
| `ping` | `id?` | server replies `pong` |
| `refresh` | `id?` | forces a fresh `snapshot` |
| `open_stream` | `pane_id, cols, rows` | starts a **read-only observe** stream for this pane. Frames follow. Re-opening the same pane replaces the stream. |
| `close_stream` | `pane_id` | stops the stream (and releases control if held) |
| `take_control` | `pane_id, cols, rows, takeover?: bool` | switches the stream to control mode (the phone owns input/resize; **this resizes the real PTY**). `takeover=true` steals from another controller. Answers with a `stream` message. |
| `release_control` | `pane_id` | back to observe mode |
| `input` | `pane_id, text?` or `bytes?` (base64) | raw keyboard input; **requires control** (else `not_controlling`) |
| `resize` | `pane_id, cols, rows` | control: resizes the PTY. observe: restarts the observer at the new size |
| `scroll` | `pane_id, direction: "up"\|"down", lines` | control mode only |
| `call` | `method, params` | passthrough to any herdr socket API method (one request). `data` = herdr `result`. Examples below. |
| `diff` | `pane_id, staged?: bool, path?: string` | runs `git diff` in the pane's `foreground_cwd` (or `cwd`). `data` = `{cwd, root, branch, staged, stat, diff, truncated, untracked: string[]}`. `diff` is capped at 2 MiB (`truncated: true`); `untracked` is empty when `staged`. Errors: `git_failed` |

### Common `call` examples (herdr protocol 19)
```json
{"type":"call","id":"1","method":"pane.send_keys","params":{"pane_id":"w1:p1","keys":["ctrl+c"]}}
{"type":"call","id":"2","method":"pane.send_text","params":{"pane_id":"w1:p1","text":"yes"}}
{"type":"call","id":"3","method":"pane.send_input","params":{"pane_id":"w1:p1","text":"run tests","keys":["enter"]}}
{"type":"call","id":"4","method":"agent.prompt","params":{"target":"w1:p1","text":"fix the build"}}
{"type":"call","id":"5","method":"workspace.create","params":{"cwd":"C:\\repo","label":"repo","focus":false}}
{"type":"call","id":"6","method":"tab.create","params":{"workspace_id":"w1","label":"agent","focus":false}}
{"type":"call","id":"7","method":"agent.start","params":{"pane_id":"w1:p3","kind":"claude","name":"claude"}}
{"type":"call","id":"8","method":"pane.close","params":{"pane_id":"w1:p3"}}
{"type":"call","id":"9","method":"pane.rename","params":{"pane_id":"w1:p3","label":"tests"}}
{"type":"call","id":"10","method":"worktree.create","params":{"workspace_id":"w1","branch":"feat/x","focus":false}}
{"type":"call","id":"11","method":"pane.read","params":{"pane_id":"w1:p1","source":"recent_unwrapped","lines":40}}
```
Key names for `keys`: printable chars, `enter`, `esc`, `tab`, `backspace`, `up`, `down`, `left`,
`right`, `pageup`, `pagedown`, `home`, `end`, `ctrl+c`, `ctrl+d`, `alt+x`, `shift+tab`, `f1`..

## Lifecycle notes

- The server sends `hello`, then `snapshot`, then pushes updates. On reconnect the client
  gets a new full snapshot, so there's no replay.
- Streams belong to one WebSocket connection. All of them stop when it closes.
- The server sends a WebSocket ping every 20 s. Clients should also reconnect with backoff.
