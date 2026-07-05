from __future__ import annotations

import asyncio

from pyrogram import Client, idle

from app.config.settings import API_ID, API_HASH, BOT_TOKEN
from app.database.base import init_db
from app.services.logger import logger
from app.services.worker import archive_worker

bot = Client("archive_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


async def run_bot():
    await asyncio.to_thread(init_db)
    await bot.start()
    logger.info("Bot started")

    worker_task = asyncio.create_task(archive_worker(bot))
    try:
        await idle()
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await bot.stop()
        logger.info("Bot stopped")
