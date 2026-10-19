"""Runtime hygiene guards (master §06 table "runtime state never committed", §28 "health probes never
trigger LLM document regeneration"). §37 identity fields, §41 master independence and §44 volume metrics
are proven in test_inventory_views.py, test_policy_master_independence.py and test_evaluation_not_measured.py."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from aeos.core.paths import repo_root

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _porcelain() -> str:
    return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo_root(),
                          capture_output=True, text=True, check=True).stdout


def test_db_upgrade_seed_and_doctor_leave_the_source_tree_untouched(settings):
    """§06: the database, evidence, logs and caches of a real CLI run land in the data dir, never the repo."""
    from aeos.cli import app

    before = _porcelain()
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    assert runner.invoke(app, ["seed"]).exit_code == 0
    r = runner.invoke(app, ["doctor", "--group", "HOST"])
    assert r.exit_code in (0, 1, 2), r.output  # findings may fail; what matters is where state was written
    assert settings.layout.db_path.is_file()
    after = _porcelain()
    new = sorted(set(after.splitlines()) - set(before.splitlines()))
    assert not new, f"runtime state written into the source tree: {new}"
    tracked_state = subprocess.run(["git", "ls-files", "*.sqlite*", "*.db", "*.log", "task-data/*", "app-data/*"],
                                   cwd=repo_root(), capture_output=True, text=True, check=True).stdout
    assert tracked_state.strip() == ""


def test_health_probes_never_render_documents_or_call_a_model(settings, db, monkeypatch):
    """§28: doctor runs and inventory reconciliation never regenerate documents and never invoke Claude."""
    from aeos.claude_bridge import bridge
    from aeos.docs_engine import service as docs
    from aeos.doctor.runner import run_doctor
    from aeos.inventory.reconcile import reconcile
    from aeos.inventory.seed import run as seed_run
    from aeos.models.publishing import DocumentRecord, DocumentRevision
    from tests.unit.test_inventory_fakes import base_runner

    calls: list[str] = []
    for name in ("render_version", "render_document_set"):
        if hasattr(docs, name):
            monkeypatch.setattr(docs, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(bridge, "run_structured", lambda *a, **k: calls.append("claude"))
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: calls.append("claude-process"))
    seed_run(db)
    db.commit()
    docs_before = (db.query(DocumentRecord).count(), db.query(DocumentRevision).count())
    run_doctor(settings, db, groups=["HOST", "RUNTIME", "RECOVERY"], runner=base_runner())
    reconcile(db, settings, runner=base_runner())
    db.commit()
    assert calls == []
    assert (db.query(DocumentRecord).count(), db.query(DocumentRevision).count()) == docs_before
