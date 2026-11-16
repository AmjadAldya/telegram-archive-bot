from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class SyncStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


MIRROR_CONFIG_ID = "default"


class MirrorConfig(Base):
    """Singleton row holding the currently selected source/destination chats.

    Chosen interactively via /chats + /setsource + /setdest (see
    app/mirror/runtime.py), so the choice survives restarts without editing
    `.env`.
    """

    __tablename__ = "mirror_config"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=lambda: MIRROR_CONFIG_ID)
    source_chat_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dest_chat_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dest_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class TransferredMedia(Base):
    """Dedup ledger: one row per media file ever sent to a destination chat.

    The unique constraint on (dest_chat_id, file_unique_id) is what actually
    prevents duplicate transfers, including across process restarts and
    concurrent backlog/live processing.
    """

    __tablename__ = "transferred_media"
    __table_args__ = (
        UniqueConstraint("dest_chat_id", "file_unique_id", name="uq_dest_file_unique_id"),
        Index("ix_transferred_media_source_chat_id", "source_chat_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_unique_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dest_chat_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    dest_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    transferred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SyncState(Base):
    """Singleton progress marker for the source -> destination mirror pair.

    Tracks the backlog scan cursor so a restart resumes instead of
    re-scanning the whole source chat history from scratch.
    """

    __tablename__ = "sync_state"
    __table_args__ = (
        UniqueConstraint("source_chat_id", "dest_chat_id", name="uq_source_dest_pair"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    dest_chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="sync_status", native_enum=False),
        nullable=False,
        default=SyncStatus.IDLE,
    )
    processed_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transferred_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resume_before_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def summary(self) -> str:
        status = self.status.value if isinstance(self.status, SyncStatus) else str(self.status)
        resume_before = (
            self.resume_before_message_id if self.resume_before_message_id is not None else "none"
        )
        line = (
            f"status={status} source={self.source_chat_id} dest={self.dest_chat_id} "
            f"processed={self.processed_messages} transferred={self.transferred_messages} "
            f"duplicates={self.duplicate_messages} resume_before={resume_before}"
        )
        if self.last_error:
            line += f"\nlast_error: {self.last_error}"
        return line


class SourceState(str, enum.Enum):
    ACTIVE = "active"
    ERROR = "error"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(Base):
    """A chat the userbot mirrors media FROM. Many sources share one destination.

    `enabled = False` is the "paused" state of a single source; `state` is a
    health marker (the userbot could not read the chat -> ERROR). Per-source
    backlog progress lives in `sync_state`, keyed by (source, destination).
    """

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_sources_chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    history_sync: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=SourceState.ACTIVE.value)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SkippedMedia(Base):
    """Media that was not transferred (user skip, timeout, auto-skip).

    These are NOT in the dedup ledger, so they can be retried later. Only
    metadata is stored - never file names, captions or message text.
    """

    __tablename__ = "skipped_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_chat_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BotSetting(Base):
    """Tiny key/value store for runtime-adjustable settings (e.g. /limit)."""

    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
