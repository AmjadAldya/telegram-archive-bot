"""The /live monitor: ONE message that edits itself while transfers run.

`render_live` and `LiveEditor` are plain Python (testable without Telegram):
the editor only calls Telegram when the rendered text actually changed, honours
`FloodWait` by backing off, and treats `MessageNotModified` as "already up to
date". `LiveView` wires an editor to a real message and a refresh loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageIdInvalid, MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.texts import esc
from app.config.settings import LIVE_REFRESH_SECONDS
from app.mirror import control
from app.mirror.jobs import Job, Snapshot, registry
from app.services.logger import logger

_MAX_QUEUE_LINES = 5


def _job_block(job: Job, now: float) -> list[str]:
    stage = texts.STAGE_LABELS.get(job.stage.value, job.stage.value)
    head = f"{stage} — {texts.media_label(job.media_type)} · {texts.fmt_bytes(job.file_size)}"
    lines = [
        f"<b>{head}</b>  ({texts.KIND_LABELS.get(job.kind, job.kind)})",
    ]
    if job.file_name:
        lines.append(f"الملف: <code>{esc(texts.shorten(job.file_name))}</code>")
    lines.append(f"المصدر: {esc(job.source_title)} · رسالة <code>{job.message_id}</code>")
    if job.stage.value != "queued":
        done = texts.fmt_bytes(job.bytes_done)
        lines.append(
            f"{texts.progress_bar(job.percent)}  ({done} / {texts.fmt_bytes(job.file_size)})"
        )
        lines.append(
            f"⚡ {texts.fmt_speed(job.speed(now))} · ⏱ متبقي {texts.fmt_duration(job.eta(now))} · "
            f"مضى {texts.fmt_duration(job.elapsed(now))}"
        )
    if job.note:
        lines.append(f"⏳ {esc(job.note)}")
    if job.skip_requested:
        lines.append("⏭ جارٍ التخطي…")
    return lines


def render_live(
    snapshot: Snapshot, paused: bool, now: float | None = None, title: str = "📡 المراقبة المباشرة"
) -> str:
    now = time.monotonic() if now is None else now
    state = "🟡 متوقف مؤقتًا" if paused else "🟢 يعمل"
    lines = [f"<b>{title}</b> · {state}", ""]
    if not snapshot.active:
        lines.append("لا يوجد شيء قيد التنفيذ الآن 😴")
    for job in snapshot.active:
        lines.extend(_job_block(job, now))
        lines.append("")
    lines.append(f"<b>الطابور:</b> {len(snapshot.queued)}")
    for index, job in enumerate(snapshot.queued[:_MAX_QUEUE_LINES], start=1):
        label = texts.media_label(job.media_type) if job.media_type else "رسالة"
        lines.append(
            f"  {index}) {label} — {esc(job.source_title)} #{job.message_id} "
            f"({texts.KIND_LABELS.get(job.kind, job.kind)})"
        )
    if len(snapshot.queued) > _MAX_QUEUE_LINES:
        lines.append(f"  … و{len(snapshot.queued) - _MAX_QUEUE_LINES} أخرى")
    return "\n".join(lines).rstrip()


def live_keyboard(paused: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏭ تخطي", callback_data="lv:skip"),
                InlineKeyboardButton(
                    "▶️ استئناف" if paused else "⏸ إيقاف مؤقت",
                    callback_data="lv:resume" if paused else "lv:pause",
                ),
            ],
            [
                InlineKeyboardButton("🔄 تحديث", callback_data="lv:ref"),
                InlineKeyboardButton("⏹ إيقاف العرض", callback_data="lv:stop"),
            ],
        ]
    )


class LiveGone(Exception):
    """The live message no longer exists; stop refreshing."""


class LiveEditor:
    """Calls `edit` only when needed. `render()` returns (text, keyboard signature, markup)."""

    def __init__(
        self,
        render: Callable[[], tuple[str, object, object]],
        edit: Callable[[str, object], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._render = render
        self._edit = edit
        self._clock = clock
        self._last: tuple[str, object] | None = None
        self._backoff_until = 0.0
        self.edits = 0
        self.flood_backoffs = 0

    async def tick(self, force: bool = False) -> bool:
        """Refresh once. Returns True if a Telegram edit was made."""
        text, signature, markup = self._render()
        if not force and self._last == (text, signature):
            return False
        now = self._clock()
        if now < self._backoff_until:
            return False
        try:
            await self._edit(text, markup)
        except MessageNotModified:
            self._last = (text, signature)
            return False
        except FloodWait as exc:
            self._backoff_until = now + float(exc.value) + 1.0
            self.flood_backoffs += 1
            logger.warning("Live view: FloodWait %ss, backing off", exc.value)
            return False
        except MessageIdInvalid as exc:
            raise LiveGone() from exc
        self._last = (text, signature)
        self.edits += 1
        return True


class LiveView:
    """Owns the single live message (if any) and the loop that refreshes it."""

    def __init__(self, interval: float = LIVE_REFRESH_SECONDS) -> None:
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._editor: LiveEditor | None = None
        self._client = None
        self._chat_id: int | str | None = None
        self._message_id: int | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def _render(self) -> tuple[str, object, object]:
        paused = control.is_paused()
        return (
            render_live(registry.snapshot(), paused),
            paused,
            live_keyboard(paused),
        )

    async def open(self, client, chat_id: int | str) -> None:
        """Send the live message and start refreshing it (replacing any previous one)."""
        await self.stop("🔁 انتقل العرض المباشر إلى رسالة جديدة.")
        text, _signature, markup = self._render()
        sent = await client.send_message(
            chat_id, text, parse_mode=enums.ParseMode.HTML, reply_markup=markup
        )
        self._client, self._chat_id, self._message_id = client, chat_id, sent.id

        async def edit(new_text: str, new_markup) -> None:
            await client.edit_message_text(
                chat_id,
                sent.id,
                new_text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=new_markup,
            )

        self._editor = LiveEditor(self._render, edit)
        self._editor._last = (text, _signature)  # the message was just sent with this content
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        assert self._editor is not None
        try:
            while True:
                await asyncio.sleep(self.interval)
                await self._editor.tick()
        except LiveGone:
            logger.info("Live view message was deleted; stopping")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the monitor must never take the bot down
            logger.exception("Live view stopped unexpectedly")

    async def refresh_now(self) -> None:
        if self._editor is not None:
            with contextlib.suppress(LiveGone):
                await self._editor.tick(force=True)

    async def stop(self, final_text: str | None = None) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if final_text and self._client is not None and self._message_id is not None:
            with contextlib.suppress(Exception):
                await self._client.edit_message_text(
                    self._chat_id, self._message_id, final_text, parse_mode=enums.ParseMode.HTML
                )
        self._editor = None
        self._message_id = None


live_view = LiveView()


def format_snapshot_text() -> str:
    """One-off snapshot for /queue."""
    return render_live(registry.snapshot(), control.is_paused(), title="🧾 الطابور الحالي")
