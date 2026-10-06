from __future__ import annotations

import sys
from pathlib import Path

import pytest

from herdr_bridge.herdr_client import HerdrClient

from .fake_herdr import FakeHerdr

FAKE_CLI = [sys.executable, str(Path(__file__).with_name("fake_herdr_cli.py"))]


@pytest.fixture
async def fake():
    f = FakeHerdr()
    await f.start_tcp()
    yield f
    await f.stop()


@pytest.fixture
def client(fake: FakeHerdr) -> HerdrClient:
    return HerdrClient(fake.address, timeout=5)
