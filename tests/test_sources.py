from __future__ import annotations

import asyncio

import pytest
from helpers import FakeUserbot, make_chat
from pyrogram import enums
from pyrogram.errors import InviteHashExpired, UserAlreadyParticipant

from app.mirror.sources import (
    ChatChoice,
    InvitePending,
    SourceError,
    join_and_resolve,
    resolve_source,
    verify_readable,
)

SRC = -100999


def run(coro):
    return asyncio.run(coro)


def userbot(**chats) -> FakeUserbot:
    bot = FakeUserbot()
    bot.chats[SRC] = make_chat(SRC, "Source A")
    public = make_chat(-100555, "Public Channel", username="somechannel")
    bot.chats["somechannel"] = public
    bot.chats[-100555] = public  # Pyrogram caches the peer once the username was resolved
    bot.chats.update(chats)
    return bot


@pytest.mark.parametrize(
    ("ref", "chat_id"),
    [
        (str(SRC), SRC),
        ("@somechannel", -100555),
        ("somechannel", -100555),
        ("https://t.me/somechannel", -100555),
        ("t.me/somechannel/", -100555),
    ],
)
def test_resolves_ids_usernames_and_public_links(ref: str, chat_id: int) -> None:
    resolved = run(resolve_source(userbot(), ref))
    assert isinstance(resolved, ChatChoice) and resolved.chat_id == chat_id


def test_only_groups_and_channels_can_be_sources() -> None:
    bot = userbot()
    bot.chats[777] = make_chat(777, "Some Person", kind=enums.ChatType.PRIVATE)
    with pytest.raises(SourceError, match="المجموعات والقنوات"):
        run(resolve_source(bot, "777"))


def test_unknown_chat_and_empty_input_give_arabic_errors() -> None:
    with pytest.raises(SourceError, match="تعذّر العثور"):
        run(resolve_source(userbot(), "-100123"))
    with pytest.raises(SourceError, match="أرسل"):
        run(resolve_source(userbot(), "   "))


def test_invite_link_for_a_chat_we_are_in_resolves_directly() -> None:
    link = "https://t.me/+AbCdEf123"
    resolved = run(resolve_source(userbot(**{link: make_chat(-100777, "Member Of")}), link))
    assert isinstance(resolved, ChatChoice) and resolved.chat_id == -100777


def test_invite_link_for_an_unjoined_chat_is_pending_and_never_auto_joins() -> None:
    from types import SimpleNamespace

    link = "https://t.me/joinchat/AbCdEf123"
    bot = userbot(**{"https://t.me/+AbCdEf123": SimpleNamespace(id=None, title="Preview Only")})
    resolved = run(resolve_source(bot, link))
    assert isinstance(resolved, InvitePending) and resolved.title == "Preview Only"
    assert bot.joined == []


def test_expired_invite_is_explained() -> None:
    class Expired(FakeUserbot):
        async def get_chat(self, identifier):
            raise InviteHashExpired()

    with pytest.raises(SourceError, match="منتهي"):
        run(resolve_source(Expired(), "https://t.me/+AbCdEf123"))


def test_join_and_resolve_handles_already_participant() -> None:
    link = "https://t.me/+AbCdEf123"

    class Already(FakeUserbot):
        async def join_chat(self, _link):
            raise UserAlreadyParticipant()

    bot = Already()
    bot.chats[link] = make_chat(-100777, "Member Of")
    assert run(join_and_resolve(bot, link)).chat_id == -100777

    joined = userbot(**{link: make_chat(-100888, "Fresh")})
    assert run(join_and_resolve(joined, link)).chat_id == -100888
    assert joined.joined == [link]


def test_verify_readable() -> None:
    bot = userbot()
    run(verify_readable(bot, SRC))  # readable: no error
    bot.unreadable.add(SRC)
    with pytest.raises(SourceError, match="ليس عضوًا"):
        run(verify_readable(bot, SRC))
