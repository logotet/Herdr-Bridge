"""Fake herdr for tests: an NDJSON socket server (TCP or Windows named pipe)."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any


def make_snapshot() -> dict[str, Any]:
    return {
        "version": "0.0.0-fake",
        "protocol": 19,
        "focused_workspace_id": "w1",
        "focused_tab_id": "w1:t1",
        "focused_pane_id": "w1:p1",
        "workspaces": [{"workspace_id": "w1", "number": 1, "label": "api", "agent_status": "working",
                        "focused": True, "pane_count": 2, "tab_count": 1, "active_tab_id": "w1:t1"}],
        "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "number": 1, "label": "1",
                  "agent_status": "working", "focused": True, "pane_count": 2}],
        "panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "terminal_id": "term_1",
             "cwd": ".", "agent": "claude", "agent_status": "working", "focused": True, "revision": 1},
            {"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1", "terminal_id": "term_2",
             "cwd": ".", "agent_status": "unknown", "focused": False, "revision": 0},
        ],
        "agents": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "terminal_id": "term_1",
             "cwd": ".", "agent": "claude", "agent_status": "working", "focused": True,
             "revision": 1, "state_change_seq": 1, "terminal_title_stripped": "Fix tests"},
        ],
        "layouts": [],
    }


class FakeHerdr:
    def __init__(self) -> None:
        self.snapshot = make_snapshot()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.subscriptions: list[list[dict[str, Any]]] = []
        self._subscribers: list[asyncio.StreamWriter] = []
        self._server: asyncio.AbstractServer | None = None
        self._pipe_server: Any = None
        self.address = ""
        self.screen_text: dict[str, str] = {"w1:p1": "line one\n\nline two\nwaiting for input\n"}

    # ---- control from tests ----
    def set_status(self, pane_id: str, status: str) -> None:
        for coll in ("panes", "agents"):
            for p in self.snapshot[coll]:
                if p["pane_id"] == pane_id:
                    p["agent_status"] = status

    def add_pane(self, pane_id: str) -> None:
        self.snapshot["panes"].append({"pane_id": pane_id, "workspace_id": "w1", "tab_id": "w1:t1",
                                       "terminal_id": f"term_{pane_id}", "cwd": ".",
                                       "agent_status": "unknown", "focused": False, "revision": 0})

    async def push_event(self, event: str, data: dict[str, Any] | None = None) -> None:
        await self.push_raw({"event": event, "data": {"type": event, **(data or {})}})

    async def push_raw(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg).encode() + b"\n"
        for w in list(self._subscribers):
            try:
                w.write(line)
                await w.drain()
            except (ConnectionError, RuntimeError):
                self._subscribers.remove(w)

    def drop_subscribers(self) -> None:
        for w in self._subscribers:
            w.close()
        self._subscribers.clear()

    # ---- server ----
    async def start_tcp(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.address = f"tcp:127.0.0.1:{port}"
        return self.address

    async def start_pipe(self, name: str) -> str:
        loop = asyncio.get_running_loop()

        def factory() -> asyncio.StreamReaderProtocol:
            return asyncio.StreamReaderProtocol(asyncio.StreamReader(), self._handle)

        servers = await loop.start_serving_pipe(factory, r"\\.\pipe" + "\\" + name)  # type: ignore[attr-defined]
        self._pipe_server = servers[0]
        self.address = f"pipe:{name}"
        return self.address

    async def stop(self) -> None:
        self.drop_subscribers()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._pipe_server:
            self._pipe_server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        if not raw:
            writer.close()
            return
        req = json.loads(raw)
        method, params, rid = req["method"], req.get("params", {}), req["id"]
        self.calls.append((method, params))
        if method == "events.subscribe":
            subs = params["subscriptions"]
            known = {p["pane_id"] for p in self.snapshot["panes"]}
            for s in subs:
                if "pane_id" in s and s["pane_id"] not in known:
                    await self._reply(writer, rid, error=("pane_not_found", s["pane_id"]))
                    writer.close()
                    return
            self.subscriptions.append(subs)
            await self._reply(writer, rid, {"type": "subscription_started"})
            self._subscribers.append(writer)
            return  # keep open
        try:
            result = self._dispatch(method, params)
        except KeyError as e:
            await self._reply(writer, rid, error=("pane_not_found", str(e)))
        else:
            await self._reply(writer, rid, result)
        writer.close()

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            return {"type": "pong", "version": "0.0.0-fake", "protocol": 19}
        if method == "session.snapshot":
            return {"type": "session_snapshot", "snapshot": copy.deepcopy(self.snapshot)}
        if method == "pane.read":
            text = self.screen_text.get(params["pane_id"], "")
            return {"type": "pane_read", "read": {"pane_id": params["pane_id"], "text": text}}
        if method == "pane.get":
            for p in self.snapshot["panes"]:
                if p["pane_id"] == params["pane_id"]:
                    return {"type": "pane_info", "pane": p}
            raise KeyError(params["pane_id"])
        if method in ("pane.send_keys", "pane.send_text", "agent.prompt"):
            pid = params.get("pane_id") or params.get("target")
            if pid not in {p["pane_id"] for p in self.snapshot["panes"]}:
                raise KeyError(pid)
            return {"type": "ok"}
        return {"type": "ok", "method": method}

    async def _reply(self, writer: asyncio.StreamWriter, rid: str, result: dict[str, Any] | None = None,
                     error: tuple[str, str] | None = None) -> None:
        msg: dict[str, Any] = {"id": rid}
        if error:
            msg["error"] = {"code": error[0], "message": error[1]}
        else:
            msg["result"] = result
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
