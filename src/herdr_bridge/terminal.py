"""Live pane streams via ``herdr terminal session observe|control`` subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .herdr_client import STREAM_LIMIT, no_window_flags

log = logging.getLogger(__name__)

FrameCallback = Callable[[dict[str, Any]], Awaitable[None]]
ClosedCallback = Callable[[str], Awaitable[None]]


class StreamError(Exception):
    pass


class TerminalStream:
    """One observe or control session for one pane."""

    def __init__(
        self,
        herdr_cmd: list[str],
        session: str | None,
        pane_id: str,
        mode: str,
        cols: int,
        rows: int,
        on_frame: FrameCallback,
        on_closed: ClosedCallback,
        takeover: bool = False,
    ):
        if mode not in ("observe", "control"):
            raise ValueError(mode)
        self.herdr_cmd = herdr_cmd
        self.session = session
        self.pane_id = pane_id
        self.mode = mode
        self.cols = max(2, min(int(cols), 1000))
        self.rows = max(2, min(int(rows), 1000))
        self.takeover = takeover
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._stderr: list[str] = []
        self._first_frame = asyncio.Event()
        self._closed_reason: str | None = None
        self._stopping = False

    def _argv(self) -> list[str]:
        argv = list(self.herdr_cmd)
        if self.session:
            argv += ["--session", self.session]
        argv += ["terminal", "session", self.mode, self.pane_id,
                 "--cols", str(self.cols), "--rows", str(self.rows)]
        if self.mode == "control" and self.takeover:
            argv.append("--takeover")
        return argv

    async def start(self, wait_first_frame: float = 8.0) -> None:
        """Spawn the process and wait for its first frame. Raises StreamError on failure."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
            creationflags=no_window_flags(),
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        exited = asyncio.create_task(self._proc.wait())
        first = asyncio.create_task(self._first_frame.wait())
        done, _ = await asyncio.wait({exited, first}, timeout=wait_first_frame,
                                     return_when=asyncio.FIRST_COMPLETED)
        first.cancel()
        if first not in done:
            await asyncio.sleep(0.05)  # let stderr drain
            reason = self.stderr_tail() or (
                "process exited" if exited in done else "timed out waiting for first frame")
            if exited not in done:
                exited.cancel()
            await self.stop(release=False)
            raise StreamError(reason)
        self._tasks.append(asyncio.create_task(self._watch_exit(exited)))

    def stderr_tail(self) -> str:
        return " | ".join(s.strip() for s in self._stderr[-3:] if s.strip())

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("non-JSON output from herdr stream %s: %r", self.pane_id, raw[:200])
                    continue
                kind = msg.get("type")
                if kind == "terminal.frame":
                    self._first_frame.set()
                    await self._on_frame(msg)
                elif kind == "terminal.closed":
                    self._closed_reason = str(msg.get("reason", "closed"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stream reader failed for %s", self.pane_id)

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            raw = await self._proc.stderr.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            self._stderr.append(line)
            del self._stderr[:-20]
            log.info("herdr stream %s [%s]: %s", self.pane_id, self.mode, line)

    async def _watch_exit(self, exited: asyncio.Task) -> None:
        await exited
        await asyncio.sleep(0.05)
        if not self._stopping:
            reason = self._closed_reason or self.stderr_tail() or "exited"
            await self._on_closed(reason)

    async def send(self, command: dict[str, Any]) -> None:
        if self.mode != "control":
            raise StreamError("stream is not in control mode")
        if not self._proc or not self._proc.stdin or self._proc.returncode is not None:
            raise StreamError("stream is not running")
        self._proc.stdin.write(json.dumps(command).encode() + b"\n")
        await self._proc.stdin.drain()

    async def stop(self, release: bool = True) -> None:
        self._stopping = True
        proc = self._proc
        if proc and proc.returncode is None:
            if release and self.mode == "control":
                with contextlib.suppress(Exception):
                    await self.send({"type": "terminal.release"})
                    await asyncio.wait_for(proc.wait(), 2)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 3)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
