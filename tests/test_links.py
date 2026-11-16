from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mirror.links import extract_links, find_links, has_message_link, parse_message_link


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


@pytest.mark.parametrize(
    ("text", "chat_ref", "message_id", "topic_id"),
    [
        ("https://t.me/c/1234567890/55", -1001234567890, 55, None),
        ("t.me/c/1234567890/12/55", -1001234567890, 55, 12),
        ("http://t.me/c/1234567890/55?single", -1001234567890, 55, None),
        ("https://www.t.me/somechannel/55", "somechannel", 55, None),
        ("https://t.me/somechannel/55?comment=7", "somechannel", 55, None),
        ("https://t.me/somechannel/55?single&thread=3", "somechannel", 55, None),
        ("https://t.me/somechannel/12/55#frag", "somechannel", 55, 12),
        ("https://t.me/s/somechannel/55", "somechannel", 55, None),
        ("https://telegram.me/somechannel/55", "somechannel", 55, None),
        ("https://telegram.dog/somechannel/55", "somechannel", 55, None),
        ("HTTPS://T.ME/SomeChannel/55", "SomeChannel", 55, None),
        ("(https://t.me/somechannel/55)", "somechannel", 55, None),
        ("see https://t.me/somechannel/55.", "somechannel", 55, None),
        ("[click](https://t.me/c/111/2)", -100111, 2, None),
        ("https://t.me/c/1234567890/2147483647", -1001234567890, 2147483647, None),
    ],
)
def test_supported_link_forms(text, chat_ref, message_id, topic_id) -> None:
    links = find_links(text)
    assert len(links) == 1
    assert (links[0].chat_ref, links[0].message_id, links[0].topic_id) == (
        chat_ref,
        message_id,
        topic_id,
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no links here",
        "https://t.me/somechannel",  # channel link, no message
        "https://t.me/+AbCdEfGh123",  # invite link
        "https://t.me/joinchat/AAAAAEabc/5",
        "https://t.me/addstickers/Pack/3",
        "https://t.me/c/0/5",  # internal id 0
        "https://t.me/c/123",  # no message id
        "https://t.me/c/123/abc",  # non-numeric message
        "https://t.me/c/123/0",  # message id 0
        "https://t.me/c/123/2147483648",  # too large
        "https://t.me/ab/5",  # username too short
        "https://t.me/1badname/5",  # must start with a letter
        "https://evilt.me/c/1/2",  # lookalike domain
        "https://example.com/t.me/c/1/2",
        "https://t.me/c/1/2/3/4",  # too many segments
    ],
)
def test_rejects_malformed_or_non_message_links(text) -> None:
    assert find_links(text) == []


def test_multiple_links_keep_order_and_drop_duplicates() -> None:
    text = "https://t.me/c/1/2 https://t.me/foobar/9 https://t.me/c/1/2 https://t.me/c/1/3"
    assert [(l.chat_ref, l.message_id) for l in find_links(text)] == [
        (-1001, 2),
        ("foobar", 9),
        (-1001, 3),
    ]


def test_links_are_found_in_text_caption_and_text_link_entities() -> None:
    message = SimpleNamespace(
        text="hello https://t.me/c/1/2",
        caption="cap t.me/foobar/9",
        entities=[SimpleNamespace(url="https://t.me/c/5/6"), SimpleNamespace(url=None)],
        caption_entities=[SimpleNamespace(url="https://t.me/c/1/2")],  # duplicate of the text
    )
    assert [(l.chat_ref, l.message_id) for l in extract_links(message)] == [
        (-1001, 2),
        ("foobar", 9),
        (-1005, 6),
    ]
    assert has_message_link(message)


def test_hidden_text_link_with_no_visible_url() -> None:
    message = SimpleNamespace(
        text="click here",
        caption=None,
        entities=[SimpleNamespace(url="https://t.me/c/7/8")],
        caption_entities=None,
    )
    assert [l.message_id for l in extract_links(message)] == [8]


def test_extract_links_tolerates_messages_without_text() -> None:
    assert (
        extract_links(
            SimpleNamespace(text=None, caption=None, entities=None, caption_entities=None)
        )
        == []
    )
    assert not has_message_link(SimpleNamespace())
