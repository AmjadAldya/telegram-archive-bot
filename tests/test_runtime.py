from __future__ import annotations

import asyncio

import app.mirror.backlog as backlog_module
from app.mirror import runtime


def test_load_returns_none_when_unconfigured() -> None:
    result = asyncio.run(runtime.load())
    assert result is None
    assert runtime.current() is None


def test_set_source_and_dest_populates_current_pair() -> None:
    async def scenario() -> None:
        await runtime.set_source(-100111, "Source Group")
        pair = await runtime.set_dest(-100222, "Dest Channel")

        assert pair is not None
        assert pair.source_chat_id == -100111
        assert pair.source_title == "Source Group"
        assert pair.dest_chat_id == -100222
        assert pair.dest_title == "Dest Channel"
        assert runtime.current() == pair

    asyncio.run(scenario())


def test_ensure_backlog_running_starts_once_and_reacts_to_pair_changes(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_sync_backlog(_client) -> None:
        calls["count"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(backlog_module, "sync_backlog", fake_sync_backlog)

    async def scenario() -> None:
        await runtime.set_source(-100111, "Source")
        await runtime.set_dest(-100222, "Dest")

        await runtime.ensure_backlog_running(object(), sync_history=True)
        await asyncio.sleep(0)  # let the freshly created task actually start
        assert runtime.is_backlog_running() is True
        assert calls["count"] == 1

        # Same pair, still running: no restart.
        await runtime.ensure_backlog_running(object(), sync_history=True)
        assert calls["count"] == 1

        # Destination changed: the old task is cancelled and a new one starts.
        await runtime.set_dest(-100333, "New Dest")
        await runtime.ensure_backlog_running(object(), sync_history=True)
        await asyncio.sleep(0)
        assert calls["count"] == 2

        # /resync forces a restart even for the same pair.
        await runtime.ensure_backlog_running(object(), sync_history=True, force=True)
        await asyncio.sleep(0)
        assert calls["count"] == 3

        await runtime.shutdown()
        assert runtime.is_backlog_running() is False

    asyncio.run(scenario())


def test_ensure_backlog_running_skips_without_sync_history(monkeypatch) -> None:
    async def fake_sync_backlog(_client) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(backlog_module, "sync_backlog", fake_sync_backlog)

    async def scenario() -> None:
        await runtime.set_source(-100111, "Source")
        await runtime.set_dest(-100222, "Dest")

        await runtime.ensure_backlog_running(object(), sync_history=False)
        assert runtime.is_backlog_running() is False

    asyncio.run(scenario())


def test_ensure_backlog_running_noop_without_configured_pair() -> None:
    async def scenario() -> None:
        await runtime.ensure_backlog_running(object(), sync_history=True)
        assert runtime.is_backlog_running() is False

    asyncio.run(scenario())
