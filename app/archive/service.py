from __future__ import annotations

import asyncio

from app.archive.resume import normalize_chat_reference
from app.config.settings import ARCHIVE_CHAT_ID
from app.database.base import session_scope
from app.database.models import ArchiveJob
from app.database.repositories import ArchiveJobRepository
from app.services.queue import ArchiveTask, add_task


def _create_archive_job(requested_by: int, source_chat_id: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.create_job(requested_by=requested_by, source_chat_id=source_chat_id)


def _list_recent_archive_jobs(limit: int) -> list[ArchiveJob]:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.list_recent_jobs(limit=limit)


def _request_cancel_archive_job(job_id: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.request_cancel(job_id)


def _retry_archive_job(job_id: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.retry_job(job_id)


async def submit_archive_job(
    requested_by: int,
    source_chat_id: str | int | None = None,
) -> ArchiveJob:
    chat_reference = normalize_chat_reference(source_chat_id or ARCHIVE_CHAT_ID)
    job = await asyncio.to_thread(_create_archive_job, requested_by, str(chat_reference))
    await add_task(ArchiveTask(job_id=job.id))
    return job


async def list_recent_archive_jobs(limit: int = 10) -> list[ArchiveJob]:
    return await asyncio.to_thread(_list_recent_archive_jobs, limit)


async def cancel_archive_job(job_id: str) -> ArchiveJob:
    return await asyncio.to_thread(_request_cancel_archive_job, job_id)


async def retry_archive_job(job_id: str) -> ArchiveJob:
    job = await asyncio.to_thread(_retry_archive_job, job_id)
    await add_task(ArchiveTask(job_id=job.id))
    return job
