from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    MIRROR_CONFIG_ID,
    BotSetting,
    MirrorConfig,
    SkippedMedia,
    Source,
    SourceState,
    SyncState,
    SyncStatus,
    TransferredMedia,
)


class MirrorRepository:
    def __init__(self, session: Session):
        self.session = session

    # -- dedup ledger -----------------------------------------------------

    def is_duplicate(self, dest_chat_id: str, file_unique_id: str) -> bool:
        statement = select(TransferredMedia.id).where(
            TransferredMedia.dest_chat_id == dest_chat_id,
            TransferredMedia.file_unique_id == file_unique_id,
        )
        return self.session.scalars(statement).first() is not None

    def record_transfer(
        self,
        *,
        file_unique_id: str,
        media_type: str,
        source_chat_id: str,
        source_message_id: int,
        dest_chat_id: str,
        dest_message_id: int | None,
    ) -> bool:
        """Record a completed transfer. Returns False if it was already recorded.

        Relies on the DB unique constraint as the source of truth so
        concurrent callers can never double-record the same file.
        """
        record = TransferredMedia(
            file_unique_id=file_unique_id,
            media_type=media_type,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            dest_chat_id=dest_chat_id,
            dest_message_id=dest_message_id,
        )
        self.session.add(record)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            return False
        return True

    # -- sync state ---------------------------------------------------------

    def get_or_create_sync_state(self, source_chat_id: str, dest_chat_id: str) -> SyncState:
        statement = select(SyncState).where(
            SyncState.source_chat_id == source_chat_id,
            SyncState.dest_chat_id == dest_chat_id,
        )
        state = self.session.scalars(statement).first()
        if state is not None:
            return state

        state = SyncState(source_chat_id=source_chat_id, dest_chat_id=dest_chat_id)
        self.session.add(state)
        self.session.flush()
        self.session.refresh(state)
        return state

    def update_sync_progress(
        self,
        sync_state_id: str,
        *,
        processed_delta: int = 0,
        transferred_delta: int = 0,
        duplicate_delta: int = 0,
        resume_before_message_id: int | None = None,
    ) -> SyncState:
        state = self.session.get(SyncState, sync_state_id)
        if state is None:
            raise LookupError(f"Sync state {sync_state_id} was not found")

        state.processed_messages += processed_delta
        state.transferred_messages += transferred_delta
        state.duplicate_messages += duplicate_delta
        if resume_before_message_id is not None:
            state.resume_before_message_id = resume_before_message_id
        state.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(state)
        return state

    def mark_status(
        self,
        sync_state_id: str,
        status: SyncStatus,
        *,
        last_error: str | None = None,
    ) -> SyncState:
        state = self.session.get(SyncState, sync_state_id)
        if state is None:
            raise LookupError(f"Sync state {sync_state_id} was not found")

        state.status = status
        state.last_error = last_error
        state.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(state)
        return state

    def reset_sync_state(self, source_chat_id: str, dest_chat_id: str) -> SyncState:
        state = self.get_or_create_sync_state(source_chat_id, dest_chat_id)
        state.resume_before_message_id = None
        state.status = SyncStatus.IDLE
        state.last_error = None
        state.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(state)
        return state

    # -- mirror config (selected source/destination) ------------------------

    def get_config(self) -> MirrorConfig:
        config = self.session.get(MirrorConfig, MIRROR_CONFIG_ID)
        if config is not None:
            return config

        config = MirrorConfig(id=MIRROR_CONFIG_ID)
        self.session.add(config)
        self.session.flush()
        self.session.refresh(config)
        return config

    def set_source(self, chat_id: str, title: str) -> MirrorConfig:
        config = self.get_config()
        config.source_chat_id = chat_id
        config.source_title = title
        config.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(config)
        return config

    def set_dest(self, chat_id: str, title: str) -> MirrorConfig:
        config = self.get_config()
        config.dest_chat_id = chat_id
        config.dest_title = title
        config.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.refresh(config)
        return config

    # -- sources (many sources -> one destination) ----------------------------

    def list_sources(self) -> list[Source]:
        return list(self.session.scalars(select(Source).order_by(Source.id)))

    def get_source(self, source_id: int) -> Source | None:
        return self.session.get(Source, source_id)

    def get_source_by_chat(self, chat_id: str) -> Source | None:
        return self.session.scalars(select(Source).where(Source.chat_id == chat_id)).first()

    def add_source(
        self, chat_id: str, title: str, *, history_sync: bool = True
    ) -> tuple[Source, bool]:
        """Add a source, or refresh the title of an existing one. Returns (row, created)."""
        existing = self.get_source_by_chat(chat_id)
        if existing is not None:
            existing.title = title or existing.title
            existing.state = SourceState.ACTIVE.value
            existing.last_error = None
            self.session.flush()
            return existing, False
        source = Source(chat_id=chat_id, title=title, history_sync=history_sync)
        self.session.add(source)
        self.session.flush()
        return source, True

    def remove_source(self, source_id: int) -> bool:
        source = self.get_source(source_id)
        if source is None:
            return False
        self.session.delete(source)
        self.session.flush()
        return True

    def set_source_enabled(self, source_id: int, enabled: bool) -> Source | None:
        source = self.get_source(source_id)
        if source is not None:
            source.enabled = enabled
            self.session.flush()
        return source

    def set_source_history_sync(self, source_id: int, history_sync: bool) -> Source | None:
        source = self.get_source(source_id)
        if source is not None:
            source.history_sync = history_sync
            self.session.flush()
        return source

    def set_source_health(self, source_id: int, state: SourceState, error: str | None) -> None:
        source = self.get_source(source_id)
        if source is not None:
            source.state = state.value
            source.last_error = error
            self.session.flush()

    def find_sync_state(self, source_chat_id: str, dest_chat_id: str) -> SyncState | None:
        return self.session.scalars(
            select(SyncState).where(
                SyncState.source_chat_id == source_chat_id,
                SyncState.dest_chat_id == dest_chat_id,
            )
        ).first()

    # -- ledger statistics -----------------------------------------------------

    def ledger_count(self, dest_chat_id: str | None = None) -> int:
        statement = select(func.count()).select_from(TransferredMedia)
        if dest_chat_id is not None:
            statement = statement.where(TransferredMedia.dest_chat_id == dest_chat_id)
        return int(self.session.scalar(statement) or 0)

    def ledger_count_since(self, since: datetime, dest_chat_id: str | None = None) -> int:
        statement = (
            select(func.count())
            .select_from(TransferredMedia)
            .where(TransferredMedia.transferred_at >= since)
        )
        if dest_chat_id is not None:
            statement = statement.where(TransferredMedia.dest_chat_id == dest_chat_id)
        return int(self.session.scalar(statement) or 0)

    def last_transfer_at(self, source_chat_id: str | None = None) -> datetime | None:
        statement = select(func.max(TransferredMedia.transferred_at))
        if source_chat_id is not None:
            statement = statement.where(TransferredMedia.source_chat_id == source_chat_id)
        return self.session.scalar(statement)

    # -- skipped media ---------------------------------------------------------

    def record_skip(
        self,
        *,
        source_chat_id: str,
        source_message_id: int,
        media_type: str,
        reason: str,
        file_unique_id: str | None = None,
        file_size: int | None = None,
    ) -> SkippedMedia:
        """Record a skipped file. One open row per (chat, message): re-skips update it."""
        existing = self.session.scalars(
            select(SkippedMedia).where(
                SkippedMedia.source_chat_id == source_chat_id,
                SkippedMedia.source_message_id == source_message_id,
                SkippedMedia.resolved_at.is_(None),
            )
        ).first()
        if existing is not None:
            existing.reason = reason
            existing.created_at = datetime.now(timezone.utc)
            if file_size is not None:
                existing.file_size = file_size
            self.session.flush()
            return existing
        row = SkippedMedia(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            media_type=media_type,
            reason=reason,
            file_unique_id=file_unique_id,
            file_size=file_size,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list_skipped(self, *, offset: int = 0, limit: int = 5) -> list[SkippedMedia]:
        statement = (
            select(SkippedMedia)
            .where(SkippedMedia.resolved_at.is_(None))
            .order_by(SkippedMedia.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def count_skipped(self) -> int:
        statement = (
            select(func.count()).select_from(SkippedMedia).where(SkippedMedia.resolved_at.is_(None))
        )
        return int(self.session.scalar(statement) or 0)

    def get_skipped(self, skipped_id: int) -> SkippedMedia | None:
        return self.session.get(SkippedMedia, skipped_id)

    def resolve_skipped(self, skipped_id: int) -> None:
        row = self.get_skipped(skipped_id)
        if row is not None and row.resolved_at is None:
            row.resolved_at = datetime.now(timezone.utc)
            self.session.flush()

    def resolve_skipped_for_message(self, source_chat_id: str, source_message_id: int) -> None:
        rows = self.session.scalars(
            select(SkippedMedia).where(
                SkippedMedia.source_chat_id == source_chat_id,
                SkippedMedia.source_message_id == source_message_id,
                SkippedMedia.resolved_at.is_(None),
            )
        )
        for row in rows:
            row.resolved_at = datetime.now(timezone.utc)
        self.session.flush()

    # -- runtime settings ------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self.session.get(BotSetting, key)
        return row.value if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        row = self.session.get(BotSetting, key)
        if row is None:
            self.session.add(BotSetting(key=key, value=value))
        else:
            row.value = value
        self.session.flush()
