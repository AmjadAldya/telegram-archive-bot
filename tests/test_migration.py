"""Migration 0003 on COPIES of a populated database (never the live one)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect

from app.database.base import Base, build_alembic_config

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).isoformat(sep=" ")
LEDGER_ROWS = 60


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty DB file migrated to 0002, with alembic pointed at it."""
    path = tmp_path / "copy.db"
    monkeypatch.setattr("app.config.settings.DATABASE_URL", f"sqlite:///{path}")
    config = build_alembic_config()
    command.upgrade(config, "0002_mirror_config")
    return config, path


def seed(path: Path, *, source: str | None = "-100111") -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO mirror_config (id, source_chat_id, source_title, dest_chat_id, dest_title, updated_at)"
        " VALUES ('default', ?, ?, '-100222', 'Archive', ?)",
        (source, "Old Source" if source else None, NOW),
    )
    connection.executemany(
        "INSERT INTO sync_state (id, source_chat_id, dest_chat_id, status, processed_messages,"
        " transferred_messages, duplicate_messages, resume_before_message_id, last_error,"
        " created_at, updated_at) VALUES (?, ?, '-100222', ?, ?, ?, ?, ?, NULL, ?, ?)",
        [
            ("s1", "-100111", "paused", 5000, 100, 7, 61525, NOW, NOW),
            ("s2", "-100999", "completed", 10, 4, 0, 1, NOW, NOW),
        ],
    )
    connection.executemany(
        "INSERT INTO transferred_media (id, file_unique_id, media_type, source_chat_id,"
        " source_message_id, dest_chat_id, dest_message_id, transferred_at)"
        " VALUES (?, ?, 'photo', '-100111', ?, '-100222', ?, ?)",
        [(f"id{i}", f"unique{i}", i, 9000 + i, NOW) for i in range(LEDGER_ROWS)],
    )
    connection.commit()
    connection.close()


def snapshot(path: Path) -> dict[str, list]:
    connection = sqlite3.connect(path)
    tables = ("mirror_config", "sync_state", "transferred_media")
    data = {t: sorted(connection.execute(f"SELECT * FROM {t}").fetchall()) for t in tables}
    connection.close()
    return data


def names(path: Path, kind: str) -> set[str]:
    connection = sqlite3.connect(path)
    found = {
        r[0] for r in connection.execute(f"SELECT name FROM sqlite_master WHERE type='{kind}'")
    }
    connection.close()
    return found


def version(path: Path) -> str:
    connection = sqlite3.connect(path)
    value = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    connection.close()
    return value


def test_upgrade_preserves_all_existing_data_and_carries_the_source_over(db) -> None:
    config, path = db
    seed(path)
    before = snapshot(path)

    command.upgrade(config, "head")

    assert snapshot(path) == before  # destination, ledger, sync_state: byte-for-byte unchanged
    assert {"sources", "skipped_media", "bot_settings"} <= names(path, "table")
    assert {"ix_transferred_media_source_chat_id", "ix_skipped_media_source_chat_id"} <= names(
        path, "index"
    )
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT chat_id, title, enabled, history_sync, state FROM sources"
    ).fetchall()
    connection.close()
    assert rows == [("-100111", "Old Source", 1, 1, "active")]
    assert version(path) == "0003_admin_v2"


def test_upgrade_without_a_legacy_source_creates_no_source(db) -> None:
    config, path = db
    seed(path, source=None)
    command.upgrade(config, "head")
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    connection.close()


def test_upgrade_on_a_database_with_no_config_row_works(db) -> None:
    config, path = db
    command.upgrade(config, "head")  # fresh deploy, nothing to carry over
    assert version(path) == "0003_admin_v2"


def test_downgrade_restores_the_single_source_and_drops_only_new_tables(db) -> None:
    config, path = db
    seed(path, source=None)
    before = snapshot(path)
    command.upgrade(config, "head")
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO sources (chat_id, title, enabled, history_sync, state, created_at)"
        " VALUES (?, ?, ?, 1, 'active', ?)",
        [("-100555", "First", 1, NOW), ("-100666", "Second", 1, NOW)],
    )
    connection.commit()
    connection.close()

    command.downgrade(config, "0002_mirror_config")

    after = snapshot(path)
    assert after["transferred_media"] == before["transferred_media"]
    assert after["sync_state"] == before["sync_state"]
    config_row = after["mirror_config"][0]
    assert (config_row[1], config_row[2]) == ("-100555", "First")  # first source restored
    assert config_row[3] == "-100222"  # destination untouched
    assert not ({"sources", "skipped_media", "bot_settings"} & names(path, "table"))
    assert "ix_transferred_media_source_chat_id" not in names(path, "index")
    assert version(path) == "0002_mirror_config"


def test_up_down_up_round_trip_keeps_the_ledger(db) -> None:
    config, path = db
    seed(path)
    before = snapshot(path)
    command.upgrade(config, "head")
    command.downgrade(config, "0002_mirror_config")
    command.upgrade(config, "head")
    assert snapshot(path) == before
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    connection.close()


def test_upgrade_is_idempotent_when_objects_already_exist(db) -> None:
    """SQLite DDL is not transactional under Alembic, so a half-applied run must be re-runnable."""
    config, path = db
    seed(path)
    command.upgrade(config, "head")
    command.stamp(config, "0002_mirror_config")  # pretend the version bump never happened

    command.upgrade(config, "head")  # every table/index already exists

    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1  # no duplicate
    assert connection.execute("SELECT COUNT(*) FROM transferred_media").fetchone()[0] == LEDGER_ROWS
    connection.close()


def test_migrated_schema_matches_the_orm_models(db) -> None:
    config, path = db
    command.upgrade(config, "head")
    from sqlalchemy import create_engine

    inspector = inspect(create_engine(f"sqlite:///{path}"))
    for table in Base.metadata.sorted_tables:
        assert inspector.has_table(table.name), table.name
        migrated = {column["name"] for column in inspector.get_columns(table.name)}
        assert migrated == {column.name for column in table.columns}, table.name


def test_downgrade_overwrites_a_stale_legacy_source_with_the_current_one(db) -> None:
    """v2 never touches mirror_config.source_*, so after the owner changed sources in v2 the
    legacy columns are stale; a v1 build must see the *current* source."""
    config, path = db
    seed(path, source="-100111")  # the old single source, still in mirror_config
    command.upgrade(config, "head")
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM sources")  # v2: the owner replaced it...
    connection.execute(
        "INSERT INTO sources (chat_id, title, enabled, history_sync, state, created_at)"
        " VALUES ('-100777', 'Replacement', 1, 1, 'active', ?)",
        (NOW,),
    )
    connection.commit()
    connection.close()

    command.downgrade(config, "0002_mirror_config")

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT source_chat_id, source_title, dest_chat_id FROM mirror_config"
    ).fetchone()
    connection.close()
    assert row == ("-100777", "Replacement", "-100222")


def test_downgrade_clears_the_legacy_source_when_all_sources_were_removed(db) -> None:
    config, path = db
    seed(path, source="-100111")
    command.upgrade(config, "head")
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM sources")
    connection.commit()
    connection.close()

    command.downgrade(config, "0002_mirror_config")

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT source_chat_id, source_title, dest_chat_id FROM mirror_config"
    ).fetchone()
    connection.close()
    assert row == (None, None, "-100222")  # a v1 build starts with no source, destination kept
