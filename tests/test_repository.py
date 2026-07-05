from __future__ import annotations

from app.database.base import init_db, session_scope
from app.database.models import ArchiveJobStatus
from app.database.repositories import ArchiveJobRepository


def test_archive_job_repository_tracks_lifecycle() -> None:
    init_db()

    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        job = repository.create_job(requested_by=123456789, source_chat_id="me")
        pending_ids = [pending_job.id for pending_job in repository.list_pending_jobs()]

        assert job.status == ArchiveJobStatus.QUEUED
        assert job.id in pending_ids

        running_job = repository.mark_running(job.id)
        assert running_job.status == ArchiveJobStatus.RUNNING

        progress_job = repository.update_progress(
            job.id,
            processed_messages=12,
            media_messages=4,
            resume_after_message_id=88,
        )
        assert progress_job.processed_messages == 12
        assert progress_job.media_messages == 4
        assert progress_job.resume_after_message_id == 88

        completed_job = repository.mark_completed(job.id)
        assert completed_job.status == ArchiveJobStatus.COMPLETED
        assert completed_job.completed_at is not None

        failed_job = repository.mark_failed(job.id, "boom")
        assert failed_job.status == ArchiveJobStatus.FAILED
        assert failed_job.failed_at is not None

        requeued_job = repository.retry_job(job.id)
        assert requeued_job.status == ArchiveJobStatus.QUEUED
        assert requeued_job.retry_count == 1

        queued_cancel_job = repository.create_job(requested_by=123456789, source_chat_id="me")
        cancelled_job = repository.request_cancel(queued_cancel_job.id)
        assert cancelled_job.status == ArchiveJobStatus.CANCELLED

        running_cancel_job = repository.create_job(requested_by=123456789, source_chat_id="me")
        repository.mark_running(running_cancel_job.id)
        cancel_requested_job = repository.request_cancel(running_cancel_job.id)
        assert cancel_requested_job.status == ArchiveJobStatus.CANCEL_REQUESTED
