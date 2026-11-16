from __future__ import annotations

import asyncio
import contextlib

from pyrogram import Client, filters, idle
from pyrogram.types import BotCommand, BotCommandScopeChat, Chat

from app.bot import handlers
from app.bot.live import live_view
from app.bot.texts import BOT_COMMANDS
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
from app.mirror import limits, runtime
from app.mirror.transfer import sweep_tmp_dir
from app.services.logger import logger

bot = Client(
    "mirror_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

# Optional dedicated control bot (see README: "Controlling the mirror from a
# real bot"). When BOT_TOKEN is set, the admin panel and all commands live in a
# normal chat with this bot instead of the userbot's own Saved Messages, with a
# proper "/" command menu. Chat discovery and every read of a source chat still
# go through `bot` (the userbot) - a BotFather bot has no access to the
# account's dialogs or the source chats' history.
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

_link_queue = None


async def _seed_from_env_if_unset() -> None:
    """One-time convenience seed from SOURCE_CHAT_ID/DEST_CHAT_ID in `.env`.

    Only applies if nothing has been configured yet (no destination and no
    sources). Once configuration exists in the database, it is always the
    source of truth - the env vars are never consulted again.
    """
    if runtime.destination() is not None or runtime.sources():
        return
    if SOURCE_CHAT_ID is None or DEST_CHAT_ID is None:
        return

    try:
        source_chat = await bot.get_chat(SOURCE_CHAT_ID)
        dest_chat = await bot.get_chat(DEST_CHAT_ID)
    except Exception:  # noqa: BLE001 - startup seed is best-effort; any failure falls back to /addsource
        logger.exception(
            "Could not resolve SOURCE_CHAT_ID/DEST_CHAT_ID from .env; skipping the seed. "
            "Use /addsource and /setdest instead."
        )
        return

    # get_chat() returns a bare ChatPreview (no .id) for chats the account
    # hasn't joined yet - only a real Chat is usable as a mirror endpoint.
    if not isinstance(source_chat, Chat) or not isinstance(dest_chat, Chat):
        logger.error(
            "SOURCE_CHAT_ID/DEST_CHAT_ID must be chats this account has already joined. "
            "Use /addsource and /setdest instead."
        )
        return

    await runtime.add_source(source_chat.id, source_chat.title or str(source_chat.id))
    await runtime.set_dest(dest_chat.id, dest_chat.title or str(dest_chat.id))
    logger.info("Seeded mirror config from .env: %s -> %s", source_chat.title, dest_chat.title)


async def _warm_peer_cache() -> None:
    """Populate Pyrogram's (in-memory, per-process) peer cache on every startup.

    Resolving a chat by bare numeric ID - as get_chat_history/get_chat do -
    fails for a channel Pyrogram hasn't "seen" yet this session: its local
    fallback (utils.get_peer_type) rejects IDs outside a hardcoded range that
    doesn't cover every valid channel ID. Walking the dialog list once
    populates the cache from real server data instead, the same way /chats
    does, so the configured source/dest chats resolve correctly afterwards.
    """
    async for _ in bot.get_dialogs():  # type: ignore[union-attr]
        pass


async def _publish_commands(client: Client, owner_id: int) -> None:
    """Arabic command menu, shown only in the owner's chat with the control bot."""
    commands = [BotCommand(name, description) for name, description in BOT_COMMANDS]
    try:
        await client.set_bot_commands(commands, scope=BotCommandScopeChat(chat_id=owner_id))
        # Hide the menu from everyone else (best effort).
        with contextlib.suppress(Exception):
            await client.delete_bot_commands()
    except Exception:  # noqa: BLE001 - e.g. the owner has not opened the bot yet
        logger.warning(
            "Could not scope the command menu to the owner's chat (open the bot and press "
            "Start); publishing it for everyone instead - commands still only work for the owner."
        )
        await client.set_bot_commands(commands)


async def run_bot() -> None:
    global _link_queue

    await asyncio.to_thread(init_db)
    await asyncio.to_thread(sweep_tmp_dir)

    await bot.start()
    me = await bot.get_me()
    logger.info("Userbot started as %s (id=%s)", me.username or me.first_name, me.id)

    await _warm_peer_cache()
    await runtime.load()
    await limits.load()

    if control_bot is not None:
        await control_bot.start()
        await _publish_commands(control_bot, me.id)
        control_me = await control_bot.get_me()
        logger.info(
            "Control bot started as @%s - commands only work for user id %s",
            control_me.username,
            me.id,
        )
        context = handlers.register_control_commands(
            control_bot,
            bot,
            owner_filter=filters.user(me.id),
            scope_filter=filters.private,
            owner_chat_id=me.id,
        )
    else:
        logger.info(
            "No BOT_TOKEN set - control commands are only available from the userbot's own "
            "Saved Messages. Set BOT_TOKEN for a dedicated control bot with a command menu."
        )
        context = handlers.register_control_commands(
            bot,
            bot,
            owner_filter=filters.me,
            scope_filter=handlers.make_saved_messages_scope(),
            owner_chat_id="me",
        )
    _link_queue = context.link_queue
    _link_queue.start()

    handlers.register_media_listener(bot)

    await _seed_from_env_if_unset()
    destination = runtime.destination()
    sources = runtime.sources()
    if destination is None:
        logger.warning(
            "No destination configured yet. Send /setdest to the control bot, then /addsource."
        )
    elif not sources:
        logger.warning("Destination is set but there are no sources yet. Send /addsource.")
    else:
        enabled = sum(1 for info in sources if info.enabled)
        logger.info(
            "Mirroring media from %d source(s) (%d enabled) into %s",
            len(sources),
            enabled,
            destination.title,
        )
        await runtime.ensure_backlog_running(bot, SYNC_HISTORY_ON_START)

    try:
        await idle()
    finally:
        await live_view.stop()
        if _link_queue is not None:
            await _link_queue.stop()
        await runtime.shutdown()
        if control_bot is not None:
            await control_bot.stop()
        await bot.stop()
        logger.info("Userbot stopped")
