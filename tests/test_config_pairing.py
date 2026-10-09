from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

from herdr_bridge.config import Config, _is_tailscale
from herdr_bridge.pairing import pair_uri, qr_ascii


def test_config_roundtrip(tmp_path):
    p = tmp_path / "sub" / "config.toml"
    cfg = Config(token="t", bind="127.0.0.1", port=9000, name='pc "x"',
                 herdr_cmd=["C:\\Program Files\\herdr.exe", "--flag"], herdr_session="spike")
    cfg.save(p)
    back = Config.load(p)
    assert back == cfg


def test_load_or_init_generates_token(tmp_path):
    p = tmp_path / "config.toml"
    cfg = Config.load_or_init(p)
    assert len(cfg.token) >= 32
    assert Config.load_or_init(p).token == cfg.token


def test_load_ignores_unknown_and_wraps_cmd(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('herdr_cmd = "herdr.exe"\nbogus = 1\nport = 1234\n', encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.herdr_cmd == ["herdr.exe"] and cfg.port == 1234


def test_is_tailscale():
    assert _is_tailscale("100.101.102.103")
    assert not _is_tailscale("100.128.0.1")
    assert not _is_tailscale("192.168.1.1")
    assert not _is_tailscale("garbage")


def test_pair_uri():
    cfg = Config(token="abc+/=", bind="127.0.0.1", port=8787, name="my pc")
    u = urlparse(pair_uri(cfg))
    assert u.scheme == "herdr-bridge" and u.netloc == "pair"
    q = parse_qs(u.query)
    assert q == {"host": ["127.0.0.1"], "port": ["8787"], "token": ["abc+/="], "name": ["my pc"]}
    cfg.advertise_host = "pc.tailnet.ts.net"
    assert parse_qs(urlparse(pair_uri(cfg)).query)["host"] == ["pc.tailnet.ts.net"]


def _pair(tmp_path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Runs ``herdr-bridge pair`` on its own configuration, with its output going to a pipe."""
    config = tmp_path / "config.toml"
    Config(token="tok", bind="127.0.0.1").save(config)
    env = {**os.environ, "HERDR_BRIDGE_CONFIG": str(config)}
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    return subprocess.run([sys.executable, "-m", "herdr_bridge", "pair", *args], env=env,
                          capture_output=True, timeout=30)


def test_pair_prints_the_qr_to_a_redirected_stream(tmp_path):
    res = _pair(tmp_path)
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    assert "herdr-bridge://pair?host=127.0.0.1" in res.stdout.decode("utf-8")


def test_pair_can_save_the_qr_as_an_image(tmp_path):
    res = _pair(tmp_path, "--svg")
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    image = tmp_path / "pairing-qr.svg"
    assert image.read_text(encoding="utf-8").lstrip().startswith("<?xml")
    assert str(image) in res.stdout.decode("utf-8")


def test_qr_ascii():
    out = qr_ascii("herdr-bridge://pair?host=1")
    assert len(out.splitlines()) > 10
