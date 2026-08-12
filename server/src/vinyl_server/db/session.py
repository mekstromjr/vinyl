"""Engine + session factory.

Deliberately SYNC SQLAlchemy: this is a single-user service, FastAPI runs sync
endpoints in a threadpool, and the async stack's server-side-default pitfalls
(MissingGreenlet on attribute refresh after commit) buy nothing at this scale.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from vinyl_server.config import settings

_engine = None
_factory = None


def _get_factory() -> sessionmaker:
    global _engine, _factory
    if _factory is None:
        _engine = create_engine(settings().db_url, pool_pre_ping=True, pool_size=5)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _factory


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, commit on success."""
    session = _get_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
