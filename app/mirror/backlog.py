from __future__ import annotations

import asyncio

from app.database.base import session_scope
from app.database.models import SyncStatus
from app.database.repositories import MirrorRepository
from app.mirror import control
from app.mirror.throttle import AccountRestrictedError
from app.mirror.transfer import transfer_message
from app.services.logger import logger


def _get_or_create_state(source_chat_id: str, dest_chat_id: str) -> tuple[str, int | None, str]:
    with session_scope() as session:
        repository = MirrorRepository(session)
        state = repository.get_or_create_sync_state(source_chat_id, dest_chat_id)
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


def _reset_state(source_chat_id: str, dest_chat_id: str) -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        repository.reset_sync_state(source_chat_id, dest_chat_id)


def _state_summary(source_chat_id: str, dest_chat_id: str) -> str:
    with session_scope() as session:
        repository = MirrorRepository(session)
        state = repository.get_or_create_sync_state(source_chat_id, dest_chat_id)
        return state.summary()


async def get_status_summary() -> str:
    from app.mirror import runtime

    pair = runtime.current()
    if pair is None:
        return "Not configured yet - send /chats, then /setsource and /setdest."
    return await asyncio.to_thread(_state_summary, str(pair.source_chat_id), str(pair.dest_chat_id))


async def sync_backlog(client) -> None:
    """One-time (resumable) scan of the source chat's existing media history.

    Walks from the newest message down to the oldest, persisting a cursor
    after every message so a restart continues instead of re-scanning
    everything. Safe to re-run at any point: the dedup ledger makes replays
    a no-op, they just cost an extra duplicate-check per message.
    """
    from app.mirror import runtime

    pair = runtime.current()
    if pair is None:
        return

    source_chat_id = str(pair.source_chat_id)
    dest_chat_id = str(pair.dest_chat_id)

    state_id, resume_before, status = await asyncio.to_thread(
        _get_or_create_state, source_chat_id, dest_chat_id
    )
    if status == SyncStatus.COMPLETED.value:
        logger.info("Backlog sync already completed for %s -> %s", source_chat_id, dest_chat_id)
        return

    await asyncio.to_thread(_mark_status, state_id, SyncStatus.RUNNING)
    logger.info(
        "Backlog sync starting for %s -> %s (resume_before=%s)",
        source_chat_id,
        dest_chat_id,
        resume_before,
    )

    offset_id = resume_before or 0
    try:
        async for message in client.get_chat_history(pair.source_chat_id, offset_id=offset_id):
            await control.wait_if_paused()
            if control.is_restricted():
                break

            result = await transfer_message(client, message, pair.dest_chat_id)
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
            logger.info("Backlog sync completed for %s -> %s", source_chat_id, dest_chat_id)
    except AccountRestrictedError as exc:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.FAILED, str(exc))
        logger.critical("Backlog sync stopped: account restricted (%s)", exc)
    except asyncio.CancelledError:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.PAUSED)
        raise
    except Exception as exc:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.FAILED, str(exc))
        logger.exception("Backlog sync failed for %s -> %s", source_chat_id, dest_chat_id)


async def reset_backlog(client) -> None:
    """Reset the resume cursor and relaunch a full backlog rescan."""
    from app.mirror import runtime

    pair = runtime.current()
    if pair is None:
        return

    await asyncio.to_thread(_reset_state, str(pair.source_chat_id), str(pair.dest_chat_id))
    await runtime.ensure_backlog_running(client, sync_history=True, force=True)
