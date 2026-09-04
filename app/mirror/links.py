from __future__ import annotations

import re
from dataclasses import dataclass

_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(?P<path>[^\s?#]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MessageLink:
    chat_ref: int | str
    message_id: int


def parse_message_link(text: str) -> MessageLink | None:
    """Parse a t.me link to a single message into a (chat, message id) pair.

    Supports public chats (t.me/username/123), private chats via their
    internal id (t.me/c/1234567890/123), and forum-topic links (an extra
    path segment for the topic id, with the message id always last).
    Returns None if `text` doesn't contain a recognizable message link.
    """
    match = _LINK_RE.search(text)
    if not match:
        return None

    segments = [segment for segment in match.group("path").split("/") if segment]

    if segments and segments[0] == "c":
        segments = segments[1:]
        if len(segments) < 2 or not segments[0].lstrip("-").isdigit():
            return None
        chat_ref: int | str = int(f"-100{segments[0]}")
        message_segment = segments[-1]
    else:
        if len(segments) < 2:
            return None
        chat_ref = segments[0]
        message_segment = segments[-1]

    if not message_segment.isdigit():
        return None

    return MessageLink(chat_ref=chat_ref, message_id=int(message_segment))
