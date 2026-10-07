"""FastAPI dependencies with an override-friendly SQLAlchemy session boundary."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from tokensql.db.session import create_db_engine, create_session_factory


engine = create_db_engine()
SessionFactory = create_session_factory(engine)


def get_session() -> Iterator[Session]:
    """Yield one transactional session.

    Tests and embedding applications can replace this dependency through
    app.dependency_overrides[get_session].
    """

    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
