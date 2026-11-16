"""Process-wide mirror configuration: one destination, many sources.

The destination lives in `mirror_config`; sources live in the `sources`
table. Both are cached here and re-read from the database on `load()`.
Backlog scanning is handled by a single background loop that walks the
enabled sources one after another (never in parallel, so FloodWait pressure
stays flat) and resumes each from its own cursor in `sync_state`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.database.base import session_scope
from app.database.models import Source, SourceState
from app.database.repositories import MirrorRepository
from app.services.logger import logger


@dataclass(frozen=True, slots=True)
class Destination:
    chat_id: int
    title: str


@dataclass(frozen=True, slots=True)
class SourceInfo:
    id: int  # database id (short, used in callback data)
    chat_id: int
    title: str
    enabled: bool
    history_sync: bool
    state: str
    last_error: str | None


_destination: Destination | None = None
_sources: dict[int, SourceInfo] = {}  # keyed by Telegram chat id

_client = None
_loop_task: asyncio.Task | None = None
_source_task: asyncio.Task | None = None
_running_chat_id: int | None = None
_auto = False  # also scan sources that were not explicitly requested
_forced: list[int] = []
_lock = asyncio.Lock()


def _to_info(row: Source) -> SourceInfo:
    return SourceInfo(
        id=row.id,
        chat_id=int(row.chat_id),
        title=row.title or row.chat_id,
        enabled=bool(row.enabled),
        history_sync=bool(row.history_sync),
        state=row.state,
        last_error=row.last_error,
    )


def _load_sync() -> tuple[Destination | None, list[SourceInfo]]:
    with session_scope() as session:
        repository = MirrorRepository(session)
        config = repository.get_config()
        destination = (
            Destination(int(config.dest_chat_id), config.dest_title or config.dest_chat_id)
            if config.dest_chat_id
            else None
        )
        return destination, [_to_info(row) for row in repository.list_sources()]


async def load() -> Destination | None:
    """(Re)load the destination and sources from the database into the cache."""
    global _destination
    destination, sources = await asyncio.to_thread(_load_sync)
    _destination = destination
    _sources.clear()
    _sources.update({info.chat_id: info for info in sources})
    return _destination


# -- read access --------------------------------------------------------------


def destination() -> Destination | None:
    return _destination


def sources() -> list[SourceInfo]:
    return sorted(_sources.values(), key=lambda info: info.id)


def get_source(source_id: int) -> SourceInfo | None:
    return next((info for info in _sources.values() if info.id == source_id), None)


def get_source_by_chat(chat_id: int) -> SourceInfo | None:
    return _sources.get(chat_id)


def is_enabled_source(chat_id: int) -> bool:
    info = _sources.get(chat_id)
    return info is not None and info.enabled


def running_source_chat_id() -> int | None:
    return _running_chat_id


# -- destination ---------------------------------------------------------------


def _set_dest_sync(chat_id: int, title: str) -> None:
    with session_scope() as session:
        MirrorRepository(session).set_dest(str(chat_id), title)


async def set_dest(chat_id: int, title: str) -> Destination | None:
    """Change the destination. Cancels a running backlog (its cursors belong to the old one)."""
    await cancel_backlog()
    await asyncio.to_thread(_set_dest_sync, chat_id, title)
    return await _reload_keep_sources()


async def _reload_keep_sources() -> Destination | None:
    return await load()


# -- sources -------------------------------------------------------------------


def _add_source_sync(chat_id: int, title: str, history_sync: bool) -> tuple[SourceInfo, bool]:
    with session_scope() as session:
        row, created = MirrorRepository(session).add_source(
            str(chat_id), title, history_sync=history_sync
        )
        return _to_info(row), created


async def add_source(
    chat_id: int, title: str, *, history_sync: bool = True
) -> tuple[SourceInfo, bool]:
    info, created = await asyncio.to_thread(_add_source_sync, chat_id, title, history_sync)
    _sources[info.chat_id] = info
    return info, created


def _remove_source_sync(source_id: int) -> bool:
    with session_scope() as session:
        return MirrorRepository(session).remove_source(source_id)


async def remove_source(source_id: int) -> bool:
    info = get_source(source_id)
    if info is None:
        return False
    await cancel_source(info.chat_id)
    removed = await asyncio.to_thread(_remove_source_sync, source_id)
    _sources.pop(info.chat_id, None)
    return removed


def _update_source_sync(source_id: int, **fields) -> SourceInfo | None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        if "enabled" in fields:
            repository.set_source_enabled(source_id, fields["enabled"])
        if "history_sync" in fields:
            repository.set_source_history_sync(source_id, fields["history_sync"])
        row = repository.get_source(source_id)
        return _to_info(row) if row is not None else None


async def set_source_enabled(source_id: int, enabled: bool) -> SourceInfo | None:
    info = get_source(source_id)
    if info is None:
        return None
    if not enabled:
        await cancel_source(info.chat_id)
    updated = await asyncio.to_thread(_update_source_sync, source_id, enabled=enabled)
    if updated is not None:
        _sources[updated.chat_id] = updated
    return updated


async def set_source_history_sync(source_id: int, history_sync: bool) -> SourceInfo | None:
    info = get_source(source_id)
    if info is None:
        return None
    if not history_sync:
        await cancel_source(info.chat_id)
    updated = await asyncio.to_thread(_update_source_sync, source_id, history_sync=history_sync)
    if updated is not None:
        _sources[updated.chat_id] = updated
    return updated


def _health_sync(source_id: int, state: SourceState, error: str | None) -> SourceInfo | None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        repository.set_source_health(source_id, state, error)
        row = repository.get_source(source_id)
        return _to_info(row) if row is not None else None


async def mark_source_health(source_id: int, state: SourceState, error: str | None) -> None:
    updated = await asyncio.to_thread(_health_sync, source_id, state, error)
    if updated is not None:
        _sources[updated.chat_id] = updated


# -- backlog manager -------------------------------------------------------------


def _next_candidate(attempted: set[int]) -> SourceInfo | None:
    while _forced:
        chat_id = _forced.pop(0)
        info = _sources.get(chat_id)
        if info is not None and info.enabled:
            attempted.add(chat_id)
            return info
    if _auto:
        for info in sources():
            if info.enabled and info.history_sync and info.chat_id not in attempted:
                attempted.add(info.chat_id)
                return info
    return None


async def _backlog_loop() -> None:
    global _loop_task, _source_task, _running_chat_id
    from app.mirror.backlog import sync_source  # deferred: avoids a module cycle

    attempted: set[int] = set()
    while True:
        async with _lock:
            destination_now = _destination
            info = _next_candidate(attempted) if destination_now is not None else None
            if destination_now is None or info is None:
                _loop_task = None  # decided under the lock so a concurrent add can restart us
                return
        _running_chat_id = info.chat_id
        _source_task = asyncio.ensure_future(sync_source(_client, info, destination_now.chat_id))
        try:
            await asyncio.gather(_source_task, return_exceptions=True)
        finally:
            _source_task = None
            _running_chat_id = None


async def ensure_backlog_running(
    client, sync_history: bool, force_sources: list[int] | None = None
) -> None:
    """Start (or extend) the backlog loop.

    `sync_history` is the SYNC_HISTORY_ON_START semantic: when False nothing
    starts automatically. `force_sources` (chat ids) are scanned regardless
    of that flag or of a source's own history_sync flag - used by /resync.
    """
    global _client, _loop_task, _auto
    async with _lock:
        if _destination is None:
            return
        _client = client
        if force_sources:
            for chat_id in force_sources:
                if chat_id not in _forced:
                    _forced.append(chat_id)
        if sync_history:
            _auto = True
        if not _forced and not _auto:
            return
        if _loop_task is None or _loop_task.done():
            _loop_task = asyncio.ensure_future(_backlog_loop())


async def cancel_source(chat_id: int) -> None:
    if _running_chat_id == chat_id and _source_task is not None and not _source_task.done():
        _source_task.cancel()
        await asyncio.gather(_source_task, return_exceptions=True)


async def cancel_backlog() -> None:
    """Stop the whole backlog loop (destination changed / shutdown)."""
    global _loop_task, _source_task
    task, source_task = _loop_task, _source_task
    if task is not None and not task.done():
        task.cancel()
    if source_task is not None and not source_task.done():
        source_task.cancel()
    pending = [t for t in (task, source_task) if t is not None]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _loop_task = None
    _source_task = None
    _forced.clear()


def is_backlog_running() -> bool:
    return _loop_task is not None and not _loop_task.done()


async def shutdown() -> None:
    await cancel_backlog()


async def reset() -> None:
    """Clear all in-process state (cancels the backlog). For tests."""
    global _destination, _client, _auto, _running_chat_id
    await cancel_backlog()
    _destination = None
    _sources.clear()
    _client = None
    _auto = False
    _running_chat_id = None
    if _forced:
        _forced.clear()
    logger.debug("runtime state reset")
