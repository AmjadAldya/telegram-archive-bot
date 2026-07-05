from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text

TEST_ROOT = Path(tempfile.gettempdir()) / "telegram-archive-bot-tests"
TEST_ROOT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ADMIN_ID", "123456789")
os.environ.setdefault("ADMIN_IDS", "123456789")
os.environ.setdefault("ARCHIVE_CHAT_ID", "me")
os.environ.setdefault("DATA_DIR", str(TEST_ROOT))
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}"


@pytest.fixture(autouse=True)
def reset_database() -> None:
    from alembic import command

    from app.database.base import Base, build_alembic_config, get_engine
    from app.services.queue import queue

    while not queue.empty():
        queue.get_nowait()
        queue.task_done()

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    config = build_alembic_config()
    command.upgrade(config, "head")
