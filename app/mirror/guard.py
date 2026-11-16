"""Per-file watchdog around a download or an upload.

`run_guarded` is `asyncio.wait_for` plus the three things a plain timeout
cannot do for large media:

* the overall limit scales with the file size (see `compute_timeout`), so a
  big file on a slow link is not killed by a fixed cap;
* a *stall* limit fires when no bytes have moved for a while;
* a user "skip" is honoured: the progress callback asks Pyrogram to stop
  cleanly, and if the transfer has not stopped after a short grace period
  the task is cancelled.

Time spent sleeping on a Telegram FloodWait is subtracted from both limits -
waiting is not a stall. Progress is logged periodically (ids and byte counts
only, never message content).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from typing import TypeVar

from app.config.settings import (
    PROGRESS_LOG_SECONDS,
    TRANSFER_MIN_SPEED_KBPS,
    TRANSFER_STALL_SECONDS,
    TRANSFER_TIMEOUT_BASE_SECONDS,
)
from app.mirror.jobs import Job
from app.services.logger import logger

T = TypeVar("T")


class TransferTimeout(Exception):
    """The transfer took too long (`kind="timeout"`) or stopped moving (`kind="stall"`)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class SkipRequested(Exception):
    """The owner asked to skip this file and it had to be force-cancelled."""


def compute_timeout(
    file_size: int | None,
    *,
    base_seconds: float = TRANSFER_TIMEOUT_BASE_SECONDS,
    min_speed_kbps: float = TRANSFER_MIN_SPEED_KBPS,
) -> float:
    """Overall limit for one stage: a fixed base plus the time at a pessimistic speed."""
    if not file_size or file_size <= 0:
        return base_seconds
    return base_seconds + file_size / (min_speed_kbps * 1024.0)


async def run_guarded(
    operation: Awaitable[T],
    job: Job,
    stage_label: str,
    *,
    timeout: float,
    stall: float = TRANSFER_STALL_SECONDS,
    log_interval: float = PROGRESS_LOG_SECONDS,
    poll: float = 1.0,
    skip_grace: float = 5.0,
) -> T:
    """Await `operation`, enforcing timeout/stall/skip. See the module docstring."""
    task = asyncio.ensure_future(operation)
    started = time.monotonic()
    last_log = started
    skip_seen_at: float | None = None
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll)
            if done:
                return task.result()

            now = time.monotonic()
            flood = job.flood_wait_total

            if job.skip_requested:
                skip_seen_at = skip_seen_at if skip_seen_at is not None else now
                if now - skip_seen_at >= skip_grace:
                    raise SkipRequested()

            if now - started - flood >= timeout:
                raise TransferTimeout("timeout")
            if now - job.last_progress_at - flood >= stall:
                raise TransferTimeout("stall")

            if now - last_log >= log_interval:
                last_log = now
                logger.info(
                    "%s in progress: message %s, %s of %s bytes",
                    stage_label,
                    job.message_id,
                    job.bytes_done,
                    job.file_size if job.file_size else "?",
                )
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
