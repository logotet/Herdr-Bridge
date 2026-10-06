from __future__ import annotations

import asyncio
from typing import Any

from herdr_bridge.herdr_client import HerdrClient
from herdr_bridge.state import StateTracker, _preview_from_text

from .fake_herdr import FakeHerdr


async def _wait(cond, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


def test_preview_from_text():
    assert _preview_from_text("a\n\n b \nc\n  \nd\n") == " b\nc\nd"
    assert _preview_from_text("") == ""


async def test_refresh_emits_snapshot_once(client: HerdrClient):
    t = StateTracker(client)
    got: list[dict[str, Any]] = []

    async def on(m):
        got.append(m)

    t.add_listener(on)
    await t.refresh()
    await t.refresh()
    snaps = [m for m in got if m["type"] == "snapshot"]
    assert len(snaps) == 1
    assert snaps[0]["previews"]["w1:p1"] == "line one\nline two\nwaiting for input"
    assert next(m for m in got if m["type"] == "herdr_status")["available"] is True


async def test_transitions(fake: FakeHerdr, client: HerdrClient):
    t = StateTracker(client)
    got: list[dict[str, Any]] = []

    async def on(m):
        got.append(m)

    t.add_listener(on)
    await t.refresh()
    fake.set_status("w1:p1", "blocked")
    await t.refresh()
    trans = [m for m in got if m["type"] == "agent_status"]
    assert len(trans) == 1
    assert trans[0]["from"] == "working" and trans[0]["to"] == "blocked"
    assert trans[0]["title"] == "Fix tests"
    assert trans[0]["workspace_label"] == "api"
    # snapshot is emitted before the transition so clients see the new state first
    assert got[-1]["type"] == "agent_status" and got[-2]["type"] == "snapshot"


async def test_new_agent_only_emits_when_blocked(fake: FakeHerdr, client: HerdrClient):
    t = StateTracker(client)
    got: list[dict[str, Any]] = []

    async def on(m):
        got.append(m)

    t.add_listener(on)
    await t.refresh()
    fake.snapshot["agents"].append({**fake.snapshot["panes"][1], "agent": "copilot",
                                    "agent_status": "working"})
    await t.refresh()
    assert not [m for m in got if m["type"] == "agent_status"]
    fake.snapshot["agents"].append({"pane_id": "w1:p9", "workspace_id": "w1", "agent": "claude",
                                    "agent_status": "blocked"})
    await t.refresh()
    trans = [m for m in got if m["type"] == "agent_status"]
    assert len(trans) == 1 and trans[0]["pane_id"] == "w1:p9" and trans[0]["from"] == "unknown"


async def test_herdr_down_emits_unavailable(fake: FakeHerdr, client: HerdrClient):
    t = StateTracker(client)
    got: list[dict[str, Any]] = []

    async def on(m):
        got.append(m)

    t.add_listener(on)
    await t.refresh()
    await fake.stop()
    try:
        await t.refresh()
    except Exception:  # noqa: BLE001
        pass
    statuses = [m["available"] for m in got if m["type"] == "herdr_status"]
    assert statuses == [True, False]


async def test_events_trigger_refresh_and_resubscribe(fake: FakeHerdr, client: HerdrClient):
    t = StateTracker(client, debounce=0.01, poll_interval=60)
    await t.start()
    try:
        await _wait(lambda: len(fake.subscriptions) >= 1)
        first = fake.subscriptions[-1]
        pane_subs = {s["pane_id"] for s in first if "pane_id" in s}
        # the first subscription may predate the pane-set; ensure we end up subscribed per pane
        await _wait(lambda: any({s.get("pane_id") for s in subs} >= {"w1:p1", "w1:p2"}
                                for subs in fake.subscriptions))
        n = len(fake.subscriptions)
        fake.add_pane("w1:p3")
        await fake.push_event("pane_created", {"pane_id": "w1:p3"})
        await _wait(lambda: t.find_pane("w1:p3") is not None)
        await _wait(lambda: len(fake.subscriptions) > n)
        assert "w1:p3" in {s.get("pane_id") for s in fake.subscriptions[-1]}
        assert pane_subs <= {"w1:p1", "w1:p2"}
    finally:
        await t.stop()
