from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text

TEST_ROOT = Path(tempfile.gettempdir()) / "telegram-archive-bot-tests"
TEST_ROOT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("SESSION_STRING", "test-session-string")
os.environ.setdefault("SOURCE_CHAT_ID", "-1001111111111")
os.environ.setdefault("DEST_CHAT_ID", "-1002222222222")
os.environ.setdefault("MIN_DELAY_SECONDS", "0")
os.environ.setdefault("MAX_DELAY_SECONDS", "0")
os.environ.setdefault("SYNC_HISTORY_ON_START", "false")
# Tests must not depend on a developer's local .env (python-dotenv never overrides
# variables that are already set), so pin everything that influences behavior.
os.environ["MEDIA_TYPES"] = "photo,video,document,audio,voice,video_note,animation"
os.environ.pop("BOT_TOKEN", None)
os.environ.setdefault("DATA_DIR", str(TEST_ROOT))
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}"


@pytest.fixture(autouse=True)
def reset_database() -> None:
    from alembic import command

    from app.database.base import Base, build_alembic_config, get_engine
    from app.mirror import control, limits, runtime, transfer
    from app.mirror.jobs import errors, registry

    control.resume()
    asyncio.run(runtime.reset())
    # Module-level asyncio primitives bind to the first loop that has to wait on
    # them; every test runs in its own loop, so give each test fresh ones.
    transfer._send_lock = asyncio.Lock()
    transfer._GUARD_KWARGS.clear()
    runtime._lock = asyncio.Lock()
    registry.clear()
    registry.on_large_file = None
    errors.clear()
    limits.reset()

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    config = build_alembic_config()
    command.upgrade(config, "head")
