"""Parsing of t.me message links.

Handles every form the owner is likely to paste:

    https://t.me/c/1234567890/55            private chat/channel -> chat id -1001234567890
    https://t.me/c/1234567890/12/55         ... inside forum topic 12
    https://t.me/somechannel/55             public chat
    https://t.me/somechannel/12/55          public forum topic
    https://t.me/s/somechannel/55           web-preview form
    t.me/..., telegram.me/..., telegram.dog/..., with or without http(s)://www.
    any of them with ?single, ?comment=..., ?thread=..., #fragment

Only the linked message id matters (a message id is unique within its chat,
so a topic id is parsed but not needed to fetch it). Links are looked for in
message text, captions and `text_link` entities, duplicates removed, order kept.
Invite links (t.me/+hash, t.me/joinchat/...), sticker/proxy/share links and bare
channel links (no message id) are ignored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_LINK_RE = re.compile(
    r"(?<![\w.@/-])(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(?P<rest>[^\s<>\"'()\[\]{}]+)",
    re.IGNORECASE,
)

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_RESERVED = frozenset(
    {
        "joinchat",
        "addstickers",
        "addemoji",
        "addtheme",
        "addlist",
        "share",
        "proxy",
        "socks",
        "login",
        "setlanguage",
        "boost",
        "invoice",
        "bg",
        "iv",
        "confirmphone",
        "contact",
        "giftcode",
        "m",
    }
)
_MAX_MESSAGE_ID = 2**31 - 1
_TRAILING_PUNCTUATION = ".,;:!?،؛"

LINK_PRESENCE_RE = re.compile(r"(?i)(?:t|telegram)\.(?:me|dog)/\S+")


@dataclass(frozen=True, slots=True)
class MessageLink:
    chat_ref: int | str
    message_id: int
    topic_id: int | None = None

    @property
    def key(self) -> str:
        return f"{self.chat_ref}/{self.message_id}"


def _parse_rest(rest: str) -> MessageLink | None:
    rest = rest.split("#", 1)[0].split("?", 1)[0].rstrip(_TRAILING_PUNCTUATION)
    segments = [segment for segment in rest.split("/") if segment]
    if len(segments) < 2:
        return None

    if segments[0].lower() == "s" and len(segments) >= 3:  # web-preview form
        segments = segments[1:]

    if segments[0].lower() == "c":
        segments = segments[1:]
        # c/<internal>/<msg>  or  c/<internal>/<topic>/<msg>
        if len(segments) not in (2, 3) or not all(s.isdigit() for s in segments):
            return None
        internal = int(segments[0])
        if internal <= 0:
            return None
        chat_ref: int | str = int(f"-100{internal}")
        topic_id = int(segments[1]) if len(segments) == 3 else None
        segments = segments[1:]  # now [msg] or [topic, msg], same shape as below
    else:
        # <username>/<msg>  or  <username>/<topic>/<msg>
        username = segments[0]
        if username.startswith("+") or username.lower() in _RESERVED:
            return None
        if not _USERNAME_RE.match(username):
            return None
        segments = segments[1:]
        if len(segments) not in (1, 2) or not all(s.isdigit() for s in segments):
            return None
        chat_ref = username
        topic_id = int(segments[0]) if len(segments) == 2 else None

    message_id = int(segments[-1])
    if message_id <= 0 or message_id > _MAX_MESSAGE_ID:
        return None
    return MessageLink(chat_ref=chat_ref, message_id=message_id, topic_id=topic_id)


def find_links(text: str | None) -> list[MessageLink]:
    """All message links in one string, in order, without duplicates."""
    if not text:
        return []
    found: list[MessageLink] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(text):
        link = _parse_rest(match.group("rest"))
        if link is not None and link.key not in seen:
            seen.add(link.key)
            found.append(link)
    return found


def parse_message_link(text: str) -> MessageLink | None:
    """First message link in `text`, or None (kept for backwards compatibility)."""
    links = find_links(text)
    return links[0] if links else None


def extract_links(message) -> list[MessageLink]:
    """Message links from a Pyrogram message: text, caption and text_link entities."""
    pieces: list[str] = []
    for attr in ("text", "caption"):
        value = getattr(message, attr, None)
        if value:
            pieces.append(str(value))
    for attr in ("entities", "caption_entities"):
        entities: Iterable | None = getattr(message, attr, None)
        for entity in entities or []:
            url = getattr(entity, "url", None)
            if url:
                pieces.append(str(url))

    found: list[MessageLink] = []
    seen: set[str] = set()
    for piece in pieces:
        for link in find_links(piece):
            if link.key not in seen:
                seen.add(link.key)
                found.append(link)
    return found


def has_message_link(message) -> bool:
    return bool(extract_links(message))
