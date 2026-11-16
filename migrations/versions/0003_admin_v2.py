from __future__ import annotations

"""admin v2: sources table, skipped_media, bot_settings, ledger source index

Additive only (no table rebuilds), idempotent (every step checks what already
exists, because SQLite DDL is not transactional under Alembic), and reversible.

Upgrade preserves all existing data: the single source stored in
`mirror_config` is copied into `sources`; `mirror_config` itself (destination,
and the legacy source columns kept for rollback compatibility), `sync_state`
and `transferred_media` are never modified.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0003_admin_v2"
down_revision = "0002_mirror_config"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()

    if "sources" not in tables:
        op.create_table(
            "sources",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("chat_id", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("history_sync", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("chat_id", name="uq_sources_chat_id"),
        )

    # Carry the legacy single source over (once; re-running never duplicates it).
    if "mirror_config" in tables:
        legacy = bind.execute(
            sa.text("SELECT source_chat_id, source_title FROM mirror_config WHERE id = 'default'")
        ).first()
        if legacy is not None and legacy[0]:
            already = bind.execute(
                sa.text("SELECT 1 FROM sources WHERE chat_id = :chat_id"), {"chat_id": legacy[0]}
            ).first()
            if already is None:
                bind.execute(
                    sa.text(
                        "INSERT INTO sources (chat_id, title, enabled, history_sync, state, "
                        "created_at) VALUES (:chat_id, :title, 1, 1, 'active', :created_at)"
                    ),
                    {
                        "chat_id": legacy[0],
                        "title": legacy[1] or "",
                        # same text format SQLAlchemy stores for DateTime on SQLite
                        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
                    },
                )

    if "skipped_media" not in tables:
        op.create_table(
            "skipped_media",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("source_chat_id", sa.String(length=255), nullable=False),
            sa.Column("source_message_id", sa.BigInteger(), nullable=False),
            sa.Column("file_unique_id", sa.String(length=255), nullable=True),
            sa.Column("media_type", sa.String(length=32), nullable=False),
            sa.Column("file_size", sa.BigInteger(), nullable=True),
            sa.Column("reason", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "ix_skipped_media_source_chat_id" not in _index_names("skipped_media"):
        op.create_index("ix_skipped_media_source_chat_id", "skipped_media", ["source_chat_id"])

    if "bot_settings" not in tables:
        op.create_table(
            "bot_settings",
            sa.Column("key", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("value", sa.String(length=255), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "ix_transferred_media_source_chat_id" not in _index_names("transferred_media"):
        op.create_index(
            "ix_transferred_media_source_chat_id", "transferred_media", ["source_chat_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables()

    # v2 never updates the legacy single-source columns, so they may be stale. Make them
    # reflect the current sources (first enabled one, else the first), or clear them if
    # every source was removed - that is what a v1 build should see.
    if "sources" in tables and "mirror_config" in tables:
        first = bind.execute(
            sa.text("SELECT chat_id, title FROM sources ORDER BY enabled DESC, id LIMIT 1")
        ).first()
        bind.execute(
            sa.text(
                "UPDATE mirror_config SET source_chat_id = :chat_id, source_title = :title "
                "WHERE id = 'default'"
            ),
            {"chat_id": first[0] if first else None, "title": first[1] if first else None},
        )

    if "transferred_media" in tables and "ix_transferred_media_source_chat_id" in _index_names(
        "transferred_media"
    ):
        op.drop_index("ix_transferred_media_source_chat_id", table_name="transferred_media")
    if "bot_settings" in tables:
        op.drop_table("bot_settings")
    if "skipped_media" in tables:
        if "ix_skipped_media_source_chat_id" in _index_names("skipped_media"):
            op.drop_index("ix_skipped_media_source_chat_id", table_name="skipped_media")
        op.drop_table("skipped_media")
    if "sources" in tables:
        op.drop_table("sources")
