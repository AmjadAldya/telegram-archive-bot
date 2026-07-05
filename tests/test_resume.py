from __future__ import annotations

from app.archive.resume import (
    ArchiveProgress,
    advance_progress,
    normalize_chat_reference,
    should_skip_message,
)


def test_normalize_chat_reference_handles_strings_and_numbers() -> None:
    assert normalize_chat_reference("me") == "me"
    assert normalize_chat_reference("  @archive-chat  ") == "@archive-chat"
    assert normalize_chat_reference("12345") == 12345
    assert normalize_chat_reference(-99) == -99


def test_progress_helpers_track_resume_state() -> None:
    progress = ArchiveProgress()
    progress = advance_progress(progress, message_id=101, has_media=False)
    progress = advance_progress(progress, message_id=100, has_media=True)

    assert progress.processed_messages == 2
    assert progress.media_messages == 1
    assert progress.resume_after_message_id == 100
    assert should_skip_message(101, progress.resume_after_message_id) is True
    assert should_skip_message(99, progress.resume_after_message_id) is False
