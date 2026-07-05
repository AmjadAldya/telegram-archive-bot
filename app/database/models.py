from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ArchiveJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class ArchiveJob(Base):
    __tablename__ = "archive_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    archive_path: Mapped[str] = mapped_column(String(255), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    status: Mapped[ArchiveJobStatus] = mapped_column(
        Enum(ArchiveJobStatus, name="archive_job_status", native_enum=False),
        nullable=False,
        default=ArchiveJobStatus.QUEUED,
        index=True,
    )
    processed_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    media_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resume_after_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def summary(self) -> str:
        status = (
            self.status.value if isinstance(self.status, ArchiveJobStatus) else str(self.status)
        )
        resume_after = (
            self.resume_after_message_id if self.resume_after_message_id is not None else "none"
        )
        return (
            f"job={self.id} status={status} source={self.source_chat_id} archive_path={self.archive_path} "
            f"processed={self.processed_messages} media={self.media_messages} resume_after={resume_after} "
            f"retries={self.retry_count}/{self.max_retries}"
        )
