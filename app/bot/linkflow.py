"""Paste a t.me link, get the media archived - no command, no confirmation.

`run_link` turns one link into a transfer and a short Arabic result;
`LinkQueue` processes the links of all pasted messages strictly in order.
Reads go through the userbot (the control bot cannot see source chats), and
the transfer itself is the shared `transfer_message` pipeline, so dedup,
FloodWait handling, timeouts, skip and the live monitor all apply.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatInvalid,
    FloodWait,
    MessageIdInvalid,
    PeerIdInvalid,
    UsernameInvalid,
    UsernameNotOccupied,
)

from app.bot import texts
from app.bot.texts import esc
from app.mirror import runtime
from app.mirror.jobs import Job, registry
from app.mirror.links import MessageLink
from app.mirror.throttle import AccountRestrictedError, call_with_flood_wait
from app.mirror.transfer import transfer_message
from app.services.logger import logger

_NO_ACCESS = "لا أستطيع الوصول لهذه المحادثة: الحساب ليس عضوًا فيها أو أنها غير موجودة."


@dataclass(frozen=True, slots=True)
class LinkResult:
    text: str
    ok: bool = False
    can_force: bool = False


class LinkError(Exception):
    """A problem reaching the linked message; `args[0]` is the Arabic explanation."""


@dataclass(slots=True)
class LinkTask:
    link: MessageLink
    job: Job
    status: object  # the reply message to keep updated
    force: bool = False
    kind: str = "link"
    on_done: Callable[[LinkResult], Awaitable[None]] | None = field(default=None, repr=False)


class TokenStore:
    """Short tokens for buttons that need to remember something (callback data is limited
    to 64 bytes): 'transfer anyway' links, pending invite links. Bounded and process-local."""

    def __init__(self, limit: int = 200) -> None:
        self._items: OrderedDict[str, object] = OrderedDict()
        self._counter = 0
        self._limit = limit

    def put(self, item: object) -> str:
        self._counter += 1
        token = str(self._counter)
        self._items[token] = item
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> Any:
        return self._items.get(token)


force_store = TokenStore()


async def _warm_dialogs(userbot) -> None:
    async for _dialog in userbot.get_dialogs():
        pass


async def resolve_message(userbot, link: MessageLink):
    """Fetch the linked message through the userbot, or raise LinkError (Arabic)."""
    for attempt in (1, 2):
        try:
            message = await call_with_flood_wait(
                userbot.get_messages, link.chat_ref, link.message_id
            )
            break
        except (UsernameNotOccupied, UsernameInvalid) as exc:
            raise LinkError("لم أجد هذه القناة أو المجموعة.") from exc
        except MessageIdInvalid as exc:
            raise LinkError("الرسالة غير موجودة أو محذوفة.") from exc
        except (ChannelPrivate, ChannelInvalid, ChatInvalid, PeerIdInvalid) as exc:
            # A chat joined after startup may not be in Pyrogram's peer cache yet.
            if attempt == 1 and isinstance(link.chat_ref, int):
                with contextlib.suppress(Exception):
                    await _warm_dialogs(userbot)
                continue
            raise LinkError(_NO_ACCESS) from exc
    if message is None or getattr(message, "empty", False):
        raise LinkError("الرسالة غير موجودة أو محذوفة.")
    return message


def _describe(job: Job) -> str:
    return f"{texts.media_label(job.media_type)} · {texts.fmt_bytes(job.file_size)}"


def result_for(outcome: str, job: Job, *, forced: bool = False) -> LinkResult:
    """Map a transfer outcome (and what the job recorded) to a short Arabic result."""
    if outcome == "transferred":
        return LinkResult(f"✅ تم النقل — {_describe(job)}", ok=True)
    if outcome == "duplicate":
        return LinkResult(
            f"♻️ هذا الملف موجود مسبقًا في الأرشيف — {_describe(job)}", can_force=not forced
        )
    if outcome == "user_skip":
        return LinkResult("⏭ تم تخطي الملف (يمكنك إعادته من /skipped).")
    if outcome == "timeout_skip":
        return LinkResult("⏱ انتهت المهلة بعد عدة محاولات — أُضيف إلى /skipped لإعادة المحاولة.")
    if outcome == "auto_skip":
        return LinkResult("🚫 تخطٍّ تلقائي لأن الحجم كبير — أُضيف إلى /skipped.")
    if outcome == "failed":
        if job.error == "dest_forbidden":
            return LinkResult("❌ لا أستطيع الكتابة في الوجهة — تأكد أن الحساب مشرف فيها.")
        return LinkResult("❌ فشل النقل.")
    # "skipped"
    if job.error == "no_media":
        return LinkResult("ℹ️ هذه الرسالة لا تحتوي وسائط قابلة للنقل (صورة/فيديو/ملف/صوت…).")
    if job.error == "unavailable":
        return LinkResult("⚠️ لم تعد الوسائط متاحة من تيليجرام (محمية أو منتهية الصلاحية).")
    if job.error == "restricted":
        return LinkResult("⛔ الحساب مقيَّد من تيليجرام — تم إيقاف النقل.")
    return LinkResult("ℹ️ لم يتم النقل (النقل متوقف مؤقتًا أو النوع غير مدعوم).")


async def run_link(userbot, task: LinkTask) -> LinkResult:
    """Run one link job to completion and return the result to show."""
    job = task.job
    try:
        destination = runtime.destination()
        if destination is None:
            return LinkResult("❗ الوجهة غير محددة — استخدم /setdest أولًا.")
        try:
            message = await resolve_message(userbot, task.link)
        except LinkError as exc:
            return LinkResult(f"❌ {esc(exc.args[0])}")
        except FloodWait as exc:
            return LinkResult(
                f"⏳ تيليجرام يطلب الانتظار {int(exc.value)} ثانية — أعد المحاولة بعدها."
            )

        job.source_title = getattr(message.chat, "title", None) or job.source_title
        try:
            outcome = await transfer_message(
                userbot,
                message,
                destination.chat_id,
                job=job,
                force=task.force,
                ignore_type_filter=True,
                kind=task.kind,
            )
        except FloodWait as exc:
            return LinkResult(
                f"⏳ تيليجرام يطلب الانتظار {int(exc.value)} ثانية — أعد المحاولة بعدها."
            )
        except AccountRestrictedError:
            return LinkResult("⛔ الحساب مقيَّد من تيليجرام — تم إيقاف النقل.")
        return result_for(outcome, job, forced=task.force)
    finally:
        registry.finish(job)


class LinkQueue:
    """Runs link tasks one at a time, in the order they were pasted."""

    def __init__(self, runner: Callable[[LinkTask], Awaitable[None]]) -> None:
        self._queue: asyncio.Queue[LinkTask] = asyncio.Queue()
        self._runner = runner
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._worker())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def submit(self, task: LinkTask) -> int:
        """Queue a task; returns its position (1 = next up)."""
        await self._queue.put(task)
        return self._queue.qsize()

    def pending(self) -> int:
        return self._queue.qsize()

    async def _worker(self) -> None:
        while True:
            task = await self._queue.get()
            try:
                await self._runner(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad link must not stop the queue
                logger.exception("Link job failed unexpectedly")
            finally:
                self._queue.task_done()
