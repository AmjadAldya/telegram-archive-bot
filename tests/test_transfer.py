from __future__ import annotations

import asyncio

import pytest
from helpers import FakeChat, FakeClient, FakeFile, FakeMessage
from pyrogram.errors import ChatWriteForbidden, FloodWait

from app.database.base import session_scope
from app.database.models import SkippedMedia
from app.database.repositories import MirrorRepository
from app.mirror import control, limits, transfer
from app.mirror.jobs import errors, registry
from app.mirror.transfer import transfer_message

DEST = -100222


@pytest.fixture
def fast_guard(monkeypatch):
    """Tiny timeouts so hang/stall/skip scenarios finish in milliseconds."""
    transfer._GUARD_KWARGS.update({"poll": 0.01, "stall": 5.0, "skip_grace": 0.05})
    monkeypatch.setattr(transfer, "compute_timeout", lambda _size: 0.15)
    monkeypatch.setattr(transfer, "TRANSFER_MAX_RETRIES", 2)


def make_message(message_id: int = 1, unique: str = "f1", **media) -> FakeMessage:
    if not media:
        media = {"photo": FakeFile(file_unique_id=unique)}
    return FakeMessage(id=message_id, chat=FakeChat(id=-100111, title="Src"), **media)


def leftovers() -> list:
    return list(transfer.tmp_root().glob("job-*")) if transfer.tmp_root().exists() else []


def skipped_rows() -> list[tuple[str, int, str]]:
    with session_scope() as session:
        return [
            (row.source_chat_id, row.source_message_id, row.reason)
            for row in session.query(SkippedMedia).all()
        ]


def in_ledger(unique: str) -> bool:
    with session_scope() as session:
        return MirrorRepository(session).is_duplicate(str(DEST), unique)


def run(coro):
    return asyncio.run(coro)


# -- the happy path and dedup ------------------------------------------------------


def test_copies_new_media_records_ledger_and_cleans_temp() -> None:
    client = FakeClient()
    result = run(transfer_message(client, make_message(), DEST))

    assert result == "transferred"
    assert [call["kind"] for call in client.send_calls] == ["photo"]
    assert client.send_calls[0]["chat_id"] == DEST
    assert in_ledger("f1")
    assert leftovers() == []


def test_duplicate_media_is_sent_once() -> None:
    client = FakeClient()
    first = run(transfer_message(client, make_message(1), DEST))
    second = run(transfer_message(client, make_message(2), DEST))

    assert (first, second) == ("transferred", "duplicate")
    assert len(client.send_calls) == 1
    assert client.download_calls == 1


def test_force_retransfers_a_duplicate_without_a_second_ledger_row() -> None:
    client = FakeClient()
    run(transfer_message(client, make_message(1), DEST))
    forced = run(transfer_message(client, make_message(1), DEST, force=True))

    assert forced == "transferred"
    assert len(client.send_calls) == 2
    with session_scope() as session:
        assert MirrorRepository(session).ledger_count(str(DEST)) == 1


@pytest.mark.parametrize(
    ("attr", "kind"),
    [
        ("photo", "photo"),
        ("video", "video"),
        ("animation", "animation"),
        ("document", "document"),
        ("audio", "audio"),
        ("voice", "voice"),
        ("video_note", "video_note"),
    ],
)
def test_every_media_type_is_really_sent(attr: str, kind: str) -> None:
    """Regression: documents/audio/voice/video notes used to be downloaded, deleted and
    recorded as done without ever being sent."""
    client = FakeClient()
    message = make_message(1, **{attr: FakeFile(file_unique_id="u1")})

    assert run(transfer_message(client, message, DEST)) == "transferred"
    assert [call["kind"] for call in client.send_calls] == [kind]


