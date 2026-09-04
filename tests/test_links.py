from __future__ import annotations

from app.mirror.links import parse_message_link


def test_parses_public_chat_link() -> None:
    link = parse_message_link("https://t.me/somegroup/123")
    assert link is not None
    assert link.chat_ref == "somegroup"
    assert link.message_id == 123


def test_parses_link_without_scheme() -> None:
    link = parse_message_link("t.me/somegroup/123")
    assert link is not None
    assert link.chat_ref == "somegroup"
    assert link.message_id == 123


def test_parses_private_chat_link() -> None:
    link = parse_message_link("https://t.me/c/1234567890/42")
    assert link is not None
    assert link.chat_ref == -1001234567890
    assert link.message_id == 42


def test_parses_forum_topic_link() -> None:
    link = parse_message_link("https://t.me/somegroup/7/123")
    assert link is not None
    assert link.chat_ref == "somegroup"
    assert link.message_id == 123


def test_ignores_surrounding_text() -> None:
    link = parse_message_link("check this out: https://t.me/somegroup/123 nice right?")
    assert link is not None
    assert link.chat_ref == "somegroup"
    assert link.message_id == 123


def test_returns_none_for_non_link_text() -> None:
    assert parse_message_link("hello world") is None


def test_returns_none_for_chat_link_without_message_id() -> None:
    assert parse_message_link("https://t.me/somegroup") is None
