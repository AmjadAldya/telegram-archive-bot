from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config.settings import DATABASE_URL


class Base(DeclarativeBase):
    pass


def _build_connect_args(database_url: str) -> dict[str, object]:
    url = make_url(database_url)
    if url.drivername.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def _prepare_sqlite_path(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return

    database_path = url.database
    if not database_path or database_path == ":memory:":
        return

    path = Path(database_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_engine():
    _prepare_sqlite_path(DATABASE_URL)
    return create_engine(
        DATABASE_URL,
        future=True,
        pool_pre_ping=True,
        connect_args=_build_connect_args(DATABASE_URL),
    )


SessionLocal = sessionmaker(
    bind=get_engine(),
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def build_alembic_config() -> Config:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


def init_db() -> None:
    command.upgrade(build_alembic_config(), "head")


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
