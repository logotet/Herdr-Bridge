"""Minimal async client for herdr's local socket API (NDJSON).

herdr closes the connection after answering one request, so every request opens a fresh
connection. Subscriptions keep their connection open and stream events.

Addresses:
  pipe:<path>      Windows named pipe ``\\\\.\\pipe\\<path>`` (``path`` = herdr socket path)
  unix:<path>      Unix domain socket
  tcp:<host>:<port> plain TCP (tests only)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STREAM_LIMIT = 64 * 1024 * 1024
_ids = itertools.count(1)
_ERROR_PIPE_BUSY = 231


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HerdrUnavailable(HerdrError):
    def __init__(self, message: str):
        super().__init__("herdr_unavailable", message)


class EventsLost(HerdrError):
    pass


@dataclass
class HerdrInfo:
    version: str
    protocol: int


def no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def default_socket_path(session: str | None = None) -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "herdr"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr"
    if session:
        base = base / "sessions" / session
    return base / "herdr.sock"


def discover_socket_path(herdr_cmd: list[str], session: str | None) -> Path:
    """Ask ``herdr status`` for the server socket; fall back to the default location."""
    env_path = os.environ.get("HERDR_SOCKET_PATH")
    if env_path and not session:
        return Path(env_path)
    cmd = list(herdr_cmd) + (["--session", session] if session else []) + ["status", "server"]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, creationflags=no_window_flags()
        ).stdout
        m = re.search(r"^\s*socket:\s*(.+?)\s*$", out, re.MULTILINE)
        if m:
            return Path(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return default_socket_path(session)


def address_for_socket(path: Path | str) -> str:
    return f"pipe:{path}" if sys.platform == "win32" else f"unix:{path}"


async def _open(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    kind, _, target = address.partition(":")
    try:
        if kind == "tcp":
            host, _, port = target.rpartition(":")
            return await asyncio.open_connection(host, int(port), limit=STREAM_LIMIT)
        if kind == "unix":
            return await asyncio.open_unix_connection(target, limit=STREAM_LIMIT)
        if kind == "pipe":
            return await _open_pipe(r"\\.\pipe" + "\\" + target)
    except FileNotFoundError as e:
        raise HerdrUnavailable(f"herdr server not running ({target})") from e
    except ConnectionRefusedError as e:
        raise HerdrUnavailable(f"herdr refused connection ({target})") from e
    raise ValueError(f"unsupported herdr address: {address}")


async def _open_pipe(pipe: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    for attempt in range(50):
        reader = asyncio.StreamReader(limit=STREAM_LIMIT, loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        try:
            transport, _ = await loop.create_pipe_connection(  # type: ignore[attr-defined]
                lambda p=protocol: p, pipe)
        except OSError as e:
            if getattr(e, "winerror", None) == _ERROR_PIPE_BUSY and attempt < 49:
                await asyncio.sleep(0.02)
                continue
            raise
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
    raise HerdrUnavailable("herdr pipe busy")


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:  # noqa: BLE001 - best effort
        pass


def _check(msg: dict[str, Any]) -> dict[str, Any]:
    err = msg.get("error")
    if err is not None:
        code = err.get("code", "herdr_error") if isinstance(err, dict) else "herdr_error"
        text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        if code == "events_lost":
            raise EventsLost(code, text)
        raise HerdrError(code, text)
    return msg.get("result", {})


class HerdrClient:
    def __init__(self, address: str, timeout: float = 15.0):
        self.address = address
        self.timeout = timeout

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = f"bridge_{next(_ids)}"
        reader, writer = await _open(self.address)
        try:
            line = json.dumps({"id": req_id, "method": method, "params": params or {}})
            writer.write(line.encode() + b"\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), self.timeout)
        except (ConnectionError, BrokenPipeError) as e:
            raise HerdrUnavailable(f"connection lost: {e}") from e
        finally:
            _close(writer)
        if not raw:
            raise HerdrUnavailable("herdr closed the connection without a response")
        return _check(json.loads(raw))

    async def ping(self) -> HerdrInfo:
        res = await self.request("ping")
        return HerdrInfo(version=str(res.get("version", "?")), protocol=int(res.get("protocol", 0)))

    async def snapshot(self) -> dict[str, Any]:
        res = await self.request("session.snapshot")
        return res.get("snapshot", res)

    async def subscribe(self, subscriptions: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Yield events until the connection closes. Raises EventsLost / HerdrError."""
        reader, writer = await _open(self.address)
        try:
            req = {"id": f"sub_{next(_ids)}", "method": "events.subscribe",
                   "params": {"subscriptions": subscriptions}}
            writer.write(json.dumps(req).encode() + b"\n")
            await writer.drain()
            first = await asyncio.wait_for(reader.readline(), self.timeout)
            if not first:
                raise HerdrUnavailable("subscription closed immediately")
            _check(json.loads(first))
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                msg = json.loads(raw)
                if "error" in msg:
                    _check(msg)
                if "event" in msg:
                    yield msg
        except (ConnectionError, BrokenPipeError) as e:
            raise HerdrUnavailable(f"subscription lost: {e}") from e
        finally:
            _close(writer)
