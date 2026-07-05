from __future__ import annotations

from dataclasses import dataclass
import asyncio


@dataclass(frozen=True, slots=True)
class ArchiveTask:
    job_id: str


queue: asyncio.Queue[ArchiveTask] = asyncio.Queue()


async def add_task(task: ArchiveTask) -> None:
    await queue.put(task)


async def get_task() -> ArchiveTask:
    return await queue.get()


def task_done() -> None:
    queue.task_done()


def pending_tasks() -> int:
    return queue.qsize()
