from app.bot.client import bot
from pyrogram import filters
from app.archive.engine import start_archive

@bot.on_message(filters.command('start'))
async def start(_, message):
    await message.reply('Archive Bot running.')

@bot.on_message(filters.command('archive'))
async def archive(_, message):
    await message.reply('Starting archive...')
    await start_archive(bot)
