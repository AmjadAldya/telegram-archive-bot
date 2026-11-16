from __future__ import annotations

import asyncio
from types import SimpleNamespace

from helpers import (
    FakeCallback,
    FakeChat,
    FakeFile,
    FakeIncoming,
    FakeMessage,
    FakeRegistrar,
    FakeSentMessage,
    FakeUserbot,
    make_chat,
)
from pyrogram import enums, filters
from pyrogram.errors import ChannelPrivate

from app.bot import handlers
from app.bot.texts import BOT_COMMANDS
from app.database.base import session_scope
from app.database.models import TransferredMedia
from app.database.repositories import MirrorRepository
from app.mirror import control, limits, runtime
from app.mirror.jobs import Job, Stage, registry

OWNER, STRANGER = 111, 222
DEST = -100222
SRC = -100999
LINK_CHAT = -1005551234
LINK_TEXT = "https://t.me/c/5551234/42"


def ledger_count() -> int:
    with session_scope() as session:
        return session.query(TransferredMedia).count()


class Harness:
    """Registers the handlers on a fake client and lets a test play the owner/others."""

    def __init__(self, userbot: FakeUserbot | None = None) -> None:
        self.bot = FakeRegistrar()
        self.userbot = userbot or FakeUserbot()
        self.ctx = handlers.register_control_commands(
            self.bot,
            self.userbot,
            owner_filter=filters.user(OWNER),
            scope_filter=filters.private,
            owner_chat_id=OWNER,
        )

    async def say(self, text: str, uid: int = OWNER, chat_type=enums.ChatType.PRIVATE):
        update = FakeIncoming(uid, text, chat_type=chat_type)
        fired = await self.bot.dispatch_message(update)
        return fired, update

    async def press(self, data: str, uid: int = OWNER, message: FakeSentMessage | None = None):
        update = FakeCallback(uid, data, message)
        fired = await self.bot.dispatch_callback(update)
        return fired, update

    async def settle(self, seconds: float = 0.15) -> None:
        await asyncio.sleep(seconds)  # let the link-queue worker run


def photo(message_id: int = 42, unique: str = "p1") -> FakeMessage:
    return FakeMessage(
        id=message_id, chat=FakeChat(id=LINK_CHAT, title="Random"), photo=FakeFile(unique)
    )


def with_destination(userbot: FakeUserbot | None = None) -> FakeUserbot:
    userbot = userbot or FakeUserbot()
    userbot.messages[(LINK_CHAT, 42)] = photo()
    userbot.admin_chats.add(DEST)
    userbot.chats[DEST] = make_chat(DEST, "Archive")
    return userbot


def run(coro):
    return asyncio.run(coro)


# -- owner only ---------------------------------------------------------------------------


COMMAND_TEXTS = [f"/{name}" for name, _ in BOT_COMMANDS] + [
    "/start",
    "/setsource -100999",
    "/convert " + LINK_TEXT,
    LINK_TEXT,
    "hello",
]
CALLBACK_DATA = [
    "p:home",
    "p:srcs:1",
    "p:src:1",
    "p:tog:1",
    "p:rmy:1",
    "p:rsy:0",
    "p:dsty:-100222",
    "p:pause",
    "lv:skip",
    "lv:stop",
    "sk:0",
    "fx:1",
    "rt:1",
    "jn:1",
    "sel:src:-100999",
    "sel:dst:-100222",
    "pg:src:2",
    "cancel:src",
]


def test_strangers_are_ignored_silently_for_every_command_and_button() -> None:
    async def scenario() -> None:
        harness = Harness(with_destination())
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC, "Source")

        for text in COMMAND_TEXTS:
            fired, update = await harness.say(text, uid=STRANGER)
            assert fired == [], text
            assert update.replies == [], text
        for data in CALLBACK_DATA:
            fired, update = await harness.press(data, uid=STRANGER)
            assert fired == [], data
            assert update.answers == [] and update.message.edits == [], data

        assert harness.bot.sent == []
        assert harness.userbot.get_messages_calls == []  # no link was ever fetched
        assert runtime.sources() and runtime.destination()  # nothing changed

    run(scenario())


