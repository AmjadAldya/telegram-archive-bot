from __future__ import annotations

from pyrogram import filters

from app.bot.client import bot
from app.config.settings import DEST_CHAT_ID, MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, SOURCE_CHAT_ID
from app.mirror import control
from app.mirror.backlog import get_status_summary, reset_backlog
from app.mirror.listener import handle_incoming_media

HELP_TEXT = (
    "Mirror userbot commands (only usable by this account):\n"
    "/status - show mirror and backlog progress\n"
    "/pause - stop transferring media until /resume\n"
    "/resume - resume transferring media\n"
    "/resync - rescan the full source history from the newest message\n"
    "/help - show this message"
)

# All control commands require filters.me: they can only be triggered by the
# logged-in account itself (e.g. from Saved Messages), never by other members
# of the source or destination chat.
_owner = filters.me


@bot.on_message(_owner & filters.command(["help", "start"]))
async def help_command(_, message):
    await message.reply(HELP_TEXT)


@bot.on_message(_owner & filters.command("status"))
async def status_command(_, message):
    summary = await get_status_summary()
    lines = [
        "Mirror status:",
        f"source={SOURCE_CHAT_ID} dest={DEST_CHAT_ID}",
        summary,
        f"paused={'yes' if control.is_paused() else 'no'}",
        f"delay={MIN_DELAY_SECONDS}-{MAX_DELAY_SECONDS}s per media",
    ]
    reason = control.restricted_reason()
    if reason:
        lines.append(f"RESTRICTED BY TELEGRAM: {reason}")
    await message.reply("\n".join(lines))


@bot.on_message(_owner & filters.command("pause"))
async def pause_command(_, message):
    control.pause()
    await message.reply("Mirror paused. Send /resume to continue.")


@bot.on_message(_owner & filters.command("resume"))
async def resume_command(_, message):
    control.resume()
    await message.reply("Mirror resumed.")


@bot.on_message(_owner & filters.command("resync"))
async def resync_command(_, message):
    await reset_backlog(bot)
    await message.reply("Backlog rescan started from the newest message in the source chat.")


@bot.on_message(filters.chat(SOURCE_CHAT_ID) & filters.media)
async def mirror_media(client, message):
    await handle_incoming_media(client, message)