def test_video_metadata_is_forwarded() -> None:
    client = FakeClient()
    video = FakeFile(file_unique_id="v1", duration=12, width=1280, height=720)
    run(transfer_message(client, make_message(1, video=video), DEST))

    call = client.send_calls[0]
    assert (call["duration"], call["width"], call["height"]) == (12, 1280, 720)
    assert call["supports_streaming"] is True


def test_non_media_and_restricted_are_skipped() -> None:
    client = FakeClient()
    assert run(transfer_message(client, make_message(1, photo=None), DEST)) == "skipped"
    # (FakeMessage with no media at all)
    empty = FakeMessage(id=2, chat=FakeChat(id=-100111))
    assert run(transfer_message(client, empty, DEST)) == "skipped"

    control.mark_restricted("banned")
    try:
        assert run(transfer_message(client, make_message(3, unique="x"), DEST)) == "skipped"
    finally:
        control.resume()
    assert client.send_calls == []


def test_media_types_filter_applies_unless_ignored(monkeypatch) -> None:
    monkeypatch.setattr("app.mirror.media.MEDIA_TYPES", ("photo",))
    client = FakeClient()
    video = make_message(1, video=FakeFile(file_unique_id="v1"))

    assert run(transfer_message(client, video, DEST)) == "skipped"
    assert run(transfer_message(client, video, DEST, ignore_type_filter=True)) == "transferred"


def test_zero_byte_download_is_reported_unavailable_and_not_recorded() -> None:
    client = FakeClient(empty_downloads=True)
    job = registry.create("link", -100111, "Src", 1)

    result = run(transfer_message(client, make_message(1), DEST, job=job))

    assert result == "skipped"
    assert job.error == "unavailable"
    assert client.send_calls == []
    assert not in_ledger("f1")
    assert leftovers() == []


def test_destination_write_error_is_reported_not_raised() -> None:
    class Forbidden(FakeClient):
        async def send_photo(self, chat_id, photo, progress=None, **kw):
            raise ChatWriteForbidden()

    job = registry.create("link", -100111, "Src", 1)
    result = run(transfer_message(Forbidden(), make_message(1), DEST, job=job))

    assert result == "failed"
    assert job.error == "dest_forbidden"
    assert not in_ledger("f1")
    assert leftovers() == []


# -- timeouts and retries ------------------------------------------------------------


def test_hung_download_is_retried_and_then_succeeds(fast_guard) -> None:
    client = FakeClient(hang_downloads=1)
    result = run(transfer_message(client, make_message(), DEST))

    assert result == "transferred"
    assert client.download_calls == 2
    assert any("timeout" in text for _stamp, text in errors.recent(5))
    assert leftovers() == []


def test_hung_upload_is_retried_and_then_succeeds(fast_guard) -> None:
    client = FakeClient(hang_uploads=1)

    assert run(transfer_message(client, make_message(), DEST)) == "transferred"
    assert client.download_calls == 1
    assert len(client.send_calls) == 1


def test_retries_exhausted_skips_and_moves_on(fast_guard) -> None:
    client = FakeClient(hang_downloads=99)
    result = run(transfer_message(client, make_message(7), DEST))

    assert result == "timeout_skip"
    assert client.download_calls == 3  # first try + 2 retries
    assert client.send_calls == []
    assert not in_ledger("f1")
    assert skipped_rows() == [("-100111", 7, "timeout")]
    assert leftovers() == []


def test_queue_is_not_blocked_after_a_timeout(fast_guard) -> None:
    async def scenario():
        client = FakeClient(hang_downloads=99)
        first = await transfer_message(client, make_message(1, unique="a"), DEST)
        client.hang_downloads = 0
        second = await transfer_message(client, make_message(2, unique="b"), DEST)
        return first, second

    assert run(scenario()) == ("timeout_skip", "transferred")


