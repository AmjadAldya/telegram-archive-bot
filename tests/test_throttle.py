from __future__ import annotations

import asyncio

import pytest
from pyrogram.errors import FloodWait, PeerFlood

from app.mirror.throttle import AccountRestrictedError, call_with_flood_wait


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def test_call_with_flood_wait_retries_then_succeeds() -> None:
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise FloodWait(1)
        return "ok"

    result = asyncio.run(call_with_flood_wait(flaky, max_retries=5))
    assert result == "ok"
    assert attempts["count"] == 3


def test_call_with_flood_wait_gives_up_after_max_retries() -> None:
    async def always_flooded() -> str:
        raise FloodWait(1)

    with pytest.raises(FloodWait):
        asyncio.run(call_with_flood_wait(always_flooded, max_retries=2))


def test_call_with_flood_wait_raises_account_restricted_on_peer_flood() -> None:
    async def flooded_account() -> str:
        raise PeerFlood()

    with pytest.raises(AccountRestrictedError):
        asyncio.run(call_with_flood_wait(flooded_account, max_retries=5))
