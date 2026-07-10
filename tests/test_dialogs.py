from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from pyrogram.enums import ChatMemberStatus, ChatType

from app.mirror.dialogs import (
    ChatRef,
    ChatReferenceError,
    build_chat_keyboard,
    fetch_administered_chats,
    fetch_mirrorable_chats,
    format_page,
    is_administered,
    picker_chats,
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


@dataclass(slots=True)
class FakeMember:
    status: ChatMemberStatus


class FakeClient:
    def __init__(self, dialogs, chats_by_ref=None, member_status=None):
        self._dialogs = dialogs
        self._chats_by_ref = chats_by_ref or {}
        self._member_status = member_status or {}

    async def get_dialogs(self):
        for dialog in self._dialogs:
            yield dialog

    async def get_chat(self, identifier):
        if identifier in self._chats_by_ref:
            return self._chats_by_ref[identifier]
        raise RuntimeError(f"unknown chat {identifier}")

    async def get_chat_member(self, chat_id, _user_id):
        if chat_id not in self._member_status:
            raise RuntimeError(f"not a member of {chat_id}")
        return FakeMember(status=self._member_status[chat_id])


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


def test_is_administered_true_for_owner_and_admin() -> None:
    client = FakeClient(
        dialogs=[],
        member_status={
            -100111: ChatMemberStatus.OWNER,
            -100222: ChatMemberStatus.ADMINISTRATOR,
            -100333: ChatMemberStatus.MEMBER,
        },
    )

    assert asyncio.run(is_administered(client, -100111)) is True
    assert asyncio.run(is_administered(client, -100222)) is True
    assert asyncio.run(is_administered(client, -100333)) is False


def test_is_administered_false_when_status_lookup_fails() -> None:
    client = FakeClient(dialogs=[])
    assert asyncio.run(is_administered(client, -100999)) is False


def test_fetch_administered_chats_filters_to_owner_and_admin() -> None:
    dialogs = [
        FakeDialog(FakeChat(id=-100111, title="Owned Group", type=ChatType.SUPERGROUP)),
        FakeDialog(FakeChat(id=-100222, title="Admin Channel", type=ChatType.CHANNEL)),
        FakeDialog(FakeChat(id=-100333, title="Just a Member", type=ChatType.SUPERGROUP)),
    ]
    client = FakeClient(
        dialogs=dialogs,
        member_status={
            -100111: ChatMemberStatus.OWNER,
            -100222: ChatMemberStatus.ADMINISTRATOR,
            -100333: ChatMemberStatus.MEMBER,
        },
    )

    chats = asyncio.run(fetch_administered_chats(client))

    assert [chat.id for chat in chats] == [-100111, -100222]


def test_build_chat_keyboard_has_one_button_per_chat_and_cancel_row() -> None:
    chats = [
        ChatRef(id=-100111, title="Group A", kind="supergroup", username=None),
        ChatRef(id=-100222, title="Channel B", kind="channel", username=None),
    ]

    markup = build_chat_keyboard(chats, role="src", page=1)

    # One row per chat, plus a trailing cancel row (no pagination needed).
    assert len(markup.inline_keyboard) == 3
    assert markup.inline_keyboard[0][0].callback_data == "sel:src:-100111"
    assert markup.inline_keyboard[1][0].callback_data == "sel:src:-100222"
    assert markup.inline_keyboard[-1][0].callback_data == "cancel:src"
    assert picker_chats("src") == chats


def test_build_chat_keyboard_paginates_and_adds_nav_buttons() -> None:
    chats = [
        ChatRef(id=-100000 - i, title=f"Chat {i}", kind="group", username=None) for i in range(10)
    ]

    first_page = build_chat_keyboard(chats, role="dst", page=1)
    # 8 chats + a "Next" nav row + a cancel row.
    assert len(first_page.inline_keyboard) == 8 + 1 + 1
    nav_row = first_page.inline_keyboard[8]
    assert [button.callback_data for button in nav_row] == ["pg:dst:2"]

    second_page = build_chat_keyboard(chats, role="dst", page=2)
    # 2 remaining chats + a "Prev" nav row + a cancel row.
    assert len(second_page.inline_keyboard) == 2 + 1 + 1
    nav_row = second_page.inline_keyboard[2]
    assert [button.callback_data for button in nav_row] == ["pg:dst:1"]
