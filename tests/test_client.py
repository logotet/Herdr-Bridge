from __future__ import annotations

import asyncio
import sys
import uuid

import pytest

from herdr_bridge.herdr_client import EventsLost, HerdrClient, HerdrError, HerdrUnavailable

from .fake_herdr import FakeHerdr


async def test_ping_and_snapshot(client: HerdrClient):
    info = await client.ping()
    assert info.protocol == 19
    snap = await client.snapshot()
    assert [p["pane_id"] for p in snap["panes"]] == ["w1:p1", "w1:p2"]


async def test_request_error(client: HerdrClient):
    with pytest.raises(HerdrError) as e:
        await client.request("pane.get", {"pane_id": "nope"})
    assert e.value.code == "pane_not_found"


async def test_unavailable():
    c = HerdrClient("tcp:127.0.0.1:1", timeout=1)
    with pytest.raises(HerdrUnavailable):
        await c.ping()


async def test_no_answer_in_time_is_unavailable(fake: FakeHerdr):
    fake.hang.add("ping")
    with pytest.raises(HerdrUnavailable, match="did not answer ping"):
        await HerdrClient(fake.address, timeout=0.2).ping()


async def test_malformed_reply_is_a_herdr_error(fake: FakeHerdr, client: HerdrClient):
    fake.garbage.add("ping")
    with pytest.raises(HerdrError) as e:
        await client.ping()
    assert e.value.code == "herdr_error"


async def test_subscribe_yields_events(fake: FakeHerdr, client: HerdrClient):
    events = client.subscribe([{"type": "pane.created"}])
    nxt = asyncio.ensure_future(anext(events))
    for _ in range(50):
        if fake.subscriptions:
            break
        await asyncio.sleep(0.02)
    await fake.push_event("pane_created", {"pane_id": "w1:p3"})
    ev = await asyncio.wait_for(nxt, 2)
    assert ev["event"] == "pane_created"
    assert ev["data"]["pane_id"] == "w1:p3"
    await events.aclose()


async def test_subscribe_rejects_unknown_pane(client: HerdrClient):
    events = client.subscribe([{"type": "pane.agent_status_changed", "pane_id": "w9:p9"}])
    with pytest.raises(HerdrError):
        await anext(events)


async def test_events_lost(fake: FakeHerdr, client: HerdrClient):
    events = client.subscribe([{"type": "pane.created"}])
    nxt = asyncio.ensure_future(anext(events))
    for _ in range(50):
        if fake.subscriptions:
            break
        await asyncio.sleep(0.02)
    await fake.push_raw({"error": {"code": "events_lost", "message": "lagged"}})
    with pytest.raises(EventsLost):
        await asyncio.wait_for(nxt, 2)


async def test_subscription_ends_when_server_closes(fake: FakeHerdr, client: HerdrClient):
    events = client.subscribe([{"type": "pane.created"}])
    nxt = asyncio.ensure_future(anext(events))
    for _ in range(50):
        if fake.subscriptions:
            break
        await asyncio.sleep(0.02)
    fake.drop_subscribers()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(nxt, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows-only")
async def test_named_pipe_transport():
    f = FakeHerdr()
    name = f"herdr-bridge-test-{uuid.uuid4().hex}"
    address = await f.start_pipe(name)
    try:
        c = HerdrClient(address, timeout=5)
        results = await asyncio.gather(*(c.ping() for _ in range(5)))
        assert all(r.protocol == 19 for r in results)
        assert (await c.snapshot())["panes"]
    finally:
        await f.stop()
