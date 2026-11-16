from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.bot import panel, texts
from app.bot.panel import SkippedView, SourceView, StatusData
from app.database.base import session_scope
from app.database.repositories import MirrorRepository
from app.mirror import runtime
from app.mirror.backlog import Progress
from app.mirror.jobs import JobRegistry

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def test_fmt_bytes() -> None:
    assert texts.fmt_bytes(None) == "؟"
    assert texts.fmt_bytes(0) == "0 B"
    assert texts.fmt_bytes(1023) == "1023 B"
    assert texts.fmt_bytes(1536) == "1.5 KB"
    assert texts.fmt_bytes(5 * 1024**2) == "5.0 MB"
    assert texts.fmt_bytes(3 * 1024**3) == "3.0 GB"
    assert texts.fmt_bytes(5 * 1024**5) == "5120.0 TB"


def test_fmt_duration_and_speed() -> None:
    assert texts.fmt_duration(None) == "—"
    assert texts.fmt_duration(0) == "0:00"
    assert texts.fmt_duration(75) == "1:15"
    assert texts.fmt_duration(3725) == "1:02:05"
    assert texts.fmt_duration(-5) == "0:00"
    assert texts.fmt_speed(None) == "—" and texts.fmt_speed(0) == "—"
    assert texts.fmt_speed(2 * 1024**2) == "2.0 MB/s"


def test_progress_bar() -> None:
    assert texts.progress_bar(0) == "▱" * 10 + " 0%"
    assert texts.progress_bar(50) == "▰" * 5 + "▱" * 5 + " 50%"
    assert texts.progress_bar(100) == "▰" * 10 + " 100%"
    assert texts.progress_bar(150).count("▰") == 10 and texts.progress_bar(-5).count("▰") == 0
    assert texts.progress_bar(None).endswith("؟")


def test_fmt_ago_accepts_naive_sqlite_datetimes() -> None:
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None)  # what SQLite returns
    assert texts.fmt_ago(naive, NOW) == "منذ 5 د"
    assert texts.fmt_ago(NOW - timedelta(seconds=10), NOW) == "الآن"
    assert texts.fmt_ago(NOW - timedelta(hours=3), NOW) == "منذ 3 س"
    assert texts.fmt_ago(NOW - timedelta(days=2), NOW) == "منذ 2 يوم"
    assert texts.fmt_ago(None, NOW) == "لا يوجد"


