"""Versioned migrations with pre-upgrade backup (§11 "migration terversi dan backup teruji", §09).

* Empty database  -> ``alembic upgrade head`` (or ``create_all`` while no revision exists yet).
* Existing database behind head -> online SQLite backup into ``<data>/backups`` first, then upgrade.
* Append-only/immutable triggers are (re)installed after every upgrade (idempotent).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from aeos.core.config import get_settings
from aeos.core.db import create_all, get_engine, install_append_only_triggers
from aeos.core.timeutil import utcnow

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def _head(cfg: Config) -> str | None:
    return ScriptDirectory.from_config(cfg).get_current_head()


def _current() -> str | None:
    with get_engine().connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _has_tables() -> bool:
    with get_engine().connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version'"
        ).scalar()
        return bool(rows)


def backup_sqlite(label: str) -> Path:
    """Consistent online backup using SQLite's backup API (safe with WAL)."""
    layout = get_settings().layout
    layout.backups.mkdir(parents=True, exist_ok=True)
    dest = layout.backups / f"{label}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    src = sqlite3.connect(layout.db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def upgrade_database() -> dict[str, Any]:
    cfg = alembic_config()
    head = _head(cfg)
    engine = get_engine()
    result: dict[str, Any] = {"head": head}
    if head is None:
        # Development bootstrap before the first revision exists.
        create_all(engine)
        result["action"] = "create_all"
        return result
    current = _current()
    if current == head:
        install_append_only_triggers(engine)
        result["action"] = "none"
        return result
    if current is None and _has_tables():
        # Created by create_all during development; align the version table without re-creating.
        create_all(engine)
        command.stamp(cfg, head)
        result["action"] = "stamped"
        return result
    if current is not None:
        result["backup"] = str(backup_sqlite(f"pre-upgrade-{current}"))
    command.upgrade(cfg, "head")
    install_append_only_triggers(engine)
    result["action"] = "upgraded"
    result["from"] = current
    return result


def migration_status() -> dict[str, Any]:
    cfg = alembic_config()
    return {"current": _current(), "head": _head(cfg), "database": str(get_settings().layout.db_path)}
