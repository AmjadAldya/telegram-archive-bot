from __future__ import annotations

import asyncio

import pytest
from helpers import FakeRegistrar
from pyrogram.errors import FloodWait, MessageIdInvalid, MessageNotModified

from app.bot.live import LiveEditor, LiveGone, LiveView, live_keyboard, render_live
from app.mirror import control
from app.mirror.jobs import JobRegistry, Stage, registry


def make_registry_with_download() -> tuple[JobRegistry, object]:
    local = JobRegistry()
    job = local.create(
        "live", -1001, "My <Channel>", 777, media_type="video", file_size=10 * 1024 * 1024
    )
    job.begin_stage(Stage.DOWNLOAD, now=0.0)
    job.update_progress(0, job.file_size, now=0.0)
    job.update_progress(2 * 1024 * 1024, job.file_size, now=2.0)
    return local, job


def test_idle_view_says_nothing_is_running() -> None:
    text = render_live(JobRegistry().snapshot(), paused=False, now=0.0)
    assert "لا يوجد شيء قيد التنفيذ" in text and "الطابور:</b> 0" in text and "🟢 يعمل" in text


def test_active_job_shows_bar_speed_eta_and_escapes_titles() -> None:
    local, _job = make_registry_with_download()
    text = render_live(local.snapshot(), paused=False, now=2.0)

    assert "⬇️ تنزيل" in text and "فيديو" in text and "10.0 MB" in text
    assert "▰▰▱▱▱▱▱▱▱▱ 20%" in text
    assert "1.0 MB/s" in text and "متبقي 0:08" in text
    assert "My &lt;Channel&gt;" in text and "<Channel>" not in text  # user text can't inject HTML
    assert "777" in text


def test_paused_state_and_skip_in_progress_are_visible() -> None:
    local, job = make_registry_with_download()
    job.skip_requested = True
    job.note = "FloodWait 30s"
    text = render_live(local.snapshot(), paused=True, now=2.0)
    assert "🟡 متوقف مؤقتًا" in text and "جارٍ التخطي" in text and "FloodWait 30s" in text


def test_queue_lists_next_items_and_summarises_the_rest() -> None:
    local = JobRegistry()
    for index in range(8):
        local.create("backlog" if index % 2 else "link", -1001, "Chan", 100 + index)
    text = render_live(local.snapshot(), paused=False, now=0.0)
    assert "الطابور:</b> 8" in text
    assert "1)" in text and "5)" in text and "6)" not in text
    assert "و3 أخرى" in text


def test_live_keyboard_buttons_and_pause_toggle() -> None:
    running = [b.callback_data for row in live_keyboard(False).inline_keyboard for b in row]
    paused = [b.callback_data for row in live_keyboard(True).inline_keyboard for b in row]
    assert running == ["lv:skip", "lv:pause", "lv:ref", "lv:stop"]
    assert paused == ["lv:skip", "lv:resume", "lv:ref", "lv:stop"]


# -- edit throttling ------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_editor(state: dict, errors: list | None = None, clock: Clock | None = None):
    edits: list[str] = []
    pending_errors = errors if errors is not None else []

    def render():
        return state["text"], state.get("kb", 0), "markup"

    async def edit(text, _markup):
        if pending_errors:
            raise pending_errors.pop(0)
        edits.append(text)

    return LiveEditor(render, edit, clock or Clock()), edits


def test_editor_only_edits_when_the_text_changed() -> None:
    async def scenario():
        state = {"text": "A"}
        editor, edits = make_editor(state)
        results = [await editor.tick()]  # first render
        results.append(await editor.tick())  # unchanged -> no Telegram call
        results.append(await editor.tick())
        state["text"] = "B"
        results.append(await editor.tick())
        return results, edits, editor.edits

    results, edits, count = asyncio.run(scenario())
    assert results == [True, False, False, True]
    assert edits == ["A", "B"] and count == 2


def test_editor_refreshes_when_only_the_keyboard_changes_or_when_forced() -> None:
    async def scenario():
        state = {"text": "A", "kb": 0}
        editor, edits = make_editor(state)
        await editor.tick()
        state["kb"] = 1  # e.g. pause toggled: same text, different buttons
        changed = await editor.tick()
        forced = await editor.tick(force=True)
        return changed, forced, edits

    changed, forced, edits = asyncio.run(scenario())
    assert (changed, forced) == (True, True) and edits == ["A", "A", "A"]


