from __future__ import annotations

import pytest

from app.mirror.jobs import Job, JobRegistry, RecentErrors, Stage


def make_job(size: int | None = 10_000) -> Job:
    return Job(
        id=1, kind="live", source_chat_id="-1", source_title="S", message_id=5, file_size=size
    )


def test_percent_is_none_without_a_known_size_and_is_clamped() -> None:
    assert make_job(None).percent is None
    job = make_job(1000)
    job.update_progress(250, 1000, now=1.0)
    assert job.percent == 25.0
    job.update_progress(5000, 1000, now=2.0)
    assert job.percent == 100.0


def test_total_from_progress_callback_fills_in_an_unknown_size() -> None:
    job = make_job(None)
    job.update_progress(100, 400, now=1.0)
    assert job.file_size == 400
    assert job.percent == 25.0


def test_speed_and_eta_from_the_sliding_window() -> None:
    job = make_job(10_000)
    job.begin_stage(Stage.DOWNLOAD, now=0.0)
    job.update_progress(0, 10_000, now=0.0)
    job.update_progress(1000, 10_000, now=1.0)
    job.update_progress(3000, 10_000, now=2.0)

    assert job.speed(now=2.0) == pytest.approx(1500.0)
    assert job.eta(now=2.0) == pytest.approx((10_000 - 3000) / 1500.0)


def test_speed_ignores_old_samples_outside_the_window() -> None:
    job = make_job(1_000_000)
    job.begin_stage(Stage.DOWNLOAD, now=0.0)
    job.update_progress(0, 1_000_000, now=0.0)
    job.update_progress(500_000, 1_000_000, now=1.0)  # a fast burst long ago
    job.update_progress(500_000, 1_000_000, now=20.0)
    job.update_progress(500_100, 1_000_000, now=21.0)  # now crawling at 100 B/s

    assert job.speed(now=21.0) == pytest.approx(100.0)


def test_speed_falls_back_to_average_with_a_single_sample() -> None:
    job = make_job(10_000)
    job.begin_stage(Stage.UPLOAD, now=10.0)
    job.update_progress(2000, 10_000, now=14.0)
    assert job.speed(now=14.0) == pytest.approx(500.0)


def test_speed_and_eta_are_none_when_nothing_moved() -> None:
    job = make_job(10_000)
    job.begin_stage(Stage.DOWNLOAD, now=0.0)
    assert job.speed(now=5.0) is None
    assert job.eta(now=5.0) is None


def test_begin_stage_resets_progress_and_elapsed_counts_from_first_start() -> None:
    job = make_job(1000)
    job.begin_stage(Stage.DOWNLOAD, now=100.0)
    job.update_progress(1000, 1000, now=105.0)
    job.begin_stage(Stage.UPLOAD, now=110.0)

    assert job.bytes_done == 0
    assert job.stage is Stage.UPLOAD
    assert job.elapsed(now=130.0) == pytest.approx(30.0)  # since the download began


def test_registry_separates_active_from_queued_and_orders_by_id() -> None:
    registry = JobRegistry()
    first = registry.create("live", 1, "A", 1)
    second = registry.create("link", 2, "B", 2)
    third = registry.create("backlog", 3, "C", 3)
    second.begin_stage(Stage.DOWNLOAD)

    snapshot = registry.snapshot()
    assert [j.id for j in snapshot.active] == [second.id]
    assert [j.id for j in snapshot.queued] == [first.id, third.id]
    assert snapshot.idle is False

    for job in (first, second, third):
        registry.finish(job)
    assert registry.snapshot().idle is True


def test_request_skip_targets_running_jobs_or_one_specific_job() -> None:
    registry = JobRegistry()
    running = registry.create("live", 1, "A", 1)
    waiting = registry.create("live", 1, "A", 2)
    running.begin_stage(Stage.UPLOAD)

    assert registry.request_skip() == 1
    assert running.skip_requested and not waiting.skip_requested
    assert registry.request_skip(waiting.id) == 1
    assert waiting.skip_requested
    assert registry.request_skip(9999) == 0


def test_request_skip_with_nothing_running_does_nothing() -> None:
    assert JobRegistry().request_skip() == 0


def test_recent_errors_are_newest_first_and_bounded() -> None:
    log = RecentErrors(size=3)
    for index in range(5):
        log.record(f"e{index}")
    assert [text for _stamp, text in log.recent(10)] == ["e4", "e3", "e2"]
    assert [text for _stamp, text in log.recent(1)] == ["e4"]
