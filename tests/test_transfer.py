from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.database.base import init_db, session_scope
from app.database.repositories import MirrorRepository
from app.mirror import control
from app.mirror.transfer import transfer_message


@dataclass(slots=True)
class FakeFile:
    file_unique_id: str


@dataclass(slots=True)
class FakeChat:
    id: int


@dataclass(slots=True)
class FakeMessage:
    id: int
    chat: FakeChat
    photo: FakeFile | None = None
    video: FakeFile | None = None


@dataclass(slots=True)
class FakeCopiedMessage:
    id: int


class FakeClient:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []
        self.download_calls: list[dict] = []
        self._next_id = 1000

    async def download_media(self, message):
        self.download_calls.append({"message": message})
        # إرجاع مسار وهمي لتجاوز شرط if downloaded_file بنجاح في الاختبار
        return "fake_downloaded_file.jpg"

    async def send_photo(self, chat_id, photo):
        self.send_calls.append({"chat_id": chat_id, "photo": photo})
        self._next_id += 1
        return FakeCopiedMessage(id=self._next_id)

    async def send_video(self, chat_id, video):
        self.send_calls.append({"chat_id": chat_id, "video": video})
        self._next_id += 1
        return FakeCopiedMessage(id=self._next_id)


def test_transfer_message_copies_new_media() -> None:
    async def scenario() -> None:
        init_db()
        client = FakeClient()
        message = FakeMessage(id=1, chat=FakeChat(id=-100111), photo=FakeFile(file_unique_id="f1"))

        result = await transfer_message(client, message, dest_chat_id=-100222)

        assert result == "transferred"
        assert len(client.send_calls) == 1
        with session_scope() as session:
            repository = MirrorRepository(session)
            assert repository.is_duplicate("-100222", "f1") is True

    asyncio.run(scenario())


def test_transfer_message_skips_duplicate_media() -> None:
    async def scenario() -> None:
        init_db()
        client = FakeClient()
        message = FakeMessage(id=1, chat=FakeChat(id=-100111), photo=FakeFile(file_unique_id="f1"))

        first = await transfer_message(client, message, dest_chat_id=-100222)
        second_message = FakeMessage(
            id=2, chat=FakeChat(id=-100111), photo=FakeFile(file_unique_id="f1")
        )
        second = await transfer_message(client, second_message, dest_chat_id=-100222)

        assert first == "transferred"
        assert second == "duplicate"
        assert len(client.send_calls) == 1

    asyncio.run(scenario())


def test_transfer_message_skips_non_media_messages() -> None:
    async def scenario() -> None:
        init_db()
        client = FakeClient()
        message = FakeMessage(id=1, chat=FakeChat(id=-100111), photo=None)

        result = await transfer_message(client, message, dest_chat_id=-100222)

        assert result == "skipped"
        assert client.send_calls == []

    asyncio.run(scenario())


def test_transfer_message_skips_when_account_restricted() -> None:
    async def scenario() -> None:
        init_db()
        client = FakeClient()
        message = FakeMessage(id=1, chat=FakeChat(id=-100111), photo=FakeFile(file_unique_id="f1"))

        control.mark_restricted("banned")
        try:
            result = await transfer_message(client, message, dest_chat_id=-100222)
        finally:
            control.resume()

        assert result == "skipped"
        assert client.send_calls == []

    asyncio.run(scenario())