def test_owner_messages_outside_a_private_chat_are_ignored() -> None:
    async def scenario() -> None:
        harness = Harness(with_destination())
        for text in ("/panel", LINK_TEXT):
            fired, update = await harness.say(text, chat_type=enums.ChatType.SUPERGROUP)
            assert fired == [] and update.replies == []

    run(scenario())


def test_saved_messages_scope_for_the_no_bot_fallback() -> None:
    scope = handlers.make_saved_messages_scope()
    bot = FakeRegistrar()

    async def check() -> tuple[bool, bool]:
        in_saved = FakeIncoming(OWNER, "x", chat_id=OWNER)  # chat id == own id: Saved Messages
        elsewhere = FakeIncoming(OWNER, "x", chat_id=555)  # e.g. a DM with a friend
        return await bot._matches(scope, in_saved), await bot._matches(scope, elsewhere)

    assert run(check()) == (True, False)


# -- help / panel / status ---------------------------------------------------------------------


def test_help_panel_status_and_lists_respond_in_arabic() -> None:
    async def scenario() -> None:
        harness = Harness()
        await runtime.set_dest(DEST, "Archive")
        await runtime.add_source(SRC, "Source A")

        fired, update = await harness.say("/start")
        assert fired == ["help_command"] and "المصادر" in update.replies[0].text
        assert "الوجهة" in update.replies[0].text and "المراقبة" in update.replies[0].text

        fired, update = await harness.say("/panel")
        text = update.replies[0].text
        assert fired == ["panel_command"]
        assert "لوحة التحكم" in text and "Source A" in text and "Archive" in text
        assert update.replies[0].reply_markup is not None

        fired, update = await harness.say("/status")
        assert fired == ["status_command"] and "الحالة" in update.replies[0].text

        for command, handler in (("/sources", "sources_command"), ("/queue", "queue_command")):
            fired, update = await harness.say(command)
            assert fired == [handler] and update.replies

    run(scenario())


def test_command_menu_is_english_names_with_arabic_descriptions() -> None:
    assert len(BOT_COMMANDS) == len({name for name, _ in BOT_COMMANDS})
    for name, description in BOT_COMMANDS:
        assert name.isascii() and name == name.lower()
        assert any("؀" <= ch <= "ۿ" for ch in description), name
        assert len(description) <= 256


# -- paste a link: no command, no confirmation --------------------------------------------------


def test_pasting_a_link_transfers_it_without_any_command() -> None:
    async def scenario():
        harness = Harness(with_destination())
        await runtime.set_dest(DEST, "Archive")
        harness.ctx.link_queue.start()

        fired, update = await harness.say(f"look at this {LINK_TEXT}")
        await harness.settle()
        await harness.ctx.link_queue.stop()
        return harness, fired, update

    harness, fired, update = run(scenario())

    assert fired == ["link_message"]
    status = update.replies[0]
    assert status.edits and "تم النقل" in status.text  # replied to the paste, then updated it
    assert ledger_count() == 1 and len(harness.userbot.send_calls) == 1


def test_several_links_in_one_message_are_processed_in_order() -> None:
    async def scenario():
        userbot = with_destination()
        userbot.messages[(LINK_CHAT, 43)] = photo(43, "p2")
        userbot.messages[(LINK_CHAT, 44)] = photo(44, "p3")
        harness = Harness(userbot)
        await runtime.set_dest(DEST, "Archive")
        harness.ctx.link_queue.start()
        _fired, update = await harness.say(
            "https://t.me/c/5551234/44 https://t.me/c/5551234/42 https://t.me/c/5551234/43"
        )
        await harness.settle(0.3)
        await harness.ctx.link_queue.stop()
        return userbot, update

    userbot, update = run(scenario())
    assert [call[1] for call in userbot.get_messages_calls] == [44, 42, 43]  # as pasted
    assert len(update.replies) == 3
    assert all("تم النقل" in reply.text for reply in update.replies)


