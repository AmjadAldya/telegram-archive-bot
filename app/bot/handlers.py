"""Control-panel wiring: commands, inline callbacks, link pasting, live monitor.

`register_control_commands` attaches everything to `command_client` (the
dedicated control bot, or the userbot itself in Saved-Messages fallback mode).
Every handler is behind `owner_filter` - anyone else is ignored silently, they
never even get an error message. Reads of source chats always go through
`userbot_client`; a BotFather bot cannot see them.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from pyrogram import enums, filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import panel, texts
from app.bot.linkflow import (
    LinkQueue,
    LinkResult,
    LinkTask,
    TokenStore,
    force_store,
    run_link,
)
from app.bot.live import format_snapshot_text, live_view
from app.bot.texts import esc
from app.config.settings import SYNC_HISTORY_ON_START
from app.database.base import session_scope
from app.database.repositories import MirrorRepository
from app.mirror import control, limits, runtime
from app.mirror.backlog import reset_backlog
from app.mirror.dialogs import (
    ChatReferenceError,
    build_chat_keyboard,
    fetch_administered_chats,
    fetch_mirrorable_chats,
    format_page,
    is_administered,
    picker_chats,
    resolve_reference,
)
from app.mirror.jobs import Job, registry
from app.mirror.links import MessageLink, extract_links
from app.mirror.listener import handle_incoming_media
from app.mirror.sources import (
    ChatChoice,
    InvitePending,
    SourceError,
    join_and_resolve,
    resolve_source,
    verify_readable,
)
from app.services.logger import logger

HTML = enums.ParseMode.HTML
PLAIN = enums.ParseMode.DISABLED

CALLBACK_RE = r"^(sel|pg|cancel|p|lv|sk|fx|rt|jn):"
invite_store = TokenStore()

_OTHER_COMMANDS_RE = ("/",)


def _link_candidate(_, __, message) -> bool:
    """Messages that may carry links: plain text/captions, plus /convert. Other commands win."""
    text = (getattr(message, "text", None) or "").lstrip()
    if text.startswith("/") and not text.lower().startswith("/convert"):
        return False
    return bool(extract_links(message)) or text.lower().startswith("/convert")


link_candidate = filters.create(_link_candidate, "LinkCandidate")


def make_saved_messages_scope():
    """Fallback mode (no BOT_TOKEN): only react inside the account's own Saved Messages."""

    def in_saved_messages(_, client, message) -> bool:
        me = getattr(client, "me", None)
        chat = getattr(message, "chat", None)
        return bool(me is not None and chat is not None and chat.id == me.id)

    return filters.create(in_saved_messages, "SavedMessagesScope")


@dataclass(slots=True)
class BotContext:
    command_client: object
    userbot: object
    owner_chat_id: int | str
    link_queue: LinkQueue


async def _safe_edit(target, text: str, markup=None) -> None:
    """Edit a Message or reply to a CallbackQuery, tolerating 'not modified' and FloodWait."""
    editor = getattr(target, "edit_message_text", None) or target.edit_text
    try:
        await editor(text, parse_mode=HTML, reply_markup=markup)
    except MessageNotModified:
        pass
    except FloodWait as exc:
        logger.warning("Edit hit FloodWait %ss; skipped", exc.value)


async def _reply(message, text: str, markup=None, *, quote: bool = False):
    return await message.reply(text, parse_mode=HTML, reply_markup=markup, quote=quote)


def _home_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="p:home")]])


