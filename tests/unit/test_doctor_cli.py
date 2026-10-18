"""`aeos doctor` bootstrap layer (§08 layer 1, §50 "Dependency inti belum terpasang").

Runs without server/LLM, works with an uninitialised data dir (results printed only, nothing
created), ASCII output, exit code 1 when a CAN_OPEN_DASHBOARD critical check fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from aeos.core import config as config_mod
from aeos.doctor.checks import host as host_mod
from aeos.inventory.probes import set_runner_override
from aeos.models.doctor_ext import DoctorRun
from aeos.models.inventory import CheckResult
from tests.unit.test_inventory_fakes import base_runner


@pytest.fixture
def fake_host(monkeypatch):
    r = base_runner()
    set_runner_override(r)
    monkeypatch.setattr(host_mod, "tcp_in_use", lambda h, p, timeout=1.0: False)
    monkeypatch.setattr(host_mod, "can_bind", lambda h, p: True)
    yield r
    set_runner_override(None)


def _cli():
    from aeos.cli import app

    return app


def test_doctor_runs_with_uninitialised_data_dir(tmp_path: Path, monkeypatch, fake_host):
    data = tmp_path / "never-created"
    monkeypatch.setenv("AEOS_DATA_DIR", str(data))
    monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "none.yaml"))
    config_mod.reset_settings_cache()
    try:
        result = CliRunner().invoke(_cli(), ["doctor", "--json"])
    finally:
        config_mod.reset_settings_cache()
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["database"] == "missing" and payload["persisted"] is False
    assert {"host.data_dir", "runtime.db", "auth.bootstrap"} <= set(payload["dashboard_critical_failures"])
    by_id = {r["check_id"]: r for r in payload["results"]}
    assert by_id["runtime.db"]["remediation"]["command"].endswith("db upgrade")
    assert by_id["auth.bootstrap"]["status"] == "UNKNOWN"
    assert not data.exists()  # the doctor never creates state as a side effect
    assert result.output.isascii()


def test_doctor_text_output_is_ascii_and_persists(settings, db, fake_host):
    result = CliRunner().invoke(_cli(), ["doctor"])
    assert result.exit_code in (0, 1)
    assert result.output.isascii()
    assert "Capabilities" in result.output and "Remedies" in result.output
    assert "DLY AEOS never runs them" in result.output
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(CheckResult)) > 30
    run = db.scalar(select(DoctorRun))
    assert run.trigger == "cli" and run.functional is False


def test_doctor_group_and_capability_filters(settings, db, fake_host):
    res = CliRunner().invoke(_cli(), ["doctor", "--json", "--group", "HOST"])
    payload = json.loads(res.output)
    assert {r["group"] for r in payload["results"]} == {"HOST"}
    res = CliRunner().invoke(_cli(), ["doctor", "--json", "--capability", "can_run_worker"])
    ids = {r["check_id"] for r in json.loads(res.output)["results"]}
    assert "worker.opencode" in ids and "host.port" not in ids
    skipped = {s["check_id"] for s in json.loads(res.output)["skipped"]}
    assert "containment.report" in skipped  # functional checks only with --functional
    bad = CliRunner().invoke(_cli(), ["doctor", "--capability", "CAN_MERGE"])
    assert bad.exit_code == 2


def test_doctor_list_subcommand(settings, fake_host):
    res = CliRunner().invoke(_cli(), ["doctor", "list"])
    assert res.exit_code == 0 and "claude.auth" in res.output and res.output.isascii()


def test_inventory_cli_reconcile_list_show(settings, db, fake_host):
    res = CliRunner().invoke(_cli(), ["inventory", "reconcile", "--component", "git"])
    assert res.exit_code == 0, res.output
    assert res.output.isascii() and "git" in res.output
    res = CliRunner().invoke(_cli(), ["inventory", "list", "--requirement", "CANDIDATE"])
    assert res.exit_code == 0 and "hermes" in res.output and res.output.isascii()
    res = CliRunner().invoke(_cli(), ["inventory", "show", "claude-code"])
    assert res.exit_code == 0 and "Claude Code" in res.output and res.output.isascii()
    res = CliRunner().invoke(_cli(), ["inventory", "show", "jira"])  # OFFICE-only in PERSONAL view
    assert res.exit_code == 1


def test_outdated_database_schema_is_reported_not_a_crash(settings, db, fake_host):
    """§50 'Dependency inti belum terpasang' for an existing DB created before a column existed (no
    Alembic revision yet): the bootstrap layer still prints every result and names the gap."""
    import sqlite3

    db.close()
    con = sqlite3.connect(str(settings.layout.db_path))
    try:
        con.execute("ALTER TABLE check_results DROP COLUMN title")
        con.commit()
    finally:
        con.close()
    res = CliRunner().invoke(_cli(), ["doctor", "--json"])
    assert res.exit_code == 1, res.output
    payload = json.loads(res.output)
    assert payload["database"] == "schema-outdated" and payload["persisted"] is False
    by_id = {r["check_id"]: r for r in payload["results"]}
    assert by_id["runtime.db"]["status"] == "FAILED" and by_id["runtime.db"]["error_code"] == "SCHEMA_OUTDATED"
    assert "check_results.title" in by_id["runtime.db"]["observed_evidence"]["columns_missing"]
    assert "runtime.db" in payload["dashboard_critical_failures"]
    text = CliRunner().invoke(_cli(), ["doctor"])
    assert "schema is older than this version" in text.output and text.output.isascii()