def test_link_in_a_caption_or_hidden_text_link_is_detected() -> None:
    async def scenario():
        harness = Harness(with_destination())
        await runtime.set_dest(DEST, "Archive")
        harness.ctx.link_queue.start()

        captioned = FakeIncoming(OWNER, "")
        captioned.text = None
        captioned.caption = f"forwarded video {LINK_TEXT}"
        assert await harness.bot.dispatch_message(captioned) == ["link_message"]

        hidden = FakeIncoming(OWNER, "click here")
        hidden.entities = [SimpleNamespace(url=LINK_TEXT)]
        assert await harness.bot.dispatch_message(hidden) == ["link_message"]
        await harness.ctx.link_queue.stop()

    run(scenario())


def test_plain_chat_without_a_link_is_not_treated_as_a_link() -> None:
    async def scenario() -> None:
        harness = Harness(with_destination())
        for text in ("hello", "https://example.com/x", "https://t.me/somechannel"):
            fired, update = await harness.say(text)
            assert fired == [] and update.replies == []

    run(scenario())


def test_convert_is_still_an_alias_and_explains_its_usage() -> None:
    async def scenario():
        harness = Harness(with_destination())
        await runtime.set_dest(DEST, "Archive")
        harness.ctx.link_queue.start()
        _, usage = await harness.say("/convert")
        fired, update = await harness.say(f"/convert {LINK_TEXT}")
        await harness.settle()
        await harness.ctx.link_queue.stop()
        return fired, usage, update

    fired, usage, update = run(scenario())
    assert "الاستخدام" in usage.replies[0].text
    assert fired == ["link_message"] and "تم النقل" in update.replies[0].text


def test_other_commands_with_links_are_not_hijacked_by_the_link_flow() -> None:
    async def scenario() -> None:
        harness = Harness(with_destination())
        fired, _ = await harness.say("/addsource https://t.me/+AbCdEf123")
        assert fired == ["addsource_command"]

    run(scenario())


def test_link_without_a_destination_is_refused_politely() -> None:
    async def scenario():
        harness = Harness()
        fired, update = await harness.say(LINK_TEXT)
        return harness, fired, update

    harness, fired, update = run(scenario())
    assert fired == ["link_message"] and "الوجهة غير محددة" in update.replies[0].text
    assert harness.userbot.get_messages_calls == []


def test_duplicate_link_offers_transfer_anyway_and_the_button_forces_it() -> None:
    async def scenario():
        harness = Harness(with_destination())
        await runtime.set_dest(DEST, "Archive")
        harness.ctx.link_queue.start()
        await harness.say(LINK_TEXT)
        await harness.settle()
        _, second = await harness.say(LINK_TEXT)
        await harness.settle()

        status = second.replies[0]
        assert "موجود مسبقًا" in status.text
        button = status.reply_markup.inline_keyboard[0][0]
        assert button.callback_data.startswith("fx:")

        stranger_fired, _ = await harness.press(button.callback_data, uid=STRANGER, message=status)
        assert stranger_fired == []  # nobody else can force it

        fired, _cb = await harness.press(button.callback_data, message=status)
        await harness.settle()
        await harness.ctx.link_queue.stop()
        return harness, fired, status

    harness, fired, status = run(scenario())
    assert fired == ["callbacks"]
    assert "تم النقل" in status.text
    assert len(harness.userbot.send_calls) == 2 and ledger_count() == 1


def test_expired_force_button_is_handled() -> None:
    async def scenario():
        harness = Harness(with_destination())
        _fired, cb = await harness.press("fx:99999")
        return cb

    assert "انتهت صلاحية" in run(scenario()).answers[0][0]