def test_stall_without_progress_is_detected(monkeypatch) -> None:
    transfer._GUARD_KWARGS.update({"poll": 0.01, "stall": 0.1, "skip_grace": 0.05})
    monkeypatch.setattr(transfer, "compute_timeout", lambda _size: 30.0)
    monkeypatch.setattr(transfer, "TRANSFER_MAX_RETRIES", 0)
    client = FakeClient(hang_downloads=5)

    assert run(transfer_message(client, make_message(), DEST)) == "timeout_skip"


def test_truncated_download_is_retried_then_skipped(fast_guard, monkeypatch) -> None:
    monkeypatch.setattr(transfer, "TRANSFER_MAX_RETRIES", 1)
    client = FakeClient(size=500)  # writes 500 B, but Telegram says the file is 2000 B
    message = make_message(1, video=FakeFile(file_unique_id="v1", file_size=2000))

    assert run(transfer_message(client, message, DEST)) == "timeout_skip"
    assert client.download_calls == 2
    assert client.send_calls == []


def test_flood_wait_is_honoured_and_reported_to_the_job(monkeypatch) -> None:
    monkeypatch.setattr("app.mirror.throttle.random.uniform", lambda *_a: 0.0)

    class Flaky(FakeClient):
        failed = False

        async def download_media(self, message, file_name=None, progress=None, **kw):
            if not self.failed:
                self.failed = True
                raise FloodWait(value=0)
            return await super().download_media(message, file_name, progress, **kw)

    waits: list[float] = []
    job = registry.create("link", -100111, "Src", 1)
    job.on_flood = waits.append

    assert run(transfer_message(Flaky(), make_message(1), DEST, job=job)) == "transferred"
    assert waits == [0.0]
    assert job.flood_wait_total == 0.0


# -- skip, size limits ------------------------------------------------------------------


def test_user_skip_during_download_cleans_up_and_leaves_ledger_alone(fast_guard) -> None:
    async def scenario():
        client = FakeClient(chunks=40, chunk_delay=0.01)
        job = registry.create("backlog", -100111, "Src", 9)
        task = asyncio.ensure_future(transfer_message(client, make_message(9), DEST, job=job))
        await asyncio.sleep(0.05)  # let the download start
        assert registry.request_skip() == 1
        return client, await task

    client, result = run(scenario())

    assert result == "user_skip"
    assert client.send_calls == []
    assert not in_ledger("f1")
    assert skipped_rows() == [("-100111", 9, "user_skip")]
    assert leftovers() == []


def test_skip_requested_while_queued_skips_without_downloading() -> None:
    client = FakeClient()
    job = registry.create("live", -100111, "Src", 3)
    job.skip_requested = True

    assert run(transfer_message(client, make_message(3), DEST, job=job)) == "user_skip"
    assert client.download_calls == 0
    assert skipped_rows() == [("-100111", 3, "user_skip")]


def test_skipped_file_can_be_transferred_later_and_the_skip_is_resolved() -> None:
    client = FakeClient()
    job = registry.create("live", -100111, "Src", 3)
    job.skip_requested = True
    run(transfer_message(client, make_message(3), DEST, job=job))

    assert run(transfer_message(client, make_message(3), DEST)) == "transferred"
    with session_scope() as session:
        assert MirrorRepository(session).count_skipped() == 0


def test_auto_skip_above_limit_does_not_download() -> None:
    async def scenario():
        await limits.set_auto_skip_mb(1)
        client = FakeClient()
        big = make_message(1, video=FakeFile(file_unique_id="big", file_size=5 * limits.MIB))
        return client, await transfer_message(client, big, DEST)

    client, result = run(scenario())

    assert result == "auto_skip"
    assert client.download_calls == 0
    assert skipped_rows() == [("-100111", 1, "auto_skip")]


