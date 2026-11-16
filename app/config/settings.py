from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MEDIA_TYPES = ("photo", "video", "document", "audio", "voice", "video_note", "animation")


@dataclass(frozen=True, slots=True)
class AppSettings:
    api_id: int
    api_hash: str
    session_string: str
    bot_token: str | None
    database_url: str
    source_chat: str | int | None
    dest_chat: str | int | None
    log_level: str
    data_dir: Path
    min_delay_seconds: float
    max_delay_seconds: float
    flood_wait_max_retries: int
    sync_history_on_start: bool
    media_types: tuple[str, ...] = field(default_factory=lambda: DEFAULT_MEDIA_TYPES)
    large_file_mb: int = 500
    transfer_timeout_base_seconds: float = 120.0
    transfer_min_speed_kbps: float = 128.0
    transfer_stall_seconds: float = 120.0
    transfer_max_retries: int = 2
    progress_log_seconds: float = 30.0
    live_refresh_seconds: float = 3.0


def _parse_required_int(value: str | None, name: str) -> int:
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _parse_optional_chat_reference(value: str | None) -> str | int | None:
    """SOURCE_CHAT_ID/DEST_CHAT_ID are optional: they only seed the mirror
    config once, the first time it runs. After that, /setsource and
    /setdest (backed by the database) are the source of truth."""
    if value is None or value.strip() == "":
        return None
    stripped = value.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def _parse_float(value: str | None, default: float, name: str) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be a number") from exc


def _parse_non_negative_int(value: str | None, default: int, name: str) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc
    if parsed < 0:
        raise RuntimeError(f"Environment variable {name} must be >= 0")
    return parsed


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_media_types(value: str | None) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return DEFAULT_MEDIA_TYPES

    types = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    invalid = [item for item in types if item not in DEFAULT_MEDIA_TYPES]
    if invalid:
        raise RuntimeError(
            "MEDIA_TYPES contains unsupported values: "
            + ", ".join(invalid)
            + f". Supported values: {', '.join(DEFAULT_MEDIA_TYPES)}"
        )
    return types or DEFAULT_MEDIA_TYPES


