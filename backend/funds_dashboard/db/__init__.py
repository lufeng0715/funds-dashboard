"""Database session management."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str):
    """Build a SQLAlchemy engine.

    SQLite needs `check_same_thread=False` for FastAPI / scheduler use
    where multiple threads share the engine; Postgres has no such
    quirk so we conditionalize.
    """
    if database_url.startswith("sqlite"):
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
    return create_engine(database_url, future=True)


_SessionLocal: sessionmaker | None = None


def init_sessionmaker(database_url: str) -> sessionmaker:
    """Bootstrap the process-wide sessionmaker.

    Called once from the FastAPI / scheduler entry points so request
    handlers can grab a session without re-creating the engine.
    """
    global _SessionLocal
    engine = make_engine(database_url)
    _SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standard context-manager session.

    Commits on clean exit, rolls back on exception, always closes.
    Use this in scheduler jobs / CLI tools where you don't have a
    FastAPI request-scoped dependency to lean on.
    """
    if _SessionLocal is None:
        raise RuntimeError(
            "session factory not initialized — call init_sessionmaker() first"
        )
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session() -> Iterator[Session]:
    """FastAPI dependency that yields one committing session per request."""
    with session_scope() as session:
        yield session
