from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from typing import Any

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from herdr_bridge.herdr_client import HerdrClient
from herdr_bridge.server import Bridge, make_app
from herdr_bridge.state import StateTracker

from .conftest import FAKE_CLI
from .fake_herdr import FakeHerdr

TOKEN = "s3cret"


@pytest.fixture
async def bridge(fake: FakeHerdr, client: HerdrClient):
    tracker = StateTracker(client, debounce=0.01, poll_interval=60)
    b = Bridge(client, FAKE_CLI, None, TOKEN, "test-pc", tracker=tracker)
    yield b


@pytest.fixture
async def http(bridge: Bridge):
    c = TestClient(TestServer(make_app(bridge)))
    await c.start_server()
    await asyncio.wait_for(bridge.tracker.ready.wait(), 5)
    yield c
    await c.close()


class WS:
    def __init__(self, ws):
        self.ws = ws
        self.seen: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self._n = 0

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send_json(msg)

    async def recv_until(self, pred, timeout: float = 5.0) -> dict[str, Any]:
        """Return the first (buffered or new) message matching ``pred``; keep the others buffered."""
        for i, m in enumerate(self._pending):
            if pred(m):
                return self._pending.pop(i)

        async def loop():
            while True:
                m = await self.ws.receive_json()
                self.seen.append(m)
                if pred(m):
                    return m
                self._pending.append(m)
        return await asyncio.wait_for(loop(), timeout)

    async def request(self, msg: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        self._n += 1
        rid = f"r{self._n}"
        await self.send({**msg, "id": rid})
        return await self.recv_until(lambda m: m.get("type") == "result" and m.get("id") == rid, timeout)

    async def frame(self, pane_id: str, contains: bytes, timeout: float = 5.0) -> dict[str, Any]:
        return await self.recv_until(
            lambda m: m.get("type") == "frame" and m["pane_id"] == pane_id
            and contains in base64.b64decode(m["bytes"]), timeout)


@pytest.fixture
async def ws(http: TestClient):
    raw = await http.ws_connect(f"/ws?token={TOKEN}")
    w = WS(raw)
    yield w
    await raw.close()


async def test_health(http: TestClient):
    r = await http.get("/health")
    assert r.status == 200
    assert (await r.json())["name"] == "test-pc"


@pytest.mark.parametrize("query,headers", [("", {}), ("?token=wrong", {}),
                                           ("", {"Authorization": "Bearer nope"})])
async def test_auth_rejected(http: TestClient, query: str, headers: dict[str, str]):
    r = await http.get(f"/ws{query}", headers=headers)
    assert r.status == 401


async def test_bearer_header_accepted(http: TestClient):
    raw = await http.ws_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"})
    hello = await raw.receive_json()
    assert hello["type"] == "hello"
    await raw.close()


async def test_hello_then_snapshot(ws: WS):
    hello = await ws.recv_until(lambda m: True)
    assert hello["type"] == "hello" and hello["protocol"] == 1
    assert hello["herdr"]["available"] is True and hello["herdr"]["protocol"] == 19
    snap = await ws.recv_until(lambda m: m["type"] == "snapshot")
    assert {a["pane_id"] for a in snap["agents"]} == {"w1:p1"}


async def test_ping_and_unknown_and_bad_json(ws: WS):
    await ws.send({"type": "ping", "id": 7})
    assert (await ws.recv_until(lambda m: m["type"] == "pong"))["id"] == 7
    res = await ws.request({"type": "frobnicate"})
    assert res["ok"] is False and res["error"]["code"] == "unknown_type"
    await ws.ws.send_str("not json")
    res = await ws.recv_until(lambda m: m.get("type") == "result" and m.get("id") is None)
    assert res["error"]["code"] == "bad_request"


async def test_call_passthrough(fake: FakeHerdr, ws: WS):
    res = await ws.request({"type": "call", "method": "pane.send_text",
                            "params": {"pane_id": "w1:p1", "text": "hi"}})
    assert res["ok"] is True
    assert ("pane.send_text", {"pane_id": "w1:p1", "text": "hi"}) in fake.calls
    res = await ws.request({"type": "call", "method": "pane.send_text", "params": {"pane_id": "zz"}})
    assert res["ok"] is False and res["error"]["code"] == "pane_not_found"


@pytest.mark.parametrize("method", ["events.subscribe", "server.stop"])
async def test_call_disallowed(fake: FakeHerdr, ws: WS, method: str):
    res = await ws.request({"type": "call", "method": method, "params": {}})
    assert res["ok"] is False and res["error"]["code"] == "bad_request"
    assert method not in [c[0] for c in fake.calls if c[0] == "server.stop"]


async def test_status_change_pushes_agent_status(fake: FakeHerdr, ws: WS, bridge: Bridge):
    await ws.recv_until(lambda m: m["type"] == "snapshot")
    fake.set_status("w1:p1", "blocked")
    await fake.push_event("pane_agent_status_changed", {"pane_id": "w1:p1"})
    m = await ws.recv_until(lambda m: m["type"] == "agent_status")
    assert m["to"] == "blocked" and m["pane_id"] == "w1:p1"


async def test_stream_lifecycle(ws: WS):
    res = await ws.request({"type": "open_stream", "pane_id": "w1:p1", "cols": 50, "rows": 20})
    assert res["ok"] and res["data"]["mode"] == "observe"
    f = await ws.frame("w1:p1", b"observe:w1:p1:50x20")
    assert f["full"] is True
    assert any(m.get("type") == "stream" and m["mode"] == "observe" for m in ws.seen)

    res = await ws.request({"type": "input", "pane_id": "w1:p1", "text": "x"})
    assert res["ok"] is False and res["error"]["code"] == "not_controlling"

    res = await ws.request({"type": "take_control", "pane_id": "w1:p1"})
    assert res["ok"] and res["data"]["mode"] == "control"
    await ws.frame("w1:p1", b"control:w1:p1:50x20")

    await ws.request({"type": "input", "pane_id": "w1:p1", "text": "hello"})
    await ws.frame("w1:p1", b"ECHO:hello")
    await ws.request({"type": "input", "pane_id": "w1:p1",
                      "bytes": base64.b64encode(b"\x03").decode()})
    await ws.frame("w1:p1", b"ECHO:\x03")
    res = await ws.request({"type": "input", "pane_id": "w1:p1", "bytes": "!!notb64"})
    assert res["ok"] is False and res["error"]["code"] == "bad_request"

    await ws.request({"type": "scroll", "pane_id": "w1:p1", "direction": "up", "lines": 5})
    await ws.frame("w1:p1", b"SCROLL:up:5")
    res = await ws.request({"type": "scroll", "pane_id": "w1:p1", "direction": "left"})
    assert res["error"]["code"] == "bad_request"

    await ws.request({"type": "resize", "pane_id": "w1:p1", "cols": 60, "rows": 25})
    f = await ws.frame("w1:p1", b"resized")
    assert f["width"] == 60 and f["height"] == 25

    res = await ws.request({"type": "release_control", "pane_id": "w1:p1"})
    assert res["ok"] and res["data"]["mode"] == "observe"
    await ws.frame("w1:p1", b"observe:w1:p1:50x20")

    res = await ws.request({"type": "close_stream", "pane_id": "w1:p1"})
    assert res["ok"]
    res = await ws.request({"type": "resize", "pane_id": "w1:p1", "cols": 60, "rows": 25})
    assert res["ok"] is False


async def test_observe_resize_restarts_stream(ws: WS):
    await ws.request({"type": "open_stream", "pane_id": "w1:p2", "cols": 40, "rows": 10})
    await ws.frame("w1:p2", b"observe:w1:p2:40x10")
    await ws.request({"type": "resize", "pane_id": "w1:p2", "cols": 70, "rows": 30})
    await ws.frame("w1:p2", b"observe:w1:p2:70x30")


async def test_observe_uses_pc_pane_size(fake: FakeHerdr, ws: WS):
    fake.snapshot["layouts"] = [{"tab_id": "w1:t1", "area": {"x": 0, "y": 0, "width": 200, "height": 40},
                                 "panes": [{"pane_id": "w1:p2", "focused": False,
                                            "rect": {"x": 100, "y": 0, "width": 100, "height": 39}}],
                                 "splits": [], "zoomed": False}]
    await fake.push_event("layout_updated", {"tab_id": "w1:t1"})
    snap = await ws.recv_until(lambda m: m["type"] == "snapshot" and m.get("pane_sizes"))
    assert snap["pane_sizes"] == {"w1:p2": [100, 39]}

    # Observe ignores the phone size and renders the whole PC pane; the app scales it.
    await ws.request({"type": "open_stream", "pane_id": "w1:p2", "cols": 40, "rows": 10})
    await ws.frame("w1:p2", b"observe:w1:p2:100x39")

    # Control still uses the phone size; release goes back to the PC size.
    await ws.request({"type": "take_control", "pane_id": "w1:p2"})
    await ws.frame("w1:p2", b"control:w1:p2:40x10")
    await ws.request({"type": "release_control", "pane_id": "w1:p2"})
    await ws.frame("w1:p2", b"observe:w1:p2:100x39")

    # The PC pane is resized: the observe stream follows.
    fake.snapshot["layouts"][0]["panes"][0]["rect"]["width"] = 120
    await fake.push_event("layout_updated", {"tab_id": "w1:t1"})
    await ws.frame("w1:p2", b"observe:w1:p2:120x39")


async def test_open_stream_failure(ws: WS):
    res = await ws.request({"type": "open_stream", "pane_id": "w1:missing"})
    assert res["ok"] is False and res["error"]["code"] == "stream_failed"
    assert "pane not found" in res["error"]["message"]


async def test_take_control_failure_falls_back_to_observe(ws: WS):
    await ws.request({"type": "open_stream", "pane_id": "w1:locked", "cols": 30, "rows": 10})
    res = await ws.request({"type": "take_control", "pane_id": "w1:locked"})
    assert res["ok"] is False and res["error"]["code"] == "stream_failed"
    assert "takeover" in res["error"]["message"]
    await ws.frame("w1:locked", b"observe:w1:locked:30x10")
    res = await ws.request({"type": "take_control", "pane_id": "w1:locked", "takeover": True})
    assert res["ok"] and res["data"]["mode"] == "control"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_diff(fake: FakeHerdr, ws: WS, tmp_path, bridge: Bridge):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    git("add", ".")
    git("commit", "-qm", "init")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    (tmp_path / "new.txt").write_text("x\n")
    fake.snapshot["panes"][0]["foreground_cwd"] = str(tmp_path)
    await bridge.tracker.refresh()

    res = await ws.request({"type": "diff", "pane_id": "w1:p1"})
    assert res["ok"], res
    d = res["data"]
    assert d["branch"] == "main"
    assert "+two" in d["diff"] and "a.txt" in d["stat"]
    assert d["untracked"] == ["new.txt"] and d["truncated"] is False

    fake.snapshot["panes"][0]["foreground_cwd"] = str(tmp_path / "nope")
    await bridge.tracker.refresh()
    res = await ws.request({"type": "diff", "pane_id": "w1:p1"})
    assert res["ok"] is False and res["error"]["code"] == "git_failed"


async def test_failed_restart_after_a_pc_resize_closes_the_stream(fake: FakeHerdr, ws: WS):
    pane = {"pane_id": "w1:p2", "focused": False, "rect": {"x": 0, "y": 0, "width": 100, "height": 39}}
    fake.snapshot["layouts"] = [{"tab_id": "w1:t1", "panes": [pane]}]
    await fake.push_event("layout_updated", {"tab_id": "w1:t1"})
    await ws.recv_until(lambda m: m["type"] == "snapshot" and m.get("pane_sizes"))
    await ws.request({"type": "open_stream", "pane_id": "w1:p2"})
    await ws.frame("w1:p2", b"observe:w1:p2:100x39")

    pane["rect"]["width"] = 999  # the fake herdr command refuses this width
    await fake.push_event("layout_updated", {"tab_id": "w1:t1"})
    closed = await ws.recv_until(lambda m: m["type"] == "stream" and m.get("mode") == "closed")
    assert closed["pane_id"] == "w1:p2" and "too wide" in closed["reason"]


async def test_slow_client_is_disconnected_at_once(http: TestClient, bridge: Bridge, monkeypatch, caplog):
    monkeypatch.setattr("herdr_bridge.server.MAX_QUEUE", 10)
    raw = await http.ws_connect(f"/ws?token={TOKEN}")
    await raw.receive_json()  # hello: the session exists now
    session = next(iter(bridge.clients))
    for n in range(200):  # no await in between, so nothing is written meanwhile
        session.send({"type": "frame", "n": n})
    assert session.queue.qsize() == 1  # only the close is left
    assert [r.message for r in caplog.records].count("client 127.0.0.1 too slow; disconnecting") == 1

    received = 0
    while (m := await raw.receive(timeout=5)).type == WSMsgType.TEXT:
        received += 1
    assert m.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
    assert received <= 1  # at most the snapshot that was already on its way
    for _ in range(100):
        if not bridge.clients:
            break
        await asyncio.sleep(0.05)
    assert not bridge.clients


async def test_disconnect_kills_streams(http: TestClient, bridge: Bridge):
    raw = await http.ws_connect(f"/ws?token={TOKEN}")
    w = WS(raw)
    await w.request({"type": "open_stream", "pane_id": "w1:p1"})
    session = next(iter(bridge.clients))
    proc = session.streams["w1:p1"]._proc
    await raw.close()
    for _ in range(100):
        if not bridge.clients and proc.returncode is not None:
            break
        await asyncio.sleep(0.05)
    assert not bridge.clients
    assert proc.returncode is not None
