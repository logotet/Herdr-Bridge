"""Keeps an authoritative cache of herdr state and pushes changes to listeners.

Every herdr event is treated as an invalidation signal: we re-read ``session.snapshot``
(debounced), diff agent statuses to produce transitions, and broadcast the new state.
A periodic refresh acts as a safety net for missed events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .herdr_client import EventsLost, HerdrClient, HerdrError, HerdrInfo

log = logging.getLogger(__name__)

GLOBAL_EVENTS = [
    "workspace.created", "workspace.updated", "workspace.renamed", "workspace.moved",
    "workspace.reordered", "workspace.closed", "workspace.focused",
    "worktree.created", "worktree.opened", "worktree.removed",
    "tab.created", "tab.closed", "tab.focused", "tab.renamed", "tab.moved",
    "pane.created", "pane.closed", "pane.updated", "pane.focused", "pane.moved",
    "pane.exited", "pane.agent_detected", "layout.updated",
]

Listener = Callable[[dict[str, Any]], Awaitable[None]]

PREVIEW_LINES = 3
PREVIEW_MAX_AGE = 8.0


def _preview_from_text(text: str, lines: int = PREVIEW_LINES) -> str:
    rows = [r.rstrip() for r in text.splitlines()]
    rows = [r for r in rows if r.strip()]
    return "\n".join(r[:200] for r in rows[-lines:])


def _read_text(result: dict[str, Any]) -> str:
    for key in ("text", "content", "output"):
        if isinstance(result.get(key), str):
            return result[key]
    read = result.get("read")
    if isinstance(read, dict):
        return _read_text(read)
    if isinstance(result.get("lines"), list):
        return "\n".join(str(x) for x in result["lines"])
    return ""


class StateTracker:
    def __init__(self, client: HerdrClient, debounce: float = 0.15, poll_interval: float = 5.0):
        self.client = client
        self.debounce = debounce
        self.poll_interval = poll_interval
        self.info: HerdrInfo | None = None
        self.available = False
        self.snapshot: dict[str, Any] = {}
        self.previews: dict[str, str] = {}
        self._preview_at: dict[str, float] = {}
        self._listeners: list[Listener] = []
        self._dirty = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._pane_ids: frozenset[str] = frozenset()
        self._resubscribe = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_sent: str = ""
        self.ready = asyncio.Event()

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(fn)

    async def _emit(self, msg: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                await fn(msg)
            except Exception:
                log.exception("listener failed")

    # ---- public helpers ----
    def snapshot_message(self) -> dict[str, Any]:
        s = self.snapshot
        return {
            "type": "snapshot",
            "workspaces": s.get("workspaces", []),
            "tabs": s.get("tabs", []),
            "panes": s.get("panes", []),
            "agents": s.get("agents", []),
            "focused_workspace_id": s.get("focused_workspace_id"),
            "focused_tab_id": s.get("focused_tab_id"),
            "focused_pane_id": s.get("focused_pane_id"),
            "pane_sizes": self.pane_sizes(),
            "previews": {k: v for k, v in self.previews.items() if v},
        }

    def pane_sizes(self) -> dict[str, list[int]]:
        """Real (PC) grid size of each pane, ``[cols, rows]``, from the snapshot's tab layouts."""
        sizes: dict[str, list[int]] = {}
        for layout in self.snapshot.get("layouts") or []:
            for p in layout.get("panes") or []:
                rect = p.get("rect") or {}
                w, h = rect.get("width"), rect.get("height")
                if p.get("pane_id") and isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                    sizes[p["pane_id"]] = [w, h]
        return sizes

    def pane_size(self, pane_id: str) -> tuple[int, int] | None:
        size = self.pane_sizes().get(pane_id)
        return (size[0], size[1]) if size else None

    def herdr_info(self) -> dict[str, Any]:
        return {
            "version": self.info.version if self.info else None,
            "protocol": self.info.protocol if self.info else None,
            "available": self.available,
        }

    def find_pane(self, pane_id: str) -> dict[str, Any] | None:
        for p in self.snapshot.get("panes", []):
            if p.get("pane_id") == pane_id:
                return p
        return None

    def mark_dirty(self) -> None:
        self._dirty.set()

    # ---- lifecycle ----
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._subscription_loop(), name="herdr-subscribe"),
            asyncio.create_task(self._refresh_loop(), name="herdr-refresh"),
            asyncio.create_task(self._poll_loop(), name="herdr-poll"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _set_available(self, available: bool) -> None:
        if available != self.available:
            self.available = available
            log.info("herdr %s", "available" if available else "unavailable")
            await self._emit({"type": "herdr_status", **self.herdr_info()})

    async def _subscription_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                self.info = await self.client.ping()
                await self.refresh()
                subs = [{"type": t} for t in GLOBAL_EVENTS]
                subs += [{"type": "pane.agent_status_changed", "pane_id": p}
                         for p in sorted(self._pane_ids)]
                self._resubscribe.clear()
                events = self.client.subscribe(subs)
                backoff = 1.0
                next_event = asyncio.ensure_future(anext(events))
                resub = asyncio.ensure_future(self._resubscribe.wait())
                try:
                    while True:
                        done, _ = await asyncio.wait({next_event, resub},
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if resub in done:
                            break
                        try:
                            next_event.result()
                        except StopAsyncIteration:
                            log.info("herdr subscription ended; resubscribing")
                            break
                        self._dirty.set()
                        next_event = asyncio.ensure_future(anext(events))
                finally:
                    next_event.cancel()
                    resub.cancel()
                    with contextlib.suppress(BaseException):
                        await next_event
                    await events.aclose()
            except asyncio.CancelledError:
                raise
            except EventsLost:
                log.warning("herdr events lost; resyncing")
            except HerdrError as e:
                # A referenced pane vanished (subscription rejected) or herdr is down.
                log.info("herdr subscription error: %s", e)
                with contextlib.suppress(Exception):
                    self.info = await self.client.ping()
                    await self.refresh()
                    await asyncio.sleep(0.3)
                    continue
                await self._set_available(False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            except Exception:
                log.exception("subscription loop error")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def _refresh_loop(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(self.debounce)
            self._dirty.clear()
            with contextlib.suppress(HerdrError):
                await self.refresh()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            self._dirty.set()

    async def refresh(self) -> None:
        async with self._refresh_lock:
            try:
                snap = await self.client.snapshot()
            except HerdrError:
                await self._set_available(False)
                raise
            await self._set_available(True)
            old_agents = {a.get("pane_id"): a for a in self.snapshot.get("agents", [])}
            had_snapshot = bool(self.snapshot)
            self.snapshot = snap
            new_agents = {a.get("pane_id"): a for a in snap.get("agents", [])}

            transitions = []
            for pid, agent in new_agents.items():
                old = old_agents.get(pid)
                new_status = agent.get("agent_status")
                old_status = old.get("agent_status") if old else None
                if had_snapshot and old_status != new_status and (old or new_status == "blocked"):
                    transitions.append(self._transition(agent, old_status or "unknown"))

            await self._update_previews(new_agents, changed={t["pane_id"] for t in transitions})

            pane_ids = frozenset(p.get("pane_id") for p in snap.get("panes", []) if p.get("pane_id"))
            if pane_ids != self._pane_ids:
                self._pane_ids = pane_ids
                self._resubscribe.set()

            self.ready.set()
            msg = self.snapshot_message()
            encoded = json.dumps(msg, sort_keys=True)
            if encoded != self._last_sent:
                self._last_sent = encoded
                await self._emit(msg)
            for t in transitions:
                await self._emit(t)

    def _transition(self, agent: dict[str, Any], old_status: str) -> dict[str, Any]:
        ws_label = None
        for ws in self.snapshot.get("workspaces", []):
            if ws.get("workspace_id") == agent.get("workspace_id"):
                ws_label = ws.get("label")
        return {
            "type": "agent_status",
            "pane_id": agent.get("pane_id"),
            "workspace_id": agent.get("workspace_id"),
            "agent": agent.get("agent"),
            "from": old_status,
            "to": agent.get("agent_status"),
            "title": agent.get("terminal_title_stripped") or agent.get("label") or agent.get("agent"),
            "workspace_label": ws_label,
        }

    async def _update_previews(self, agents: dict[str, dict[str, Any]], changed: set[str]) -> None:
        now = time.monotonic()
        for pid in list(self.previews):
            if pid not in agents:
                self.previews.pop(pid, None)
                self._preview_at.pop(pid, None)
        stale = [
            pid for pid, a in agents.items()
            if pid in changed or pid not in self.previews
            or (a.get("agent_status") == "working" and now - self._preview_at.get(pid, 0) > PREVIEW_MAX_AGE)
        ]
        if not stale:
            return
        sem = asyncio.Semaphore(4)

        async def one(pid: str) -> None:
            async with sem:
                try:
                    res = await self.client.request(
                        "pane.read",
                        {"pane_id": pid, "source": "recent_unwrapped", "lines": 15, "strip_ansi": True},
                    )
                    self.previews[pid] = _preview_from_text(_read_text(res))
                except HerdrError as e:
                    log.debug("preview read failed for %s: %s", pid, e)
                self._preview_at[pid] = time.monotonic()

        await asyncio.gather(*(one(p) for p in stale))
