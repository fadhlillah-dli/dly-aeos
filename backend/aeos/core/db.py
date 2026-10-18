"""SQLite metadata store (§11 V1) with WAL, foreign keys and append-only guards.

The engine is created lazily from settings so tests can point at a temporary data dir.
Schema evolution goes through Alembic migrations (``aeos db upgrade``); ``create_all`` is only
used for tests and first-run bootstrap of an empty database.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from aeos.core.config import Settings, get_settings


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC in SQLite, always returns timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map: dict[Any, Any] = {datetime: UTCDateTime()}


_lock = threading.Lock()
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def build_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def init_engine(settings: Settings | None = None, url: str | None = None) -> Engine:
    """(Re)initialise the global engine. Tests call this with a temp data dir."""
    global _engine, _session_factory
    settings = settings or get_settings()
    with _lock:
        if _engine is not None:
            _engine.dispose()
        settings.layout.ensure()
        _engine = build_engine(url or settings.database_url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Tables whose rows must never be updated or deleted through the application (§34: local
# append-only table — explicitly NOT a WORM/tamper-proof claim against a host administrator).
APPEND_ONLY_TABLES = (
    "audit_events", "events", "evidence_artifacts", "plan_versions", "task_specs", "architecture_snapshots",
)

# Tables whose binding columns are immutable once written; lifecycle/status columns may change.
IMMUTABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "decisions": (
        "kind", "actor_user_id", "target_type", "target_id", "target_version", "target_hash",
        "allowed_operations", "auth_scope", "project_id", "domain", "created_at",
    ),
    "projects": ("domain",),  # §04: domain never changes via model output or manifest edit
    "acceptance_records": ("artifact_hash", "base_commit", "head_commit", "config_hash", "task_id", "run_id"),
}


def _table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    )


def install_append_only_triggers(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, cols in IMMUTABLE_COLUMNS.items():
            if not _table_exists(conn, table):
                continue
            conn.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_cols BEFORE UPDATE OF {', '.join(cols)} "
                f"ON {table} BEGIN SELECT RAISE(ABORT, '{table}: binding columns are immutable'); END"
            )
        for table in APPEND_ONLY_TABLES:
            if not _table_exists(conn, table):
                continue
            conn.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
            conn.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )


def import_all_models() -> None:
    """Register every mapper, including module-owned ``aeos/models/<module>_ext.py`` files."""
    import importlib
    import pkgutil

    import aeos.models as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"aeos.models.{info.name}")


def create_all(engine: Engine | None = None) -> None:
    """Create tables for an empty DB (tests / first run) and install guards."""
    import_all_models()
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    install_append_only_triggers(engine)
