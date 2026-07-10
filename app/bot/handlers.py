from __future__ import annotations

from pyrogram import filters

from app.bot.client import bot
from app.config.settings import MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, SYNC_HISTORY_ON_START
from app.mirror import control, runtime
from app.mirror.backlog import get_status_summary, reset_backlog
from app.mirror.dialogs import (
    ChatReferenceError,
    fetch_mirrorable_chats,
    format_page,
    resolve_reference,
)
from app.mirror.listener import handle_incoming_media

HELP_TEXT = (
    "Mirror userbot commands (only usable by this account):\n"
    "/chats [page] - list your groups/channels to pick from\n"
    "/setsource <number|id|@username> - set the chat to mirror media from\n"
    "/setdest <number|id|@username> - set the chat to mirror media into\n"
    "/status - show the configured pair and backlog progress\n"
    "/pause - stop transferring media until /resume\n"
    "/resume - resume transferring media\n"
    "/resync - rescan the full source history from the newest message\n"
    "/help - show this message"
)

# All control commands require filters.me: they can only be triggered by the
# logged-in account itself (e.g. from Saved Messages), never by other members
# of the source or destination chat.
_owner = filters.me


async def _configured_notice(client) -> str | None:
    pair = runtime.current()
    if pair is None:
        return None
    await runtime.ensure_backlog_running(client, SYNC_HISTORY_ON_START)
    return f"Now mirroring: {pair.source_title} -> {pair.dest_title}"


@bot.on_message(_owner & filters.command(["help", "start"]))
async def help_command(_, message):
    await message.reply(HELP_TEXT)


@bot.on_message(_owner & filters.command("chats"))
async def chats_command(client, message):
    command = getattr(message, "command", []) or []
    page = int(command[1]) if len(command) > 1 and command[1].isdigit() else 1

    await message.reply("Fetching your groups and channels...")
    chats = await fetch_mirrorable_chats(client)
    await message.reply(format_page(chats, page))


@bot.on_message(_owner & filters.command("setsource"))
async def setsource_command(client, message):
    command = getattr(message, "command", []) or []
    if len(command) < 2:
        await message.reply("Usage: /setsource <number from /chats, chat id, or @username>")
        return

    try:
        chat_id, title = await resolve_reference(client, command[1])
    except ChatReferenceError as exc:
        await message.reply(str(exc))
        return

    await runtime.set_source(chat_id, title)
    reply = f"Source set to: {title} (id={chat_id})"
    notice = await _configured_notice(client)
    if notice:
        reply += f"\n{notice}"
    await message.reply(reply)


@bot.on_message(_owner & filters.command("setdest"))
async def setdest_command(client, message):
    command = getattr(message, "command", []) or []
    if len(command) < 2:
        await message.reply("Usage: /setdest <number from /chats, chat id, or @username>")
        return

    try:
        chat_id, title = await resolve_reference(client, command[1])
    except ChatReferenceError as exc:
        await message.reply(str(exc))
        return

    await runtime.set_dest(chat_id, title)
    reply = f"Destination set to: {title} (id={chat_id})"
    notice = await _configured_notice(client)
    if notice:
        reply += f"\n{notice}"
    await message.reply(reply)


@bot.on_message(_owner & filters.command("status"))
async def status_command(_, message):
    pair = runtime.current()
    lines = ["Mirror status:"]
    if pair is None:
        lines.append("Not configured yet - send /chats, then /setsource and /setdest.")
    else:
        lines.append(f"source={pair.source_title} (id={pair.source_chat_id})")
        lines.append(f"dest={pair.dest_title} (id={pair.dest_chat_id})")
        lines.append(await get_status_summary())
    lines.append(f"paused={'yes' if control.is_paused() else 'no'}")
    lines.append(f"delay={MIN_DELAY_SECONDS}-{MAX_DELAY_SECONDS}s per media")
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
async def resync_command(client, message):
    if runtime.current() is None:
        await message.reply("Not configured yet - send /chats, then /setsource and /setdest.")
        return

    await reset_backlog(client)
    await message.reply("Backlog rescan started from the newest message in the source chat.")


@bot.on_message(filters.media)
async def mirror_media(client, message):
    # No static chat filter here: the source chat is chosen at runtime via
    # /setsource and can change, so handle_incoming_media checks the current
    # configuration itself instead of a filter baked in at import time.
    await handle_incoming_media(client, message)
