from __future__ import annotations

from pyrogram import filters

from app.config.settings import MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, SYNC_HISTORY_ON_START
from app.mirror import control, runtime
from app.mirror.backlog import get_status_summary, reset_backlog
from app.mirror.dialogs import (
    ChatReferenceError,
    build_chat_keyboard,
    fetch_administered_chats,
    fetch_mirrorable_chats,
    format_page,
    is_administered,
    picker_chats,
    resolve_reference,
)
from app.mirror.listener import handle_incoming_media

HELP_TEXT = (
    "Mirror control panel - commands only work for the account owner.\n\n"
    "/chats [page] - list your groups/channels\n"
    "/setsource - tap the chat to mirror media from\n"
    "/setdest - tap the chat to mirror media into (owner/admin only)\n"
    "/status - show the configured pair and backlog progress\n"
    "/pause - stop transferring media until /resume\n"
    "/resume - resume transferring media\n"
    "/resync - rescan the full source history from the newest message\n"
    "/help - show this message"
)

BOT_COMMAND_MENU = [
    ("start", "Show help"),
    ("chats", "List your groups/channels"),
    ("setsource", "Pick the chat to mirror media from"),
    ("setdest", "Pick the chat to mirror media into"),
    ("status", "Show the configured pair and backlog progress"),
    ("pause", "Stop transferring media"),
    ("resume", "Resume transferring media"),
    ("resync", "Rescan the full source history"),
    ("help", "Show help"),
]

_ROLE_LABELS = {"src": "Source", "dst": "Destination"}


def register_control_commands(command_client, userbot_client, owner_filter) -> None:
    """Wire up /chats /setsource /setdest /status /pause /resume /resync /help.

    Registered on `command_client` (either the dedicated control bot, or the
    userbot itself as a fallback) but chat discovery/resolution always goes
    through `userbot_client`, since only the logged-in account can see its
    own dialog list and chat history - a BotFather bot can't.

    /setsource and /setdest show a tappable inline keyboard of chats by
    default; typing an explicit number/id/@username after the command still
    works too, for scripting or when a chat isn't in the picker's list.
    """

    async def configured_notice() -> str | None:
        pair = runtime.current()
        if pair is None:
            return None
        await runtime.ensure_backlog_running(userbot_client, SYNC_HISTORY_ON_START)
        return f"Now mirroring: {pair.source_title} -> {pair.dest_title}"

    async def apply_selection(role: str, chat_id: int, title: str) -> str:
        if role == "dst":
            await runtime.set_dest(chat_id, title)
        else:
            await runtime.set_source(chat_id, title)
        reply = f"{_ROLE_LABELS[role]} set to: {title} (id={chat_id})"
        notice = await configured_notice()
        if notice:
            reply += f"\n{notice}"
        return reply

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
        if len(command) >= 2:
            try:
                chat_id, title = await resolve_reference(userbot_client, command[1])
            except ChatReferenceError as exc:
                await message.reply(str(exc))
                return
            await message.reply(await apply_selection("src", chat_id, title))
            return

        await message.reply("Fetching your groups and channels...")
        chats = await fetch_mirrorable_chats(userbot_client)
        if not chats:
            await message.reply("No groups or channels found in this account's chat list.")
            return
        await message.reply(
            "Tap the group/channel to mirror media FROM:",
            reply_markup=build_chat_keyboard(chats, role="src", page=1),
        )

    @command_client.on_message(owner_filter & filters.command("setdest"))
    async def setdest_command(_, message):
        command = getattr(message, "command", []) or []
        if len(command) >= 2:
            try:
                chat_id, title = await resolve_reference(userbot_client, command[1])
            except ChatReferenceError as exc:
                await message.reply(str(exc))
                return
            if not await is_administered(userbot_client, chat_id):
                await message.reply(
                    "You need to be the owner or an admin of that chat to mirror into it."
                )
                return
            await message.reply(await apply_selection("dst", chat_id, title))
            return

        await message.reply("Fetching groups/channels you own or administer...")
        chats = await fetch_administered_chats(userbot_client)
        if not chats:
            await message.reply("No groups/channels found where you're the owner or an admin.")
            return
        await message.reply(
            "Tap the group/channel to mirror media INTO:",
            reply_markup=build_chat_keyboard(chats, role="dst", page=1),
        )

    @command_client.on_callback_query(owner_filter & filters.regex(r"^(sel|pg|cancel):"))
    async def picker_callback(_, callback_query):
        action, role, *rest = callback_query.data.split(":")

        if action == "cancel":
            await callback_query.edit_message_text("Cancelled.")
            await callback_query.answer()
            return

        if action == "pg":
            page = int(rest[0])
            chats = picker_chats(role)
            await callback_query.edit_message_reply_markup(
                build_chat_keyboard(chats, role=role, page=page)
            )
            await callback_query.answer()
            return

        # action == "sel"
        chat_id = int(rest[0])
        chats = picker_chats(role)
        match = next((chat for chat in chats if chat.id == chat_id), None)
        title = match.title if match else str(chat_id)

        if role == "dst" and not await is_administered(userbot_client, chat_id):
            await callback_query.answer(
                "You're no longer an owner/admin of that chat.", show_alert=True
            )
            return

        await callback_query.edit_message_text(await apply_selection(role, chat_id, title))
        await callback_query.answer()

    @command_client.on_message(owner_filter & filters.command("status"))
    async def status_command(_, message):
        pair = runtime.current()
        lines = ["Mirror status:"]
        if pair is None:
            lines.append("Not configured yet - send /setsource and /setdest.")
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
            await message.reply("Not configured yet - send /setsource and /setdest.")
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
