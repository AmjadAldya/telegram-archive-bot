from pyrogram import Client, idle
from app.config.settings import API_ID, API_HASH, BOT_TOKEN

bot = Client(
    'archive_bot',
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

async def run_bot():
    await bot.start()
    print('Bot started')
    await idle()
    await bot.stop()
