from __future__ import annotations

from app.database.base import init_db, session_scope
from app.database.models import SyncStatus
from app.database.repositories import MirrorRepository


def test_record_transfer_prevents_duplicates_for_same_destination() -> None:
    init_db()

    with session_scope() as session:
        repository = MirrorRepository(session)

        first = repository.record_transfer(
            file_unique_id="file-1",
            media_type="photo",
            source_chat_id="-100111",
            source_message_id=10,
            dest_chat_id="-100222",
            dest_message_id=99,
        )
        assert first is True
        assert repository.is_duplicate("-100222", "file-1") is True

    with session_scope() as session:
        repository = MirrorRepository(session)
        second = repository.record_transfer(
            file_unique_id="file-1",
            media_type="photo",
            source_chat_id="-100111",
            source_message_id=11,
            dest_chat_id="-100222",
            dest_message_id=100,
        )
        assert second is False


def test_record_transfer_allows_same_file_to_different_destinations() -> None:
    init_db()

    with session_scope() as session:
        repository = MirrorRepository(session)
        assert repository.record_transfer(
            file_unique_id="file-1",
            media_type="photo",
            source_chat_id="-100111",
            source_message_id=10,
            dest_chat_id="-100222",
            dest_message_id=99,
        )
        assert repository.record_transfer(
            file_unique_id="file-1",
            media_type="photo",
            source_chat_id="-100111",
            source_message_id=10,
            dest_chat_id="-100333",
            dest_message_id=1,
        )


def test_sync_state_lifecycle() -> None:
    init_db()

    with session_scope() as session:
        repository = MirrorRepository(session)
        state = repository.get_or_create_sync_state("-100111", "-100222")
        assert state.status == SyncStatus.IDLE
        same_state = repository.get_or_create_sync_state("-100111", "-100222")
        assert same_state.id == state.id

        updated = repository.update_sync_progress(
            state.id,
            processed_delta=5,
            transferred_delta=3,
            duplicate_delta=2,
            resume_before_message_id=42,
        )
        assert updated.processed_messages == 5
        assert updated.transferred_messages == 3
        assert updated.duplicate_messages == 2
        assert updated.resume_before_message_id == 42

        completed = repository.mark_status(state.id, SyncStatus.COMPLETED)
        assert completed.status == SyncStatus.COMPLETED

        reset = repository.reset_sync_state("-100111", "-100222")
        assert reset.status == SyncStatus.IDLE
        assert reset.resume_before_message_id is None


def test_sync_state_summary_includes_last_error_only_when_present() -> None:
    init_db()

    with session_scope() as session:
        repository = MirrorRepository(session)
        state = repository.get_or_create_sync_state("-100111", "-100222")
        assert "last_error" not in state.summary()

        failed = repository.mark_status(
            state.id, SyncStatus.FAILED, last_error="boom: PEER_ID_INVALID"
        )
        summary = failed.summary()
        assert "status=failed" in summary
        assert "last_error: boom: PEER_ID_INVALID" in summary


def test_mirror_config_is_a_singleton_updated_in_place() -> None:
    init_db()

    with session_scope() as session:
        repository = MirrorRepository(session)

        empty = repository.get_config()
        assert empty.source_chat_id is None
        assert empty.dest_chat_id is None

        with_source = repository.set_source("-100111", "Source Group")
        assert with_source.id == empty.id
        assert with_source.source_chat_id == "-100111"
        assert with_source.source_title == "Source Group"
        assert with_source.dest_chat_id is None

        with_dest = repository.set_dest("-100222", "Dest Channel")
        assert with_dest.id == empty.id
        assert with_dest.source_chat_id == "-100111"
        assert with_dest.dest_chat_id == "-100222"
        assert with_dest.dest_title == "Dest Channel"


# -- sources, skipped media, settings, stats (admin v2) -------------------------------------


def test_source_crud_and_uniqueness() -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        first, created = repository.add_source("-100111", "A")
        again, created_again = repository.add_source("-100111", "A renamed")
        second, _ = repository.add_source("-100222", "B", history_sync=False)

        assert (created, created_again) == (True, False)
        assert again.id == first.id and again.title == "A renamed"
        assert [s.chat_id for s in repository.list_sources()] == ["-100111", "-100222"]
        assert second.history_sync is False and second.enabled is True

        repository.set_source_enabled(first.id, False)
        repository.set_source_history_sync(first.id, False)
        assert repository.get_source_by_chat("-100111").enabled is False
        assert repository.get_source_by_chat("-100111").history_sync is False
        assert repository.remove_source(first.id) is True
        assert repository.remove_source(first.id) is False
        assert repository.get_source(999) is None


def test_skip_rows_are_deduplicated_listed_and_resolved() -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        first = repository.record_skip(
            source_chat_id="-1",
            source_message_id=5,
            media_type="video",
            reason="timeout",
            file_size=9,
        )
        again = repository.record_skip(
            source_chat_id="-1", source_message_id=5, media_type="video", reason="user_skip"
        )
        repository.record_skip(
            source_chat_id="-1", source_message_id=6, media_type="photo", reason="auto_skip"
        )

        assert again.id == first.id and again.reason == "user_skip" and again.file_size == 9
        assert repository.count_skipped() == 2
        assert [row.source_message_id for row in repository.list_skipped(limit=1)] == [
            6
        ]  # newest first
        assert [row.source_message_id for row in repository.list_skipped(offset=1, limit=5)] == [5]

        repository.resolve_skipped_for_message("-1", 5)
        assert repository.count_skipped() == 1
        repository.resolve_skipped(first.id)  # already resolved: harmless


def test_settings_roundtrip() -> None:
    with session_scope() as session:
        repository = MirrorRepository(session)
        assert repository.get_setting("large_file_mb") is None
        repository.set_setting("large_file_mb", "800")
        repository.set_setting("large_file_mb", "900")
        assert repository.get_setting("large_file_mb") == "900"


def test_ledger_statistics() -> None:
    from datetime import datetime, timedelta, timezone

    with session_scope() as session:
        repository = MirrorRepository(session)
        for index, source in enumerate(("-1", "-1", "-2")):
            repository.record_transfer(
                file_unique_id=f"f{index}",
                media_type="photo",
                source_chat_id=source,
                source_message_id=index,
                dest_chat_id="-9",
                dest_message_id=None,
            )
        assert repository.ledger_count() == 3
        assert repository.ledger_count("-9") == 3 and repository.ledger_count("-8") == 0
        day_ago = datetime.now(timezone.utc) - timedelta(days=1)
        assert repository.ledger_count_since(day_ago, "-9") == 3
        assert repository.last_transfer_at("-1") is not None
        assert repository.last_transfer_at("-404") is None
        assert repository.find_sync_state("-1", "-9") is None
