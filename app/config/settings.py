from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class AppSettings:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: tuple[int, ...]
    database_url: str
    archive_chat: str
    log_level: str
    data_dir: Path


def _parse_required_int(value: str | None, name: str) -> int:
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("ADMIN_ID and ADMIN_IDS must contain integers") from exc


def _parse_admin_ids() -> tuple[int, ...]:
    admin_ids: list[int] = []

    single_admin_id = _parse_optional_int(os.getenv("ADMIN_ID"))
    if single_admin_id is not None:
        admin_ids.append(single_admin_id)

    raw_admin_ids = os.getenv("ADMIN_IDS", "")
    for raw_value in raw_admin_ids.split(","):
        value = raw_value.strip()
        if not value:
            continue
        admin_ids.append(_parse_required_int(value, "ADMIN_IDS"))

    return tuple(dict.fromkeys(admin_ids))


@lru_cache(maxsize=1)
def load_settings() -> AppSettings:
    api_id = _parse_required_int(os.getenv("API_ID"), "API_ID")
    api_hash = os.getenv("API_HASH")
    bot_token = os.getenv("BOT_TOKEN")

    missing: list[str] = []
    if not api_hash:
        missing.append("API_HASH")
    if not bot_token:
        missing.append("BOT_TOKEN")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

    assert api_hash is not None
    assert bot_token is not None

    archive_chat = os.getenv("ARCHIVE_CHAT_ID", "me").strip() or "me"
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/telegram_archive.db")
    data_dir = Path(os.getenv("DATA_DIR", "data"))

    return AppSettings(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        admin_ids=_parse_admin_ids(),
        database_url=database_url,
        archive_chat=archive_chat,
        log_level=log_level,
        data_dir=data_dir,
    )


SETTINGS = load_settings()

API_ID = SETTINGS.api_id
API_HASH = SETTINGS.api_hash
BOT_TOKEN = SETTINGS.bot_token
ADMIN_IDS = SETTINGS.admin_ids
ADMIN_ID = SETTINGS.admin_ids[0] if SETTINGS.admin_ids else 0
DATABASE_URL = SETTINGS.database_url
ARCHIVE_CHAT_ID = SETTINGS.archive_chat
LOG_LEVEL = SETTINGS.log_level
DATA_DIR = SETTINGS.data_dir
