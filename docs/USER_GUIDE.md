# herdr-bridge user guide

What the bridge does, how to install and run it, and how to keep it safe. For how it works
inside, see [ARCHITECTURE.md](ARCHITECTURE.md). For the message format, see
[PROTOCOL.md](../PROTOCOL.md).

## 1. What the bridge is

[herdr](https://herdr.dev) only talks to programs on the same PC. The bridge is a small program
that runs next to herdr and lets the Herdr App on a phone reach it over the network.

```
phone (Herdr App)  ──network, with a token──▶  herdr-bridge  ──local only──▶  herdr
```

The phone never talks to herdr directly. Everything goes through the bridge, and the bridge lets
in only clients that present its token.

## 2. What it lets the phone do

| The phone can | How |
|---|---|
| See workspaces, tabs, panes and agents, with each agent's status | The bridge keeps a live copy of herdr's state and pushes it on every change. |
| See the last lines of each agent | The bridge reads a short preview of every agent pane. |
| Watch a pane live | The bridge streams the pane's screen, read-only. |
| Type into a pane | After taking control of that pane. This resizes the pane on the PC. |
| Send a prompt or single keys without taking control | Through herdr's own calls, passed along by the bridge. |
| Rename, close and create panes, tabs and workspaces | The same way: herdr calls passed along. |
| Read a `git diff` of a pane's folder | The bridge runs `git` in that folder. |

## 3. Requirements

- herdr 0.8.0-preview or newer. Tested on Windows; Linux and macOS should work but are untested.
- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- A network path from the phone to the PC: Tailscale on both, or a LAN address the phone can
  reach.
- `git` on PATH, for the diff feature only.

## 4. Install

```powershell
git clone <this repo>
cd herdr-bridge
uv sync
uv run herdr-bridge check
```

`check` connects to herdr and prints its version and the agents it finds. If it fails, herdr is
not running or the bridge cannot find it; see section 10.

## 5. The token

The token is the bridge's password.

- It is created automatically the first time the bridge needs its configuration, as a long random
  string, and saved in the configuration file.
- There is one token per bridge, shared by every phone paired with that PC.
- It never changes unless you rotate it.
- Each PC has its own. A phone that uses two PCs keeps two.

You never need to read or type it. Pairing hands it to the phone.

## 6. Pair a phone

```powershell
uv run herdr-bridge pair
```

This prints a QR code, the PC's name, and the same information as a line of text. Add `--png` to
also save the code as an image in the configuration folder.

In the Herdr App, open **Hosts**, add a host and scan the code. If the camera is not an option,
type the address, the port and the token from the printed line.

To pair another phone, run `pair` again. It shows the same code.

## 7. Run it

```powershell
uv run herdr-bridge serve
```

Runs in the foreground. Stop it with Ctrl+C. It does not show the pairing code, because the code
holds the token and a terminal can be read by others, including agents in herdr panes. Use `pair`
for that.

Options for `serve`, each overriding the configuration file for that run:

| Option | Meaning |
|---|---|
| `--bind <address>` | Address to listen on. |
| `--port <number>` | Port to listen on. |
| `--session <name>` | Use a named herdr session. |
| `--qr` | Also print the pairing code on start. |
| `-v` | More detail in the log. |

### Start hidden at logon (Windows)

```powershell
uv run herdr-bridge install-task     # install and start now
uv run herdr-bridge task-status
uv run herdr-bridge uninstall-task
```

This creates a scheduled task named `herdr-bridge` for your user. It starts 15 seconds after
logon, shows no window, needs no administrator rights, and is restarted if it stops.

## 8. Configuration

The file is `%APPDATA%\herdr-bridge\config.toml` on Windows and
`~/.config/herdr-bridge/config.toml` elsewhere. Set `HERDR_BRIDGE_CONFIG` to use another path.

```powershell
uv run herdr-bridge config
```

prints the current values, with the token hidden, the address it would listen on and whether that
address is up, and where it expects herdr.

| Key | Default | Meaning |
|---|---|---|
| `token` | generated | The password every client must send. |
| `bind` | `"tailscale"` | Where to listen. `"tailscale"` means this PC's Tailscale address only. Otherwise an explicit address, such as a fixed LAN address or `127.0.0.1`. |
| `port` | `8787` | Port to listen on. |
| `name` | the PC's host name | The name the app shows. |
| `herdr_cmd` | `["herdr"]` | How to run the herdr command. Use the full path to `herdr.exe` if it is not on PATH for the scheduled task. |
| `herdr_session` | `""` | A named herdr session. Empty means the default one. |
| `herdr_address` | `""` | Where herdr listens, when it cannot be found automatically. |
| `advertise_host` | `""` | The address written into the pairing code, for example a Tailscale MagicDNS name. Empty means the listening address. |

Changes take effect the next time the bridge starts.

## 9. Choosing the network

| | Tailscale (default) | LAN address |
|---|---|---|
| Who can reach the port | Only devices in your tailnet | Every device on that network |
| Is the token encrypted in transit | Yes | No |
| Works away from home | Yes | No |
| Setup | Install Tailscale on PC and phone | Set `bind`, allow the firewall prompt |

If the listening address is not up, for example because Wi-Fi is off, the bridge waits and starts
listening again when the address returns. It does not need a restart.

Never set `bind` to `0.0.0.0`. That listens on every network the PC is on.

## 10. Troubleshooting

The log is `bridge.log` in the configuration folder. It is rotated at 2 MB, with three old files
kept.

| What you see | Cause | What to do |
|---|---|---|
| `check` fails with "herdr server not running" | herdr is not running, or it uses a named session | Start herdr, or set `herdr_session`. |
| `serve` fails with "No Tailscale IPv4 address found" | `bind` is `"tailscale"` and Tailscale is off | Start Tailscale, or set `bind` to an address. |
| Log says "waiting for … to come up" | The listening address is not present | Connect that network. The bridge recovers by itself. |
| Log says "cannot listen on …" | Another program holds the port, or the firewall refuses | Change `port`, or allow the bridge in the firewall. |
| The app shows "…was 401" | The phone has a different token | Pair again. |
| The app shows "herdr unavailable" | The bridge runs, herdr does not | Start herdr. The app recovers by itself. |
| The app connects but a pane stays blank | The pane's stream failed to start | Look for `herdr stream` lines in the log. |
| Log says "client … too slow; disconnecting" | The phone could not keep up with the output | It reconnects and gets a fresh picture. |

## 11. Security

herdr's own socket has no password: anything on the PC that can reach it can type into your
terminals. The bridge is therefore the only gate between the network and your sessions.

- The token is the only check. A client that passes it can do everything in section 2, including
  typing commands and closing panes.
- Outside Tailscale the connection is not encrypted. Use a LAN address only on a network you
  trust.
- The pairing code contains the token. Do not photograph or share it.
- If the token may have leaked:

  ```powershell
  uv run herdr-bridge rotate-token
  ```

  then restart the bridge and pair every phone again. The old token stops working at the restart.
- With Tailscale, consider an access rule so that only your phone can reach the bridge's port.
- The health address `/health` answers without a token. It reveals only that a bridge is there,
  and its name.

## 12. Command reference

| Command | What it does |
|---|---|
| `serve` | Runs the bridge. This is the default when no command is given. |
| `pair [--png]` | Shows the pairing code, creating the token if there is none. |
| `rotate-token` | Replaces the token. Restart and pair again afterwards. |
| `config` | Shows the configuration, with the token hidden. |
| `check` | Tests the connection to herdr and lists the agents. |
| `install-task` | Windows: start hidden at logon. |
| `uninstall-task` | Windows: remove that task. |
| `task-status` | Windows: show the task's state. |
| `--version` | Prints the bridge's version. |
