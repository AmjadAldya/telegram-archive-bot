from __future__ import annotations

from pyrogram import filters

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
    "Mirror control panel - commands only work for the account owner.\n\n"
    "/chats [page] - list your groups/channels to pick from\n"
    "/setsource <number|id|@username> - set the chat to mirror media from\n"
    "/setdest <number|id|@username> - set the chat to mirror media into\n"
    "/status - show the configured pair and backlog progress\n"
    "/pause - stop transferring media until /resume\n"
    "/resume - resume transferring media\n"
    "/resync - rescan the full source history from the newest message\n"
    "/help - show this message"
)

BOT_COMMAND_MENU = [
    ("start", "Show help"),
    ("chats", "List your groups/channels"),
    ("setsource", "Set the chat to mirror media from"),
    ("setdest", "Set the chat to mirror media into"),
    ("status", "Show the configured pair and backlog progress"),
    ("pause", "Stop transferring media"),
    ("resume", "Resume transferring media"),
    ("resync", "Rescan the full source history"),
    ("help", "Show help"),
]


def register_control_commands(command_client, userbot_client, owner_filter) -> None:
    """Wire up /chats /setsource /setdest /status /pause /resume /resync /help.

    Registered on `command_client` (either the dedicated control bot, or the
    userbot itself as a fallback) but chat discovery/resolution always goes
    through `userbot_client`, since only the logged-in account can see its
    own dialog list and chat history - a BotFather bot can't.
    """

    async def configured_notice() -> str | None:
        pair = runtime.current()
        if pair is None:
            return None
        await runtime.ensure_backlog_running(userbot_client, SYNC_HISTORY_ON_START)
        return f"Now mirroring: {pair.source_title} -> {pair.dest_title}"

    @command_client.on_message(owner_filter & filters.command(["help", "start"]))
    async def help_command(_, message):
        await message.reply(HELP_TEXT)

    @command_client.on_message(owner_filter & filters.command("chats"))
    async def chats_command(_, message):
        command = getattr(message, "command", []) or []
        page = int(command[1]) if len(command) > 1 and command[1].isdigit() else 1

        await message.reply("Fetching your groups and channels...")
        chats = await fetch_mirrorable_chats(userbot_client)
        await message.reply(format_page(chats, page))

    @command_client.on_message(owner_filter & filters.command("setsource"))
    async def setsource_command(_, message):
        command = getattr(message, "command", []) or []
        if len(command) < 2:
            await message.reply("Usage: /setsource <number from /chats, chat id, or @username>")
            return

        try:
            chat_id, title = await resolve_reference(userbot_client, command[1])
        except ChatReferenceError as exc:
            await message.reply(str(exc))
            return

        await runtime.set_source(chat_id, title)
        reply = f"Source set to: {title} (id={chat_id})"
        notice = await configured_notice()
        if notice:
            reply += f"\n{notice}"
        await message.reply(reply)

    @command_client.on_message(owner_filter & filters.command("setdest"))
    async def setdest_command(_, message):
        command = getattr(message, "command", []) or []
        if len(command) < 2:
            await message.reply("Usage: /setdest <number from /chats, chat id, or @username>")
            return

        try:
            chat_id, title = await resolve_reference(userbot_client, command[1])
        except ChatReferenceError as exc:
            await message.reply(str(exc))
            return

        await runtime.set_dest(chat_id, title)
        reply = f"Destination set to: {title} (id={chat_id})"
        notice = await configured_notice()
        if notice:
            reply += f"\n{notice}"
        await message.reply(reply)

    @command_client.on_message(owner_filter & filters.command("status"))
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

    @command_client.on_message(owner_filter & filters.command("pause"))
    async def pause_command(_, message):
        control.pause()
        await message.reply("Mirror paused. Send /resume to continue.")

    @command_client.on_message(owner_filter & filters.command("resume"))
    async def resume_command(_, message):
        control.resume()
        await message.reply("Mirror resumed.")

    @command_client.on_message(owner_filter & filters.command("resync"))
    async def resync_command(_, message):
        if runtime.current() is None:
            await message.reply("Not configured yet - send /chats, then /setsource and /setdest.")
            return

        await reset_backlog(userbot_client)
        await message.reply("Backlog rescan started from the newest message in the source chat.")


def register_media_listener(userbot_client) -> None:
    """Mirror new media in the source chat. Always lives on the userbot -
    it's the only client with access to the source group's messages."""

    @userbot_client.on_message(filters.media)
    async def mirror_media(client, message):
        # No static chat filter here: the source chat is chosen at runtime
        # via /setsource and can change, so handle_incoming_media checks the
        # current configuration itself instead of a filter baked in at
        # import time.
        await handle_incoming_media(client, message)
