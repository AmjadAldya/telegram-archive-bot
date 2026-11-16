from __future__ import annotations

import asyncio
import logging

import pytest

from app.mirror.guard import SkipRequested, TransferTimeout, compute_timeout, run_guarded
from app.mirror.jobs import Job, Stage

FAST = {"poll": 0.01, "skip_grace": 0.05}


def make_job() -> Job:
    job = Job(
        id=1, kind="live", source_chat_id="-1", source_title="S", message_id=42, file_size=1000
    )
    job.begin_stage(Stage.DOWNLOAD)
    return job


def test_compute_timeout_scales_with_file_size() -> None:
    small = compute_timeout(1024, base_seconds=100, min_speed_kbps=100)
    large = compute_timeout(1024 * 1024 * 100, base_seconds=100, min_speed_kbps=100)
    assert small == pytest.approx(100 + 1024 / (100 * 1024))
    assert large == pytest.approx(100 + 1024)
    assert compute_timeout(None, base_seconds=100) == 100
    assert compute_timeout(0, base_seconds=100) == 100


def test_returns_the_result_and_propagates_errors() -> None:
    async def ok():
        return "done"

    async def boom():
        raise ValueError("bad")

    assert asyncio.run(run_guarded(ok(), make_job(), "Download", timeout=5, **FAST)) == "done"
    with pytest.raises(ValueError):
        asyncio.run(run_guarded(boom(), make_job(), "Download", timeout=5, **FAST))


def test_overall_timeout_cancels_the_operation() -> None:
    state = {"cancelled": False}

    async def hang():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    with pytest.raises(TransferTimeout) as caught:
        asyncio.run(run_guarded(hang(), make_job(), "Download", timeout=0.1, stall=60, **FAST))
    assert caught.value.kind == "timeout"
    assert state["cancelled"] is True


def test_stall_is_reported_separately_from_timeout() -> None:
    async def hang():
        await asyncio.sleep(3600)

    with pytest.raises(TransferTimeout) as caught:
        asyncio.run(run_guarded(hang(), make_job(), "Upload", timeout=60, stall=0.1, **FAST))
    assert caught.value.kind == "stall"


def test_steady_progress_prevents_a_stall() -> None:
    job = make_job()

    async def slow_but_moving():
        for step in range(1, 9):
            await asyncio.sleep(0.05)
            job.update_progress(step * 100, 1000)
        return "finished"

    result = asyncio.run(
        run_guarded(slow_but_moving(), job, "Download", timeout=5, stall=0.2, **FAST)
    )
    assert result == "finished"


def test_flood_wait_time_is_not_counted_as_a_stall_or_timeout() -> None:
    job = make_job()
    job.flood_wait_total = 100.0  # the operation is sleeping on a FloodWait

    async def waits():
        await asyncio.sleep(0.3)
        return "ok"

    assert (
        asyncio.run(run_guarded(waits(), job, "Download", timeout=0.1, stall=0.1, **FAST)) == "ok"
    )


def test_skip_cancels_an_operation_that_ignores_stop_transmission() -> None:
    job = make_job()

    async def stubborn():
        await asyncio.sleep(3600)

    async def scenario():
        task = asyncio.ensure_future(run_guarded(stubborn(), job, "Download", timeout=60, **FAST))
        await asyncio.sleep(0.05)
        job.skip_requested = True
        await task

    with pytest.raises(SkipRequested):
        asyncio.run(scenario())


def test_skip_lets_a_cooperative_operation_stop_by_itself() -> None:
    job = make_job()

    async def cooperative():
        while not job.skip_requested:
            await asyncio.sleep(0.01)

    async def scenario():
        task = asyncio.ensure_future(
            run_guarded(cooperative(), job, "Download", timeout=60, poll=0.01, skip_grace=5.0)
        )
        await asyncio.sleep(0.05)
        job.skip_requested = True
        return await task

    assert asyncio.run(scenario()) is None


def test_progress_is_logged_with_ids_and_byte_counts_only(caplog) -> None:
    job = make_job()
    job.update_progress(300, 1000)

    async def slow():
        await asyncio.sleep(0.25)
        return 1

    with caplog.at_level(logging.INFO, logger="archive-bot"):
        asyncio.run(run_guarded(slow(), job, "Download", timeout=5, log_interval=0.05, **FAST))

    lines = [r.getMessage() for r in caplog.records if "in progress" in r.getMessage()]
    assert lines, "expected periodic progress logging"
    assert "message 42" in lines[0] and "300 of 1000 bytes" in lines[0]
