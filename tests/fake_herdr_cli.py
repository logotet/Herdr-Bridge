"""Fake ``herdr terminal session observe|control`` CLI used by tests.

Usage: fake_herdr_cli.py [--session X] terminal session <observe|control> <pane>
       --cols N --rows N [--takeover]
"""

import base64
import json
import sys


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def frame(seq, data, cols, rows, full=False):
    out({"type": "terminal.frame", "encoding": "ansi", "full": full, "width": cols, "height": rows,
         "seq": seq, "bytes": base64.b64encode(data).decode()})


def main():
    args = sys.argv[1:]
    if args[:1] == ["--session"]:
        args = args[2:]
    assert args[:2] == ["terminal", "session"], args
    mode, pane = args[2], args[3]
    cols = int(args[args.index("--cols") + 1])
    rows = int(args[args.index("--rows") + 1])
    if pane == "w1:missing":
        sys.stderr.write("error: pane not found\n")
        sys.exit(1)
    if cols == 999:
        sys.stderr.write("error: too wide\n")
        sys.exit(1)
    if mode == "control" and pane == "w1:locked" and "--takeover" not in args:
        sys.stderr.write("error: terminal already has a controller; use --takeover\n")
        sys.exit(1)
    seq = 1
    frame(seq, f"\x1b[2J\x1b[H{mode}:{pane}:{cols}x{rows}".encode(), cols, rows, full=True)
    if mode == "observe":
        for _ in sys.stdin:  # stay alive until killed / stdin closed
            pass
        return
    for line in sys.stdin:
        cmd = json.loads(line)
        seq += 1
        t = cmd["type"]
        if t == "terminal.input":
            data = cmd["text"].encode() if "text" in cmd else base64.b64decode(cmd["bytes"])
            frame(seq, b"ECHO:" + data, cols, rows)
        elif t == "terminal.resize":
            cols, rows = cmd["cols"], cmd["rows"]
            frame(seq, b"\x1b[2J\x1b[Hresized", cols, rows, full=True)
        elif t == "terminal.scroll":
            frame(seq, f"SCROLL:{cmd['direction']}:{cmd['lines']}".encode(), cols, rows)
        elif t == "terminal.release":
            out({"type": "terminal.closed", "reason": "detached"})
            return


if __name__ == "__main__":
    main()
