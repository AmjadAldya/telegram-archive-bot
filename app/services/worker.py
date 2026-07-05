from __future__ import annotations

import asyncio
from typing import Any

from app.archive.engine import start_archive
from app.database.base import session_scope
from app.database.repositories import ArchiveJobRepository
from app.services.logger import logger
from app.services.queue import ArchiveTask, add_task, get_task, task_done


def _get_pending_job_ids() -> list[str]:
    with session_scope() as session:
        repository = ArchiveJobRepository(session)
        return [job.id for job in repository.list_pending_jobs()]


async def seed_pending_jobs() -> None:
    pending_job_ids = await asyncio.to_thread(_get_pending_job_ids)
    for job_id in pending_job_ids:
        await add_task(ArchiveTask(job_id=job_id))


async def process_next_archive_task(client: Any) -> bool:
    task = await get_task()
    try:
        await start_archive(client, task.job_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Archive worker failed for job %s", task.job_id)
    finally:
        task_done()

    return True


async def archive_worker(client: Any) -> None:
    await seed_pending_jobs()
    logger.info("Archive worker started")

    while True:
        await process_next_archive_task(client)
