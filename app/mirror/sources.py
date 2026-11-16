"""Resolving and verifying a chat before it is saved as a mirror source.

The owner can add a source by picking it from the dialog list, or by typing
a numeric ID, an @username, a t.me/<username> link or an invite link. In
every case the userbot must be able to actually *read* the chat before it is
saved - otherwise the mirror would silently do nothing for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pyrogram.enums import ChatType
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    InviteHashExpired,
    InviteHashInvalid,
    PeerIdInvalid,
    UserAlreadyParticipant,
    UsernameInvalid,
    UsernameNotOccupied,
)

from app.mirror.dialogs import ChatReferenceError, resolve_reference

_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t(?:elegram)?\.(?:me|dog)/(?:\+|joinchat/)(?P<hash>[\w-]+)",
    re.IGNORECASE,
)
_PUBLIC_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.(?:me|dog)/(?P<name>[A-Za-z][\w]{3,31})/?$",
    re.IGNORECASE,
)
_READABLE_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}


class SourceError(ValueError):
    """The chat can't be used as a source. The message is shown to the owner (Arabic)."""


@dataclass(frozen=True, slots=True)
class ChatChoice:
    chat_id: int
    title: str
    kind: str


@dataclass(frozen=True, slots=True)
class InvitePending:
    """The invite link points to a chat the userbot has not joined yet."""

    link: str
    title: str


def explain_error(exc: BaseException) -> str:
    """Owner-facing Arabic explanation of a Telegram error while resolving a chat."""
    if isinstance(exc, (UsernameNotOccupied, UsernameInvalid)):
        return "لم أجد قناة أو مجموعة بهذا الاسم."
    if isinstance(exc, (ChannelPrivate, ChannelInvalid, PeerIdInvalid)):
        return "لا أستطيع الوصول لهذه المحادثة: الحساب ليس عضوًا فيها، أو المعرّف غير صحيح."
    if isinstance(exc, (InviteHashExpired, InviteHashInvalid)):
        return "رابط الدعوة منتهي الصلاحية أو غير صالح."
    return f"تعذّر الوصول للمحادثة ({type(exc).__name__})."


def _choice(chat) -> ChatChoice:
    if getattr(chat, "type", None) not in _READABLE_TYPES:
        raise SourceError("يمكن إضافة المجموعات والقنوات فقط كمصادر.")
    return ChatChoice(chat_id=chat.id, title=chat.title or str(chat.id), kind=chat.type.value)


async def resolve_source(client, ref: str) -> ChatChoice | InvitePending:
    """Turn what the owner typed into a chat (or a pending invite). Does not verify reading."""
    ref = ref.strip()
    if not ref:
        raise SourceError("أرسل معرّف المحادثة أو @username أو رابط دعوة.")

    invite = _INVITE_RE.search(ref)
    if invite:
        link = f"https://t.me/+{invite.group('hash')}"
        try:
            chat = await client.get_chat(link)
        except Exception as exc:
            raise SourceError(explain_error(exc)) from exc
        if getattr(chat, "id", None) is None:  # ChatPreview: not a member yet
            return InvitePending(link=link, title=getattr(chat, "title", None) or "؟")
        return _choice(chat)

    public = _PUBLIC_LINK_RE.match(ref)
    if public:
        ref = "@" + public.group("name")

    try:
        chat_id, _title = await resolve_reference(
            client, ref.lstrip("@") if ref.startswith("@") else ref
        )
    except ChatReferenceError as exc:
        raise SourceError(str(exc)) from exc
    try:
        chat = await client.get_chat(chat_id)
    except Exception as exc:
        raise SourceError(explain_error(exc)) from exc
    return _choice(chat)


async def join_and_resolve(client, link: str) -> ChatChoice:
    """Join through an invite link (explicitly confirmed by the owner) and return the chat."""
    try:
        chat = await client.join_chat(link)
    except UserAlreadyParticipant:
        chat = await client.get_chat(link)
    except Exception as exc:
        raise SourceError(explain_error(exc)) from exc
    return _choice(chat)


async def verify_readable(client, chat_id: int) -> None:
    """Raise SourceError unless the userbot can read this chat's history."""
    try:
        async for _message in client.get_chat_history(chat_id, limit=1):  # type: ignore[union-attr]
            break
    except Exception as exc:
        raise SourceError(explain_error(exc)) from exc
