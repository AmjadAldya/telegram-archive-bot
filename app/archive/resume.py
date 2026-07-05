from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArchiveProgress:
    processed_messages: int = 0
    media_messages: int = 0
    resume_after_message_id: int | None = None


def normalize_chat_reference(chat_reference: str | int) -> str | int:
    if isinstance(chat_reference, int):
        return chat_reference

    value = chat_reference.strip()
    if not value:
        return "me"

    if value.lstrip("-").isdigit():
        return int(value)

    return value


def should_skip_message(message_id: int, resume_after_message_id: int | None) -> bool:
    return resume_after_message_id is not None and message_id >= resume_after_message_id


def advance_progress(
    progress: ArchiveProgress,
    *,
    message_id: int,
    has_media: bool,
) -> ArchiveProgress:
    return ArchiveProgress(
        processed_messages=progress.processed_messages + 1,
        media_messages=progress.media_messages + (1 if has_media else 0),
        resume_after_message_id=message_id,
    )
