"""In-memory registry of transfer jobs (live, backlog and link transfers).

Everything here is process-local and cheap: the registry only holds the
handful of jobs that are queued or running right now, and it never stores
message content - only ids, media type, size and progress numbers.
"""

from __future__ import annotations

import enum
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

_SPEED_WINDOW_SECONDS = 8.0
_MAX_SAMPLES = 64


class Stage(str, enum.Enum):
    QUEUED = "queued"
    DOWNLOAD = "download"
    UPLOAD = "upload"


@dataclass(slots=True)
class Job:
    id: int
    kind: str  # "live" | "backlog" | "link" | "retry"
    source_chat_id: str
    source_title: str
    message_id: int
    media_type: str | None = None
    file_size: int | None = None
    file_name: str | None = None  # in memory only: shown in /live, never logged or stored
    stage: Stage = Stage.QUEUED
    bytes_done: int = 0
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    stage_started_at: float | None = None
    last_progress_at: float = field(default_factory=time.monotonic)
    skip_requested: bool = False
    note: str = ""
    # filled in by the transfer pipeline, read by callers (e.g. the link flow)
    error: str | None = None
    detail: str | None = None
    flood_wait_total: float = 0.0
    large_notified: bool = False
    on_flood: Callable[[float], None] | None = field(default=None, repr=False)
    _samples: deque = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES), repr=False)

    # -- progress ---------------------------------------------------------

    def begin_stage(self, stage: Stage, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.stage = stage
        self.bytes_done = 0
        self.stage_started_at = now
        self.last_progress_at = now
        self._samples.clear()
        if self.started_at is None and stage is not Stage.QUEUED:
            self.started_at = now

    def update_progress(
        self, current: int, total: int | None = None, now: float | None = None
    ) -> None:
        now = time.monotonic() if now is None else now
        self.bytes_done = max(0, int(current))
        if total and total > 0 and not self.file_size:
            self.file_size = int(total)
        self.last_progress_at = now
        self._samples.append((now, self.bytes_done))

    @property
    def percent(self) -> float | None:
        if not self.file_size or self.file_size <= 0:
            return None
        return max(0.0, min(100.0, self.bytes_done * 100.0 / self.file_size))

    def speed(self, now: float | None = None) -> float | None:
        """Bytes per second over a short sliding window (None until measurable)."""
        now = time.monotonic() if now is None else now
        if len(self._samples) >= 2:
            recent = [s for s in self._samples if now - s[0] <= _SPEED_WINDOW_SECONDS]
            if len(recent) < 2:
                recent = list(self._samples)[-2:]
            (t0, b0), (t1, b1) = recent[0], recent[-1]
            if t1 > t0 and b1 >= b0:
                return (b1 - b0) / (t1 - t0)
        if self.stage_started_at is not None and self.bytes_done > 0:
            elapsed = now - self.stage_started_at
            if elapsed > 0:
                return self.bytes_done / elapsed
        return None

    def eta(self, now: float | None = None) -> float | None:
        speed = self.speed(now)
        if not speed or speed <= 0 or not self.file_size:
            return None
        return max(0.0, (self.file_size - self.bytes_done) / speed)

    def elapsed(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        start = self.started_at if self.started_at is not None else self.created_at
        return max(0.0, now - start)


@dataclass(frozen=True, slots=True)
class Snapshot:
    active: list[Job]
    queued: list[Job]

    @property
    def idle(self) -> bool:
        return not self.active and not self.queued


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._next_id = 1
        # Set by the bot layer: called (fire-and-forget) when a large file starts.
        self.on_large_file: Callable[[Job], Awaitable[None]] | None = None

    def create(
        self,
        kind: str,
        source_chat_id: str | int,
        source_title: str,
        message_id: int,
        *,
        media_type: str | None = None,
        file_size: int | None = None,
    ) -> Job:
        job = Job(
            id=self._next_id,
            kind=kind,
            source_chat_id=str(source_chat_id),
            source_title=source_title or str(source_chat_id),
            message_id=message_id,
            media_type=media_type,
            file_size=file_size,
        )
        self._next_id += 1
        self._jobs[job.id] = job
        return job

    def finish(self, job: Job) -> None:
        self._jobs.pop(job.id, None)

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def snapshot(self) -> Snapshot:
        jobs = sorted(self._jobs.values(), key=lambda j: j.id)
        active = [j for j in jobs if j.stage is not Stage.QUEUED]
        queued = [j for j in jobs if j.stage is Stage.QUEUED]
        return Snapshot(active=active, queued=queued)

    def request_skip(self, job_id: int | None = None) -> int:
        """Flag the given job (or every running job) to be skipped. Returns how many."""
        if job_id is not None:
            job = self._jobs.get(job_id)
            if job is None:
                return 0
            job.skip_requested = True
            return 1
        running = self.snapshot().active
        for job in running:
            job.skip_requested = True
        return len(running)

    def clear(self) -> None:
        self._jobs.clear()


class RecentErrors:
    """Small ring buffer of recent problems for the panel. Ids/codes only, no content."""

    def __init__(self, size: int = 20) -> None:
        self._items: deque[tuple[float, str]] = deque(maxlen=size)

    def record(self, text: str) -> None:
        self._items.append((time.time(), text))

    def recent(self, count: int = 3) -> list[tuple[float, str]]:
        return list(self._items)[-count:][::-1]

    def clear(self) -> None:
        self._items.clear()


registry = JobRegistry()
errors = RecentErrors()
