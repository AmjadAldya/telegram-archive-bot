from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import SyncState, SyncStatus, TransferredMedia


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
