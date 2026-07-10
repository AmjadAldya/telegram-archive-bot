from __future__ import annotations

"""create mirror_config table"""

from alembic import op
import sqlalchemy as sa


revision = "0002_mirror_config"
down_revision = "0001_mirror_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mirror_config",
        sa.Column("id", sa.String(length=16), primary_key=True, nullable=False),
        sa.Column("source_chat_id", sa.String(length=255), nullable=True),
        sa.Column("source_title", sa.String(length=255), nullable=True),
        sa.Column("dest_chat_id", sa.String(length=255), nullable=True),
        sa.Column("dest_title", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mirror_config")
