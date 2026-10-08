"""aiohttp WebSocket server implementing PROTOCOL.md."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from . import __version__, gitdiff
from .herdr_client import HerdrClient, HerdrError
from .state import StateTracker
from .terminal import StreamError, TerminalStream

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_QUEUE = 5000
OBSERVE_DEFAULT = (80, 40)
INLINE_TYPES = frozenset({"input", "scroll"})


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Bridge:
    """Shared state: herdr client, state tracker, connected clients."""

    def __init__(self, client: HerdrClient, herdr_cmd: list[str], herdr_session: str | None,
                 token: str, name: str, tracker: StateTracker | None = None):
        self.client = client
        self.herdr_cmd = herdr_cmd
        self.herdr_session = herdr_session or None
        self.token = token
        self.name = name
        self.tracker = tracker or StateTracker(client)
        self.clients: set[ClientSession] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.tracker.add_listener(self._broadcast)

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        for c in list(self.clients):
            c.send(msg)
        if msg.get("type") == "snapshot":
            # Pane sizes may have changed (PC window or split resized): re-open observe streams.
            for c in list(self.clients):
                task = asyncio.create_task(c.sync_observe_sizes())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def check_token(self, request: web.Request) -> bool:
        supplied = ""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            supplied = request.query.get("token", "")
        return bool(self.token) and hmac.compare_digest(supplied.encode(), self.token.encode())

    def hello(self) -> dict[str, Any]:
        return {"type": "hello", "protocol": PROTOCOL_VERSION, "bridge_version": __version__,
                "name": self.name, "herdr": self.tracker.herdr_info()}


class ClientSession:
    def __init__(self, bridge: Bridge, ws: web.WebSocketResponse, peer: str):
        self.bridge = bridge
        self.ws = ws
        self.peer = peer
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.streams: dict[str, TerminalStream] = {}
        self.observe_size: dict[str, tuple[int, int]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._dropped = False

    # ---- outgoing ----
    def send(self, msg: dict[str, Any]) -> None:
        if self._dropped:
            return
        if self.queue.qsize() > MAX_QUEUE:
            log.warning("client %s too slow; disconnecting", self.peer)
            self._dropped = True
            # What is waiting will never be read in time: forget it, so the close is not queued
            # behind it. The writer may be stuck in a send, so the socket is also closed directly.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)
            self.spawn(self._drop())
            return
        self.queue.put_nowait(json.dumps(msg, separators=(",", ":")))

    async def _drop(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(code=WSCloseCode.TRY_AGAIN_LATER,
                                                 message=b"too slow"), 2)

    async def writer(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None or self.ws.closed:
                await self.ws.close()
                return
            try:
                await self.ws.send_str(item)
            except (ConnectionError, RuntimeError):
                return

    def _lock(self, pane_id: str) -> asyncio.Lock:
        return self._locks.setdefault(pane_id, asyncio.Lock())

    # ---- incoming ----
    def parse(self, raw: str) -> dict[str, Any] | None:
        try:
            msg = json.loads(raw)
            if isinstance(msg, dict):
                return msg
        except ValueError:
            pass
        self.send({"type": "result", "id": None, "ok": False,
                   "error": {"code": "bad_request", "message": "invalid JSON object"}})
        return None

    async def handle(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        kind = msg.get("type")
        if kind == "ping":
            self.send({"type": "pong", "id": req_id})
            return
        handler = getattr(self, f"op_{kind}", None) if isinstance(kind, str) else None
        try:
            if handler is None:
                raise BridgeError("unknown_type", f"unknown message type: {kind!r}")
            data = await handler(msg)
            if req_id is not None:
                self.send({"type": "result", "id": req_id, "ok": True, "data": data or {}})
        except BridgeError as e:
            self._fail(req_id, e.code, e.message)
        except HerdrError as e:
            self._fail(req_id, e.code, e.message)
        except StreamError as e:
            self._fail(req_id, "stream_failed", str(e))
        except gitdiff.GitError as e:
            self._fail(req_id, "git_failed", str(e))
        except (KeyError, TypeError, ValueError) as e:
            self._fail(req_id, "bad_request", f"invalid parameters: {e}")
        except Exception as e:
            log.exception("handler %s failed", kind)
            self._fail(req_id, "internal", str(e))

    def _fail(self, req_id: Any, code: str, message: str) -> None:
        if req_id is not None:
            self.send({"type": "result", "id": req_id, "ok": False,
                       "error": {"code": code, "message": message}})
        else:
            log.info("request failed (%s): %s", code, message)

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---- ops ----
    async def op_refresh(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self.bridge.tracker.refresh()
        self.send(self.bridge.tracker.snapshot_message())
        return {}

    async def op_call(self, msg: dict[str, Any]) -> dict[str, Any]:
        method = msg["method"]
        if not isinstance(method, str) or not method:
            raise BridgeError("bad_request", "method required")
        if method == "events.subscribe" or method == "server.stop":
            raise BridgeError("bad_request", f"{method} is not allowed through the bridge")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            raise BridgeError("bad_request", "params must be an object")
        result = await self.bridge.client.request(method, params)
        self.bridge.tracker.mark_dirty()
        return result

    async def op_diff(self, msg: dict[str, Any]) -> dict[str, Any]:
        pane_id = msg["pane_id"]
        pane = self.bridge.tracker.find_pane(pane_id)
        if pane is None:
            res = await self.bridge.client.request("pane.get", {"pane_id": pane_id})
            pane = res.get("pane", res)
        cwd = pane.get("foreground_cwd") or pane.get("cwd") or ""
        return await gitdiff.diff(cwd, bool(msg.get("staged", False)), msg.get("path"))

    def _size(self, msg: dict[str, Any], default: tuple[int, int]) -> tuple[int, int]:
        return int(msg.get("cols") or default[0]), int(msg.get("rows") or default[1])

    def _observe_dims(self, pane_id: str, cols: int, rows: int) -> tuple[int, int]:
        """Observe at the pane's real size: herdr crops observe frames to the requested grid, so a
        phone-sized request would cut off the right side. The app scales the frames to fit."""
        return self.bridge.tracker.pane_size(pane_id) or (cols, rows)

    async def sync_observe_sizes(self) -> None:
        for pane_id, stream in list(self.streams.items()):
            if self.ws.closed or stream.mode != "observe":
                continue
            size = self.bridge.tracker.pane_size(pane_id)
            if not size or size == (stream.cols, stream.rows):
                continue
            async with self._lock(pane_id):
                if self.streams.get(pane_id) is not stream:
                    continue
                await self._stop(pane_id)
                await self._restart_observe(pane_id, *size)

    async def _restart_observe(self, pane_id: str, cols: int, rows: int) -> bool:
        """Start observing again after the previous stream was stopped. If that fails the pane
        has no stream left, so the client is told instead of being left with a frozen screen."""
        try:
            await self._start(pane_id, "observe", cols, rows)
        except (StreamError, OSError) as e:
            log.warning("cannot observe %s at %sx%s: %s", pane_id, cols, rows, e)
            self._mode_msg(pane_id, "closed", str(e) or "stream failed")
            return False
        return True

    async def _start(self, pane_id: str, mode: str, cols: int, rows: int,
                     takeover: bool = False) -> TerminalStream:
        stream: TerminalStream

        async def on_frame(frame: dict[str, Any]) -> None:
            if self.streams.get(pane_id) is stream:
                self.send({"type": "frame", "pane_id": pane_id, "seq": frame.get("seq"),
                           "full": bool(frame.get("full")), "width": frame.get("width"),
                           "height": frame.get("height"), "bytes": frame.get("bytes", "")})

        async def on_closed(reason: str) -> None:
            if self.streams.get(pane_id) is stream:
                self.streams.pop(pane_id, None)
                self.send({"type": "stream", "pane_id": pane_id, "mode": "closed", "reason": reason})

        stream = TerminalStream(self.bridge.herdr_cmd, self.bridge.herdr_session, pane_id, mode,
                                cols, rows, on_frame, on_closed, takeover=takeover)
        self.streams[pane_id] = stream
        try:
            await stream.start()
        except Exception:
            if self.streams.get(pane_id) is stream:
                self.streams.pop(pane_id, None)
            raise
        return stream

    async def _stop(self, pane_id: str) -> None:
        stream = self.streams.pop(pane_id, None)
        if stream:
            await stream.stop()

    def _mode_msg(self, pane_id: str, mode: str, reason: str | None = None) -> None:
        m = {"type": "stream", "pane_id": pane_id, "mode": mode}
        if reason:
            m["reason"] = reason
        self.send(m)

    async def op_open_stream(self, msg: dict[str, Any]) -> dict[str, Any]:
        pane_id = msg["pane_id"]
        cols, rows = self._size(msg, OBSERVE_DEFAULT)
        async with self._lock(pane_id):
            await self._stop(pane_id)
            self.observe_size[pane_id] = (cols, rows)
            await self._start(pane_id, "observe", *self._observe_dims(pane_id, cols, rows))
            self._mode_msg(pane_id, "observe")
        return {"mode": "observe"}

    async def op_close_stream(self, msg: dict[str, Any]) -> dict[str, Any]:
        pane_id = msg["pane_id"]
        async with self._lock(pane_id):
            await self._stop(pane_id)
            self.observe_size.pop(pane_id, None)
        return {}

    async def op_take_control(self, msg: dict[str, Any]) -> dict[str, Any]:
        pane_id = msg["pane_id"]
        cols, rows = self._size(msg, self.observe_size.get(pane_id, OBSERVE_DEFAULT))
        async with self._lock(pane_id):
            await self._stop(pane_id)
            try:
                await self._start(pane_id, "control", cols, rows, bool(msg.get("takeover", False)))
            except StreamError:
                # Fall back to observing so the user still sees the pane.
                o_cols, o_rows = self.observe_size.get(pane_id, (cols, rows))
                if await self._restart_observe(pane_id, *self._observe_dims(pane_id, o_cols, o_rows)):
                    self._mode_msg(pane_id, "observe")
                raise
            self._mode_msg(pane_id, "control")
        return {"mode": "control"}

    async def op_release_control(self, msg: dict[str, Any]) -> dict[str, Any]:
        pane_id = msg["pane_id"]
        async with self._lock(pane_id):
            current = self.streams.get(pane_id)
            size = self.observe_size.get(pane_id) or (
                (current.cols, current.rows) if current else OBSERVE_DEFAULT)
            await self._stop(pane_id)
            await self._start(pane_id, "observe", *self._observe_dims(pane_id, *size))
            self._mode_msg(pane_id, "observe")
        return {"mode": "observe"}

    def _controlling(self, pane_id: str) -> TerminalStream:
        stream = self.streams.get(pane_id)
        if stream is None or stream.mode != "control":
            raise BridgeError("not_controlling", f"not controlling {pane_id}; send take_control first")
        return stream

    async def op_input(self, msg: dict[str, Any]) -> dict[str, Any]:
        stream = self._controlling(msg["pane_id"])
        if msg.get("bytes") is not None:
            base64.b64decode(msg["bytes"], validate=True)
            await stream.send({"type": "terminal.input", "bytes": msg["bytes"]})
        elif msg.get("text") is not None:
            await stream.send({"type": "terminal.input", "text": str(msg["text"])})
        else:
            raise BridgeError("bad_request", "input needs text or bytes")
        return {}

    async def op_resize(self, msg: dict[str, Any]) -> dict[str, Any]:
        pane_id = msg["pane_id"]
        cols, rows = self._size(msg, OBSERVE_DEFAULT)
        async with self._lock(pane_id):
            stream = self.streams.get(pane_id)
            if stream is None:
                raise BridgeError("bad_request", f"no open stream for {pane_id}")
            if stream.mode == "control":
                stream.cols, stream.rows = cols, rows
                await stream.send({"type": "terminal.resize", "cols": cols, "rows": rows})
            else:
                self.observe_size[pane_id] = (cols, rows)
                dims = self._observe_dims(pane_id, cols, rows)
                # The view size doesn't matter while observing a known PC size; don't restart for it.
                if dims != (stream.cols, stream.rows):
                    await self._stop(pane_id)
                    await self._start(pane_id, "observe", *dims)
        return {}

    async def op_scroll(self, msg: dict[str, Any]) -> dict[str, Any]:
        stream = self._controlling(msg["pane_id"])
        direction = msg.get("direction")
        if direction not in ("up", "down"):
            raise BridgeError("bad_request", "direction must be 'up' or 'down'")
        lines = max(1, min(int(msg.get("lines", 3)), 1000))
        await stream.send({"type": "terminal.scroll", "direction": direction, "lines": lines})
        return {}

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for pane_id in list(self.streams):
            with contextlib.suppress(Exception):
                await self._stop(pane_id)


BRIDGE_KEY: web.AppKey[Bridge] = web.AppKey("bridge", Bridge)


async def health(request: web.Request) -> web.Response:
    bridge = request.app[BRIDGE_KEY]
    return web.json_response({"ok": True, "protocol": PROTOCOL_VERSION, "name": bridge.name})


async def ws_handler(request: web.Request) -> web.StreamResponse:
    bridge = request.app[BRIDGE_KEY]
    if not bridge.check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    peer = request.remote or "?"
    session = ClientSession(bridge, ws, peer)
    log.info("client connected: %s", peer)
    writer = asyncio.create_task(session.writer())
    session.send(bridge.hello())
    if bridge.tracker.snapshot:
        session.send(bridge.tracker.snapshot_message())
    bridge.clients.add(session)
    try:
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                # Keystrokes are handled inline to keep their order; other requests run
                # concurrently so a slow call doesn't block typing.
                msg = session.parse(m.data)
                if msg is None:
                    continue
                if msg.get("type") in INLINE_TYPES:
                    await session.handle(msg)
                else:
                    session.spawn(session.handle(msg))
            elif m.type == WSMsgType.ERROR:
                break
    finally:
        bridge.clients.discard(session)
        await session.close()
        session.queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer, 2)
        log.info("client disconnected: %s", peer)
    return ws


def make_app(bridge: Bridge) -> web.Application:
    app = web.Application()
    app[BRIDGE_KEY] = bridge
    app.router.add_get("/health", health)
    app.router.add_get("/ws", ws_handler)

    async def on_startup(app: web.Application) -> None:
        await bridge.tracker.start()

    async def on_cleanup(app: web.Application) -> None:
        for c in list(bridge.clients):
            await c.close()
        await bridge.tracker.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