def test_esc_neutralises_html() -> None:
    assert texts.esc("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"


def make_source(index: int, *, enabled=True, state="active", history=True) -> runtime.SourceInfo:
    return runtime.SourceInfo(
        id=index,
        chat_id=-1000 - index,
        title=f"Source <{index}>",
        enabled=enabled,
        history_sync=history,
        state=state,
        last_error="ChannelPrivate" if state == "error" else None,
    )


def make_status(sources: list[SourceView], **overrides) -> StatusData:
    values = {
        "destination": runtime.Destination(-100222, "Archive <main>"),
        "sources": sources,
        "total": 25498,
        "last_day": 120,
        "last_transfer": NOW - timedelta(minutes=3),
        "skipped": 2,
        "paused": False,
        "restricted_reason": None,
        "jobs": JobRegistry().snapshot(),
        "running_source": None,
        "recent_errors": [(NOW.timestamp() - 60, "Download timeout (msg 5)")],
    }
    values.update(overrides)
    return StatusData(**values)


def view(index: int, **kwargs) -> SourceView:
    progress = Progress("running", 100, 40, 3, 61525, None, NOW)
    return SourceView(make_source(index, **kwargs), progress, NOW - timedelta(minutes=index))


def test_home_summary_has_everything_the_owner_needs_and_escapes_titles() -> None:
    text = panel.render_home(
        make_status([view(1), view(2, enabled=False), view(3, state="error")]), NOW
    )

    assert "لوحة التحكم" in text and "⚪ خامل" in text
    assert "Archive &lt;main&gt;" in text and "<main>" not in text
    assert "المصادر:</b> 3 (2 مفعّل)" in text
    assert (
        "🟢 Source &lt;1&gt;" in text
        and "⏸ Source &lt;2&gt;" in text
        and "⚠️ Source &lt;3&gt;" in text
    )
    assert "25,498" in text and "120" in text  # counters
    assert "منذ 3 د" in text  # last transfer
    assert "Download timeout (msg 5)" in text  # recent errors
    assert "قيد الفحص" in text  # per-source history state


def test_home_states_paused_restricted_and_missing_destination() -> None:
    assert "🟡 متوقف مؤقتًا" in panel.render_home(make_status([], paused=True), NOW)
    restricted = panel.render_home(make_status([], restricted_reason="PeerFlood"), NOW)
    assert "🔴" in restricted and "PeerFlood" in restricted
    nothing = panel.render_home(make_status([], destination=None, recent_errors=[]), NOW)
    assert "غير محددة" in nothing and "/addsource" in nothing


def all_callback_data(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_home_keyboard_and_pause_toggle() -> None:
    running = all_callback_data(panel.home_keyboard(paused=False))
    paused = all_callback_data(panel.home_keyboard(paused=True))
    assert "p:pause" in running and "p:resume" not in running
    assert "p:resume" in paused and "p:pause" not in paused
    for expected in ("p:home", "lv:open", "p:srcs:1", "p:add", "p:dest", "p:skipped:1", "sk:0"):
        assert expected in running


def test_sources_list_is_paginated() -> None:
    data = make_status([view(i) for i in range(1, 15)])  # 14 sources, 6 per page
    text1, markup1 = panel.render_sources(data, 1)
    _text2, markup2 = panel.render_sources(data, 2)
    text3, markup3 = panel.render_sources(data, 3)

    assert "صفحة 1/3" in text1 and "Source &lt;1&gt;" in text1 and "Source &lt;7&gt;" not in text1
    assert "p:srcs:2" in all_callback_data(markup1) and "p:srcs:0" not in all_callback_data(markup1)
    assert {"p:srcs:1", "p:srcs:3"} <= set(all_callback_data(markup2))
    assert "p:srcs:2" in all_callback_data(markup3) and "p:srcs:4" not in all_callback_data(markup3)
    assert "صفحة 3/3" in text3
    # out-of-range pages are clamped instead of failing
    assert "صفحة 3/3" in panel.render_sources(data, 99)[0]
    assert "صفحة 1/3" in panel.render_sources(data, -4)[0]


def test_source_detail_shows_cursor_counters_and_action_buttons() -> None:
    text, markup = panel.render_source_detail(view(1, state="error"))
    assert "61525" in text and "100" in text and "ChannelPrivate" in text
    assert [d for d in all_callback_data(markup) if d.startswith("p:")] == [
        "p:tog:1",
        "p:hist:1",
        "p:rs:1",
        "p:rm:1",
        "p:srcs:1",
    ]
    _paused_text, paused_markup = panel.render_source_detail(view(1, enabled=False, history=False))
    labels = [b.text for row in paused_markup.inline_keyboard for b in row]
    assert "▶️ تفعيل المصدر" in labels and "📚 تفعيل السجل" in labels


def test_confirm_keyboard() -> None:
    markup = panel.confirm_keyboard("p:rmy:3", "p:src:3")
    assert all_callback_data(markup) == ["p:rmy:3", "p:src:3"]


def skipped(index: int) -> SkippedView:
    return SkippedView(
        index, "-1005", 100 + index, "video", 600 * 1024**2, "timeout", NOW - timedelta(hours=1)
    )


def test_skipped_list_has_retry_buttons_and_pagination() -> None:
    rows = [skipped(i) for i in range(1, 4)]
    text, markup = panel.render_skipped(rows, total=12, page=1)
    assert "(12)" in text and "صفحة 1/3" in text and "انتهت المهلة" in text and "600.0 MB" in text
    data = all_callback_data(markup)
    assert data[:3] == ["rt:1", "rt:2", "rt:3"] and "p:skipped:2" in data and "p:home" in data
    empty_text, _ = panel.render_skipped([], total=0, page=1)
    assert "لا توجد ملفات متخطاة" in empty_text


def test_every_button_fits_telegrams_64_byte_callback_limit() -> None:
    data = make_status([view(i) for i in range(1, 15)])
    markups = [
        panel.home_keyboard(False),
        panel.render_sources(data, 2)[1],
        panel.render_source_detail(view(14))[1],
        panel.confirm_keyboard("p:dsty:-1001234567890123", "p:no"),
        panel.render_skipped([skipped(i) for i in range(5)], 40, 2)[1],
    ]
    for markup in markups:
        for callback in all_callback_data(markup):
            assert len(callback.encode()) <= 64, callback


@pytest.mark.parametrize("page", [1, 2])
def test_status_gathering_reads_the_database(page: int) -> None:
    async def scenario() -> StatusData:
        await runtime.set_dest(-100222, "Archive")
        await runtime.add_source(-100111, "A")
        with session_scope() as session:
            repository = MirrorRepository(session)
            repository.record_transfer(
                file_unique_id="u1",
                media_type="photo",
                source_chat_id="-100111",
                source_message_id=1,
                dest_chat_id="-100222",
                dest_message_id=None,
            )
            repository.record_skip(
                source_chat_id="-100111", source_message_id=2, media_type="video", reason="timeout"
            )
        return await panel.gather_status()

    data = asyncio.run(scenario())
    assert (data.total, data.last_day, data.skipped) == (1, 1, 1)
    assert data.last_transfer is not None and data.sources[0].last_transfer is not None
    assert data.sources[0].progress is None  # no backlog started yet