# -- sources: add / list / remove ------------------------------------------------------------------


def userbot_with_source() -> FakeUserbot:
    userbot = with_destination()
    userbot.chats[SRC] = make_chat(SRC, "Source A")
    userbot.histories[SRC] = []
    return userbot


def test_addsource_by_id_verifies_readability_then_saves() -> None:
    async def scenario():
        harness = Harness(userbot_with_source())
        await runtime.set_dest(DEST, "Archive")
        _, update = await harness.say(f"/addsource {SRC}")
        return update

    update = run(scenario())
    assert "تمت إضافة المصدر" in update.replies[0].text
    assert [s.chat_id for s in runtime.sources()] == [SRC]


def test_setsource_remains_an_alias_that_adds() -> None:
    async def scenario():
        harness = Harness(userbot_with_source())
        await runtime.set_dest(DEST, "Archive")
        await harness.say(f"/setsource {SRC}")

    run(scenario())
    assert [s.chat_id for s in runtime.sources()] == [SRC]


def test_unreadable_unknown_or_destination_chats_are_rejected_and_not_saved() -> None:
    async def scenario():
        userbot = userbot_with_source()
        userbot.unreadable.add(SRC)
        harness = Harness(userbot)
        await runtime.set_dest(DEST, "Archive")
        _, unreadable = await harness.say(f"/addsource {SRC}")
        _, unknown = await harness.say("/addsource -100123")
        _, same_as_dest = await harness.say(f"/addsource {DEST}")
        return unreadable, unknown, same_as_dest

    unreadable, unknown, same_as_dest = run(scenario())
    assert "❌" in unreadable.replies[0].text and "ليس عضوًا" in unreadable.replies[0].text
    assert "❌" in unknown.replies[0].text
    assert "الوجهة نفسها" in same_as_dest.replies[0].text
    assert runtime.sources() == []


def test_addsource_without_arguments_shows_a_tappable_picker() -> None:
    async def scenario():
        userbot = userbot_with_source()
        userbot.dialog_chats = [make_chat(SRC, "Source A"), make_chat(-100777, "Other")]
        harness = Harness(userbot)
        await runtime.set_dest(DEST, "Archive")
        _, update = await harness.say("/addsource")
        picker = update.replies[-1]
        data = [row[0].callback_data for row in picker.reply_markup.inline_keyboard[:2]]
        _, cb = await harness.press(f"sel:src:{SRC}")
        return data, cb

    data, cb = run(scenario())
    assert data == [f"sel:src:{SRC}", "sel:src:-100777"]
    assert "تمت إضافة المصدر" in cb.message.text
    assert [s.chat_id for s in runtime.sources()] == [SRC]


def test_invite_link_asks_before_joining_then_adds_the_source() -> None:
    async def scenario():
        userbot = userbot_with_source()
        link = "https://t.me/+AbCdEf123"
        userbot.chats[link] = SimpleNamespace(id=None, title="Private Group")  # a preview
        harness = Harness(userbot)
        await runtime.set_dest(DEST, "Archive")
        _, update = await harness.say(f"/addsource {link}")
        ask = update.replies[0]
        assert "هل تريد الانضمام" in ask.text and userbot.joined == []
        token_button = ask.reply_markup.inline_keyboard[0][0]
        assert token_button.callback_data.startswith("jn:")

        userbot.chats[link] = make_chat(SRC, "Private Group")  # after joining
        _, cb = await harness.press(token_button.callback_data)
        return userbot, cb

    userbot, cb = run(scenario())
    assert userbot.joined == ["https://t.me/+AbCdEf123"]
    assert "تمت إضافة المصدر" in cb.message.text
    assert [s.chat_id for s in runtime.sources()] == [SRC]


