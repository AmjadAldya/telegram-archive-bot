from __future__ import annotations

import asyncio

import pytest
from helpers import FakeChat, FakeFile, FakeMessage, FakeSentMessage, FakeUserbot
from pyrogram.errors import (
    ChannelPrivate,
    FloodWait,
    MessageIdInvalid,
    PeerIdInvalid,
    UsernameNotOccupied,
)

from app.bot.linkflow import LinkQueue, LinkTask, TokenStore, result_for, run_link
from app.database.base import session_scope
from app.database.repositories import MirrorRepository
from app.mirror import control, runtime
from app.mirror.jobs import Job, registry
from app.mirror.links import MessageLink

DEST = -100222
LINK = MessageLink(-1005551234, 42)  # a chat that is NOT a configured source


def photo_message(unique: str = "p1", message_id: int = 42) -> FakeMessage:
    return FakeMessage(
        id=message_id, chat=FakeChat(id=-1005551234, title="Random Chat"), photo=FakeFile(unique)
    )


def run_one(userbot, link: MessageLink = LINK, *, force: bool = False, with_dest: bool = True):
    async def scenario():
        if with_dest:
            await runtime.set_dest(DEST, "Archive")
        job = registry.create("link", link.chat_ref, str(link.chat_ref), link.message_id)
        return await run_link(userbot, LinkTask(link, job, FakeSentMessage("…"), force=force)), job

    return asyncio.run(scenario())


def in_ledger(unique: str) -> bool:
    with session_scope() as session:
        return MirrorRepository(session).is_duplicate(str(DEST), unique)


def test_pasted_link_from_a_non_source_chat_is_transferred_and_recorded() -> None:
    userbot = FakeUserbot(messages={(LINK.chat_ref, 42): photo_message()})
    result, _job = run_one(userbot)

    assert result.ok and "تم النقل" in result.text and "صورة" in result.text
    assert in_ledger("p1")
    assert userbot.get_messages_calls == [(LINK.chat_ref, 42)]  # only the linked message is fetched


def test_only_the_linked_message_is_sent_even_if_it_is_part_of_an_album() -> None:
    album_message = photo_message("a2", message_id=42)
    album_message.id = 42
    userbot = FakeUserbot(
        messages={(LINK.chat_ref, 42): album_message, (LINK.chat_ref, 43): photo_message("a3", 43)}
    )
    run_one(userbot)
    assert len(userbot.send_calls) == 1 and in_ledger("a2") and not in_ledger("a3")


def test_duplicate_offers_a_forced_retransfer_which_works() -> None:
    userbot = FakeUserbot(messages={(LINK.chat_ref, 42): photo_message()})
    first, _ = run_one(userbot)
    second, _ = run_one(userbot)
    forced, _ = run_one(userbot, force=True)

    assert first.ok
    assert not second.ok and second.can_force and "موجود مسبقًا" in second.text
    assert forced.ok and len(userbot.send_calls) == 2


def test_message_without_media_is_reported_clearly() -> None:
    text_only = FakeMessage(id=42, chat=FakeChat(id=-1005551234), text="hello")
    result, _ = run_one(FakeUserbot(messages={(LINK.chat_ref, 42): text_only}))
    assert "لا تحتوي وسائط" in result.text


def test_link_transfer_ignores_the_media_types_filter(monkeypatch) -> None:
    monkeypatch.setattr("app.mirror.media.MEDIA_TYPES", ("photo",))
    video = FakeMessage(id=42, chat=FakeChat(id=-1005551234), video=FakeFile("v1"))
    result, _ = run_one(FakeUserbot(messages={(LINK.chat_ref, 42): video}))
    assert result.ok


def test_deleted_message_is_reported() -> None:
    gone = FakeMessage(id=42, chat=FakeChat(id=-1005551234), empty=True)
    assert "محذوفة" in run_one(FakeUserbot(messages={(LINK.chat_ref, 42): gone}))[0].text
    assert "محذوفة" in run_one(FakeUserbot())[0].text  # get_messages returns None
    invalid = FakeUserbot(get_messages_errors=[MessageIdInvalid()])
    assert "محذوفة" in run_one(invalid)[0].text


def test_unknown_public_channel_is_reported() -> None:
    userbot = FakeUserbot(get_messages_errors=[UsernameNotOccupied()])
    result, _ = run_one(userbot, MessageLink("nosuchchannel", 5))
    assert "لم أجد" in result.text


