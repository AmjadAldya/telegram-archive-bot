from __future__ import annotations

"""create archive jobs table"""

from alembic import op
import sqlalchemy as sa


revision = "0001_archive_jobs"
down_revision = None
branch_labels = None
depends_on = None


archive_job_status = sa.Enum(
    "queued",
    "running",
    "completed",
    "failed",
    "cancel_requested",
    "cancelled",
    name="archive_job_status",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "archive_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("source_chat_id", sa.String(length=255), nullable=False),
        sa.Column("archive_path", sa.String(length=255), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("status", archive_job_status, nullable=False),
        sa.Column("processed_messages", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("media_messages", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("resume_after_message_id", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_archive_jobs_requested_by", "archive_jobs", ["requested_by"])
    op.create_index("ix_archive_jobs_status", "archive_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_archive_jobs_status", table_name="archive_jobs")
    op.drop_index("ix_archive_jobs_requested_by", table_name="archive_jobs")
    op.drop_table("archive_jobs")
    archive_job_status.drop(op.get_bind(), checkfirst=True)
