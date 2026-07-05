from __future__ import annotations

import asyncio

from app.services.queue import ArchiveTask, add_task, get_task, pending_tasks, task_done


def test_queue_round_trip() -> None:
    async def round_trip() -> None:
        before = pending_tasks()
        await add_task(ArchiveTask(job_id="job-1"))
        task = await get_task()
        task_done()

        assert task.job_id == "job-1"
        assert pending_tasks() == before

    asyncio.run(round_trip())
