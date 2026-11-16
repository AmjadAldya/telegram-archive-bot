from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from helpers import make_chat
from pyrogram.types import Chat

from app.bot import client as client_module
from app.mirror import runtime, transfer


class RecordingClient:
    def __init__(self, fail_scoped: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail_scoped = fail_scoped

    async def set_bot_commands(self, commands, scope=None):
        if scope is not None and self.fail_scoped:
            raise RuntimeError("PEER_ID_INVALID")
        self.calls.append(("set", [c.command for c in commands], scope))

    async def delete_bot_commands(self, scope=None):
        self.calls.append(("delete", scope))


def test_command_menu_is_scoped_to_the_owner_chat_and_hidden_from_others() -> None:
    fake = RecordingClient()
    asyncio.run(client_module._publish_commands(fake, 424242))

    kind, names, scope = fake.calls[0]
    assert kind == "set" and "panel" in names and "live" in names and "addsource" in names
    assert scope.chat_id == 424242
    assert ("delete", None) in fake.calls  # the default (everyone) menu is cleared


def test_command_menu_falls_back_to_the_default_scope_if_scoping_fails() -> None:
    fake = RecordingClient(fail_scoped=True)
    asyncio.run(client_module._publish_commands(fake, 424242))
    assert fake.calls == [("set", fake.calls[0][1], None)]
    assert "panel" in fake.calls[0][1]


def test_tmp_sweep_removes_leftovers_from_interrupted_transfers() -> None:
    leftover = transfer.tmp_root() / "job-1-9"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "media.bin").write_bytes(b"partial")

    transfer.sweep_tmp_dir()

    assert not transfer.tmp_root().exists() or list(transfer.tmp_root().iterdir()) == []


def real_chat(chat_id: int, title: str) -> Chat:
    return Chat(id=chat_id, type=make_chat(chat_id, title).type, title=title)


def test_env_seed_only_applies_to_an_empty_configuration(monkeypatch) -> None:
    class Userbot:
        async def get_chat(self, ref):
            return real_chat(int(ref), f"chat{ref}")

    monkeypatch.setattr(client_module, "bot", Userbot())
    monkeypatch.setattr(client_module, "SOURCE_CHAT_ID", -100111)
    monkeypatch.setattr(client_module, "DEST_CHAT_ID", -100222)

    async def scenario():
        await client_module._seed_from_env_if_unset()
        first = (runtime.destination(), [s.chat_id for s in runtime.sources()])

        # Once configured, the database wins: a changed env var is never re-applied.
        monkeypatch.setattr(client_module, "SOURCE_CHAT_ID", -100999)
        await client_module._seed_from_env_if_unset()
        return first, [s.chat_id for s in runtime.sources()]

    (destination, sources), after = asyncio.run(scenario())
    assert destination.chat_id == -100222 and sources == [-100111]
    assert after == [-100111]


def test_env_seed_skips_chats_the_account_has_not_joined(monkeypatch) -> None:
    class Userbot:
        async def get_chat(self, ref):
            return SimpleNamespace(title="Preview only")  # a ChatPreview: no .id

    monkeypatch.setattr(client_module, "bot", Userbot())
    monkeypatch.setattr(client_module, "SOURCE_CHAT_ID", -100111)
    monkeypatch.setattr(client_module, "DEST_CHAT_ID", -100222)

    asyncio.run(client_module._seed_from_env_if_unset())
    assert runtime.destination() is None and runtime.sources() == []


@pytest.mark.parametrize("source_env", [None])
def test_env_seed_needs_both_ids(monkeypatch, source_env) -> None:
    monkeypatch.setattr(client_module, "SOURCE_CHAT_ID", source_env)
    monkeypatch.setattr(client_module, "DEST_CHAT_ID", -100222)
    asyncio.run(client_module._seed_from_env_if_unset())
    assert runtime.destination() is None
