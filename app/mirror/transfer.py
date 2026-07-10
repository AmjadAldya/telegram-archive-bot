from __future__ import annotations

import asyncio
from typing import Literal

from app.database.base import session_scope
from app.database.repositories import MirrorRepository
from app.mirror import control
from app.mirror.media import is_transferable_media
from app.mirror.throttle import AccountRestrictedError, call_with_flood_wait, rate_limiter
from app.services.logger import logger

TransferResult = Literal["transferred", "duplicate", "skipped"]

# Serializes every outgoing copy_message call across the live listener and the
# backlog scanner so only one transfer is ever in flight at a time, which is
# the core defense against tripping Telegram's flood limits.
_send_lock = asyncio.Lock()


def _is_duplicate(dest_chat_id: str, file_unique_id: str) -> bool:
    with session_scope() as session:
        repository = MirrorRepository(session)
        return repository.is_duplicate(dest_chat_id, file_unique_id)


def _record_transfer(**kwargs: object) -> bool:
    with session_scope() as session:
        repository = MirrorRepository(session)
        return repository.record_transfer(**kwargs)  # type: ignore[arg-type]


async def transfer_message(client, message, dest_chat_id: str | int) -> TransferResult:
    """Copy a message's media to the destination chat if it hasn't been sent before.

    Non-media messages and disallowed media types are skipped. Duplicate media
    (same Telegram file, already sent to this destination) is skipped without
    touching the network. Everything else goes through a rate-limited,
    flood-wait-aware, single-file-at-a-time send.
    """
    if control.is_restricted():
        return "skipped"

    media_info = is_transferable_media(message)
    if media_info is None:
        return "skipped"

    dest_chat_key = str(dest_chat_id)
    source_chat_id = str(message.chat.id)

    if await asyncio.to_thread(_is_duplicate, dest_chat_key, media_info.file_unique_id):
        logger.info(
            "Duplicate media %s (source message %s), skipping",
            media_info.file_unique_id,
            message.id,
        )
        return "duplicate"

    await control.wait_if_paused()

    async with _send_lock:
        if control.is_restricted():
            return "skipped"
        if await asyncio.to_thread(_is_duplicate, dest_chat_key, media_info.file_unique_id):
            return "duplicate"

await rate_limiter.wait()
        try:
            # 1. تنزيل الوسائط إلى السيرفر المحلي
            downloaded_file = await call_with_flood_wait(
                client.download_media,
                message=message
            )
            
            copied = None
            if downloaded_file:
                # 2. إعادة الرفع بناءً على نوع الملف
                if media_info.media_type == "photo":
                    copied = await call_with_flood_wait(
                        client.send_photo,
                        chat_id=dest_chat_id,
                        photo=downloaded_file
                    )
                elif media_info.media_type == "video":
                    copied = await call_with_flood_wait(
                        client.send_video,
                        chat_id=dest_chat_id,
                        video=downloaded_file
                    )
                
                # 3. حذف الملف المؤقت
                import os
                if os.path.exists(downloaded_file):
                    os.remove(downloaded_file)
                    
        except AccountRestrictedError as exc:
            control.mark_restricted(str(exc))
            raise
        recorded = await asyncio.to_thread(
            _record_transfer,
            file_unique_id=media_info.file_unique_id,
            media_type=media_info.media_type,
            source_chat_id=source_chat_id,
            source_message_id=message.id,
            dest_chat_id=dest_chat_key,
            dest_message_id=getattr(copied, "id", None),
        )
        if not recorded:
            logger.info(
                "Media %s was recorded concurrently, treating as duplicate",
                media_info.file_unique_id,
            )
            return "duplicate"

    logger.info(
        "Transferred %s: message %s -> chat %s",
        media_info.media_type,
        message.id,
        dest_chat_key,
    )
    return "transferred"
