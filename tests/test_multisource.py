from __future__ import annotations

import asyncio

from helpers import FakeChat, FakeFile, FakeMessage, FakeUserbot

from app.database.base import session_scope
from app.database.models import SkippedMedia, SyncStatus
from app.database.repositories import MirrorRepository
from app.mirror import runtime
from app.mirror.backlog import read_progress, reset_backlog
from app.mirror.jobs import registry
from app.mirror.listener import handle_incoming_media

DEST = -100222
SRC_A, SRC_B, SRC_C = -100111, -100333, -100444


def msg(chat: int, message_id: int, unique: str) -> FakeMessage:
    return FakeMessage(
        id=message_id, chat=FakeChat(id=chat, title=f"chat{chat}"), photo=FakeFile(unique)
    )


async def wait_backlog_done(timeout: float = 5.0) -> None:
    async def poll() -> None:
        while runtime.is_backlog_running():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def ledger_rows() -> list[tuple[str, str]]:
    with session_scope() as session:
        from app.database.models import TransferredMedia

        return [
            (row.source_chat_id, row.file_unique_id)
            for row in session.query(TransferredMedia).all()
        ]


# -- live routing ----------------------------------------------------------------------


def test_live_handler_routes_all_enabled_sources_and_ignores_the_rest() -> None:
    async def scenario() -> FakeUserbot:
        client = FakeUserbot()
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        paused, _ = await runtime.add_source(SRC_B, "B")
        await runtime.set_source_enabled(paused.id, False)

        await handle_incoming_media(client, msg(SRC_A, 1, "a1"))  # enabled source
        await handle_incoming_media(client, msg(SRC_B, 2, "b1"))  # source, but paused
        await handle_incoming_media(client, msg(SRC_C, 3, "c1"))  # not a source at all
        await handle_incoming_media(client, msg(DEST, 4, "d1"))  # the destination itself
        return client

    client = asyncio.run(scenario())
    assert [c["kind"] for c in client.send_calls] == ["photo"]
    assert ledger_rows() == [(str(SRC_A), "a1")]


def test_live_handler_needs_a_destination() -> None:
    async def scenario() -> FakeUserbot:
        client = FakeUserbot()
        await runtime.add_source(SRC_A, "A")
        await handle_incoming_media(client, msg(SRC_A, 1, "a1"))
        return client

    assert asyncio.run(scenario()).send_calls == []


def test_two_sources_with_the_same_media_archive_it_once() -> None:
    """Dedup is keyed by (destination, file_unique_id) - not by source."""

    async def scenario() -> FakeUserbot:
        client = FakeUserbot()
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        await runtime.add_source(SRC_B, "B")
        await handle_incoming_media(client, msg(SRC_A, 10, "same-file"))
        await handle_incoming_media(client, msg(SRC_B, 77, "same-file"))
        return client

    client = asyncio.run(scenario())
    assert len(client.send_calls) == 1
    assert client.download_calls == 1
    assert ledger_rows() == [(str(SRC_A), "same-file")]


# -- backlog over several sources ----------------------------------------------------------


def test_backlog_runs_sources_one_after_another_each_with_its_own_cursor() -> None:
    async def scenario() -> FakeUserbot:
        client = FakeUserbot(
            histories={
                SRC_A: [msg(SRC_A, i, f"a{i}") for i in (1, 2, 3)],
                SRC_B: [msg(SRC_B, i, f"b{i}") for i in (1, 2)],
            }
        )
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        await runtime.add_source(SRC_B, "B")
        await runtime.ensure_backlog_running(client, sync_history=True)
        await wait_backlog_done()
        return client

    client = asyncio.run(scenario())

    order = [c["path"] for c in client.send_calls]
    assert len(order) == 5
    scanned = [chat for chat, _offset in client.history_calls]
    assert scanned == [SRC_A, SRC_B]  # strictly sequential, in the order they were added

    a, b = read_progress(str(SRC_A), str(DEST)), read_progress(str(SRC_B), str(DEST))
    assert (a.status, a.processed, a.transferred, a.resume_before) == ("completed", 3, 3, 1)
    assert (b.status, b.processed, b.transferred, b.resume_before) == ("completed", 2, 2, 1)


def test_backlog_resumes_from_each_sources_saved_cursor() -> None:
    async def scenario() -> FakeUserbot:
        client = FakeUserbot(histories={SRC_A: [msg(SRC_A, i, f"a{i}") for i in range(1, 7)]})
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        with session_scope() as session:  # a previous run got as far as message 4
            repository = MirrorRepository(session)
            state = repository.get_or_create_sync_state(str(SRC_A), str(DEST))
            repository.update_sync_progress(state.id, processed_delta=3, resume_before_message_id=4)
        await runtime.ensure_backlog_running(client, sync_history=True)
        await wait_backlog_done()
        return client

    client = asyncio.run(scenario())
    assert client.history_calls == [(SRC_A, 4)]
    assert len(client.send_calls) == 3  # only messages 3, 2, 1 were left


