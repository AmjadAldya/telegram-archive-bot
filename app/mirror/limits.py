"""Runtime-adjustable size limits (`/limit`), persisted in the `bot_settings` table.

`large_file_mb`  - files at or above this size trigger a notice with a Skip
                   button when they start (0 = notices off). Default comes from
                   the `LARGE_FILE_MB` env var.
`auto_skip_mb`   - files above this size are skipped automatically and listed
                   in /skipped (0 = off, the default).
"""

from __future__ import annotations

import asyncio

from app.config.settings import LARGE_FILE_MB
from app.database.base import session_scope
from app.database.repositories import MirrorRepository

MIB = 1024 * 1024
KEY_LARGE = "large_file_mb"
KEY_AUTO = "auto_skip_mb"

_values: dict[str, int] = {}


def _load_sync() -> dict[str, int]:
    loaded: dict[str, int] = {}
    with session_scope() as session:
        repository = MirrorRepository(session)
        for key in (KEY_LARGE, KEY_AUTO):
            raw = repository.get_setting(key)
            if raw is not None and raw.isdigit():
                loaded[key] = int(raw)
    return loaded


def _save_sync(key: str, value: int) -> None:
    with session_scope() as session:
        MirrorRepository(session).set_setting(key, str(value))


async def load() -> None:
    _values.clear()
    _values.update(await asyncio.to_thread(_load_sync))


def large_file_mb() -> int:
    return _values.get(KEY_LARGE, LARGE_FILE_MB)


def auto_skip_mb() -> int:
    return _values.get(KEY_AUTO, 0)


async def set_large_file_mb(value: int) -> None:
    _values[KEY_LARGE] = max(0, value)
    await asyncio.to_thread(_save_sync, KEY_LARGE, max(0, value))


async def set_auto_skip_mb(value: int) -> None:
    _values[KEY_AUTO] = max(0, value)
    await asyncio.to_thread(_save_sync, KEY_AUTO, max(0, value))


def is_large(file_size: int | None) -> bool:
    threshold = large_file_mb()
    return bool(file_size) and threshold > 0 and file_size >= threshold * MIB  # type: ignore[operator]


def should_auto_skip(file_size: int | None) -> bool:
    threshold = auto_skip_mb()
    return bool(file_size) and threshold > 0 and file_size > threshold * MIB  # type: ignore[operator]


def reset() -> None:
    """Forget cached values (tests)."""
    _values.clear()