def test_large_file_notice_fires_without_stopping_the_transfer() -> None:
    noticed: list[int] = []

    async def notifier(job) -> None:
        noticed.append(job.message_id)

    async def scenario():
        registry.on_large_file = notifier
        await limits.set_large_file_mb(1)
        small = make_message(1, unique="s", video=FakeFile(file_unique_id="s", file_size=1000))
        big = make_message(
            2, unique="b", video=FakeFile(file_unique_id="b", file_size=2 * limits.MIB)
        )
        first = await transfer_message(FakeClient(size=1000), small, DEST)
        second = await transfer_message(FakeClient(size=2 * limits.MIB), big, DEST)
        await asyncio.sleep(0)  # let the fire-and-forget notice run
        return first, second

    assert run(scenario()) == ("transferred", "transferred")
    assert noticed == [2]


def test_large_file_notice_is_off_when_limit_is_zero() -> None:
    noticed: list[int] = []

    async def notifier(job) -> None:
        noticed.append(job.message_id)

    async def scenario():
        registry.on_large_file = notifier
        await limits.set_large_file_mb(0)
        big = make_message(2, video=FakeFile(file_unique_id="b", file_size=2 * limits.MIB))
        assert await transfer_message(FakeClient(size=2 * limits.MIB), big, DEST) == "transferred"
        await asyncio.sleep(0)

    run(scenario())
    assert noticed == []


def test_progress_is_visible_on_the_job_while_running() -> None:
    seen: list[float | None] = []

    class Spy(FakeClient):
        async def _simulate(self, progress, total):
            ok = await super()._simulate(progress, total)
            job = registry.snapshot().active[0]
            seen.append(job.percent)
            return ok

    run(
        transfer_message(Spy(size=1000), make_message(1, video=FakeFile("v", file_size=1000)), DEST)
    )
    assert seen and seen[0] == 100.0


def test_relative_data_dir_still_cleans_up_where_pyrogram_actually_writes(
    tmp_path, monkeypatch
) -> None:
    """Regression: with the production default DATA_DIR=data, Pyrogram resolves a relative
    download directory against its PARENT_DIR (not the cwd), so the temp file used to land
    somewhere the cleanup never looked."""
    parent = tmp_path / "pyrogram_parent_dir"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(transfer, "DATA_DIR", "data")  # the live, relative setting

    assert transfer.tmp_root().is_absolute()
    assert transfer.tmp_root() == workdir.resolve() / "data" / "tmp"

    client = FakeClient(parent_dir=parent)
    assert run(transfer_message(client, make_message(), DEST)) == "transferred"

    assert not parent.exists() or list(parent.rglob("*")) == []  # nothing leaked into PARENT_DIR
    assert (
        not (workdir / "data" / "tmp").exists() or list((workdir / "data" / "tmp").iterdir()) == []
    )
    assert client.downloaded_paths and not client.downloaded_paths[0].exists()


def test_the_returned_download_path_is_deleted_even_if_it_is_outside_the_temp_dir(tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere.bin"

    class OddClient(FakeClient):
        async def download_media(self, message, file_name=None, progress=None, **kw):
            elsewhere.write_bytes(b"x" * 10)
            return str(elsewhere)

    assert run(transfer_message(OddClient(), make_message(), DEST)) == "transferred"
    assert not elsewhere.exists()


def test_file_name_is_shown_on_the_job_but_never_logged(caplog) -> None:
    import logging

    secret_name = "private-holiday-photos-2026.zip"
    seen: list[str | None] = []

    class Spy(FakeClient):
        async def _simulate(self, progress, total):
            seen.append(registry.snapshot().active[0].file_name)
            return await super()._simulate(progress, total)

    message = make_message(1, document=FakeFile("d1", file_name=secret_name))
    with caplog.at_level(logging.DEBUG):
        assert run(transfer_message(Spy(), message, DEST)) == "transferred"

    assert seen and set(seen) == {secret_name}  # visible to /live while it runs
    assert secret_name not in caplog.text  # ...but never written to the logs
    with session_scope() as session:  # ...and never stored in the database
        dump = " ".join(
            str(value)
            for model in (SkippedMedia,)
            for row in session.query(model).all()
            for value in vars(row).values()
        )
    assert secret_name not in dump
