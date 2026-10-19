"""Runtime hygiene and honesty guards (master §06 table "runtime state never committed", §28 "health
probes never trigger LLM document regeneration", §37 component identity fields, §41 "a master change does
not modify MCI/higher policy", §44 "do not judge by number of agents, messages or lines of code")."""

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


def test_component_identity_fields_are_present_and_logical(db, settings):
    """§37 Identity: stable id, kind, friendly name, function, category, upstream identity, instances."""
    import json
    from pathlib import Path

    from aeos.inventory import views
    from aeos.inventory.reconcile import reconcile
    from aeos.inventory.seed import run as seed_run
    from tests.unit.test_inventory_fakes import base_runner

    seed_run(db)
    db.commit()
    runner = base_runner()
    runner.add_binary("node", "v24.15.0")
    reconcile(db, settings, runner=runner)
    db.commit()
    ident = views.component_detail(db, "nodejs-runtime", domain="PERSONAL")["identity"]
    assert ident["component_id"] == "nodejs-runtime" and ident["kind"] and ident["friendly_name"]
    assert ident["function"] and ident["category"] and ident["category_label"]
    assert ident["instances"], "an observed install must appear as an instance"
    assert str(Path.home()) not in json.dumps(ident) and str(settings.data_dir) not in json.dumps(ident)
    unverified = views.component_detail(db, "ddorchestrator", domain="PERSONAL")["identity"]
    assert unverified["upstream_identity_status"] == "UNVERIFIED"


def test_master_change_never_changes_policy_snapshot_or_mci_status(db, monkeypatch, tmp_path):
    """§41: editing the master is documentation; policy comes only from recorded decisions."""
    from aeos.core import paths
    from aeos.policy import facade as policy

    def snapshot() -> tuple:
        office = policy.office_policy(db)
        return (office.get("office_authorization"), office.get("external_worker_allowed"))

    before = snapshot()
    fake_root = tmp_path / "repo"
    shutil.copytree(repo_root() / "policy", fake_root / "policy")
    (fake_root / "docs" / "architecture").mkdir(parents=True)
    master = (repo_root() / "docs" / "architecture" / "DLY_AEOS_Master_Architecture_Final.md").read_text(encoding="utf-8")
    (fake_root / "docs" / "architecture" / "DLY_AEOS_Master_Architecture_Final.md").write_text(
        master + "\n\nOFFICE external_worker_allowed: true. MCI status: VERIFIED.\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: fake_root)
    assert snapshot() == before and before[1] is False
    mci = (fake_root / "policy" / "mci-reference.yaml").read_text(encoding="utf-8")
    status = mci.split("status:", 1)[1].splitlines()[0].split("#", 1)[0].strip()
    assert status != "VERIFIED"  # the MCI reference status is unaffected by master text


def test_leaderboard_never_ranks_by_agent_message_or_code_volume():
    """§44: no metric or sort key counts agents, messages or lines of code."""
    from aeos.evaluation import definitions
    from aeos.evaluation.service import compute_metrics

    metrics = compute_metrics([{"status": "ACCEPTED", "first_pass": True, "agents": 9, "messages": 400,
                                "lines_of_code": 9000}])
    keys = " ".join(metrics).lower()
    for banned in ("agent", "message", "lines", "loc"):
        assert banned not in keys, banned
    assert definitions.NOT_MEASURED == ["jumlah agent", "jumlah pesan", "jumlah baris kode"]
    import inspect

    from aeos.evaluation import service

    rank_src = inspect.getsource(service._rank)
    assert "final_acceptance_rate" in rank_src and "agent" not in rank_src and "message" not in rank_src
