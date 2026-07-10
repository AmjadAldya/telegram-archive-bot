from __future__ import annotations

from dataclasses import dataclass

from app.mirror.media import extract_media_info, is_transferable_media


@dataclass(slots=True)
class FakeFile:
    file_unique_id: str


@dataclass(slots=True)
class FakeMessage:
    photo: FakeFile | None = None
    video: FakeFile | None = None
    document: FakeFile | None = None
    audio: FakeFile | None = None
    voice: FakeFile | None = None
    video_note: FakeFile | None = None
    animation: FakeFile | None = None


def test_extract_media_info_returns_none_for_text_message() -> None:
    assert extract_media_info(FakeMessage()) is None


def test_extract_media_info_detects_photo() -> None:
    info = extract_media_info(FakeMessage(photo=FakeFile(file_unique_id="abc")))
    assert info is not None
    assert info.media_type == "photo"
    assert info.file_unique_id == "abc"


def test_extract_media_info_detects_document_over_none_fields() -> None:
    info = extract_media_info(FakeMessage(document=FakeFile(file_unique_id="doc-1")))
    assert info is not None
    assert info.media_type == "document"


def test_is_transferable_media_respects_configured_media_types(monkeypatch) -> None:
    monkeypatch.setattr("app.mirror.media.MEDIA_TYPES", ("photo",))

    assert is_transferable_media(FakeMessage(photo=FakeFile(file_unique_id="abc"))) is not None
    assert is_transferable_media(FakeMessage(video=FakeFile(file_unique_id="def"))) is None
