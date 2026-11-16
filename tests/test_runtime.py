from __future__ import annotations

import asyncio

from app.database.models import SourceState
from app.mirror import runtime


def test_load_returns_none_when_unconfigured() -> None:
    assert asyncio.run(runtime.load()) is None
    assert runtime.destination() is None
    assert runtime.sources() == []


def test_destination_and_sources_survive_a_reload() -> None:
    async def scenario() -> None:
        await runtime.set_dest(-100222, "Archive")
        first, created = await runtime.add_source(-100111, "Source A")
        second, _ = await runtime.add_source(-100333, "Source B", history_sync=False)
        assert created is True and first.id != second.id

        # Simulate a restart: drop the in-process cache, re-read the database.
        await runtime.reset()
        destination = await runtime.load()

        assert destination is not None and destination.chat_id == -100222
        assert destination.title == "Archive"
        assert [(s.chat_id, s.title, s.history_sync) for s in runtime.sources()] == [
            (-100111, "Source A", True),
            (-100333, "Source B", False),
        ]

    asyncio.run(scenario())


def test_adding_the_same_source_twice_updates_instead_of_duplicating() -> None:
    async def scenario() -> None:
        await runtime.add_source(-100111, "Old title")
        _info, created = await runtime.add_source(-100111, "New title")
        assert created is False
        assert [s.title for s in runtime.sources()] == ["New title"]

    asyncio.run(scenario())


def test_enable_disable_remove_and_history_flag() -> None:
    async def scenario() -> None:
        info, _ = await runtime.add_source(-100111, "A")
        assert runtime.is_enabled_source(-100111) is True

        await runtime.set_source_enabled(info.id, False)
        assert runtime.is_enabled_source(-100111) is False
        assert runtime.get_source(info.id).enabled is False

        await runtime.set_source_history_sync(info.id, False)
        assert runtime.get_source_by_chat(-100111).history_sync is False

        await runtime.mark_source_health(info.id, SourceState.ERROR, "ChannelPrivate")
        assert runtime.get_source(info.id).state == "error"

        assert await runtime.remove_source(info.id) is True
        assert runtime.sources() == []
        assert await runtime.remove_source(info.id) is False

        await runtime.reset()
        await runtime.load()
        assert runtime.sources() == []  # removal was persisted

    asyncio.run(scenario())


def test_changing_the_destination_keeps_the_sources() -> None:
    async def scenario() -> None:
        await runtime.add_source(-100111, "A")
        await runtime.set_dest(-100222, "Old")
        await runtime.set_dest(-100999, "New")
        assert runtime.destination().chat_id == -100999
        assert [s.chat_id for s in runtime.sources()] == [-100111]

    asyncio.run(scenario())


def test_ensure_backlog_is_a_noop_without_destination_or_without_sync_flag(monkeypatch) -> None:
    started: list[int] = []

    async def fake_sync_source(_client, source, _dest) -> None:
        started.append(source.chat_id)

    monkeypatch.setattr("app.mirror.backlog.sync_source", fake_sync_source)

    async def scenario() -> None:
        await runtime.add_source(-100111, "A")
        await runtime.ensure_backlog_running(object(), sync_history=True)  # no destination yet
        assert runtime.is_backlog_running() is False

        await runtime.set_dest(-100222, "Dest")
        await runtime.ensure_backlog_running(object(), sync_history=False)  # flag off
        assert runtime.is_backlog_running() is False
        assert started == []

    asyncio.run(scenario())
