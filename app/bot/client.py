from __future__ import annotations

import asyncio

from pyrogram import Client, idle

from app.config.settings import (
    API_HASH,
    API_ID,
    DEST_CHAT_ID,
    SESSION_STRING,
    SOURCE_CHAT_ID,
    SYNC_HISTORY_ON_START,
)
from app.database.base import init_db
from app.mirror.backlog import sync_backlog
from app.services.logger import logger

bot = Client(
    "mirror_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)


async def run_bot() -> None:
    await asyncio.to_thread(init_db)

    # Deferred import: handlers.py imports `bot` from this module, so importing
    # it at module scope would create a circular import. Importing it here,
    # right before start, registers every @bot.on_message handler exactly once.
    import app.bot.handlers  # noqa: F401

    await bot.start()

    me = await bot.get_me()
    logger.info(
        "Userbot started as %s (id=%s); mirroring media %s -> %s",
        me.username or me.first_name,
        me.id,
        SOURCE_CHAT_ID,
        DEST_CHAT_ID,
    )

    backlog_task: asyncio.Task | None = None
    if SYNC_HISTORY_ON_START:
        backlog_task = asyncio.create_task(sync_backlog(bot))

    try:
        await idle()
    finally:
        if backlog_task is not None:
            backlog_task.cancel()
            await asyncio.gather(backlog_task, return_exceptions=True)
        await bot.stop()
        logger.info("Userbot stopped")
