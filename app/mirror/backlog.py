from __future__ import annotations

import asyncio

from app.config.settings import DEST_CHAT_ID, SOURCE_CHAT_ID
from app.database.base import session_scope
from app.database.models import SyncStatus
from app.database.repositories import MirrorRepository
from app.mirror import control
from app.mirror.throttle import AccountRestrictedError
from app.mirror.transfer import transfer_message
from app.services.logger import logger


def _get_or_create_state() -> tuple[str, int | None, str]:
    with session_scope() as session:
        repository = MirrorRepository(session)
        state = repository.get_or_create_sync_state(str(SOURCE_CHAT_ID), str(DEST_CHAT_ID))
        return state.id, state.resume_before_message_id, state.status.value


def _mark_status(state_id: str, status: SyncStatus, last_error: str | None = None) -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        repository.mark_status(state_id, status, last_error=last_error)


def _update_progress(
    state_id: str,
    *,
    transferred_delta: int,
    duplicate_delta: int,
    resume_before_message_id: int,
) -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        repository.update_sync_progress(
            state_id,
            processed_delta=1,
            transferred_delta=transferred_delta,
            duplicate_delta=duplicate_delta,
            resume_before_message_id=resume_before_message_id,
        )


def _reset_state() -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        repository.reset_sync_state(str(SOURCE_CHAT_ID), str(DEST_CHAT_ID))


def _state_summary() -> str:
    with session_scope() as session:
        repository = MirrorRepository(session)
        state = repository.get_or_create_sync_state(str(SOURCE_CHAT_ID), str(DEST_CHAT_ID))
        return state.summary()


async def get_status_summary() -> str:
    return await asyncio.to_thread(_state_summary)


async def sync_backlog(client) -> None:
    """One-time (resumable) scan of the source chat's existing media history.

    Walks from the newest message down to the oldest, persisting a cursor
    after every message so a restart continues instead of re-scanning
    everything. Safe to re-run at any point: the dedup ledger makes replays
    a no-op, they just cost an extra duplicate-check per message.
    """
    state_id, resume_before, status = await asyncio.to_thread(_get_or_create_state)
    if status == SyncStatus.COMPLETED.value:
        logger.info("Backlog sync already completed for %s -> %s", SOURCE_CHAT_ID, DEST_CHAT_ID)
        return

    await asyncio.to_thread(_mark_status, state_id, SyncStatus.RUNNING)
    logger.info(
        "Backlog sync starting for %s -> %s (resume_before=%s)",
        SOURCE_CHAT_ID,
        DEST_CHAT_ID,
        resume_before,
    )

    offset_id = resume_before or 0
    try:
        async for message in client.get_chat_history(SOURCE_CHAT_ID, offset_id=offset_id):
            await control.wait_if_paused()
            if control.is_restricted():
                break

            result = await transfer_message(client, message, DEST_CHAT_ID)
            await asyncio.to_thread(
                _update_progress,
                state_id,
                transferred_delta=1 if result == "transferred" else 0,
                duplicate_delta=1 if result == "duplicate" else 0,
                resume_before_message_id=message.id,
            )

        if control.is_restricted():
            await asyncio.to_thread(
                _mark_status, state_id, SyncStatus.FAILED, control.restricted_reason()
            )
        else:
            await asyncio.to_thread(_mark_status, state_id, SyncStatus.COMPLETED)
            logger.info("Backlog sync completed for %s -> %s", SOURCE_CHAT_ID, DEST_CHAT_ID)
    except AccountRestrictedError as exc:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.FAILED, str(exc))
        logger.critical("Backlog sync stopped: account restricted (%s)", exc)
    except asyncio.CancelledError:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.PAUSED)
        raise
    except Exception as exc:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.FAILED, str(exc))
        logger.exception("Backlog sync failed for %s -> %s", SOURCE_CHAT_ID, DEST_CHAT_ID)


async def reset_backlog(client) -> None:
    """Reset the resume cursor and relaunch a full backlog rescan."""
    await asyncio.to_thread(_reset_state)
    asyncio.create_task(sync_backlog(client))
