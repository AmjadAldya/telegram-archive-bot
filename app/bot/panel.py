"""Panel screens: status data plus pure text/keyboard builders.

Data is gathered once per refresh (`gather_status`, DB work in a worker
thread); the `render_*` / `*_keyboard` functions are pure so they are easy to
test. All callback data is short (<64 bytes) and namespaced:

    p:home  p:srcs:<page>  p:src:<id>  p:tog:<id>  p:hist:<id>
    p:rm:<id> -> p:rmy:<id>   p:rs:<id|0> -> p:rsy:<id|0>   p:add  p:dest
    p:dsty:<chat_id>  p:pause  p:resume  p:skipped:<page>  p:help  p:no
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.texts import esc
from app.database.base import session_scope
from app.database.repositories import MirrorRepository
from app.mirror import control, limits, runtime
from app.mirror.backlog import Progress, progress_from_state
from app.mirror.jobs import Snapshot, errors, registry

SOURCES_PER_PAGE = 6
SKIPPED_PER_PAGE = 5

_HISTORY_LABELS = {
    "idle": "لم يبدأ",
    "running": "قيد الفحص",
    "paused": "متوقف",
    "completed": "مكتمل ✅",
    "failed": "فشل ⚠️",
}


@dataclass(frozen=True, slots=True)
class SourceView:
    info: runtime.SourceInfo
    progress: Progress | None
    last_transfer: datetime | None


@dataclass(frozen=True, slots=True)
class StatusData:
    destination: runtime.Destination | None
    sources: list[SourceView]
    total: int
    last_day: int
    last_transfer: datetime | None
    skipped: int
    paused: bool
    restricted_reason: str | None
    jobs: Snapshot
    running_source: int | None
    recent_errors: list[tuple[float, str]]


@dataclass(frozen=True, slots=True)
class SkippedView:
    id: int
    source_chat_id: str
    source_message_id: int
    media_type: str
    file_size: int | None
    reason: str
    created_at: datetime


def _gather_sync(dest_key: str | None, source_keys: list[str]):
    with session_scope() as session:
        repository = MirrorRepository(session)
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        total = repository.ledger_count(dest_key)
        last_day = repository.ledger_count_since(since, dest_key)
        last = repository.last_transfer_at()
        skipped = repository.count_skipped()
        per_source: dict[str, tuple[Progress | None, datetime | None]] = {}
        for key in source_keys:
            state = repository.find_sync_state(key, dest_key) if dest_key else None
            per_source[key] = (
                progress_from_state(state) if state is not None else None,
                repository.last_transfer_at(key),
            )
        return total, last_day, last, skipped, per_source


async def gather_status() -> StatusData:
    destination = runtime.destination()
    infos = runtime.sources()
    dest_key = str(destination.chat_id) if destination else None
    total, last_day, last, skipped, per_source = await asyncio.to_thread(
        _gather_sync, dest_key, [str(info.chat_id) for info in infos]
    )
    views = [SourceView(info, *per_source[str(info.chat_id)]) for info in infos]
    return StatusData(
        destination=destination,
        sources=views,
        total=total,
        last_day=last_day,
        last_transfer=last,
        skipped=skipped,
        paused=control.is_paused(),
        restricted_reason=control.restricted_reason(),
        jobs=registry.snapshot(),
        running_source=runtime.running_source_chat_id(),
        recent_errors=errors.recent(3),
    )


# -- rendering -------------------------------------------------------------------


def _state_line(data: StatusData) -> str:
    if data.restricted_reason:
        return "🔴 مقيَّد من تيليجرام"
    if data.paused:
        return "🟡 متوقف مؤقتًا"
    if data.jobs.active:
        return "🟢 يعمل — جاري نقل ملف"
    if data.jobs.queued:
        return "🟢 يعمل — بالانتظار"
    return "⚪ خامل (بانتظار وسائط جديدة)"


def source_icon(view: SourceView) -> str:
    if view.info.state == "error":
        return "⚠️"
    return "🟢" if view.info.enabled else "⏸"


def _history_label(view: SourceView) -> str:
    if not view.info.history_sync:
        return "السجل: معطّل"
    status = view.progress.status if view.progress else "idle"
    return f"السجل: {_HISTORY_LABELS.get(status, status)}"


def render_home(data: StatusData, now: datetime | None = None) -> str:
    lines = ["<b>📊 لوحة التحكم</b>", ""]
    lines.append(f"<b>الحالة:</b> {_state_line(data)}")
    if data.restricted_reason:
        lines.append(f"<i>{esc(data.restricted_reason)}</i>")
    if data.destination:
        lines.append(
            f"<b>الوجهة:</b> {esc(data.destination.title)} (<code>{data.destination.chat_id}</code>)"
        )
    else:
        lines.append("<b>الوجهة:</b> ❗ غير محددة — استخدم /setdest")

    enabled = sum(1 for view in data.sources if view.info.enabled)
    lines.append(f"<b>المصادر:</b> {len(data.sources)} ({enabled} مفعّل)")
    if not data.sources:
        lines.append("  لا توجد مصادر — أضف واحدًا بـ /addsource")
    for view in data.sources[:8]:
        lines.append(
            f"  {source_icon(view)} {esc(view.info.title)} — {_history_label(view)} · "
            f"آخر نقل: {texts.fmt_ago(view.last_transfer, now)}"
        )
    if len(data.sources) > 8:
        lines.append(f"  … و{len(data.sources) - 8} آخرين (/sources)")

    lines.append("")
    lines.append(
        f"<b>الإحصاءات:</b> الإجمالي {data.total:,} · آخر 24 ساعة {data.last_day:,} · "
        f"المتخطاة {data.skipped}"
    )
    lines.append(
        f"<b>آخر نقل:</b> {texts.fmt_ago(data.last_transfer, now)} · "
        f"<b>الطابور:</b> {len(data.jobs.queued)}"
    )
    lines.append(
        f"<b>حد الإشعار بالملفات الكبيرة:</b> "
        f"{limits.large_file_mb() or 'مغلق'}{' MB' if limits.large_file_mb() else ''}"
    )
    if data.recent_errors:
        lines.append("")
        lines.append("<b>⚠️ أخطاء حديثة:</b>")
        for stamp, text in data.recent_errors:
            ago = texts.fmt_ago(datetime.fromtimestamp(stamp, tz=timezone.utc), now)
            lines.append(f"  • {esc(text)} ({ago})")
    return "\n".join(lines)


def home_keyboard(paused: bool) -> InlineKeyboardMarkup:
    pause_button = (
        InlineKeyboardButton("▶️ استئناف", callback_data="p:resume")
        if paused
        else InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data="p:pause")
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 تحديث", callback_data="p:home"),
                InlineKeyboardButton("📡 مراقبة مباشرة", callback_data="lv:open"),
            ],
            [
                InlineKeyboardButton("📚 المصادر", callback_data="p:srcs:1"),
                InlineKeyboardButton("➕ إضافة مصدر", callback_data="p:add"),
            ],
            [
                InlineKeyboardButton("🎯 تغيير الوجهة", callback_data="p:dest"),
                InlineKeyboardButton("🚫 المتخطاة", callback_data="p:skipped:1"),
            ],
            [pause_button, InlineKeyboardButton("⏭ تخطي الحالي", callback_data="sk:0")],
            [InlineKeyboardButton("📖 المساعدة", callback_data="p:help")],
        ]
    )


def _page(items: list, page: int, size: int) -> tuple[list, int, int]:
    pages = max(1, ceil(len(items) / size))
    page = max(1, min(page, pages))
    start = (page - 1) * size
    return items[start : start + size], page, pages


def _nav_row(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("◀ السابق", callback_data=f"{prefix}:{page - 1}"))
    if page < pages:
        row.append(InlineKeyboardButton("التالي ▶", callback_data=f"{prefix}:{page + 1}"))
    return row


def render_sources(data: StatusData, page: int) -> tuple[str, InlineKeyboardMarkup]:
    items, page, pages = _page(data.sources, page, SOURCES_PER_PAGE)
    lines = [f"<b>📚 المصادر</b> ({len(data.sources)}) — صفحة {page}/{pages}", ""]
    if not data.sources:
        lines.append("لا توجد مصادر بعد. أضف واحدًا بـ /addsource.")
    rows: list[list[InlineKeyboardButton]] = []
    for view in items:
        info = view.info
        lines.append(
            f"{source_icon(view)} <b>{esc(info.title)}</b>\n"
            f"   <code>{info.chat_id}</code> · {_history_label(view)} · "
            f"آخر نقل: {texts.fmt_ago(view.last_transfer)}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"{source_icon(view)} {info.title[:32]}", callback_data=f"p:src:{info.id}"
                )
            ]
        )
    nav = _nav_row("p:srcs", page, pages)
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton("➕ إضافة مصدر", callback_data="p:add"),
            InlineKeyboardButton("🏠 الرئيسية", callback_data="p:home"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_source_detail(view: SourceView) -> tuple[str, InlineKeyboardMarkup]:
    info, progress = view.info, view.progress
    lines = [
        f"{source_icon(view)} <b>{esc(info.title)}</b>",
        f"المعرّف: <code>{info.chat_id}</code>",
        f"الحالة: {'مفعّل' if info.enabled else 'متوقف'}"
        + (f" — ⚠️ {esc(info.last_error)}" if info.state == "error" and info.last_error else ""),
        f"سجل الرسائل: {'مفعّل' if info.history_sync else 'معطّل'} ({_history_label(view)})",
    ]
    if progress:
        cursor = progress.resume_before if progress.resume_before is not None else "—"
        lines.append(
            f"المؤشر: <code>{cursor}</code> · فُحصت {progress.processed:,} · "
            f"نُقلت {progress.transferred:,} · مكررة {progress.duplicates:,}"
        )
        if progress.last_error:
            lines.append(f"آخر خطأ: {esc(progress.last_error)}")
    lines.append(f"آخر نقل: {texts.fmt_ago(view.last_transfer)}")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏸ إيقاف المصدر" if info.enabled else "▶️ تفعيل المصدر",
                    callback_data=f"p:tog:{info.id}",
                ),
                InlineKeyboardButton(
                    "📚 تعطيل السجل" if info.history_sync else "📚 تفعيل السجل",
                    callback_data=f"p:hist:{info.id}",
                ),
            ],
            [
                InlineKeyboardButton("🔄 إعادة مزامنة", callback_data=f"p:rs:{info.id}"),
                InlineKeyboardButton("🗑 حذف", callback_data=f"p:rm:{info.id}"),
            ],
            [InlineKeyboardButton("◀ رجوع", callback_data="p:srcs:1")],
        ]
    )
    return "\n".join(lines), keyboard


def confirm_keyboard(yes_data: str, no_data: str = "p:no") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ نعم، تأكيد", callback_data=yes_data),
                InlineKeyboardButton("❌ إلغاء", callback_data=no_data),
            ]
        ]
    )


def render_skipped(
    rows: list[SkippedView], total: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, ceil(total / SKIPPED_PER_PAGE))
    page = max(1, min(page, pages))
    lines = [f"<b>🚫 الملفات المتخطاة</b> ({total}) — صفحة {page}/{pages}", ""]
    if not rows:
        lines.append("لا توجد ملفات متخطاة 🎉")
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        source = runtime.get_source_by_chat(int(row.source_chat_id))
        title = source.title if source else row.source_chat_id
        lines.append(
            f"#{row.id} · {texts.media_label(row.media_type)} · {texts.fmt_bytes(row.file_size)} · "
            f"{texts.REASON_LABELS.get(row.reason, esc(row.reason))} · "
            f"{texts.fmt_ago(row.created_at)}\n   {esc(title)} — رسالة {row.source_message_id}"
        )
        keyboard.append(
            [InlineKeyboardButton(f"🔁 إعادة المحاولة #{row.id}", callback_data=f"rt:{row.id}")]
        )
    nav = _nav_row("p:skipped", page, pages)
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🏠 الرئيسية", callback_data="p:home")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _skipped_sync(page: int) -> tuple[list[SkippedView], int]:
    with session_scope() as session:
        repository = MirrorRepository(session)
        total = repository.count_skipped()
        rows = repository.list_skipped(
            offset=(max(1, page) - 1) * SKIPPED_PER_PAGE, limit=SKIPPED_PER_PAGE
        )
        return (
            [
                SkippedView(
                    id=row.id,
                    source_chat_id=row.source_chat_id,
                    source_message_id=row.source_message_id,
                    media_type=row.media_type,
                    file_size=row.file_size,
                    reason=row.reason,
                    created_at=row.created_at,
                )
                for row in rows
            ],
            total,
        )


async def skipped_screen(page: int) -> tuple[str, InlineKeyboardMarkup]:
    rows, total = await asyncio.to_thread(_skipped_sync, page)
    return render_skipped(rows, total, page)


async def home_screen() -> tuple[str, InlineKeyboardMarkup]:
    data = await gather_status()
    return render_home(data), home_keyboard(data.paused)


async def sources_screen(page: int) -> tuple[str, InlineKeyboardMarkup]:
    return render_sources(await gather_status(), page)


async def source_screen(source_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    data = await gather_status()
    view = next((v for v in data.sources if v.info.id == source_id), None)
    return render_source_detail(view) if view else None
