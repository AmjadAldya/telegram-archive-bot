from __future__ import annotations

from app.archive.service import list_recent_archive_jobs
from app.database.base import init_db


def test_list_recent_archive_jobs_returns_empty_list_for_new_database() -> None:
    init_db()
    jobs = asyncio_run(list_recent_archive_jobs(limit=5))
    assert jobs == []


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
