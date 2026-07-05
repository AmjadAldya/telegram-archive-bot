from __future__ import annotations

import asyncio
from pathlib import Path

from app.archive.downloader import download_media
from app.archive.resume import (
    ArchiveProgress,
    advance_progress,
    normalize_chat_reference,
    should_skip_message,
)
from app.database.base import session_scope
from app.database.models import ArchiveJob, ArchiveJobStatus
from app.database.repositories import ArchiveJobRepository
from app.services.logger import logger


def _load_job(job_id: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.get_job_or_raise(job_id)


def _mark_running(job_id: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.mark_running(job_id)


def _update_progress(job_id: str, progress: ArchiveProgress) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.update_progress(
            job_id,
            processed_messages=progress.processed_messages,
            media_messages=progress.media_messages,
            resume_after_message_id=progress.resume_after_message_id,
        )


def _mark_completed(job_id: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.mark_completed(job_id)


def _mark_failed(job_id: str, error_message: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.mark_failed(job_id, error_message)


def _mark_cancelled(job_id: str) -> ArchiveJob:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return repository.mark_cancelled(job_id)


async def start_archive(client, job_id: str) -> ArchiveJob:
    job = await asyncio.to_thread(_load_job, job_id)
    if job.status == ArchiveJobStatus.CANCEL_REQUESTED:
        cancelled_job = await asyncio.to_thread(_mark_cancelled, job.id)
        logger.info("Archive job %s cancelled before start", job.id)
        return cancelled_job
    if job.status in {ArchiveJobStatus.CANCELLED, ArchiveJobStatus.COMPLETED, ArchiveJobStatus.FAILED}:
        logger.info("Archive job %s is already %s", job.id, job.status.value)
        return job
    if job.status not in {ArchiveJobStatus.QUEUED, ArchiveJobStatus.RUNNING}:
        logger.info("Archive job %s is already %s", job.id, job.status.value)
        return job

    archive_target = normalize_chat_reference(job.source_chat_id)
    logger.info("Archive job %s started for %s", job.id, archive_target)
    await asyncio.to_thread(_mark_running, job.id)

    archive_dir = Path(job.archive_path)
    archive_dir.mkdir(parents=True, exist_ok=True)

    progress = ArchiveProgress(
        processed_messages=job.processed_messages,
        media_messages=job.media_messages,
        resume_after_message_id=job.resume_after_message_id,
    )

    try:
        async for message in client.get_chat_history(archive_target):
            current_job = await asyncio.to_thread(_load_job, job.id)
            if current_job.status == ArchiveJobStatus.CANCEL_REQUESTED:
                cancelled_job = await asyncio.to_thread(_mark_cancelled, job.id)
                logger.info("Archive job %s cancelled during processing", job.id)
                return cancelled_job
            if current_job.status == ArchiveJobStatus.CANCELLED:
                logger.info("Archive job %s already cancelled", job.id)
                return current_job

            if should_skip_message(message.id, progress.resume_after_message_id):
                continue

            if message.media:
                await download_media(client, message, archive_dir)

            progress = advance_progress(
                progress,
                message_id=message.id,
                has_media=bool(message.media),
            )
            await asyncio.to_thread(_update_progress, job.id, progress)

        completed_job = await asyncio.to_thread(_mark_completed, job.id)
        logger.info("Archive job %s completed: %s", job.id, completed_job.summary())
        return completed_job
    except Exception as exc:
        await asyncio.to_thread(_mark_failed, job.id, str(exc))
        logger.exception("Archive job %s failed", job.id)
        raise
