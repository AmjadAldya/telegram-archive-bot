from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import MEDIA_TYPES

_MEDIA_ATTRS = ("photo", "video", "document", "audio", "voice", "video_note", "animation")


@dataclass(frozen=True, slots=True)
class MediaInfo:
    media_type: str
    file_unique_id: str
    file_size: int | None = None
    file_name: str | None = None  # display only: shown in /live, never logged or stored


def extract_media_info(message) -> MediaInfo | None:
    """Return the media type and stable file identifier for a message, if any.

    Only real media attachments are considered; text, polls, contacts, locations,
    stickers, and web page previews are intentionally excluded.
    """
    for attr in _MEDIA_ATTRS:
        media_object = getattr(message, attr, None)
        if media_object is None:
            continue
        file_unique_id = getattr(media_object, "file_unique_id", None)
        if not file_unique_id:
            continue
        file_size = getattr(media_object, "file_size", None)
        file_name = getattr(media_object, "file_name", None)
        return MediaInfo(
            media_type=attr,
            file_unique_id=file_unique_id,
            file_size=file_size if isinstance(file_size, int) else None,
            file_name=file_name if isinstance(file_name, str) and file_name else None,
        )
    return None


def is_transferable_media(message) -> MediaInfo | None:
    info = extract_media_info(message)
    if info is None:
        return None
    if info.media_type not in MEDIA_TYPES:
        return None
    return info
