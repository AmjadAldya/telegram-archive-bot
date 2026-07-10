from __future__ import annotations

import asyncio

from pyrogram import Client, idle
from pyrogram.types import Chat

from app.config.settings import (
    API_HASH,
    API_ID,
    DEST_CHAT_ID,
    SESSION_STRING,
    SOURCE_CHAT_ID,
    SYNC_HISTORY_ON_START,
)
from app.database.base import init_db
from app.mirror import runtime
from app.services.logger import logger

bot = Client(
    "mirror_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)


async def _seed_from_env_if_unset() -> None:
    """One-time convenience seed from SOURCE_CHAT_ID/DEST_CHAT_ID in `.env`.

    Only applies if nothing has been configured yet (via /setsource and
    /setdest). Once a pair exists in the database, it's always the source
    of truth - the env vars are never consulted again.
    """
    pair = await runtime.load()
    if pair is not None or SOURCE_CHAT_ID is None or DEST_CHAT_ID is None:
        return

    try:
        source_chat = await bot.get_chat(SOURCE_CHAT_ID)
        dest_chat = await bot.get_chat(DEST_CHAT_ID)
    except Exception:
        logger.exception(
            "Could not resolve SOURCE_CHAT_ID/DEST_CHAT_ID from .env; skipping the seed. "
            "Use /chats, /setsource and /setdest instead."
        )
        return

    # get_chat() returns a bare ChatPreview (no .id) for chats the account
    # hasn't joined yet - only a real Chat is usable as a mirror endpoint.
    if not isinstance(source_chat, Chat) or not isinstance(dest_chat, Chat):
        logger.error(
            "SOURCE_CHAT_ID/DEST_CHAT_ID must be chats this account has already joined. "
            "Use /chats, /setsource and /setdest instead."
        )
        return

    await runtime.set_source(source_chat.id, source_chat.title or str(source_chat.id))
    await runtime.set_dest(dest_chat.id, dest_chat.title or str(dest_chat.id))
    logger.info("Seeded mirror config from .env: %s -> %s", source_chat.title, dest_chat.title)


async def run_bot() -> None:
    await asyncio.to_thread(init_db)

    # Deferred import: handlers.py imports `bot` from this module, so importing
    # it at module scope would create a circular import. Importing it here,
    # right before start, registers every @bot.on_message handler exactly once.
    import app.bot.handlers  # noqa: F401

    await bot.start()

    me = await bot.get_me()
    logger.info("Userbot started as %s (id=%s)", me.username or me.first_name, me.id)

    await _seed_from_env_if_unset()
    pair = runtime.current()
    if pair is None:
        logger.warning(
            "No source/destination chat configured yet. From Saved Messages, send /chats "
            "to list your groups and channels, then /setsource and /setdest to pick them."
        )
    else:
        logger.info("Mirroring media: %s -> %s", pair.source_title, pair.dest_title)
        await runtime.ensure_backlog_running(bot, SYNC_HISTORY_ON_START)

    try:
        await idle()
    finally:
        await runtime.shutdown()
        await bot.stop()
        logger.info("Userbot stopped")
