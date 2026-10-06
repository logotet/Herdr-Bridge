"""Bridge configuration (TOML) and Tailscale address detection."""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PORT = 8787
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "herdr-bridge"


def config_path() -> Path:
    return Path(os.environ.get("HERDR_BRIDGE_CONFIG", config_dir() / "config.toml"))


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class Config:
    token: str = ""
    # "tailscale" = bind only to this machine's Tailscale IPv4. Otherwise an explicit address.
    bind: str = "tailscale"
    port: int = DEFAULT_PORT
    name: str = field(default_factory=socket.gethostname)
    herdr_cmd: list[str] = field(default_factory=lambda: ["herdr"])
    herdr_session: str = ""
    # Override the herdr socket address (e.g. "pipe:C:\\...\\herdr.sock"); empty = auto.
    herdr_address: str = ""
    # Address put into the pairing QR; empty = the bind address.
    advertise_host: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or config_path()
        cfg = cls()
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        if isinstance(cfg.herdr_cmd, str):
            cfg.herdr_cmd = [cfg.herdr_cmd]
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# herdr-bridge configuration"]
        for k, v in asdict(self).items():
            lines.append(f"{k} = {_toml_value(v)}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _restrict_permissions(path)
        return path

    @classmethod
    def load_or_init(cls, path: Path | None = None) -> Config:
        path = path or config_path()
        cfg = cls.load(path)
        if not cfg.token:
            cfg.token = new_token()
            cfg.save(path)
        return cfg


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _restrict_permissions(path: Path) -> None:
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def tailscale_ipv4() -> str | None:
    """Return this machine's Tailscale IPv4 (100.64.0.0/10), if any."""
    try:
        import psutil  # optional, more reliable

        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and _is_tailscale(a.address):
                    return a.address
    except ImportError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_tailscale(ip):
                return ip
    except OSError:
        pass
    return None


def _is_tailscale(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_NET
    except ValueError:
        return False


def resolve_bind(cfg: Config) -> str:
    if cfg.bind != "tailscale":
        return cfg.bind
    ip = tailscale_ipv4()
    if not ip:
        raise RuntimeError(
            "No Tailscale IPv4 address found. Start Tailscale, or set `bind` in "
            f"{config_path()} (e.g. 127.0.0.1 for local testing)."
        )
    return ip
