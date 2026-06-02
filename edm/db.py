from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from edm.config import require_database


_engine: Engine | None = None
_engine_url: str | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Lazily build the engine. Rebuilds if the runtime DB URL changes (used by
    the auto-setup wizard after the user enters their connection string)."""
    global _engine, _engine_url, _SessionLocal
    url = require_database()
    if _engine is None or _engine_url != url:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        _engine_url = url
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Force the next get_engine() call to rebuild from current settings."""
    global _engine, _engine_url, _SessionLocal
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _engine_url = None
    _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
