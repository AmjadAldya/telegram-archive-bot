from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import DATA_DIR
from app.database.models import ArchiveJob, ArchiveJobStatus


class ArchiveJobRepository:
    def __init__(self, session: Session):
        self.session = session

    def create_job(self, requested_by: int, source_chat_id: str) -> ArchiveJob:
        job = ArchiveJob(
            requested_by=requested_by,
            source_chat_id=source_chat_id,
            archive_path="",
            status=ArchiveJobStatus.QUEUED,
        )
        self.session.add(job)
        self.session.flush()
        job.archive_path = str(DATA_DIR / "archive" / job.id)
        self.session.flush()
        self.session.refresh(job)
        return job

    def get_job(self, job_id: str) -> ArchiveJob | None:
        return self.session.get(ArchiveJob, job_id)

    def get_job_or_raise(self, job_id: str) -> ArchiveJob:
        job = self.get_job(job_id)
        if job is None:
            raise LookupError(f"Archive job {job_id} was not found")
        return job

    def list_recent_jobs(self, limit: int = 10) -> list[ArchiveJob]:
        statement = select(ArchiveJob).order_by(ArchiveJob.updated_at.desc()).limit(limit)
        return list(self.session.scalars(statement))

    def list_pending_jobs(self) -> list[ArchiveJob]:
        statement = select(ArchiveJob).where(
            ArchiveJob.status.in_([ArchiveJobStatus.QUEUED, ArchiveJobStatus.RUNNING])
        )
        return list(self.session.scalars(statement))

    def mark_running(self, job_id: str) -> ArchiveJob:
        job = self.get_job_or_raise(job_id)
        job.status = ArchiveJobStatus.RUNNING
        job.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(job)
        return job

    def update_progress(
        self,
        job_id: str,
        *,
        processed_messages: int,
        media_messages: int,
        resume_after_message_id: int | None,
    ) -> ArchiveJob:
        job = self.get_job_or_raise(job_id)
        job.processed_messages = processed_messages
        job.media_messages = media_messages
        job.resume_after_message_id = resume_after_message_id
        job.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(job)
        return job

    def mark_completed(self, job_id: str) -> ArchiveJob:
        job = self.get_job_or_raise(job_id)
        job.status = ArchiveJobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = job.completed_at
        self.session.flush()
        self.session.refresh(job)
        return job

    def mark_failed(self, job_id: str, error_message: str) -> ArchiveJob:
        job = self.get_job_or_raise(job_id)
        job.status = ArchiveJobStatus.FAILED
        job.last_error = error_message
        job.failed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(job)
        return job

    def request_cancel(self, job_id: str) -> ArchiveJob:
        job = self.get_job_or_raise(job_id)
        now = datetime.now(timezone.utc)

        if job.status == ArchiveJobStatus.QUEUED:
            job.status = ArchiveJobStatus.CANCELLED
            job.cancelled_at = now
        elif job.status == ArchiveJobStatus.RUNNING:
            job.status = ArchiveJobStatus.CANCEL_REQUESTED
            job.cancel_requested_at = now
        elif job.status in {
            ArchiveJobStatus.COMPLETED,
            ArchiveJobStatus.FAILED,
            ArchiveJobStatus.CANCELLED,
        }:
            raise RuntimeError(f"Archive job {job_id} is already {job.status.value}")

        job.updated_at = now
        self.session.flush()
        self.session.refresh(job)
        return job

    def mark_cancelled(self, job_id: str) -> ArchiveJob:
        job = self.get_job_or_raise(job_id)
        job.status = ArchiveJobStatus.CANCELLED
        job.cancelled_at = datetime.now(timezone.utc)
        job.updated_at = job.cancelled_at
        self.session.flush()
        self.session.refresh(job)
        return job

    def retry_job(self, job_id: str) -> ArchiveJob:
        job = self.get_job_or_raise(job_id)

        if job.status != ArchiveJobStatus.FAILED:
            raise RuntimeError(f"Archive job {job_id} is not failed")
        if job.retry_count >= job.max_retries:
            raise RuntimeError(f"Archive job {job_id} reached max retries")

        job.retry_count += 1
        job.status = ArchiveJobStatus.QUEUED
        job.last_error = None
        job.failed_at = None
        job.completed_at = None
        job.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(job)
        return job