def test_removing_a_source_needs_confirmation_and_keeps_the_ledger() -> None:
    async def scenario():
        harness = Harness(userbot_with_source())
        await runtime.set_dest(DEST, "Archive")
        info, _ = await runtime.add_source(SRC, "Source A")
        with session_scope() as session:  # something already archived from it
            MirrorRepository(session).record_transfer(
                file_unique_id="old",
                media_type="photo",
                source_chat_id=str(SRC),
                source_message_id=1,
                dest_chat_id=str(DEST),
                dest_message_id=None,
            )

        _, listing = await harness.say("/removesource")
        _, ask = await harness.say(f"/removesource {info.id}")
        confirm = ask.replies[0]
        assert "حذف المصدر" in confirm.text and runtime.sources()  # not removed yet

        await harness.press("p:no")  # cancel
        assert runtime.sources()
        await harness.press(f"p:rmy:{info.id}", uid=STRANGER)  # a stranger cannot confirm
        assert runtime.sources()

        _, done = await harness.press(f"p:rmy:{info.id}")
        return listing, confirm, done

    listing, confirm, done = run(scenario())
    assert "اختر المصدر" in listing.replies[0].text
    assert confirm.reply_markup.inline_keyboard[0][0].callback_data.startswith("p:rmy:")
    assert "تم حذف المصدر" in done.message.text
    assert runtime.sources() == []
    assert ledger_count() == 1  # archived files and dedup history survive


def test_removesource_unknown_reference() -> None:
    async def scenario():
        harness = Harness()
        _, update = await harness.say("/removesource 42")
        return update

    assert "لم أجد" in run(scenario()).replies[0].text


# -- destination --------------------------------------------------------------------------------


def test_setdest_sets_a_first_destination_directly_but_confirms_a_change() -> None:
    async def scenario():
        userbot = with_destination()
        other = -100888
        userbot.chats[other] = make_chat(other, "Other archive")
        userbot.admin_chats.add(other)
        harness = Harness(userbot)

        _, first = await harness.say(f"/setdest {DEST}")
        assert runtime.destination().chat_id == DEST

        _, change = await harness.say(f"/setdest {other}")
        ask = change.replies[0]
        assert "تبدأ من جديد" in ask.text and runtime.destination().chat_id == DEST  # not yet

        await harness.press(f"p:dsty:{other}", uid=STRANGER)
        assert runtime.destination().chat_id == DEST
        await harness.press(f"p:dsty:{other}")
        return first

    first = run(scenario())
    assert "تم تحديد الوجهة" in first.replies[0].text
    assert runtime.destination().chat_id == -100888


def test_setdest_requires_owner_or_admin_rights_in_the_chat() -> None:
    async def scenario():
        userbot = with_destination()
        userbot.admin_chats.clear()  # the account is only a regular member
        harness = Harness(userbot)
        _, update = await harness.say(f"/setdest {DEST}")
        return update

    update = run(scenario())
    assert "مالكًا أو مشرفًا" in update.replies[0].text
    assert runtime.destination() is None


# -- resync, pause, limits, skip ---------------------------------------------------------------------


def test_resync_asks_for_confirmation_then_resets_the_chosen_sources(monkeypatch) -> None:
    calls: list = []

    async def fake_reset(_client, chat_ids=None):
        calls.append(chat_ids)
        return chat_ids or [SRC]

    monkeypatch.setattr(handlers, "reset_backlog", fake_reset)

    async def scenario():
        harness = Harness(userbot_with_source())
        await runtime.set_dest(DEST, "Archive")
        info, _ = await runtime.add_source(SRC, "Source A")

        _, all_sources = await harness.say("/resync")
        _, one = await harness.say(f"/resync {info.id}")
        assert calls == []  # nothing happens before confirming
        await harness.press("p:rsy:0", uid=STRANGER)
        assert calls == []
        await harness.press("p:rsy:0")
        await harness.press(f"p:rsy:{info.id}")
        return all_sources, one

    all_sources, one = run(scenario())
    assert "كل المصادر" in all_sources.replies[0].text and "Source A" in one.replies[0].text
    assert calls == [None, [SRC]]