@lru_cache(maxsize=1)
def load_settings() -> AppSettings:
    api_id = _parse_required_int(os.getenv("API_ID"), "API_ID")
    api_hash = os.getenv("API_HASH")
    session_string = os.getenv("SESSION_STRING")

    missing: list[str] = []
    if not api_hash:
        missing.append("API_HASH")
    if not session_string:
        missing.append("SESSION_STRING")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Run `python -m scripts.generate_session` to create a SESSION_STRING."
        )

    assert api_hash is not None
    assert session_string is not None

    bot_token = os.getenv("BOT_TOKEN", "").strip() or None

    source_chat = _parse_optional_chat_reference(os.getenv("SOURCE_CHAT_ID"))
    dest_chat = _parse_optional_chat_reference(os.getenv("DEST_CHAT_ID"))

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/telegram_mirror.db")
    data_dir = Path(os.getenv("DATA_DIR", "data"))

    min_delay = _parse_float(os.getenv("MIN_DELAY_SECONDS"), 3.0, "MIN_DELAY_SECONDS")
    max_delay = _parse_float(os.getenv("MAX_DELAY_SECONDS"), 7.0, "MAX_DELAY_SECONDS")
    if min_delay < 0 or max_delay < min_delay:
        raise RuntimeError(
            "MIN_DELAY_SECONDS must be >= 0 and MAX_DELAY_SECONDS must be >= MIN_DELAY_SECONDS"
        )

    flood_wait_max_retries = _parse_required_int(
        os.getenv("FLOOD_WAIT_MAX_RETRIES", "5"), "FLOOD_WAIT_MAX_RETRIES"
    )
    sync_history_on_start = _parse_bool(os.getenv("SYNC_HISTORY_ON_START"), True)
    media_types = _parse_media_types(os.getenv("MEDIA_TYPES"))

    large_file_mb = _parse_non_negative_int(os.getenv("LARGE_FILE_MB"), 500, "LARGE_FILE_MB")
    timeout_base = _parse_float(
        os.getenv("TRANSFER_TIMEOUT_BASE_SECONDS"), 120.0, "TRANSFER_TIMEOUT_BASE_SECONDS"
    )
    min_speed = _parse_float(os.getenv("TRANSFER_MIN_SPEED_KBPS"), 128.0, "TRANSFER_MIN_SPEED_KBPS")
    stall_seconds = _parse_float(
        os.getenv("TRANSFER_STALL_SECONDS"), 120.0, "TRANSFER_STALL_SECONDS"
    )
    max_retries = _parse_non_negative_int(
        os.getenv("TRANSFER_MAX_RETRIES"), 2, "TRANSFER_MAX_RETRIES"
    )
    progress_log_seconds = _parse_float(
        os.getenv("PROGRESS_LOG_SECONDS"), 30.0, "PROGRESS_LOG_SECONDS"
    )
    live_refresh_seconds = _parse_float(
        os.getenv("LIVE_REFRESH_SECONDS"), 3.0, "LIVE_REFRESH_SECONDS"
    )
    if timeout_base <= 0 or min_speed <= 0 or stall_seconds <= 0:
        raise RuntimeError(
            "TRANSFER_TIMEOUT_BASE_SECONDS, TRANSFER_MIN_SPEED_KBPS and "
            "TRANSFER_STALL_SECONDS must be greater than 0"
        )

    return AppSettings(
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        bot_token=bot_token,
        database_url=database_url,
        source_chat=source_chat,
        dest_chat=dest_chat,
        log_level=log_level,
        data_dir=data_dir,
        min_delay_seconds=min_delay,
        max_delay_seconds=max_delay,
        flood_wait_max_retries=flood_wait_max_retries,
        sync_history_on_start=sync_history_on_start,
        media_types=media_types,
        large_file_mb=large_file_mb,
        transfer_timeout_base_seconds=timeout_base,
        transfer_min_speed_kbps=min_speed,
        transfer_stall_seconds=stall_seconds,
        transfer_max_retries=max_retries,
        progress_log_seconds=progress_log_seconds,
        live_refresh_seconds=max(1.0, live_refresh_seconds),
    )


SETTINGS = load_settings()

API_ID = SETTINGS.api_id
API_HASH = SETTINGS.api_hash
SESSION_STRING = SETTINGS.session_string
BOT_TOKEN = SETTINGS.bot_token
DATABASE_URL = SETTINGS.database_url
SOURCE_CHAT_ID = SETTINGS.source_chat
DEST_CHAT_ID = SETTINGS.dest_chat
LOG_LEVEL = SETTINGS.log_level
DATA_DIR = SETTINGS.data_dir
MIN_DELAY_SECONDS = SETTINGS.min_delay_seconds
MAX_DELAY_SECONDS = SETTINGS.max_delay_seconds
FLOOD_WAIT_MAX_RETRIES = SETTINGS.flood_wait_max_retries
SYNC_HISTORY_ON_START = SETTINGS.sync_history_on_start
MEDIA_TYPES = SETTINGS.media_types
LARGE_FILE_MB = SETTINGS.large_file_mb
TRANSFER_TIMEOUT_BASE_SECONDS = SETTINGS.transfer_timeout_base_seconds
TRANSFER_MIN_SPEED_KBPS = SETTINGS.transfer_min_speed_kbps
TRANSFER_STALL_SECONDS = SETTINGS.transfer_stall_seconds
TRANSFER_MAX_RETRIES = SETTINGS.transfer_max_retries
PROGRESS_LOG_SECONDS = SETTINGS.progress_log_seconds
LIVE_REFRESH_SECONDS = SETTINGS.live_refresh_seconds
