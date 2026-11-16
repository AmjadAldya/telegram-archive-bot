"""Resumable per-source history scan.

`sync_source` walks one source chat from its newest message to its oldest,
persisting a cursor after every message in `sync_state` (keyed by source +
destination) so a restart continues instead of starting over. The runtime's
backlog loop calls it for each enabled source in turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatForbidden,
    PeerIdInvalid,
)

from app.database.base import session_scope
from app.database.models import SourceState, SyncStatus
from app.database.repositories import MirrorRepository
from app.mirror import control, runtime
from app.mirror.jobs import errors, registry
from app.mirror.media import is_transferable_media
from app.mirror.throttle import AccountRestrictedError
from app.mirror.transfer import transfer_message
from app.services.logger import logger

_ACCESS_ERRORS = (ChannelPrivate, ChannelInvalid, ChatForbidden, PeerIdInvalid)


@dataclass(frozen=True, slots=True)
class Progress:
    status: str
    processed: int
    transferred: int
    duplicates: int
    resume_before: int | None
    last_error: str | None
    updated_at: datetime | None


def _get_or_create_state(source_chat_id: str, dest_chat_id: str) -> tuple[str, int | None, str]:
    with session_scope() as session:
        repository = MirrorRepository(session)
        state = repository.get_or_create_sync_state(source_chat_id, dest_chat_id)
        return state.id, state.resume_before_message_id, state.status.value


def _mark_status(state_id: str, status: SyncStatus, last_error: str | None = None) -> None:
    with session_scope() as session:
        MirrorRepository(session).mark_status(state_id, status, last_error=last_error)


def _update_progress(
    state_id: str,
    *,
    transferred_delta: int,
    duplicate_delta: int,
    resume_before_message_id: int,
) -> None:
    with session_scope() as session:
        MirrorRepository(session).update_sync_progress(
            state_id,
            processed_delta=1,
            transferred_delta=transferred_delta,
            duplicate_delta=duplicate_delta,
            resume_before_message_id=resume_before_message_id,
        )


def _reset_state(source_chat_id: str, dest_chat_id: str) -> None:
    with session_scope() as session:
        MirrorRepository(session).reset_sync_state(source_chat_id, dest_chat_id)


def progress_from_state(state) -> Progress:
    return Progress(
        status=state.status.value,
        processed=state.processed_messages,
        transferred=state.transferred_messages,
        duplicates=state.duplicate_messages,
        resume_before=state.resume_before_message_id,
        last_error=state.last_error,
        updated_at=state.updated_at,
    )


def read_progress(source_chat_id: str, dest_chat_id: str) -> Progress | None:
    """Current cursor/counters for a (source, destination) pair, without creating a row."""
    with session_scope() as session:
        state = MirrorRepository(session).find_sync_state(source_chat_id, dest_chat_id)
        return progress_from_state(state) if state is not None else None


async def sync_source(client, source: runtime.SourceInfo, dest_chat_id: int) -> None:
    """Scan one source's history (newest to oldest), transferring media not yet archived."""
    source_key = str(source.chat_id)
    dest_key = str(dest_chat_id)

    state_id, resume_before, status = await asyncio.to_thread(
        _get_or_create_state, source_key, dest_key
    )
    if status == SyncStatus.COMPLETED.value:
        logger.info("Backlog sync already completed for %s -> %s", source_key, dest_key)
        return

    await asyncio.to_thread(_mark_status, state_id, SyncStatus.RUNNING)
    logger.info(
        "Backlog sync starting for %s -> %s (resume_before=%s)", source_key, dest_key, resume_before
    )

    try:
        async for message in client.get_chat_history(source.chat_id, offset_id=resume_before or 0):
            await control.wait_if_paused()
            if control.is_restricted():
                break

            result = "skipped"
            if is_transferable_media(message) is not None:
                job = registry.create("backlog", source.chat_id, source.title, message.id)
                try:
                    result = await transfer_message(client, message, dest_chat_id, job=job)
                except AccountRestrictedError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad message must not stop the whole scan
                    logger.exception(
                        "Failed to transfer message %s, skipping it and continuing", message.id
                    )
                    errors.record(f"Transfer failed (msg {message.id})")

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
            logger.info("Backlog sync completed for %s -> %s", source_key, dest_key)
    except AccountRestrictedError as exc:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.FAILED, str(exc))
        logger.critical("Backlog sync stopped: account restricted (%s)", exc)
    except asyncio.CancelledError:
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.PAUSED)
        raise
    except _ACCESS_ERRORS as exc:
        message_text = f"{type(exc).__name__}: cannot read this chat"
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.FAILED, message_text)
        await runtime.mark_source_health(source.id, SourceState.ERROR, message_text)
        errors.record(f"Source unreadable ({type(exc).__name__})")
        logger.error("Backlog sync failed for %s: %s", source_key, message_text)
    except Exception as exc:  # noqa: BLE001 - top-level guard, recorded as FAILED
        await asyncio.to_thread(_mark_status, state_id, SyncStatus.FAILED, str(exc))
        errors.record(f"Backlog failed ({type(exc).__name__})")
        logger.exception("Backlog sync failed for %s -> %s", source_key, dest_key)


async def reset_backlog(client, chat_ids: list[int] | None = None) -> list[int]:
    """Reset the cursor of the given sources (default: all enabled) and rescan them.

    Safe to repeat: the dedup ledger prevents re-sending anything already
    archived. Returns the chat ids that were restarted.
    """
    destination = runtime.destination()
    if destination is None:
        return []
    targets = [
        info
        for info in runtime.sources()
        if info.enabled and (chat_ids is None or info.chat_id in chat_ids)
    ]
    for info in targets:
        await runtime.cancel_source(info.chat_id)
        await asyncio.to_thread(_reset_state, str(info.chat_id), str(destination.chat_id))
    restarted = [info.chat_id for info in targets]
    if restarted:
        await runtime.ensure_backlog_running(client, False, force_sources=restarted)
    return restarted
