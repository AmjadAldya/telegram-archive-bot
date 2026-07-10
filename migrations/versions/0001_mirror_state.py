from __future__ import annotations

"""create transferred_media and sync_state tables"""

from alembic import op
import sqlalchemy as sa


revision = "0001_mirror_state"
down_revision = None
branch_labels = None
depends_on = None


sync_status = sa.Enum(
    "idle",
    "running",
    "paused",
    "completed",
    "failed",
    name="sync_status",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "transferred_media",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("file_unique_id", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("source_chat_id", sa.String(length=255), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("dest_chat_id", sa.String(length=255), nullable=False),
        sa.Column("dest_message_id", sa.BigInteger(), nullable=True),
        sa.Column("transferred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "dest_chat_id", "file_unique_id", name="uq_dest_file_unique_id"
        ),
    )
    op.create_index(
        "ix_transferred_media_file_unique_id", "transferred_media", ["file_unique_id"]
    )
    op.create_index(
        "ix_transferred_media_dest_chat_id", "transferred_media", ["dest_chat_id"]
    )

    op.create_table(
        "sync_state",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("source_chat_id", sa.String(length=255), nullable=False),
        sa.Column("dest_chat_id", sa.String(length=255), nullable=False),
        sa.Column("status", sync_status, nullable=False),
        sa.Column("processed_messages", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("transferred_messages", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duplicate_messages", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("resume_before_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_chat_id", "dest_chat_id", name="uq_source_dest_pair"),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    sync_status.drop(op.get_bind(), checkfirst=True)
    op.drop_index("ix_transferred_media_dest_chat_id", table_name="transferred_media")
    op.drop_index("ix_transferred_media_file_unique_id", table_name="transferred_media")
    op.drop_table("transferred_media")
