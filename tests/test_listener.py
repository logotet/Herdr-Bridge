from __future__ import annotations

import asyncio
import socket

import aiohttp
from aiohttp import web

from herdr_bridge import listener
from herdr_bridge.config import address_is_up


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _reachable(port: int) -> bool:
    try:
        async with aiohttp.ClientSession() as http, http.get(
                f"http://127.0.0.1:{port}/health", timeout=aiohttp.ClientTimeout(total=1)) as r:
            return r.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


async def _wait_for(port: int, want: bool) -> bool:
    for _ in range(50):
        if await _reachable(port) == want:
            return True
        await asyncio.sleep(0.05)
    return False


def test_address_is_up_basics():
    assert address_is_up("127.0.0.1")
    assert address_is_up("0.0.0.0")
    assert address_is_up("my-host.example")
    assert not address_is_up("203.0.113.77")  # TEST-NET-3, never assigned


async def test_serve_waits_and_rebinds():
    events: list[str] = []
    app = web.Application()

    async def health(_req: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def on_startup(_app: web.Application) -> None:
        events.append("startup")

    async def on_cleanup(_app: web.Application) -> None:
        events.append("cleanup")

    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    port = _free_port()
    up = {"v": False}
    stop = asyncio.Event()
    task = asyncio.create_task(listener.serve(app, "127.0.0.1", port, interval=0.05,
                                              is_up=lambda _h: up["v"], stop=stop))
    await asyncio.sleep(0.2)
    assert not await _reachable(port)  # address down: not listening yet

    up["v"] = True
    assert await _wait_for(port, True)

    up["v"] = False  # e.g. Wi-Fi dropped
    assert await _wait_for(port, False)

    up["v"] = True  # back again: rebinds
    assert await _wait_for(port, True)

    stop.set()
    await asyncio.wait_for(task, 2)
    assert events == ["startup", "cleanup"]  # app/herdr connection survived the rebinds


async def test_serve_retries_when_port_busy():
    port = _free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    app = web.Application()

    async def health(_req: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", health)
    stop = asyncio.Event()
    task = asyncio.create_task(listener.serve(app, "127.0.0.1", port, interval=0.05,
                                              is_up=lambda _h: True, stop=stop))
    await asyncio.sleep(0.2)
    assert not task.done()
    blocker.close()
    assert await _wait_for(port, True)
    stop.set()
    await asyncio.wait_for(task, 2)
