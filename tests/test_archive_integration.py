from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.archive.service import submit_archive_job
from app.database.base import init_db, session_scope
from app.database.repositories import ArchiveJobRepository
from app.services.queue import pending_tasks
from app.services.worker import process_next_archive_task


@dataclass(slots=True)
class FakeMessage:
    id: int
    media: object | None


class FakeClient:
    def __init__(self, messages: list[FakeMessage]):
        self.messages = messages

    async def get_chat_history(self, chat_reference):
        _ = chat_reference
        for message in self.messages:
            yield message

    async def download_media(self, message, file_name):
        archive_dir = Path(file_name)
        archive_dir.mkdir(parents=True, exist_ok=True)
        downloaded_file = archive_dir / f"message-{message.id}.bin"
        downloaded_file.write_text(f"payload-{message.id}")
        return str(downloaded_file)


def test_archive_job_flow_processes_queue_and_persists_final_state() -> None:
    async def scenario() -> None:
        init_db()
        job = await submit_archive_job(requested_by=123456789, source_chat_id="me")
        assert pending_tasks() == 1

        client = FakeClient(
            [
                FakeMessage(id=3, media=object()),
                FakeMessage(id=2, media=None),
                FakeMessage(id=1, media=object()),
            ]
        )

        await process_next_archive_task(client)

        with session_scope() as session:
            repository = ArchiveJobRepository(session)
            persisted_job = repository.get_job_or_raise(job.id)

        assert persisted_job.status.value == "completed"
        assert persisted_job.processed_messages == 3
        assert persisted_job.media_messages == 2
        assert persisted_job.retry_count == 0
        assert pending_tasks() == 0
        archive_dir = Path(persisted_job.archive_path)
        assert archive_dir.exists()
        assert sorted(path.name for path in archive_dir.glob("*.bin")) == [
            "message-1.bin",
            "message-3.bin",
        ]

    import asyncio

    asyncio.run(scenario())
