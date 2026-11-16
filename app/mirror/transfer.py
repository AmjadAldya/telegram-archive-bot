"""The single choke point for sending media to the destination.

Every transfer - live, backlog and pasted-link jobs - goes through
`transfer_message`. It is serialised by one lock, spaced by the rate limiter,
protected from FloodWait, guarded by per-file timeouts/stall detection, and
skippable by the owner. Do not add a second send path.

Media is downloaded by the userbot and uploaded again (not `copy_message`):
that is what makes protected chats work. Downloads land in a per-job temp
directory that is always removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any, Literal

from pyrogram.errors import (
    ChannelPrivate,
    ChatAdminRequired,
    ChatWriteForbidden,
    UserBannedInChannel,
)

from app.config.settings import DATA_DIR, TRANSFER_MAX_RETRIES
from app.database.base import session_scope
from app.database.repositories import MirrorRepository
from app.mirror import control, limits
from app.mirror.guard import SkipRequested, TransferTimeout, compute_timeout, run_guarded
from app.mirror.jobs import Job, Stage, errors, registry
from app.mirror.media import MediaInfo, extract_media_info, is_transferable_media
from app.mirror.throttle import AccountRestrictedError, call_with_flood_wait, rate_limiter
from app.services.logger import logger

TransferResult = Literal[
    "transferred",
    "duplicate",
    "skipped",
    "user_skip",
    "timeout_skip",
    "auto_skip",
    "failed",
]

_send_lock = asyncio.Lock()
_background: set[asyncio.Task] = set()

# Extra keyword arguments for run_guarded (tests shrink the poll/stall/grace values).
_GUARD_KWARGS: dict[str, Any] = {}

_DEST_WRITE_ERRORS = (ChatWriteForbidden, ChatAdminRequired, UserBannedInChannel, ChannelPrivate)


class _AttemptFailed(Exception):
    """A download/upload attempt produced nothing usable; retry it."""


class _Unavailable(Exception):
    """Telegram no longer serves this media (expired / protected): do not retry."""


# -- database helpers (run in a worker thread) --------------------------------


def _is_duplicate(dest_chat_id: str, file_unique_id: str) -> bool:
    with session_scope() as session:
        return MirrorRepository(session).is_duplicate(dest_chat_id, file_unique_id)


def _record_transfer(**kwargs: Any) -> bool:
    with session_scope() as session:
        repository = MirrorRepository(session)
        recorded = repository.record_transfer(**kwargs)
        repository.resolve_skipped_for_message(
            kwargs["source_chat_id"], kwargs["source_message_id"]
        )
        return recorded


def _record_skip(**kwargs: Any) -> None:
    with session_scope() as session:
        MirrorRepository(session).record_skip(**kwargs)


# -- helpers -------------------------------------------------------------------


def tmp_root() -> Path:
    """Absolute on purpose: Pyrogram resolves a *relative* download directory against
    `Client.PARENT_DIR` (the directory of sys.argv[0], e.g. /app/app), not the working
    directory, so a relative path would be written somewhere our cleanup never looks."""
    return Path(DATA_DIR).resolve() / "tmp"


def sweep_tmp_dir() -> None:
    """Remove leftovers of transfers interrupted by a crash/restart."""
    shutil.rmtree(tmp_root(), ignore_errors=True)


def _job_tmp_dir(job: Job) -> Path:
    return tmp_root() / f"job-{os.getpid()}-{job.id}"


def _chat_title(message) -> str:
    chat = getattr(message, "chat", None)
    return getattr(chat, "title", None) or str(getattr(chat, "id", "?"))


def _progress_callback(client, job: Job):
    """Progress hook for Pyrogram: record bytes, and abort cleanly on skip."""

    def callback(current: int, total: int, *_args: object) -> None:
        job.update_progress(current, total)
        if job.skip_requested:
            stop = getattr(client, "stop_transmission", None)
            if stop is not None:
                stop()

    return callback


def _send_spec(client, info: MediaInfo, message, dest_chat_id, path: str, progress):
    """Pick the right Pyrogram send_* call (and metadata) for the media type."""
    media_type = info.media_type
    source_obj = getattr(message, media_type, None)
    kwargs: dict[str, Any] = {"chat_id": dest_chat_id, "progress": progress}

    def copy_meta(*names: str) -> None:
        for name in names:
            value = getattr(source_obj, name, None)
            if value is not None:
                kwargs[name] = value

    if media_type == "photo":
        return client.send_photo, {**kwargs, "photo": path}
    if media_type == "video":
        copy_meta("duration", "width", "height")
        kwargs["supports_streaming"] = True
        return client.send_video, {**kwargs, "video": path}
    if media_type == "animation":
        copy_meta("duration", "width", "height")
        return client.send_animation, {**kwargs, "animation": path}
    if media_type == "audio":
        copy_meta("duration", "performer", "title")
        return client.send_audio, {**kwargs, "audio": path}
    if media_type == "voice":
        copy_meta("duration")
        return client.send_voice, {**kwargs, "voice": path}
    if media_type == "video_note":
        copy_meta("duration", "length")
        return client.send_video_note, {**kwargs, "video_note": path}
    return client.send_document, {**kwargs, "document": path}


def _flood_note(job: Job):
    def on_wait(seconds: float) -> None:
        job.flood_wait_total += seconds
        job.note = f"FloodWait {int(seconds)}s"
        if job.on_flood is not None:
            job.on_flood(seconds)

    return on_wait


async def _attempt(stage: Stage, job: Job, size: int | None, make_operation) -> Any:
    """Run one stage with the watchdog and up to TRANSFER_MAX_RETRIES retries.

    Raises TransferTimeout once retries are exhausted, SkipRequested if the
    owner skipped, and lets everything else (FloodWait, restrictions) through.
    """
    label = stage.value.capitalize()
    retries = 0
    while True:
        job.begin_stage(stage)
        job.note = ""
        try:
            return await run_guarded(
                make_operation(),
                job,
                label,
                timeout=compute_timeout(size),
                **_GUARD_KWARGS,
            )
        except (TransferTimeout, _AttemptFailed) as exc:
            retries += 1
            reason = exc.kind if isinstance(exc, TransferTimeout) else "no result"
            logger.warning(
                "%s attempt failed for message %s (%s), attempt %d/%d",
                label,
                job.message_id,
                reason,
                retries,
                TRANSFER_MAX_RETRIES + 1,
            )
            errors.record(f"{label} {reason} (msg {job.message_id})")
            if retries > TRANSFER_MAX_RETRIES:
                if isinstance(exc, TransferTimeout):
                    raise
                raise TransferTimeout("failed") from exc


async def _download(client, message, info: MediaInfo, job: Job, tmp_dir: Path) -> str:
    progress = _progress_callback(client, job)

    async def operation() -> str:
        path = await call_with_flood_wait(
            client.download_media,
            message,
            file_name=str(tmp_dir) + os.sep,
            progress=progress,
            on_wait=_flood_note(job),
        )
        if job.skip_requested:
            raise SkipRequested()
        if not path:
            raise _AttemptFailed("download returned nothing")
        actual = os.path.getsize(path)
        if actual == 0:
            raise _Unavailable()
        # Photos report a different "size" than the downloaded variant; only
        # trust the expected size for other media, and only against truncation.
        if info.media_type != "photo" and info.file_size and actual < info.file_size:
            raise _AttemptFailed(f"truncated download ({actual}/{info.file_size})")
        return path

    return await _attempt(Stage.DOWNLOAD, job, info.file_size, operation)


async def _upload(client, message, info: MediaInfo, dest_chat_id, path: str, job: Job):
    progress = _progress_callback(client, job)
    size = info.file_size or os.path.getsize(path)

    async def operation():
        func, kwargs = _send_spec(client, info, message, dest_chat_id, path, progress)
        sent = await call_with_flood_wait(func, on_wait=_flood_note(job), **kwargs)
        if job.skip_requested:
            raise SkipRequested()
        if sent is None:
            raise _AttemptFailed("upload returned nothing")
        return sent

    return await _attempt(Stage.UPLOAD, job, size, operation)


async def _finish_skip(message, info: MediaInfo, job: Job, reason: str) -> None:
    job.error = reason
    await asyncio.to_thread(
        _record_skip,
        source_chat_id=str(message.chat.id),
        source_message_id=message.id,
        media_type=info.media_type,
        reason=reason,
        file_unique_id=info.file_unique_id,
        file_size=info.file_size,
    )
    logger.info("Skipped message %s (%s)", message.id, reason)


def _announce_large_file(job: Job) -> None:
    notifier = registry.on_large_file
    if notifier is None or job.large_notified:
        return
    job.large_notified = True
    task = asyncio.ensure_future(notifier(job))
    _background.add(task)
    task.add_done_callback(_background.discard)


# -- public API ------------------------------------------------------------------


async def transfer_message(
    client,
    message,
    dest_chat_id: str | int,
    *,
    job: Job | None = None,
    force: bool = False,
    ignore_type_filter: bool = False,
    kind: str = "live",
) -> TransferResult:
    """Send one message's media to the destination. Consumes (finishes) `job`.

    `force` bypasses the dedup check (the ledger row, if any, is kept).
    `ignore_type_filter` accepts every supported media type regardless of
    MEDIA_TYPES (used for explicit, owner-requested link transfers).
    The returned job (if you passed one) carries `media_type`, `file_size`,
    `error` and `detail` for the caller's report.
    """
    media_info = (
        extract_media_info(message) if ignore_type_filter else is_transferable_media(message)
    )
    if job is None:
        job = registry.create(kind, message.chat.id, _chat_title(message), message.id)
    try:
        return await _transfer(client, message, dest_chat_id, media_info, job, force)
    finally:
        registry.finish(job)


async def _transfer(
    client, message, dest_chat_id, media_info: MediaInfo | None, job: Job, force: bool
) -> TransferResult:
    if control.is_restricted():
        job.error = "restricted"
        return "skipped"
    if media_info is None:
        job.error = "no_media"
        return "skipped"

    job.media_type = media_info.media_type
    job.file_size = media_info.file_size
    job.file_name = media_info.file_name
    dest_key = str(dest_chat_id)
    source_key = str(message.chat.id)

    if not force and await asyncio.to_thread(_is_duplicate, dest_key, media_info.file_unique_id):
        logger.info("Duplicate media (source message %s), skipping", message.id)
        return "duplicate"

    await control.wait_if_paused()

    async with _send_lock:
        if control.is_restricted():
            job.error = "restricted"
            return "skipped"
        if not force and await asyncio.to_thread(
            _is_duplicate, dest_key, media_info.file_unique_id
        ):
            return "duplicate"

        if job.skip_requested:
            await _finish_skip(message, media_info, job, "user_skip")
            return "user_skip"
        if limits.should_auto_skip(media_info.file_size):
            await _finish_skip(message, media_info, job, "auto_skip")
            return "auto_skip"
        if limits.is_large(media_info.file_size):
            _announce_large_file(job)

        await rate_limiter.wait()
        tmp_dir = _job_tmp_dir(job)
        copied = None
        path: str | None = None
        try:
            path = await _download(client, message, media_info, job, tmp_dir)
            copied = await _upload(client, message, media_info, dest_chat_id, path, job)
        except SkipRequested:
            await _finish_skip(message, media_info, job, "user_skip")
            return "user_skip"
        except TransferTimeout as exc:
            logger.error(
                "Giving up on message %s after %d retries (%s)",
                message.id,
                TRANSFER_MAX_RETRIES,
                exc.kind,
            )
            await _finish_skip(message, media_info, job, "timeout")
            return "timeout_skip"
        except _Unavailable:
            logger.warning(
                "Downloaded 0 B for message %s (media likely no longer available from "
                "Telegram - protected/expired content), skipping",
                message.id,
            )
            job.error = "unavailable"
            return "skipped"
        except _DEST_WRITE_ERRORS as exc:
            job.error = "dest_forbidden"
            job.detail = type(exc).__name__
            errors.record(f"Cannot write to destination ({type(exc).__name__})")
            return "failed"
        except AccountRestrictedError as exc:
            control.mark_restricted(str(exc))
            raise
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if path is not None:  # belt and braces: delete exactly what the download returned
                with contextlib.suppress(OSError):
                    os.remove(path)

        recorded = await asyncio.to_thread(
            _record_transfer,
            file_unique_id=media_info.file_unique_id,
            media_type=media_info.media_type,
            source_chat_id=source_key,
            source_message_id=message.id,
            dest_chat_id=dest_key,
            dest_message_id=getattr(copied, "id", None),
        )
        if not recorded and not force:
            logger.info("Media was recorded concurrently, treating as duplicate")
            return "duplicate"

    logger.info(
        "Transferred %s: message %s -> chat %s", media_info.media_type, message.id, dest_key
    )
    return "transferred"
