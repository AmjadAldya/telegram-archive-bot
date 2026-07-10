from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from pyrogram.enums import ChatType

from app.mirror.dialogs import (
    ChatRef,
    ChatReferenceError,
    fetch_mirrorable_chats,
    format_page,
    resolve_reference,
)


@dataclass(slots=True)
class FakeChat:
    id: int
    title: str
    type: ChatType
    username: str | None = None


@dataclass(slots=True)
class FakeDialog:
    chat: FakeChat


class FakeClient:
    def __init__(self, dialogs, chats_by_ref=None):
        self._dialogs = dialogs
        self._chats_by_ref = chats_by_ref or {}

    async def get_dialogs(self):
        for dialog in self._dialogs:
            yield dialog

    async def get_chat(self, identifier):
        if identifier in self._chats_by_ref:
            return self._chats_by_ref[identifier]
        raise RuntimeError(f"unknown chat {identifier}")


def test_fetch_mirrorable_chats_excludes_private_and_bot_dialogs() -> None:
    dialogs = [
        FakeDialog(FakeChat(id=1, title="DM", type=ChatType.PRIVATE)),
        FakeDialog(FakeChat(id=2, title="A Bot", type=ChatType.BOT)),
        FakeDialog(
            FakeChat(id=-100111, title="My Group", type=ChatType.SUPERGROUP, username="mygroup")
        ),
        FakeDialog(FakeChat(id=-100222, title="My Channel", type=ChatType.CHANNEL)),
    ]
    client = FakeClient(dialogs)

    chats = asyncio.run(fetch_mirrorable_chats(client))

    assert [chat.id for chat in chats] == [-100111, -100222]


def test_format_page_lists_all_chats() -> None:
    chats = [
        ChatRef(id=-100111, title="Group A", kind="supergroup", username="groupa"),
        ChatRef(id=-100222, title="Channel B", kind="channel", username=None),
    ]

    text = format_page(chats, page=1)

    assert "Group A" in text
    assert "Channel B" in text
    assert "1." in text
    assert "2." in text


def test_format_page_reports_no_chats() -> None:
    assert "No groups or channels" in format_page([], page=1)


def test_resolve_reference_by_index_from_last_listing() -> None:
    chats = [ChatRef(id=-100111, title="Group A", kind="supergroup", username="groupa")]
    format_page(chats, page=1)

    client = FakeClient(dialogs=[])
    chat_id, title = asyncio.run(resolve_reference(client, "1"))

    assert chat_id == -100111
    assert title == "Group A"


def test_resolve_reference_by_raw_id_or_username() -> None:
    target_chat = FakeChat(id=-100999, title="Direct", type=ChatType.CHANNEL)
    client = FakeClient(dialogs=[], chats_by_ref={-100999: target_chat, "@direct": target_chat})

    chat_id, title = asyncio.run(resolve_reference(client, "-100999"))
    assert chat_id == -100999
    assert title == "Direct"

    chat_id, title = asyncio.run(resolve_reference(client, "@direct"))
    assert chat_id == -100999
    assert title == "Direct"


def test_resolve_reference_raises_for_unknown_chat() -> None:
    client = FakeClient(dialogs=[])
    with pytest.raises(ChatReferenceError):
        asyncio.run(resolve_reference(client, "@doesnotexist"))


def test_resolve_reference_raises_for_empty_input() -> None:
    client = FakeClient(dialogs=[])
    with pytest.raises(ChatReferenceError):
        asyncio.run(resolve_reference(client, "   "))