def test_resync_needs_a_configured_mirror() -> None:
    async def scenario():
        _, update = await Harness().say("/resync")
        return update

    assert "حدّد الوجهة" in run(scenario()).replies[0].text


def test_pause_and_resume_commands_and_panel_buttons() -> None:
    async def scenario():
        harness = Harness()
        await runtime.set_dest(DEST, "Archive")
        await harness.say("/pause")
        paused = control.is_paused()
        _, panel_paused = await harness.press("p:resume")
        resumed = not control.is_paused()
        await harness.press("p:pause")
        again = control.is_paused()
        await harness.say("/resume")
        return paused, resumed, again, control.is_paused(), panel_paused

    paused, resumed, again, finally_paused, _ = run(scenario())
    assert (paused, resumed, again, finally_paused) == (True, True, True, False)


def test_limit_command_shows_sets_and_validates() -> None:
    async def scenario():
        harness = Harness()
        _, show = await harness.say("/limit")
        _, large = await harness.say("/limit 800")
        _, auto = await harness.say("/limit auto 2000")
        _, off = await harness.say("/limit 0")
        _, bad = await harness.say("/limit lots")
        _, bad_auto = await harness.say("/limit auto")
        return show, large, auto, off, bad, bad_auto

    show, large, auto, off, bad, bad_auto = run(scenario())
    assert "حدود الحجم" in show.replies[0].text
    assert "800 MB" in large.replies[0].text and "فوق 2000 MB" in auto.replies[0].text
    assert "مغلق" in off.replies[0].text
    assert "❌" in bad.replies[0].text and "❌" in bad_auto.replies[0].text
    assert (limits.large_file_mb(), limits.auto_skip_mb()) == (0, 2000)


def test_limits_persist_across_a_restart() -> None:
    async def scenario() -> None:
        await limits.set_large_file_mb(750)
        await limits.set_auto_skip_mb(3000)
        limits.reset()
        assert limits.large_file_mb() == 500  # back to the env default...
        await limits.load()  # ...until the saved values are loaded at startup

    run(scenario())
    assert (limits.large_file_mb(), limits.auto_skip_mb()) == (750, 3000)


def running_job(message_id: int = 5) -> Job:
    job = registry.create("backlog", SRC, "Source A", message_id)
    job.begin_stage(Stage.DOWNLOAD)
    return job


def test_skip_command_and_buttons_flag_the_running_job() -> None:
    async def scenario():
        harness = Harness()
        _, idle = await harness.say("/skip")
        job = running_job()
        _, running = await harness.say("/skip")
        flagged = job.skip_requested
        job.skip_requested = False

        await harness.press(f"sk:{job.id}", uid=STRANGER)
        assert job.skip_requested is False
        _, cb = await harness.press(f"sk:{job.id}")
        by_id = job.skip_requested
        job.skip_requested = False
        await harness.press("sk:0")
        _, gone = await harness.press("sk:9999")
        return idle, running, flagged, by_id, job.skip_requested, cb, gone

    idle, running, flagged, by_id, all_flag, cb, gone = run(scenario())
    assert "لا يوجد ملف" in idle.replies[0].text and "جارٍ تخطي" in running.replies[0].text
    assert flagged and by_id and all_flag
    assert "جارٍ التخطي" in cb.answers[0][0]
    assert "انتهى" in gone.answers[0][0]


def test_large_file_notice_goes_to_the_owner_with_a_skip_button() -> None:
    async def scenario():
        harness = Harness()
        job = registry.create(
            "live", SRC, "Source <A>", 5, media_type="video", file_size=900 * limits.MIB
        )
        await registry.on_large_file(job)
        return harness, job

    harness, job = run(scenario())
    chat_id, text, kwargs = harness.bot.sent[0]
    assert chat_id == OWNER
    assert "ملف كبير" in text and "900.0 MB" in text and "Source &lt;A&gt;" in text  # escaped
    assert "الملف:" not in text  # no name known -> no empty line
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"sk:{job.id}"