def _find_source(ref: str):
    """Find a configured source by its list id, Telegram chat id, or -100-prefixed id."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        number = int(ref)
        found = runtime.get_source(number) or runtime.get_source_by_chat(number)
        if found is None and number > 0:
            found = runtime.get_source_by_chat(int(f"-100{number}"))
        return found
    return None


def register_control_commands(
    command_client,
    userbot_client,
    owner_filter,
    scope_filter=None,
    owner_chat_id: int | str = "me",
) -> BotContext:
    scope = scope_filter if scope_filter is not None else filters.private
    guard = owner_filter & scope

    # -- shared flows ------------------------------------------------------------

    async def after_config_change() -> None:
        await runtime.ensure_backlog_running(userbot_client, SYNC_HISTORY_ON_START)

    async def finalize_source(choice: ChatChoice) -> tuple[str, InlineKeyboardMarkup]:
        destination = runtime.destination()
        if destination is not None and choice.chat_id == destination.chat_id:
            return "❌ لا يمكن أن يكون المصدر هو قناة الوجهة نفسها.", _home_button()
        try:
            await verify_readable(userbot_client, choice.chat_id)
        except SourceError as exc:
            return f"❌ {esc(exc)}", _home_button()

        info, created = await runtime.add_source(choice.chat_id, choice.title)
        await after_config_change()
        lines = [
            ("✅ تمت إضافة المصدر" if created else "ℹ️ المصدر موجود مسبقًا (حدّثت بياناته)")
            + f": <b>{esc(info.title)}</b> (<code>{info.chat_id}</code>)"
        ]
        if destination is None:
            lines.append("❗ لم تحدد الوجهة بعد — استخدم /setdest.")
        if not SYNC_HISTORY_ON_START:
            lines.append(
                "ℹ️ سيُنقل الجديد فقط. لفحص السجل القديم استخدم /resync "
                f"{info.id} (SYNC_HISTORY_ON_START=false)."
            )
        return "\n".join(lines), InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📚 المصادر", callback_data="p:srcs:1"),
                    InlineKeyboardButton("🏠 الرئيسية", callback_data="p:home"),
                ]
            ]
        )

    async def add_source_by_ref(ref: str) -> tuple[str, InlineKeyboardMarkup]:
        try:
            resolved = await resolve_source(userbot_client, ref)
        except SourceError as exc:
            return f"❌ {esc(exc)}", _home_button()
        if isinstance(resolved, InvitePending):
            token = invite_store.put(resolved.link)
            return (
                f"الحساب ليس عضوًا في «{esc(resolved.title)}» بعد.\nهل تريد الانضمام إليها لتصبح مصدرًا؟",
                panel.confirm_keyboard(f"jn:{token}"),
            )
        return await finalize_source(resolved)

    async def source_picker_markup():
        chats = await fetch_mirrorable_chats(userbot_client)
        return chats, build_chat_keyboard(chats, role="src", page=1) if chats else None

    async def start_dest_change(
        chat_id: int, title: str
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        if not await is_administered(userbot_client, chat_id):
            return "❌ يجب أن يكون الحساب مالكًا أو مشرفًا في القناة لنقل الوسائط إليها.", None
        current = runtime.destination()
        if current is not None and current.chat_id != chat_id:
            return (
                (
                    f"⚠️ الوجهة الحالية: <b>{esc(current.title)}</b>.\n"
                    f"تغييرها إلى <b>{esc(title)}</b> يعني أن الأرشفة تبدأ من جديد للوجهة الجديدة "
                    "(منع التكرار محفوظ لكل وجهة). هل أنت متأكد؟"
                ),
                panel.confirm_keyboard(f"p:dsty:{chat_id}"),
            )
        return await apply_dest(chat_id, title), _home_button()

    async def apply_dest(chat_id: int, title: str) -> str:
        await runtime.set_dest(chat_id, title)
        await after_config_change()
        return f"✅ تم تحديد الوجهة: <b>{esc(title)}</b> (<code>{chat_id}</code>)"

    async def submit_link(link: MessageLink, status, *, force: bool = False, kind: str = "link"):
        job = registry.create(kind, link.chat_ref, str(link.chat_ref), link.message_id)
        task = LinkTask(link=link, job=job, status=status, force=force, kind=kind)
        return await ctx.link_queue.submit(task)

    # -- link queue runner ---------------------------------------------------------

    async def run_task(task: LinkTask) -> None:
        def on_flood(seconds: float) -> None:
            asyncio.ensure_future(
                _safe_edit(
                    task.status,
                    f"⏳ تيليجرام طلب الانتظار {int(seconds)} ثانية — سأعيد المحاولة تلقائيًا…",
                )
            )

        task.job.on_flood = on_flood
        await _safe_edit(task.status, "⏳ جارٍ النقل…")
        result: LinkResult = await run_link(userbot_client, task)
        markup = None
        if result.can_force:
            token = force_store.put(task.link)
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📥 نقل رغم ذلك", callback_data=f"fx:{token}")]]
            )
        await _safe_edit(task.status, f"{result.text}\n<code>{esc(task.link.key)}</code>", markup)

    ctx = BotContext(
        command_client=command_client,
        userbot=userbot_client,
        owner_chat_id=owner_chat_id,
        link_queue=LinkQueue(run_task),
    )

    async def notify_large_file(job: Job) -> None:
        text = (
            "📦 <b>ملف كبير بدأ نقله</b>\n"
            f"{texts.media_label(job.media_type)} · {texts.fmt_bytes(job.file_size)}\n"
            + (
                f"الملف: <code>{esc(texts.shorten(job.file_name))}</code>\n"
                if job.file_name
                else ""
            )
            + f"المصدر: {esc(job.source_title)} · رسالة <code>{job.message_id}</code>\n"
            "لن يتوقف النقل ما لم تضغط تخطي."
        )
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⏭ تخطي هذا الملف", callback_data=f"sk:{job.id}")]]
        )
        try:
            await command_client.send_message(
                owner_chat_id, text, parse_mode=HTML, reply_markup=markup
            )
        except Exception:  # noqa: BLE001 - a failed notice must never affect the transfer
            logger.warning("Could not send the large-file notice")

    registry.on_large_file = notify_large_file

    # -- commands --------------------------------------------------------------------

    @command_client.on_message(guard & filters.command(["help", "start"]))
    async def help_command(_, message):
        await _reply(message, texts.HELP_HTML)

    @command_client.on_message(guard & filters.command("panel"))
    async def panel_command(_, message):
        text, markup = await panel.home_screen()
        await _reply(message, text, markup)

    @command_client.on_message(guard & filters.command("status"))
    async def status_command(_, message):
        data = await panel.gather_status()
        await _reply(message, panel.render_home(data))

    @command_client.on_message(guard & filters.command("chats"))
    async def chats_command(_, message):
        command = getattr(message, "command", []) or []
        page = int(command[1]) if len(command) > 1 and command[1].isdigit() else 1
        await message.reply("⏳ أجلب مجموعاتك وقنواتك…", parse_mode=PLAIN)
        chats = await fetch_mirrorable_chats(userbot_client)
        await message.reply(format_page(chats, page), parse_mode=PLAIN)

    @command_client.on_message(guard & filters.command(["addsource", "setsource"]))
    async def addsource_command(_, message):
        command = getattr(message, "command", []) or []
        if len(command) >= 2:
            text, markup = await add_source_by_ref(" ".join(command[1:]))
            await _reply(message, text, markup)
            return
        await message.reply("⏳ أجلب مجموعاتك وقنواتك…", parse_mode=PLAIN)
        chats, markup = await source_picker_markup()
        if not chats:
            await message.reply("لا توجد مجموعات أو قنوات في هذا الحساب.", parse_mode=PLAIN)
            return
        await message.reply(
            "اختر المجموعة/القناة التي تريد النقل <b>منها</b> (أو أرسل "
            "<code>/addsource رقم|@username|رابط دعوة</code>):",
            parse_mode=HTML,
            reply_markup=markup,
        )

    @command_client.on_message(guard & filters.command("sources"))
    async def sources_command(_, message):
        text, markup = await panel.sources_screen(1)
        await _reply(message, text, markup)

    @command_client.on_message(guard & filters.command("removesource"))
    async def removesource_command(_, message):
        command = getattr(message, "command", []) or []
        if len(command) < 2:
            text, markup = await panel.sources_screen(1)
            await _reply(message, "اختر المصدر ثم اضغط «حذف»:\n\n" + text, markup)
            return
        info = _find_source(" ".join(command[1:]))
        if info is None:
            await _reply(message, "❌ لم أجد هذا المصدر. استخدم /sources لرؤية الأرقام.")
            return
        await _reply(
            message,
            f"🗑 حذف المصدر <b>{esc(info.title)}</b>؟\n"
            "يتوقف النقل منه، وتبقى الملفات المؤرشفة وسجل منع التكرار كما هي.",
            panel.confirm_keyboard(f"p:rmy:{info.id}"),
        )

    @command_client.on_message(guard & filters.command("setdest"))
    async def setdest_command(_, message):
        command = getattr(message, "command", []) or []
        if len(command) >= 2:
            try:
                chat_id, title = await resolve_reference(userbot_client, command[1])
            except ChatReferenceError as exc:
                await message.reply(str(exc), parse_mode=PLAIN)
                return
            text, markup = await start_dest_change(chat_id, title)
            await _reply(message, text, markup)
            return
        await message.reply("⏳ أجلب القنوات التي تملكها أو تشرف عليها…", parse_mode=PLAIN)
        chats = await fetch_administered_chats(userbot_client)
        if not chats:
            await message.reply("لا توجد مجموعات/قنوات تملكها أو تشرف عليها.", parse_mode=PLAIN)
            return
        await message.reply(
            "اختر القناة التي تُحفظ فيها الوسائط (<b>الوجهة</b>):",
            parse_mode=HTML,
            reply_markup=build_chat_keyboard(chats, role="dst", page=1),
        )

    @command_client.on_message(guard & filters.command("pause"))
    async def pause_command(_, message):
        control.pause()
        await _reply(message, "⏸ تم إيقاف النقل مؤقتًا. أرسل /resume للمتابعة.")

    @command_client.on_message(guard & filters.command("resume"))
    async def resume_command(_, message):
        control.resume()
        await _reply(message, "▶️ تم استئناف النقل.")

    @command_client.on_message(guard & filters.command("resync"))
    async def resync_command(_, message):
        if runtime.destination() is None or not runtime.sources():
            await _reply(message, "❗ حدّد الوجهة وأضف مصدرًا أولًا (/setdest ثم /addsource).")
            return
        command = getattr(message, "command", []) or []
        if len(command) >= 2:
            info = _find_source(" ".join(command[1:]))
            if info is None:
                await _reply(message, "❌ لم أجد هذا المصدر. استخدم /sources لرؤية الأرقام.")
                return
            target, label = info.id, f"المصدر <b>{esc(info.title)}</b>"
        else:
            target, label = 0, "<b>كل المصادر المفعّلة</b>"
        await _reply(
            message,
            f"🔄 إعادة فحص السجل لـ {label} من أحدث رسالة؟\n"
            "آمن: الملفات المؤرشفة سابقًا لا تُرسل مرة أخرى.",
            panel.confirm_keyboard(f"p:rsy:{target}"),
        )

    @command_client.on_message(guard & link_candidate)
    async def link_message(_, message):
        links = extract_links(message)
        if not links:
            await _reply(
                message, "الاستخدام: /convert &lt;رابط t.me لرسالة&gt; — أو ألصق الرابط مباشرة."
            )
            return
        if runtime.destination() is None:
            await _reply(message, "❗ الوجهة غير محددة — استخدم /setdest أولًا.")
            return
        for link in links:
            status = await _reply(
                message, f"⏳ في الطابور… <code>{esc(link.key)}</code>", quote=True
            )
            await submit_link(link, status)

    @command_client.on_message(guard & filters.command("live"))
    async def live_command(_, message):
        await live_view.open(command_client, message.chat.id)

    @command_client.on_message(guard & filters.command("queue"))
    async def queue_command(_, message):
        await _reply(message, format_snapshot_text())

    @command_client.on_message(guard & filters.command("skip"))
    async def skip_command(_, message):
        count = registry.request_skip()
        await _reply(
            message,
            "⏭ جارٍ تخطي الملف الحالي…" if count else "لا يوجد ملف قيد النقل حاليًا.",
        )

    @command_client.on_message(guard & filters.command("skipped"))
    async def skipped_command(_, message):
        text, markup = await panel.skipped_screen(1)
        await _reply(message, text, markup)

    @command_client.on_message(guard & filters.command("limit"))
    async def limit_command(_, message):
        command = getattr(message, "command", []) or []
        args = command[1:]
        if not args:
            large, auto = limits.large_file_mb(), limits.auto_skip_mb()
            await _reply(
                message,
                "<b>حدود الحجم</b>\n"
                f"• إشعار الملفات الكبيرة: {f'{large} MB' if large else 'مغلق'}\n"
                f"• التخطي التلقائي: {f'فوق {auto} MB' if auto else 'مغلق'}\n\n"
                "<code>/limit 800</code> — غيّر حد الإشعار (0 = إغلاق)\n"
                "<code>/limit auto 2000</code> — تخطٍّ تلقائي فوق هذا الحجم (0 = إغلاق)",
            )
            return
        auto_mode = args[0].lower() == "auto"
        value_text = args[1] if auto_mode and len(args) > 1 else args[0]
        if auto_mode and len(args) < 2 or not value_text.isdigit():
            await _reply(
                message, "❌ القيمة يجب أن تكون رقمًا بالميغابايت، مثل <code>/limit 800</code>."
            )
            return
        value = int(value_text)
        if auto_mode:
            await limits.set_auto_skip_mb(value)
            await _reply(
                message,
                f"✅ التخطي التلقائي: {'فوق ' + str(value) + ' MB' if value else 'مغلق'}",
            )
        else:
            await limits.set_large_file_mb(value)
            await _reply(
                message, f"✅ حد إشعار الملفات الكبيرة: {f'{value} MB' if value else 'مغلق'}"
            )

    # -- callbacks ---------------------------------------------------------------------

    async def show(cq, screen: tuple[str, InlineKeyboardMarkup | None]) -> None:
        await _safe_edit(cq, screen[0], screen[1])
        await cq.answer()

    async def do_picker(cq, action: str, role: str, rest: list[str]) -> None:
        if action == "cancel":
            await _safe_edit(cq, "تم الإلغاء.")
            await cq.answer()
            return
        if action == "pg":
            chats = picker_chats(role)
            await cq.edit_message_reply_markup(build_chat_keyboard(chats, role, int(rest[0])))
            await cq.answer()
            return
        chat_id = int(rest[0])
        match = next((chat for chat in picker_chats(role) if chat.id == chat_id), None)
        title = match.title if match else str(chat_id)
        if role == "dst":
            text, markup = await start_dest_change(chat_id, title)
            if markup is None:
                await cq.answer("ليس لديك صلاحية الكتابة هناك.", show_alert=True)
                return
            await show(cq, (text, markup))
            return
        kind = match.kind if match else "channel"
        text, markup = await finalize_source(ChatChoice(chat_id, title, kind))
        await show(cq, (text, markup))

    async def do_panel(cq, parts: list[str]) -> None:
        action = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        if action == "home" or action == "no":
            await show(cq, await panel.home_screen())
        elif action == "help":
            await show(cq, (texts.HELP_HTML, _home_button()))
        elif action == "srcs":
            await show(cq, await panel.sources_screen(int(arg or 1)))
        elif action == "src":
            screen = await panel.source_screen(int(arg))
            await show(cq, screen or ("❌ هذا المصدر لم يعد موجودًا.", _home_button()))
        elif action == "tog":
            info = runtime.get_source(int(arg))
            if info is not None:
                await runtime.set_source_enabled(info.id, not info.enabled)
                await after_config_change()
            screen = await panel.source_screen(int(arg))
            await show(cq, screen or ("❌ هذا المصدر لم يعد موجودًا.", _home_button()))
        elif action == "hist":
            info = runtime.get_source(int(arg))
            if info is not None:
                await runtime.set_source_history_sync(info.id, not info.history_sync)
                if not info.history_sync:  # was off, now on
                    await after_config_change()
            screen = await panel.source_screen(int(arg))
            await show(cq, screen or ("❌ هذا المصدر لم يعد موجودًا.", _home_button()))
        elif action == "rm":
            info = runtime.get_source(int(arg))
            if info is None:
                await show(cq, ("❌ هذا المصدر لم يعد موجودًا.", _home_button()))
                return
            await show(
                cq,
                (
                    (
                        f"🗑 حذف المصدر <b>{esc(info.title)}</b>؟\n"
                        "يتوقف النقل منه، وتبقى الملفات المؤرشفة وسجل منع التكرار كما هي."
                    ),
                    panel.confirm_keyboard(f"p:rmy:{info.id}", f"p:src:{info.id}"),
                ),
            )
        elif action == "rmy":
            info = runtime.get_source(int(arg))
            removed = await runtime.remove_source(int(arg)) if info else False
            note = (
                f"✅ تم حذف المصدر <b>{esc(info.title)}</b>."
                if removed and info
                else "ℹ️ المصدر غير موجود."
            )
            await show(cq, (note, _home_button()))
        elif action == "rs":
            target = int(arg)
            info = runtime.get_source(target) if target else None
            label = f"المصدر <b>{esc(info.title)}</b>" if info else "<b>كل المصادر المفعّلة</b>"
            await show(
                cq,
                (
                    (
                        f"🔄 إعادة فحص السجل لـ {label} من أحدث رسالة؟\n"
                        "آمن: الملفات المؤرشفة سابقًا لا تُرسل مرة أخرى."
                    ),
                    panel.confirm_keyboard(
                        f"p:rsy:{target}", f"p:src:{target}" if target else "p:home"
                    ),
                ),
            )
        elif action == "rsy":
            target = int(arg)
            info = runtime.get_source(target) if target else None
            chat_ids = [info.chat_id] if info else None
            restarted = await reset_backlog(userbot_client, chat_ids)
            note = (
                f"✅ بدأت إعادة الفحص لـ {len(restarted)} مصدر."
                if restarted
                else "ℹ️ لا توجد مصادر مفعّلة لإعادة فحصها."
            )
            await show(cq, (note, _home_button()))
        elif action == "add":
            chats, markup = await source_picker_markup()
            if not chats:
                await show(cq, ("لا توجد مجموعات أو قنوات في هذا الحساب.", _home_button()))
                return
            await show(cq, ("اختر المجموعة/القناة التي تريد النقل <b>منها</b>:", markup))
        elif action == "dest":
            chats = await fetch_administered_chats(userbot_client)
            if not chats:
                await show(cq, ("لا توجد قنوات تملكها أو تشرف عليها.", _home_button()))
                return
            await show(
                cq,
                (
                    "اختر قناة <b>الوجهة</b> (التي تُحفظ فيها الوسائط):",
                    build_chat_keyboard(chats, role="dst", page=1),
                ),
            )
        elif action == "dsty":
            chat_id = int(arg)
            title = str(chat_id)
            with contextlib.suppress(Exception):
                title = (await userbot_client.get_chat(chat_id)).title or title
            if not await is_administered(userbot_client, chat_id):
                await cq.answer("ليس لديك صلاحية الكتابة هناك.", show_alert=True)
                return
            await show(cq, (await apply_dest(chat_id, title), _home_button()))
        elif action == "pause":
            control.pause()
            await show(cq, await panel.home_screen())
        elif action == "resume":
            control.resume()
            await show(cq, await panel.home_screen())
        elif action == "skipped":
            await show(cq, await panel.skipped_screen(int(arg or 1)))
        else:
            await cq.answer()

    async def do_live(cq, action: str) -> None:
        if action == "open":
            await live_view.open(command_client, cq.message.chat.id)
            await cq.answer("📡 فتحت المراقبة المباشرة")
        elif action == "skip":
            count = registry.request_skip()
            await cq.answer("⏭ جارٍ التخطي…" if count else "لا يوجد ملف قيد النقل.")
        elif action == "pause":
            control.pause()
            await live_view.refresh_now()
            await cq.answer("⏸ تم الإيقاف المؤقت")
        elif action == "resume":
            control.resume()
            await live_view.refresh_now()
            await cq.answer("▶️ تم الاستئناف")
        elif action == "ref":
            await live_view.refresh_now()
            await cq.answer("🔄")
        elif action == "stop":
            await live_view.stop("⏹ تم إيقاف العرض المباشر. أرسل /live لإعادة فتحه.")
            await cq.answer("تم الإيقاف")
        else:
            await cq.answer()

    async def do_skip(cq, arg: str) -> None:
        job_id = int(arg) if arg.isdigit() else 0
        count = registry.request_skip(job_id or None)
        await cq.answer("⏭ جارٍ التخطي…" if count else "انتهى هذا الملف بالفعل.", show_alert=False)

    async def do_force(cq, token: str) -> None:
        link = force_store.get(token)
        if link is None:
            await cq.answer("انتهت صلاحية هذا الزر — ألصق الرابط من جديد.", show_alert=True)
            return
        await cq.answer("📥 جارٍ النقل…")
        await _safe_edit(cq.message, f"⏳ في الطابور… <code>{esc(link.key)}</code>")
        await submit_link(link, cq.message, force=True)

    async def do_retry(cq, arg: str) -> None:
        def load():
            with session_scope() as session:
                row = MirrorRepository(session).get_skipped(int(arg))
                if row is None or row.resolved_at is not None:
                    return None
                return row.source_chat_id, row.source_message_id

        found = await asyncio.to_thread(load)
        if found is None:
            await cq.answer("لم يعد هذا العنصر موجودًا.", show_alert=True)
            return
        chat_id, message_id = found
        await cq.answer("🔁 أعدت المحاولة")
        status = await _reply(
            cq.message, f"⏳ إعادة محاولة… <code>{esc(chat_id)}/{message_id}</code>"
        )
        await submit_link(MessageLink(int(chat_id), message_id), status, kind="retry")

    async def do_join(cq, token: str) -> None:
        link = invite_store.get(token)
        if link is None:
            await cq.answer("انتهت صلاحية هذا الزر.", show_alert=True)
            return
        try:
            choice = await join_and_resolve(userbot_client, link)
        except SourceError as exc:
            await show(cq, (f"❌ {esc(exc)}", _home_button()))
            return
        await show(cq, await finalize_source(choice))

    @command_client.on_callback_query(owner_filter & filters.regex(CALLBACK_RE))
    async def callbacks(_, cq):
        action, _sep, rest = (cq.data or "").partition(":")
        try:
            if action in ("sel", "pg", "cancel"):
                role, *args = rest.split(":")
                await do_picker(cq, action, role, args)
            elif action == "p":
                await do_panel(cq, rest.split(":"))
            elif action == "lv":
                await do_live(cq, rest)
            elif action == "sk":
                await do_skip(cq, rest)
            elif action == "fx":
                await do_force(cq, rest)
            elif action == "rt":
                await do_retry(cq, rest)
            elif action == "jn":
                await do_join(cq, rest)
        except Exception:  # noqa: BLE001 - a broken button must not kill the handler
            logger.exception("Callback %s failed", action)
            with contextlib.suppress(Exception):
                await cq.answer("حدث خطأ غير متوقع.", show_alert=True)

    return ctx


def register_media_listener(userbot_client) -> None:
    """Mirror new media in any enabled source. Always lives on the userbot -
    it's the only client with access to the source chats' messages."""

    @userbot_client.on_message(filters.media)
    async def mirror_media(client, message):
        # No static chat filter: sources are chosen at runtime, so
        # handle_incoming_media checks the enabled-source cache itself.
        await handle_incoming_media(client, message)
