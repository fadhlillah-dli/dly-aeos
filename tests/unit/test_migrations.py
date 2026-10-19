"""§11 versioned migrations: the Alembic history reproduces the model schema exactly, triggers are
installed, databases created by create_all are stamped, and a behind-head database is backed up."""

from __future__ import annotations

import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from aeos.core import config as config_mod
from aeos.core.db import Base, create_all, get_engine, import_all_models, init_engine
from aeos.core.migrate import migration_status, upgrade_database


def _script_head() -> str:
    """The single head of the Alembic history (revisions are added by module clusters)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from aeos.core.migrate import MIGRATIONS_DIR

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"the migration history must have exactly one head: {heads}"
    return heads[0]


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AEOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "none.yaml"))
    config_mod.reset_settings_cache()
    engine = init_engine(config_mod.get_settings())
    yield engine
    engine.dispose()
    config_mod.reset_settings_cache()


def test_fresh_upgrade_matches_models_and_installs_triggers(empty_db):
    result = upgrade_database()
    assert result["action"] == "upgraded" and result["head"] == _script_head()
    import_all_models()
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        triggers = {r[0] for r in conn.exec_driver_sql("select name from sqlite_master where type='trigger'")}
    assert diff == [], diff
    assert set(Base.metadata.tables) <= set(inspect(get_engine()).get_table_names())
    assert {"trg_audit_events_no_update", "trg_plan_versions_no_delete", "trg_decisions_immutable_cols"} <= triggers
    assert migration_status()["current"] == _script_head()
    assert upgrade_database()["action"] == "none"


def test_database_created_by_create_all_is_stamped_not_recreated(empty_db):
    create_all(empty_db)
    result = upgrade_database()
    assert result["action"] == "stamped" and migration_status()["current"] == _script_head()


def test_behind_head_database_is_backed_up_before_upgrade(empty_db, monkeypatch):
    """A database at an older revision is copied (SQLite online backup) before the upgrade runs (§09/§11)."""
    from pathlib import Path

    import aeos.core.migrate as migrate

    upgrade_database()  # now at the current head
    head = _script_head()
    calls: list[str] = []
    monkeypatch.setattr(migrate, "_head", lambda cfg: "9999_future")
    monkeypatch.setattr(migrate.command, "upgrade", lambda cfg, rev: calls.append(rev))
    result = upgrade_database()
    assert result["action"] == "upgraded" and result["from"] == head and calls == ["head"]
    backup = Path(result["backup"])
    assert backup.is_file() and backup.parent == config_mod.get_settings().layout.backups
    with sqlite3.connect(backup) as raw:  # the copy is a readable database at the old revision
        assert raw.execute("select version_num from alembic_version").fetchone()[0] == head