# -- panel callbacks ------------------------------------------------------------------------------


def test_source_detail_and_toggles_update_the_runtime_and_database() -> None:
    async def scenario():
        harness = Harness(userbot_with_source())
        await runtime.set_dest(DEST, "Archive")
        info, _ = await runtime.add_source(SRC, "Source A")

        _, listing = await harness.press("p:srcs:1")
        _, detail = await harness.press(f"p:src:{info.id}")
        await harness.press(f"p:tog:{info.id}")
        disabled = runtime.get_source(info.id).enabled
        await harness.press(f"p:hist:{info.id}")
        history_off = runtime.get_source(info.id).history_sync
        _, missing = await harness.press("p:src:999")
        _, home = await harness.press("p:home")
        await runtime.reset()
        await runtime.load()
        return listing, detail, disabled, history_off, missing, home

    listing, detail, disabled, history_off, missing, home = run(scenario())
    assert "Source A" in listing.message.text and "Source A" in detail.message.text
    assert disabled is False and history_off is False
    assert "لم يعد موجودًا" in missing.message.text
    assert "لوحة التحكم" in home.message.text
    persisted = runtime.sources()[0]
    assert (persisted.enabled, persisted.history_sync) == (False, False)


def test_a_failing_button_answers_with_an_error_instead_of_crashing(monkeypatch) -> None:
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(handlers.panel, "home_screen", boom)

    async def scenario():
        harness = Harness()
        _, cb = await harness.press("p:home")
        return cb

    cb = run(scenario())
    assert cb.answers and "خطأ" in cb.answers[0][0] and cb.answers[0][1] is True


def test_retry_button_requeues_a_skipped_file_and_resolves_it() -> None:
    async def scenario():
        userbot = with_destination()
        harness = Harness(userbot)
        await runtime.set_dest(DEST, "Archive")
        with session_scope() as session:
            row = MirrorRepository(session).record_skip(
                source_chat_id=str(LINK_CHAT),
                source_message_id=42,
                media_type="photo",
                reason="user_skip",
            )
            skipped_id = row.id
        harness.ctx.link_queue.start()
        await harness.press(f"rt:{skipped_id}")
        await harness.settle()
        await harness.ctx.link_queue.stop()
        _, stale = await harness.press(f"rt:{skipped_id}")
        return userbot, stale

    userbot, stale = run(scenario())
    assert len(userbot.send_calls) == 1 and ledger_count() == 1
    with session_scope() as session:
        assert MirrorRepository(session).count_skipped() == 0
    assert "لم يعد" in stale.answers[0][0]


def test_peer_error_does_not_leak_through_unreadable_picker_source() -> None:
    async def scenario():
        userbot = userbot_with_source()
        userbot.dialog_chats = [make_chat(SRC, "Source A")]
        userbot.unreadable.add(SRC)
        harness = Harness(userbot)
        await runtime.set_dest(DEST, "Archive")
        await harness.say("/addsource")
        _, cb = await harness.press(f"sel:src:{SRC}")
        return cb

    cb = run(scenario())
    assert "❌" in cb.message.text and runtime.sources() == []
    assert ChannelPrivate  # (the unreadable fake raises it; the owner only sees Arabic text)


def test_large_file_notice_includes_the_file_name_escaped() -> None:
    async def scenario():
        harness = Harness()
        job = registry.create("live", SRC, "Src", 5, media_type="video", file_size=900 * limits.MIB)
        job.file_name = "movie <final>.mkv"
        await registry.on_large_file(job)
        return harness

    text = run(scenario()).bot.sent[0][1]
    assert "movie &lt;final&gt;.mkv" in text and "<final>" not in text