def test_message_not_modified_is_swallowed_and_remembered() -> None:
    async def scenario():
        state = {"text": "A"}
        editor, edits = make_editor(state, errors=[MessageNotModified()])
        first = await editor.tick()
        second = await editor.tick()  # remembered as up to date: no second attempt
        return first, second, edits

    assert asyncio.run(scenario()) == (False, False, [])


def test_flood_wait_backs_off_then_resumes() -> None:
    async def scenario():
        clock = Clock()
        state = {"text": "A"}
        editor, edits = make_editor(state, errors=[FloodWait(value=10)], clock=clock)
        assert await editor.tick() is False  # Telegram says wait 10 s
        state["text"] = "B"
        clock.now = 5.0
        during = await editor.tick()  # still inside the back-off window: no call at all
        clock.now = 12.0
        after = await editor.tick()
        return during, after, edits, editor.flood_backoffs

    during, after, edits, backoffs = asyncio.run(scenario())
    assert during is False and after is True
    assert edits == ["B"] and backoffs == 1


def test_deleted_live_message_stops_the_view() -> None:
    async def scenario() -> None:
        editor, _ = make_editor({"text": "A"}, errors=[MessageIdInvalid()])
        await editor.tick()

    with pytest.raises(LiveGone):
        asyncio.run(scenario())


# -- the view that owns the single message ------------------------------------------------------------


def test_live_view_sends_one_message_and_edits_it_only_when_something_changes() -> None:
    async def scenario():
        bot = FakeRegistrar()
        view = LiveView(interval=0.02)
        await view.open(bot, 111)
        await asyncio.sleep(0.15)  # nothing is happening: no edits at all
        idle_edits = len(bot.edited)

        job = registry.create("live", -1001, "Chan", 5, media_type="photo", file_size=1000)
        job.begin_stage(Stage.UPLOAD)
        await asyncio.sleep(0.1)  # a job appeared: the same message is edited
        await view.stop("bye")
        return bot, idle_edits

    bot, idle_edits = asyncio.run(scenario())
    assert len(bot.sent) == 1  # ONE message, ever
    assert idle_edits == 0
    live_edits = [e for e in bot.edited if "رفع" in e[2]]
    sent_message_id = 7001  # FakeRegistrar numbers sent messages 7000 + n
    assert live_edits
    assert all(edit[:2] == (111, sent_message_id) for edit in bot.edited)  # always the same message
    assert bot.edited[-1][2] == "bye"


def test_reopening_live_moves_the_single_message() -> None:
    async def scenario():
        bot = FakeRegistrar()
        view = LiveView(interval=0.05)
        await view.open(bot, 111)
        await view.open(bot, 111)
        await view.stop()
        return bot

    bot = asyncio.run(scenario())
    assert len(bot.sent) == 2
    assert any("انتقل العرض" in edit[2] for edit in bot.edited)  # the old one was retired


def test_live_view_reflects_pause_state_in_its_buttons() -> None:
    async def scenario():
        bot = FakeRegistrar()
        view = LiveView(interval=0.02)
        await view.open(bot, 111)
        control.pause()
        await asyncio.sleep(0.1)
        await view.stop()
        return bot

    bot = asyncio.run(scenario())
    last_markup = [e for e in bot.edited if e[3] is not None][-1][3]
    assert last_markup.inline_keyboard[0][1].callback_data == "lv:resume"


def test_file_name_is_shown_shortened_and_escaped() -> None:
    local = JobRegistry()
    job = local.create("live", -1001, "Chan", 5, media_type="document", file_size=1000)
    job.file_name = "<b>very</b>_" + "x" * 80 + ".mp4"
    job.begin_stage(Stage.DOWNLOAD, now=0.0)

    text = render_live(local.snapshot(), paused=False, now=1.0)

    name_line = next(line for line in text.splitlines() if line.startswith("الملف:"))
    assert name_line.endswith(".mp4</code>")  # the extension survives shortening
    assert "…" in name_line and len(name_line) < 90  # long names are trimmed
    assert "<b>very</b>" not in text  # a file name can never inject raw HTML
