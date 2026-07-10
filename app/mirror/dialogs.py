from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from pyrogram.enums import ChatType

_MIRRORABLE_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}
PAGE_SIZE = 40


@dataclass(frozen=True, slots=True)
class ChatRef:
    id: int
    title: str
    kind: str
    username: str | None


class ChatReferenceError(ValueError):
    """Raised when /setsource or /setdest can't resolve what the user typed."""


# Last /chats listing, so /setsource <n> and /setdest <n> can use the short
# index instead of a full chat ID. Process-local by design: it's only ever
# read back within the same running session that produced it.
_last_listing: dict[int, ChatRef] = {}


async def fetch_mirrorable_chats(client) -> list[ChatRef]:
    """List the account's groups, supergroups, and channels (not DMs/bots)."""
    chats: list[ChatRef] = []
    async for dialog in client.get_dialogs():
        chat = dialog.chat
        if chat.type not in _MIRRORABLE_TYPES:
            continue
        chats.append(
            ChatRef(
                id=chat.id,
                title=chat.title or str(chat.id),
                kind=chat.type.value,
                username=chat.username,
            )
        )
    return chats


def format_page(chats: list[ChatRef], page: int) -> str:
    global _last_listing
    _last_listing = {index: chat for index, chat in enumerate(chats, start=1)}

    if not chats:
        return "No groups or channels found in this account's chat list."

    total_pages = max(1, ceil(len(chats) / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE

    lines = [f"Your groups/channels ({len(chats)} total) - page {page}/{total_pages}:"]
    for index, chat in list(enumerate(chats, start=1))[start : start + PAGE_SIZE]:
        handle = f"@{chat.username}" if chat.username else "no username"
        lines.append(f"{index}. {chat.title} [{chat.kind}] {handle} id={chat.id}")

    if page < total_pages:
        lines.append(f"\nMore: /chats {page + 1}")
    lines.append("\nPick one with /setsource <number> or /setdest <number>.")
    return "\n".join(lines)


async def resolve_reference(client, ref: str) -> tuple[int, str]:
    """Resolve a /setsource or /setdest argument to a (chat_id, title) pair.

    Accepts the index shown by the last /chats listing, a raw numeric chat
    ID, or a @username - in that priority order.
    """
    ref = ref.strip()
    if not ref:
        raise ChatReferenceError("Provide a number from /chats, a chat id, or a @username.")

    if ref.isdigit() and int(ref) in _last_listing:
        entry = _last_listing[int(ref)]
        return entry.id, entry.title

    identifier: int | str = int(ref) if ref.lstrip("-").isdigit() else ref
    try:
        chat = await client.get_chat(identifier)
    except Exception as exc:
        raise ChatReferenceError(
            f"Could not find that chat ({exc}). Run /chats to list your groups/channels first."
        ) from exc
    return chat.id, chat.title or str(chat.id)
