"""Arabic UI text and small formatting helpers for the control bot.

Everything the owner reads lives here so wording stays consistent. Messages use
Telegram HTML; any chat title or other user-controlled text must go through
`esc()` before being interpolated.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

# (command, short Arabic description) - names stay English so they are tappable.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("panel", "لوحة التحكم الرئيسية"),
    ("status", "حالة النظام الآن"),
    ("live", "مراقبة النقل لحظيًا"),
    ("queue", "عرض الطابور الحالي"),
    ("sources", "عرض المصادر"),
    ("addsource", "إضافة مصدر جديد"),
    ("removesource", "حذف مصدر"),
    ("setdest", "تحديد قناة الوجهة"),
    ("pause", "إيقاف النقل مؤقتًا"),
    ("resume", "استئناف النقل"),
    ("resync", "إعادة مزامنة السجل"),
    ("convert", "نقل رسالة عبر رابطها"),
    ("skip", "تخطي الملف الحالي"),
    ("skipped", "الملفات المتخطاة"),
    ("limit", "حد حجم الملفات الكبيرة"),
    ("chats", "عرض مجموعاتك وقنواتك"),
    ("help", "المساعدة"),
]

HELP_HTML = (
    "<b>🤖 لوحة تحكم أرشيف الوسائط</b>\n"
    "الأوامر تعمل لمالك الحساب فقط.\n\n"
    "<b>📚 المصادر</b>\n"
    "/addsource — إضافة مصدر (اختيار من القائمة، أو رقم/@username/رابط دعوة)\n"
    "/sources — عرض المصادر وحالتها\n"
    "/removesource — حذف مصدر\n"
    "/chats — عرض مجموعاتك وقنواتك\n\n"
    "<b>🎯 الوجهة</b>\n"
    "/setdest — تحديد القناة التي تُحفظ فيها الوسائط\n\n"
    "<b>🔄 المزامنة</b>\n"
    "/resync [مصدر] — إعادة فحص السجل من الأحدث\n"
    "/pause — إيقاف النقل مؤقتًا\n"
    "/resume — استئناف النقل\n\n"
    "<b>🔗 النقل</b>\n"
    "ألصق أي رابط <code>t.me</code> لرسالة وسيتم نقلها فورًا بلا أمر.\n"
    "/convert &lt;رابط&gt; — الأمر نفسه\n\n"
    "<b>📡 المراقبة</b>\n"
    "/live — رسالة تتحدث لحظيًا بتقدم النقل\n"
    "/queue — لقطة للطابور\n"
    "/skip — تخطي الملف الجاري\n"
    "/skipped — الملفات المتخطاة (مع إعادة المحاولة)\n"
    "/limit [MB] — حد الإشعار بالملفات الكبيرة؛ "
    "<code>/limit auto MB</code> للتخطي التلقائي\n\n"
    "<b>📊 الحالة</b>\n"
    "/panel — لوحة بأزرار وملخص\n"
    "/status — ملخص نصي سريع"
)


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def shorten(value: str, limit: int = 60) -> str:
    """Trim long names for display, keeping the end (extensions matter)."""
    return value if len(value) <= limit else "…" + value[-(limit - 1) :]


def fmt_bytes(count: float | None) -> str:
    if count is None:
        return "؟"
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover


def fmt_speed(bytes_per_second: float | None) -> str:
    return "—" if not bytes_per_second else f"{fmt_bytes(bytes_per_second)}/s"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_bar(percent: float | None, width: int = 10) -> str:
    if percent is None:
        return "▱" * width + "  ؟"
    filled = max(0, min(width, round(percent / 100 * width)))
    return "▰" * filled + "▱" * (width - filled) + f" {percent:.0f}%"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def fmt_ago(moment: datetime | None, now: datetime | None = None) -> str:
    """'منذ 5 دقائق' style; SQLite hands back naive datetimes, which are UTC."""
    if moment is None:
        return "لا يوجد"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - _as_utc(moment)).total_seconds()))
    if seconds < 60:
        return "الآن"
    if seconds < 3600:
        return f"منذ {seconds // 60} د"
    if seconds < 86400:
        return f"منذ {seconds // 3600} س"
    return f"منذ {seconds // 86400} يوم"


MEDIA_LABELS = {
    "photo": "صورة",
    "video": "فيديو",
    "animation": "GIF",
    "document": "ملف",
    "audio": "صوت",
    "voice": "رسالة صوتية",
    "video_note": "فيديو دائري",
}


def media_label(media_type: str | None) -> str:
    return MEDIA_LABELS.get(media_type or "", "وسائط")


STAGE_LABELS = {"queued": "⏳ بالانتظار", "download": "⬇️ تنزيل", "upload": "⬆️ رفع"}
KIND_LABELS = {"live": "مباشر", "backlog": "سجل", "link": "رابط", "retry": "إعادة"}
REASON_LABELS = {
    "user_skip": "تخطّيته يدويًا",
    "timeout": "انتهت المهلة",
    "auto_skip": "تخطٍّ تلقائي (حجم كبير)",
    "error": "خطأ",
}
