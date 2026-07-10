from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_MIRRORABLE_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}
_ADMIN_STATUSES = {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}
PAGE_SIZE = 40
BUTTON_PAGE_SIZE = 8


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

# Chat lists behind an active inline-keyboard picker, keyed by role ("src"/
# "dst"), so pagination taps don't need to re-fetch the dialog list.
_picker_chats: dict[str, list[ChatRef]] = {}


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


async def is_administered(client, chat_id: int) -> bool:
    """Whether this account is the owner or an admin of the given chat -
    the only chats media can actually be copied into."""
    try:
        member = await client.get_chat_member(chat_id, "me")
    except Exception:
        return False
    return member.status in _ADMIN_STATUSES


async def fetch_administered_chats(client) -> list[ChatRef]:
    """Groups/channels where this account is the owner or an admin."""
    candidates = await fetch_mirrorable_chats(client)
    administered: list[ChatRef] = []
    for chat in candidates:
        if await is_administered(client, chat.id):
            administered.append(chat)
    return administered


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


def _kind_emoji(kind: str) -> str:
    return "📢" if kind == "channel" else "👥"


def build_chat_keyboard(chats: list[ChatRef], role: str, page: int) -> InlineKeyboardMarkup:
    """A tappable, paginated picker: one button per chat, plus nav/cancel."""
    _picker_chats[role] = chats

    total_pages = max(1, ceil(len(chats) / BUTTON_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * BUTTON_PAGE_SIZE
    page_chats = chats[start : start + BUTTON_PAGE_SIZE]

    rows = [
        [
            InlineKeyboardButton(
                f"{_kind_emoji(chat.kind)} {chat.title[:40]}",
                callback_data=f"sel:{role}:{chat.id}",
            )
        ]
        for chat in page_chats
    ]

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"pg:{role}:{page - 1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"pg:{role}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{role}")])

    return InlineKeyboardMarkup(rows)


def picker_chats(role: str) -> list[ChatRef]:
    return _picker_chats.get(role, [])
