"""Pairing URI + QR code."""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import urlencode

import qrcode

from .config import Config, config_dir, resolve_bind, tailscale_ipv4


def pair_host(cfg: Config) -> str:
    if cfg.advertise_host:
        return cfg.advertise_host
    if cfg.bind in ("0.0.0.0", "::", "tailscale"):
        return tailscale_ipv4() or resolve_bind(cfg)
    return cfg.bind


def pair_uri(cfg: Config) -> str:
    q = urlencode({"host": pair_host(cfg), "port": cfg.port, "token": cfg.token, "name": cfg.name})
    return f"herdr-bridge://pair?{q}"


def qr_ascii(data: str) -> str:
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def qr_png(data: str, path: Path | None = None) -> Path:
    path = path or (config_dir() / "pairing-qr.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(data, box_size=8, border=3)
    with path.open("wb") as f:
        img.save(f)
    return path
