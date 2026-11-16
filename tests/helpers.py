"""Shared fakes for the tests. They mimic the Pyrogram behavior the code relies on:
progress callbacks, `stop_transmission()` aborting a transfer (returning None), and
transfers that hang until the watchdog cancels them."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from pyrogram import StopTransmission, enums
from pyrogram.types import CallbackQuery


@dataclass(slots=True)
class FakeFile:
    file_unique_id: str
    file_size: int | None = None
    file_name: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class FakeChat:
    id: int
    title: str | None = None


@dataclass(slots=True)
class FakeMessage:
    id: int
    chat: FakeChat
    photo: FakeFile | None = None
    video: FakeFile | None = None
    animation: FakeFile | None = None
    document: FakeFile | None = None
    audio: FakeFile | None = None
    voice: FakeFile | None = None
    video_note: FakeFile | None = None
    text: str | None = None
    caption: str | None = None
    entities: list | None = None
    caption_entities: list | None = None
    empty: bool = False


@dataclass(slots=True)
class FakeSent:
    id: int


@dataclass
class FakeClient:
    """Userbot stand-in. Knobs let a test make downloads/uploads slow, hang, or fail."""

    size: int = 1000
    chunks: int = 4
    chunk_delay: float = 0.0
    hang_downloads: int = 0  # the next N downloads never finish (until cancelled)
    hang_uploads: int = 0
    empty_downloads: bool = False
    # Pyrogram joins a *relative* download directory to Client.PARENT_DIR; mimic that.
    parent_dir: Path | None = None
    send_calls: list = field(default_factory=list)
    download_calls: int = 0
    downloaded_paths: list = field(default_factory=list)
    _next_id: int = 1000

    def stop_transmission(self) -> None:
        raise StopTransmission()

    async def _simulate(self, progress, total: int) -> bool:
        """Run the chunk loop; False if the progress callback asked to stop."""
        written = 0
        for _ in range(self.chunks):
            await asyncio.sleep(self.chunk_delay)
            written += total // self.chunks
            if progress is not None:
                try:
                    progress(written, total)
                except StopTransmission:
                    return False
        return True

    async def download_media(self, message, file_name=None, progress=None, **_kwargs):
        self.download_calls += 1
        if self.hang_downloads > 0:
            self.hang_downloads -= 1
            await asyncio.sleep(3600)
        directory = Path(file_name or "downloads/")
        if self.parent_dir is not None and not directory.is_absolute():
            directory = self.parent_dir / directory
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "media.bin"
        path.write_bytes(b"")  # leaves a partial file behind if cancelled/stopped
        self.downloaded_paths.append(path)
        if not await self._simulate(progress, self.size):
            return None
        path.write_bytes(b"" if self.empty_downloads else b"x" * self.size)
        return str(path)

    async def _send(self, kind: str, chat_id, path, progress, kwargs):
        if self.hang_uploads > 0:
            self.hang_uploads -= 1
            await asyncio.sleep(3600)
        if not await self._simulate(progress, Path(path).stat().st_size or 1):
            return None
        self.send_calls.append({"kind": kind, "chat_id": chat_id, "path": path, **kwargs})
        self._next_id += 1
        return FakeSent(id=self._next_id)

    async def send_photo(self, chat_id, photo, progress=None, **kw):
        return await self._send("photo", chat_id, photo, progress, kw)

    async def send_video(self, chat_id, video, progress=None, **kw):
        return await self._send("video", chat_id, video, progress, kw)

    async def send_animation(self, chat_id, animation, progress=None, **kw):
        return await self._send("animation", chat_id, animation, progress, kw)

    async def send_document(self, chat_id, document, progress=None, **kw):
        return await self._send("document", chat_id, document, progress, kw)

    async def send_audio(self, chat_id, audio, progress=None, **kw):
        return await self._send("audio", chat_id, audio, progress, kw)

    async def send_voice(self, chat_id, voice, progress=None, **kw):
        return await self._send("voice", chat_id, voice, progress, kw)

    async def send_video_note(self, chat_id, video_note, progress=None, **kw):
        return await self._send("video_note", chat_id, video_note, progress, kw)


# -- fakes for the bot layer --------------------------------------------------------


class FakeSentMessage:
    """What `message.reply()` returns: records edits so tests can read the final text."""

    def __init__(self, text: str, reply_markup=None) -> None:
        self.id = 5000
        self.text = text
        self.reply_markup = reply_markup
        self.edits: list[tuple[str, object]] = []
        self.chat = SimpleNamespace(id=1, type=enums.ChatType.PRIVATE)

    async def edit_text(self, text: str, parse_mode=None, reply_markup=None, **_kw):
        self.text, self.reply_markup = text, reply_markup
        self.edits.append((text, reply_markup))
        return self

    async def reply(self, text: str, quote=None, parse_mode=None, reply_markup=None, **_kw):
        return FakeSentMessage(text, reply_markup)


class FakeIncoming:
    """An incoming message from `uid` (for handlers and filters)."""

    def __init__(self, uid: int, text: str, chat_type=enums.ChatType.PRIVATE, chat_id=None):
        self.from_user = SimpleNamespace(id=uid, username=None)
        self.text = text
        self.caption = None
        self.entities = None
        self.caption_entities = None
        self.chat = SimpleNamespace(id=chat_id or uid, type=chat_type, username=None)
        self.outgoing = False
        self.command = None
        self.sender_chat = None
        self.scheduled = False
        self.via_bot = None
        self.empty = False
        self.replies: list[FakeSentMessage] = []

    async def reply(self, text: str, quote=None, parse_mode=None, reply_markup=None, **_kw):
        sent = FakeSentMessage(text, reply_markup)
        self.replies.append(sent)
        return sent


class FakeCallback(CallbackQuery):
    """A real `CallbackQuery` subclass (Pyrogram's regex filter does an isinstance check)
    whose network-facing methods are recorded instead of sent."""

    def __init__(self, uid: int, data: str, message: FakeSentMessage | None = None):
        super().__init__(
            client=None,
            id="cb1",
            from_user=SimpleNamespace(id=uid, username=None),
            chat_instance="ci",
            message=message or FakeSentMessage("panel"),
            data=data,
        )
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False, **_kw):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, parse_mode=None, reply_markup=None, **_kw):
        self.message.text, self.message.reply_markup = text, reply_markup
        self.message.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None, **_kw):
        self.message.reply_markup = reply_markup


class FakeRegistrar:
    """Stands in for the Pyrogram client's decorators and records each handler + filter."""

    def __init__(self) -> None:
        self.message_handlers: list[tuple[object, object]] = []
        self.callback_handlers: list[tuple[object, object]] = []
        self.sent: list[tuple] = []
        self.edited: list[tuple] = []
        self.me = SimpleNamespace(id=111, username="example_control_bot")

    def on_message(self, flt):
        def decorator(fn):
            self.message_handlers.append((flt, fn))
            return fn

        return decorator

    def on_callback_query(self, flt):
        def decorator(fn):
            self.callback_handlers.append((flt, fn))
            return fn

        return decorator

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        message = FakeSentMessage(text, kw.get("reply_markup"))
        message.id = 7000 + len(self.sent)
        return message

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edited.append((chat_id, message_id, text, kw.get("reply_markup")))

    async def _matches(self, flt, update) -> bool:
        loop = asyncio.get_running_loop()
        client = SimpleNamespace(loop=loop, executor=None, me=self.me)
        # Same decision as pyrogram.handlers.Handler.check(): plain sync filters run in the
        # executor, coroutine filters (and And/Or compositions) are awaited.
        if inspect.iscoroutinefunction(flt.__call__):
            return bool(await flt(client, update))
        return bool(await loop.run_in_executor(None, flt, client, update))

    async def dispatch_message(self, update) -> list:
        """Run every matching handler for an incoming message (like Pyrogram would)."""
        fired = []
        for flt, fn in self.message_handlers:
            if await self._matches(flt, update):
                await fn(self, update)
                fired.append(fn.__name__)
                break  # Pyrogram runs only the first match per handler group
        return fired

    async def dispatch_callback(self, update) -> list:
        fired = []
        for flt, fn in self.callback_handlers:
            if await self._matches(flt, update):
                await fn(self, update)
                fired.append(fn.__name__)
                break
        return fired


@dataclass
class FakeUserbot(FakeClient):
    """FakeClient plus per-chat history, for backlog scans and link fetches."""

    histories: dict = field(default_factory=dict)  # chat_id -> list[FakeMessage]
    unreadable: set = field(default_factory=set)
    history_calls: list = field(default_factory=list)  # (chat_id, offset_id)
    messages: dict = field(default_factory=dict)  # (chat_ref, message_id) -> FakeMessage
    get_messages_errors: list = field(default_factory=list)  # raised one per call, in order
    get_messages_calls: list = field(default_factory=list)
    chats: dict = field(default_factory=dict)  # chat id / "username" -> chat object
    dialog_chats: list = field(default_factory=list)
    admin_chats: set = field(default_factory=set)
    dialogs_calls: int = 0
    joined: list = field(default_factory=list)

    async def get_chat_history(self, chat_id, limit=0, offset=0, offset_id=0, **_kw):
        from pyrogram.errors import ChannelPrivate

        self.history_calls.append((chat_id, offset_id))
        if chat_id in self.unreadable:
            raise ChannelPrivate()
        messages = sorted(self.histories.get(chat_id, []), key=lambda m: m.id, reverse=True)
        if offset_id:
            messages = [m for m in messages if m.id < offset_id]
        if limit:
            messages = messages[:limit]
        for message in messages:
            await asyncio.sleep(0)
            yield message

    async def get_messages(self, chat_id, message_ids=None, **_kw):
        self.get_messages_calls.append((chat_id, message_ids))
        if self.get_messages_errors:
            raise self.get_messages_errors.pop(0)
        return self.messages.get((chat_id, message_ids))

    async def get_dialogs(self, **_kw):
        self.dialogs_calls += 1
        for chat in self.dialog_chats:
            yield SimpleNamespace(chat=chat)

    async def get_chat(self, identifier):
        from pyrogram.errors import PeerIdInvalid

        if identifier not in self.chats:
            raise PeerIdInvalid()
        return self.chats[identifier]

    async def get_chat_member(self, chat_id, _user):
        from pyrogram.enums import ChatMemberStatus

        status = ChatMemberStatus.OWNER if chat_id in self.admin_chats else ChatMemberStatus.MEMBER
        return SimpleNamespace(status=status)

    async def join_chat(self, link):
        self.joined.append(link)
        return self.chats[link]


def make_chat(chat_id: int, title: str, kind=enums.ChatType.CHANNEL, username=None):
    return SimpleNamespace(id=chat_id, title=title, type=kind, username=username)
