from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.database.base import session_scope
from app.database.models import MirrorConfig
from app.database.repositories import MirrorRepository
from app.services.logger import logger


@dataclass(frozen=True, slots=True)
class MirrorPair:
    source_chat_id: int
    source_title: str
    dest_chat_id: int
    dest_title: str


_current: MirrorPair | None = None
_backlog_task: asyncio.Task | None = None
_backlog_pair: MirrorPair | None = None
_lock = asyncio.Lock()


def _get_config_sync() -> MirrorConfig:
    with session_scope() as session:
        repository = MirrorRepository(session)
        return repository.get_config()


def _set_source_sync(chat_id: int, title: str) -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        repository.set_source(str(chat_id), title)


def _set_dest_sync(chat_id: int, title: str) -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        repository.set_dest(str(chat_id), title)


def _to_pair(config: MirrorConfig) -> MirrorPair | None:
    if not config.source_chat_id or not config.dest_chat_id:
        return None
    return MirrorPair(
        source_chat_id=int(config.source_chat_id),
        source_title=config.source_title or config.source_chat_id,
        dest_chat_id=int(config.dest_chat_id),
        dest_title=config.dest_title or config.dest_chat_id,
    )


async def load() -> MirrorPair | None:
    """(Re)load the configured pair from the database into the process cache."""
    global _current
    config = await asyncio.to_thread(_get_config_sync)
    _current = _to_pair(config)
    return _current


def current() -> MirrorPair | None:
    return _current


async def set_source(chat_id: int, title: str) -> MirrorPair | None:
    await asyncio.to_thread(_set_source_sync, chat_id, title)
    return await load()


async def set_dest(chat_id: int, title: str) -> MirrorPair | None:
    await asyncio.to_thread(_set_dest_sync, chat_id, title)
    return await load()


async def ensure_backlog_running(client, sync_history: bool, force: bool = False) -> None:
    """(Re)start the backlog scan for the currently configured pair.

    A no-op if nothing is configured yet, or if a backlog task is already
    running for this exact pair (unless `force`, used by /resync).
    """
    global _backlog_task, _backlog_pair

    async with _lock:
        pair = _current
        if pair is None or not sync_history:
            return

        already_running = (
            _backlog_task is not None and not _backlog_task.done() and _backlog_pair == pair
        )
        if already_running and not force:
            return

        if _backlog_task is not None and not _backlog_task.done():
            logger.info("Mirror pair changed, restarting backlog sync")
            _backlog_task.cancel()
            await asyncio.gather(_backlog_task, return_exceptions=True)

        from app.mirror.backlog import sync_backlog  # deferred: avoids a module cycle

        _backlog_task = asyncio.create_task(sync_backlog(client))
        _backlog_pair = pair


def is_backlog_running() -> bool:
    return _backlog_task is not None and not _backlog_task.done()


async def shutdown() -> None:
    global _backlog_task
    if _backlog_task is not None and not _backlog_task.done():
        _backlog_task.cancel()
        await asyncio.gather(_backlog_task, return_exceptions=True)
    _backlog_task = None


async def reset() -> None:
    """Clear all in-process state (cancels any backlog task). For tests."""
    global _current, _backlog_pair
    await shutdown()
    _current = None
    _backlog_pair = None