def test_not_a_member_is_reported_after_one_peer_cache_refresh() -> None:
    userbot = FakeUserbot(get_messages_errors=[ChannelPrivate(), ChannelPrivate()])
    result, _ = run_one(userbot)
    assert "ليس عضوًا" in result.text
    assert userbot.dialogs_calls == 1  # refreshed Pyrogram's peer cache once, then gave up


def test_a_chat_joined_after_startup_works_after_the_peer_cache_refresh() -> None:
    userbot = FakeUserbot(
        messages={(LINK.chat_ref, 42): photo_message()}, get_messages_errors=[PeerIdInvalid()]
    )
    result, _ = run_one(userbot)
    assert result.ok and userbot.dialogs_calls == 1


def test_missing_destination_is_reported() -> None:
    result, _ = run_one(
        FakeUserbot(messages={(LINK.chat_ref, 42): photo_message()}), with_dest=False
    )
    assert "الوجهة غير محددة" in result.text


def test_flood_wait_is_reported_with_the_wait_time(monkeypatch) -> None:
    from types import SimpleNamespace

    real_sleep = asyncio.sleep

    async def instant_sleep(_seconds):
        await real_sleep(0)

    # Only the throttle module's view of asyncio, so the retries don't really sleep.
    monkeypatch.setattr("app.mirror.throttle.asyncio", SimpleNamespace(sleep=instant_sleep))
    monkeypatch.setattr("app.mirror.throttle.random.uniform", lambda *_a: 0.0)
    userbot = FakeUserbot(get_messages_errors=[FloodWait(value=7)] * 20)

    result, _ = run_one(userbot)

    assert "7 ثانية" in result.text and "⏳" in result.text


def test_restricted_account_is_reported() -> None:
    control.mark_restricted("banned")
    try:
        result, _ = run_one(FakeUserbot(messages={(LINK.chat_ref, 42): photo_message()}))
    finally:
        control.resume()
    assert "مقيَّد" in result.text


def test_job_is_always_removed_from_the_registry() -> None:
    run_one(FakeUserbot(get_messages_errors=[UsernameNotOccupied()]))
    run_one(FakeUserbot(messages={(LINK.chat_ref, 42): photo_message()}))
    assert registry.snapshot().idle


@pytest.mark.parametrize(
    ("outcome", "error", "expected"),
    [
        ("transferred", None, "✅"),
        ("duplicate", None, "♻️"),
        ("user_skip", None, "⏭"),
        ("timeout_skip", None, "⏱"),
        ("auto_skip", None, "🚫"),
        ("failed", "dest_forbidden", "الكتابة في الوجهة"),
        ("skipped", "no_media", "لا تحتوي وسائط"),
        ("skipped", "unavailable", "لم تعد الوسائط متاحة"),
        ("skipped", "restricted", "مقيَّد"),
    ],
)
def test_result_messages_cover_every_outcome(outcome, error, expected) -> None:
    job = Job(id=1, kind="link", source_chat_id="-1", source_title="S", message_id=1)
    job.error = error
    assert expected in result_for(outcome, job).text


def test_link_queue_processes_tasks_strictly_in_order() -> None:
    order: list[int] = []

    async def runner(task: LinkTask) -> None:
        await asyncio.sleep(0.02 if task.link.message_id == 1 else 0)  # first one is slower
        order.append(task.link.message_id)

    async def scenario() -> None:
        queue = LinkQueue(runner)
        queue.start()
        positions = [
            await queue.submit(LinkTask(MessageLink("chan1", i), None, None))  # type: ignore[arg-type]
            for i in (1, 2, 3)
        ]
        assert positions == [1, 2, 3]
        await asyncio.sleep(0.2)
        await queue.stop()

    asyncio.run(scenario())
    assert order == [1, 2, 3]


def test_link_queue_survives_a_failing_task() -> None:
    seen: list[int] = []

    async def runner(task: LinkTask) -> None:
        if task.link.message_id == 1:
            raise RuntimeError("boom")
        seen.append(task.link.message_id)

    async def scenario() -> None:
        queue = LinkQueue(runner)
        queue.start()
        for i in (1, 2):
            await queue.submit(LinkTask(MessageLink("chan1", i), None, None))  # type: ignore[arg-type]
        await asyncio.sleep(0.1)
        await queue.stop()

    asyncio.run(scenario())
    assert seen == [2]


def test_token_store_is_bounded_and_returns_none_for_unknown_tokens() -> None:
    store = TokenStore(limit=2)
    first, second, third = store.put("a"), store.put("b"), store.put("c")
    assert store.get(first) is None  # evicted
    assert (store.get(second), store.get(third)) == ("b", "c")
    assert store.get("999") is None
    assert len(first) < 8  # tiny callback data