def test_completed_sources_are_not_rescanned() -> None:
    async def scenario() -> FakeUserbot:
        client = FakeUserbot(histories={SRC_A: [msg(SRC_A, 1, "a1")]})
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        with session_scope() as session:
            repository = MirrorRepository(session)
            state = repository.get_or_create_sync_state(str(SRC_A), str(DEST))
            repository.mark_status(state.id, SyncStatus.COMPLETED)
        await runtime.ensure_backlog_running(client, sync_history=True)
        await wait_backlog_done()
        return client

    assert asyncio.run(scenario()).send_calls == []


def test_sources_with_history_sync_off_are_skipped_unless_forced() -> None:
    async def scenario() -> list[tuple[int, int]]:
        client = FakeUserbot(histories={SRC_A: [msg(SRC_A, 1, "a1")], SRC_B: [msg(SRC_B, 1, "b1")]})
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A", history_sync=False)
        await runtime.add_source(SRC_B, "B")
        await runtime.ensure_backlog_running(client, sync_history=True)
        await wait_backlog_done()
        auto = [chat for chat, _ in client.history_calls]

        # /resync <A> forces it even though its history flag is off and the global flag is off.
        await reset_backlog(client, [SRC_A])
        await wait_backlog_done()
        return auto, [chat for chat, _ in client.history_calls]

    auto, after_force = asyncio.run(scenario())
    assert auto == [SRC_B]
    assert after_force == [SRC_B, SRC_A]


def test_a_source_added_while_the_backlog_runs_is_picked_up() -> None:
    async def scenario() -> list[int]:
        client = FakeUserbot(
            chunk_delay=0.02,
            histories={
                SRC_A: [msg(SRC_A, i, f"a{i}") for i in range(1, 6)],
                SRC_B: [msg(SRC_B, 1, "b1")],
            },
        )
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        await runtime.ensure_backlog_running(client, sync_history=True)
        await asyncio.sleep(0.05)  # A is mid-scan
        await runtime.add_source(SRC_B, "B")
        await runtime.ensure_backlog_running(client, sync_history=True)
        await wait_backlog_done(10)
        return [chat for chat, _ in client.history_calls]

    assert asyncio.run(scenario()) == [SRC_A, SRC_B]


def test_removing_the_running_source_cancels_only_it() -> None:
    async def scenario():
        client = FakeUserbot(
            chunk_delay=0.05,
            histories={
                SRC_A: [msg(SRC_A, i, f"a{i}") for i in range(1, 30)],
                SRC_B: [msg(SRC_B, 1, "b1")],
            },
        )
        await runtime.set_dest(DEST, "Archive")
        a, _ = await runtime.add_source(SRC_A, "A")
        await runtime.add_source(SRC_B, "B")
        await runtime.ensure_backlog_running(client, sync_history=True)
        await asyncio.sleep(0.2)
        assert runtime.running_source_chat_id() == SRC_A
        await runtime.remove_source(a.id)
        await wait_backlog_done(10)
        return client

    client = asyncio.run(scenario())
    assert (SRC_B, 0) in client.history_calls  # the loop went on to the next source
    assert any(row[1] == "b1" for row in ledger_rows())


def test_unreadable_source_is_marked_failed_and_flagged_without_blocking_others() -> None:
    async def scenario():
        client = FakeUserbot(histories={SRC_B: [msg(SRC_B, 1, "b1")]}, unreadable={SRC_A})
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        await runtime.add_source(SRC_B, "B")
        await runtime.ensure_backlog_running(client, sync_history=True)
        await wait_backlog_done()

    asyncio.run(scenario())
    assert read_progress(str(SRC_A), str(DEST)).status == "failed"
    assert read_progress(str(SRC_B), str(DEST)).status == "completed"
    asyncio.run(runtime.load())
    assert runtime.get_source_by_chat(SRC_A).state == "error"


def test_skipping_a_file_during_backlog_advances_the_cursor_and_leaves_the_ledger_clean() -> None:
    async def scenario() -> None:
        client = FakeUserbot(
            chunks=40,
            chunk_delay=0.01,
            histories={SRC_A: [msg(SRC_A, i, f"a{i}") for i in (1, 2, 3)]},
        )
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC_A, "A")
        await runtime.ensure_backlog_running(client, sync_history=True)
        await asyncio.sleep(0.1)  # message 3 is downloading
        assert registry.request_skip() == 1
        await wait_backlog_done(10)

    asyncio.run(scenario())

    uniques = {unique for _chat, unique in ledger_rows()}
    assert uniques == {"a2", "a1"}  # 3 was skipped, 2 and 1 still went through
    progress = read_progress(str(SRC_A), str(DEST))
    assert progress.status == "completed" and progress.processed == 3
    with session_scope() as session:
        rows = [(r.source_message_id, r.reason) for r in session.query(SkippedMedia).all()]
    assert rows == [(3, "user_skip")]
