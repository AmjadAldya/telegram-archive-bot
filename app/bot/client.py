from __future__ import annotations

import asyncio

from pyrogram import Client, filters, idle
from pyrogram.types import BotCommand, Chat

from app.bot import handlers
from app.config.settings import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
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

# Optional dedicated control bot (see README: "Controlling the mirror from a
# real bot"). When BOT_TOKEN is set, /chats /setsource /setdest /status
# /pause /resume /resync live in a normal chat with this bot instead of in
# the userbot's own Saved Messages, with a proper "/" command menu. Chat
# discovery still goes through `bot` (the userbot) - a BotFather bot has no
# access to the account's dialogs or the source chat's history.
control_bot: Client | None = (
    Client(
        "mirror_control_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )
    if BOT_TOKEN
    else None
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

    await bot.start()
    me = await bot.get_me()
    logger.info("Userbot started as %s (id=%s)", me.username or me.first_name, me.id)

    if control_bot is not None:
        await control_bot.start()
        await control_bot.set_bot_commands(
            [BotCommand(name, description) for name, description in handlers.BOT_COMMAND_MENU]
        )
        control_me = await control_bot.get_me()
        logger.info(
            "Control bot started as @%s - commands only work for user id %s",
            control_me.username,
            me.id,
        )
        handlers.register_control_commands(control_bot, bot, owner_filter=filters.user(me.id))
    else:
        logger.info(
            "No BOT_TOKEN set - control commands are only available from the userbot's own "
            "Saved Messages. Set BOT_TOKEN for a dedicated control bot with a command menu."
        )
        handlers.register_control_commands(bot, bot, owner_filter=filters.me)

    handlers.register_media_listener(bot)

    await _seed_from_env_if_unset()
    pair = runtime.current()
    if pair is None:
        logger.warning(
            "No source/destination chat configured yet. Send /chats to list your groups "
            "and channels, then /setsource and /setdest to pick them."
        )
    else:
        logger.info("Mirroring media: %s -> %s", pair.source_title, pair.dest_title)
        await runtime.ensure_backlog_running(bot, SYNC_HISTORY_ON_START)

    try:
        await idle()
    finally:
        await runtime.shutdown()
        if control_bot is not None:
            await control_bot.stop()
        await bot.stop()
        logger.info("Userbot stopped")
