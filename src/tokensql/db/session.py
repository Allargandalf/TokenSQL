"""Database engine and session construction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from tokensql.config import get_settings


def create_db_engine(
    url: str | URL | None = None,
    *,
    echo: bool | None = None,
    sqlite_busy_timeout_ms: int | None = None,
) -> Engine:
    """Create an engine without opening a connection or creating a database file."""

    settings = get_settings()
    database_url = url or settings.database_url
    engine = create_engine(
        database_url,
        echo=settings.database_echo if echo is None else echo,
        pool_pre_ping=True,
    )

    if engine.dialect.name == "sqlite":
        timeout_ms = (
            settings.sqlite_busy_timeout_ms
            if sqlite_busy_timeout_ms is None
            else sqlite_busy_timeout_ms
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a transactional session and reliably close it."""

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
