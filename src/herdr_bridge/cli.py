"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys

from . import __version__, autostart, listener
from .config import Config, address_is_up, config_dir, config_path, new_token, resolve_bind
from .herdr_client import HerdrClient, address_for_socket, discover_socket_path
from .pairing import pair_uri, qr_ascii, qr_png
from .server import Bridge, make_app

log = logging.getLogger("herdr_bridge")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_dir = config_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "bridge.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:  # None under pythonw
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def herdr_address(cfg: Config) -> str:
    if cfg.herdr_address:
        return cfg.herdr_address
    return address_for_socket(discover_socket_path(cfg.herdr_cmd, cfg.herdr_session or None))


def cmd_serve(args: argparse.Namespace) -> None:
    cfg = Config.load_or_init()
    if args.bind:
        cfg.bind = args.bind
    if args.port:
        cfg.port = args.port
    if args.session is not None:
        cfg.herdr_session = args.session
    setup_logging(args.verbose)
    host = resolve_bind(cfg)
    address = herdr_address(cfg)
    log.info("herdr-bridge %s: bind %s:%s, herdr at %s", __version__, host, cfg.port, address)
    bridge = Bridge(HerdrClient(address), cfg.herdr_cmd, cfg.herdr_session or None,
                    cfg.token, cfg.name)
    if sys.stderr is not None and args.qr:
        print(f"\nPair the Herdr App by scanning:\n{qr_ascii(pair_uri(cfg))}", file=sys.stderr)
    try:
        asyncio.run(listener.serve(make_app(bridge), host, cfg.port))
    except KeyboardInterrupt:
        pass


def cmd_pair(args: argparse.Namespace) -> None:
    cfg = Config.load_or_init()
    uri = pair_uri(cfg)
    print(qr_ascii(uri))
    print(f"Name: {cfg.name}\nURI:  {uri}")
    if args.png:
        print(f"PNG:  {qr_png(uri)}")


def cmd_rotate_token(args: argparse.Namespace) -> None:
    cfg = Config.load_or_init()
    cfg.token = new_token()
    cfg.save()
    print("Token rotated. Restart the bridge and re-pair the app (herdr-bridge pair).")


def cmd_config(args: argparse.Namespace) -> None:
    cfg = Config.load_or_init()
    print(f"Config: {config_path()}")
    for k, v in vars(cfg).items():
        print(f"  {k} = {'<hidden>' if k == 'token' else v!r}")
    try:
        host = resolve_bind(cfg)
        print(f"Bind address: {host}" + ("" if address_is_up(host) else " (not up right now)"))
    except RuntimeError as e:
        print(f"Bind address: ERROR {e}")
    print(f"herdr address: {herdr_address(cfg)}")


def cmd_check(args: argparse.Namespace) -> None:
    cfg = Config.load_or_init()
    client = HerdrClient(herdr_address(cfg))

    async def run() -> None:
        info = await client.ping()
        snap = await client.snapshot()
        print(f"herdr {info.version} (protocol {info.protocol}) at {client.address}")
        print(f"{len(snap.get('workspaces', []))} workspaces, {len(snap.get('panes', []))} panes, "
              f"{len(snap.get('agents', []))} agents")
        for a in snap.get("agents", []):
            print(f"  {a.get('pane_id'):>8}  {a.get('agent', '?'):<10} {a.get('agent_status'):<8} "
                  f"{a.get('terminal_title_stripped') or ''}")

    asyncio.run(run())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="herdr-bridge", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the bridge (default)")
    s.add_argument("--bind", help="override bind address (default: config, else Tailscale IP)")
    s.add_argument("--port", type=int)
    s.add_argument("--session", help="herdr named session")
    s.add_argument("--qr", action="store_true", help="also print the pairing QR (it holds the token)")
    s.add_argument("--no-qr", action="store_true", help=argparse.SUPPRESS)  # the default now
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_serve)

    pp = sub.add_parser("pair", help="show the pairing QR code")
    pp.add_argument("--png", action="store_true", help="also write a PNG")
    pp.set_defaults(func=cmd_pair)

    sub.add_parser("rotate-token", help="generate a new token").set_defaults(func=cmd_rotate_token)
    sub.add_parser("config", help="show configuration").set_defaults(func=cmd_config)
    sub.add_parser("check", help="check the herdr connection").set_defaults(func=cmd_check)
    sub.add_parser("install-task", help="run hidden at logon (Windows)").set_defaults(
        func=lambda a: print(autostart.install()))
    sub.add_parser("uninstall-task", help="remove the logon task").set_defaults(
        func=lambda a: print(autostart.uninstall()))
    sub.add_parser("task-status", help="show logon task status").set_defaults(
        func=lambda a: print(autostart.status()))

    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(["serve", *(argv or sys.argv[1:])])
    try:
        args.func(args)
    except (RuntimeError, OSError) as e:
        if sys.stderr is not None:
            print(f"error: {e}", file=sys.stderr)
        logging.getLogger("herdr_bridge").error("%s", e)
        sys.exit(1)
