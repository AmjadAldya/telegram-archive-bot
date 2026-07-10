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
