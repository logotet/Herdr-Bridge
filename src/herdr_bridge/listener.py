"""Serve the aiohttp app on an address that may come and go (e.g. Wi-Fi with a static IP)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from aiohttp import web

from .config import address_is_up

log = logging.getLogger(__name__)


async def serve(app: web.Application, host: str, port: int, *, interval: float = 5.0,
                is_up: Callable[[str], bool] = address_is_up,
                stop: asyncio.Event | None = None) -> None:
    """Listen on host:port while the address is up; wait and rebind when it reappears.

    The app (and its startup/cleanup hooks, i.e. the herdr connection) lives for the whole run;
    only the listening socket follows the address.
    """
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site: web.TCPSite | None = None
    last_state = ""
    try:
        while True:
            up = is_up(host)
            if up and site is None:
                candidate = web.TCPSite(runner, host, port)
                try:
                    await candidate.start()
                    site = candidate
                    log.info("listening on %s:%s", host, port)
                    last_state = "listening"
                except OSError as e:
                    await candidate.stop()
                    if last_state != "error":
                        log.warning("cannot listen on %s:%s (%s); retrying", host, port, e)
                    last_state = "error"
            elif not up and site is not None:
                log.warning("%s went down; waiting for it to come back", host)
                await site.stop()
                site = None
                last_state = "waiting"
            elif not up and last_state != "waiting":
                log.info("waiting for %s to come up", host)
                last_state = "waiting"
            if stop is None:
                await asyncio.sleep(interval)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), interval)
                    return
                except TimeoutError:
                    pass
    finally:
        await runner.cleanup()
