"""Inventory -> Readiness invalidation, OBSERVED-lens honesty and probe environment (completeness review).

§50 rows covered:
* "Config/model/provider berubah setelah doctor" / "Binary/model route/config berganti dari approved
  pin" -> a version/identity change seen by reconciliation, or a new InstallManifest, makes exactly the
  dependent checks STALE (recheck before the capability is used again); unrelated checks stay fresh.
* "Tool ditemukan dalam README atau lockfile" -> a declaration never hides a fresh NOT_INSTALLED.
* §12 -> no probe child process inherits ANTHROPIC_* variables, even without an explicit env.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from aeos.core.paths import repo_root
from aeos.doctor.runner import latest_results, run_doctor
from aeos.inventory import manifest as mf
from aeos.inventory import views
from aeos.inventory.probes import SystemProbeRunner
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run
from aeos.models.audit import Event
from tests.unit.test_inventory_fakes import base_runner, git_handler, ok


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


def _claude_runner(version: str):
    r = base_runner()
    r.add_binary("claude")
    r.outputs[("claude", ("--version",))] = ok(f"{version} (Claude Code)\n")
    return r


def test_reconciled_binary_version_change_marks_only_dependent_checks_stale(seeded, settings):
    r1 = _claude_runner("2.1.302")
    run_doctor(settings, seeded, groups=["CLAUDE_NATIVE"], runner=r1)
    seeded.commit()
    first = reconcile(seeded, settings, runner=r1, component_id="claude-code")
    seeded.commit()
    assert first.summary["invalidated"] == {}  # nothing to compare with yet
    same = reconcile(seeded, settings, runner=r1, component_id="claude-code")
    seeded.commit()
    assert same.summary["invalidated"] == {}  # unchanged version: nothing invalidated
    assert latest_results(seeded, ["claude.binary"])["claude.binary"].status == "VERIFIED"

    r2 = _claude_runner("2.1.400")  # the binary was upgraded in place
    run = reconcile(seeded, settings, runner=r2, component_id="claude-code")
    seeded.commit()
    inv = run.summary["invalidated"]["claude-code"]
    assert inv["invalidators"] == ["binary:claude"] and inv["checks_marked_stale"] >= 1
    assert any(c["field"] == "version" and c["before"] == "2.1.302" and c["after"] == "2.1.400"
               for c in inv["changes"])
    rows = latest_results(seeded, ["claude.binary", "claude.env", "claude.auth"])
    assert rows["claude.binary"].status == "STALE"
    assert "2.1.302 -> 2.1.400" in rows["claude.binary"].observed_evidence["stale_reason"]
    assert rows["claude.env"].status != "STALE"  # an environment check does not depend on the binary
    assert seeded.scalar(select(Event).where(Event.kind == "inventory.component.changed")) is not None
    assert seeded.scalar(select(Event).where(Event.kind == "doctor.check.stale")) is not None


def test_new_commit_only_invalidates_head_dependent_repo_checks(seeded, settings):
    r = base_runner()
    run_doctor(settings, seeded, groups=["REPOSITORY"], runner=r)
    seeded.commit()
    reconcile(seeded, settings, runner=r, component_id="dly-aeos-platform-repo")
    seeded.commit()
    r.outputs["git"] = git_handler(head="c" * 40)  # a new commit on the platform repo
    run = reconcile(seeded, settings, runner=r, component_id="dly-aeos-platform-repo")
    seeded.commit()
    assert run.summary["invalidated"]["dly-aeos-platform-repo"]["invalidators"] == ["repo:head"]
    rows = latest_results(seeded, ["repo.state", "repo.origin"])
    assert rows["repo.state"].status == "STALE"
    assert rows["repo.origin"].status == "VERIFIED"  # identity unchanged -> origin check stays fresh

    r.outputs["git"] = git_handler(head="c" * 40, remote="https://github.com/someone/fork.git")
    run = reconcile(seeded, settings, runner=r, component_id="dly-aeos-platform-repo")
    seeded.commit()
    assert "repo:origin" in run.summary["invalidated"]["dly-aeos-platform-repo"]["invalidators"]
    assert latest_results(seeded, ["repo.origin"])["repo.origin"].status == "STALE"


def test_manifest_change_invalidates_manifest_dependent_checks(seeded, settings, tmp_path: Path):
    run_doctor(settings, seeded, groups=["REPOSITORY"], runner=base_runner())
    seeded.commit()
    assert latest_results(seeded, ["repo.install_manifest"])["repo.install_manifest"].status == "VERIFIED"
    data = yaml.safe_load((repo_root() / mf.MANIFEST_REL).read_text(encoding="utf-8"))
    data["components"] = [c for c in data["components"] if c["component_id"] != "ralph"]
    root = tmp_path / "repo"
    for rel in (mf.SCHEMA_REL, mf.MASTER_REL):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text((repo_root() / rel).read_text(encoding="utf-8"), encoding="utf-8")
    (root / mf.MANIFEST_REL).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = mf.apply_manifest(seeded, actor="test", root=root)
    seeded.commit()
    assert result["stored_new_record"] is True and result["checks_marked_stale"] >= 1
    row = latest_results(seeded, ["repo.install_manifest"])["repo.install_manifest"]
    assert row.status == "STALE" and "InstallManifest berubah" in row.observed_evidence["stale_reason"]
    again = mf.apply_manifest(seeded, actor="test", root=root)  # same content: nothing new to invalidate
    assert again["stored_new_record"] is False and again["checks_marked_stale"] == 0


def test_declaration_never_hides_a_fresh_not_installed(seeded, settings, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "frontend").mkdir(parents=True)
    lock = {"lockfileVersion": 3, "packages": {"": {"devDependencies": {"vitest": "^5.0.0"}},
                                               "node_modules/vitest": {"version": "5.0.5"}}}
    (repo / "frontend" / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n', encoding="utf-8")
    reconcile(seeded, settings, runner=base_runner(), repo=repo)  # node_modules absent: not installed
    seeded.commit()
    nodes = {n["id"]: n for n in views.system_map(seeded, lens="OBSERVED", domain="PERSONAL")["nodes"]}
    vitest = nodes["vitest"]
    assert vitest["status"] == "NOT_INSTALLED" and vitest["present"] is False  # lockfile entry != installed
    detail = views.component_detail(seeded, "vitest", domain="PERSONAL")
    assert detail["lifecycle"]["installation"]["value"] == "NOT_INSTALLED"
    assert any(d["source"] == "frontend/package-lock.json" for d in detail["dependencies"])


def test_system_probe_runner_scrubs_provider_env_without_explicit_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "z" * 30)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AEOS_PROBE_MARKER", "kept")
    code = ("import os, json; print(json.dumps({k: os.environ.get(k) for k in "
            "('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'AEOS_PROBE_MARKER')}))")
    rr = SystemProbeRunner().run([sys.executable, "-c", code], timeout=30)
    assert rr.ok, rr
    seen = json.loads(rr.stdout)
    assert seen == {"ANTHROPIC_API_KEY": None, "ANTHROPIC_BASE_URL": None, "CLAUDE_CODE_USE_BEDROCK": None,
                    "AEOS_PROBE_MARKER": "kept"}


def test_system_probe_runner_kills_on_timeout():
    rr = SystemProbeRunner().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert rr.error == "TIMEOUT" and rr.ok is False and rr.duration_ms < 20_000


def test_component_invalidator_targets_only_checks_about_that_component(seeded, settings):
    from aeos.doctor import facade as doctor
    from aeos.inventory.reconcile import invalidators_for
    from aeos.models.inventory import ComponentReference

    change = {"changes": [{"dimension": "installation", "field": "version", "before": "3.12.1", "after": "3.13.0"}]}
    assert invalidators_for(seeded.get(ComponentReference, "python-runtime"), change) == ["component:python-runtime"]
    assert invalidators_for(seeded.get(ComponentReference, "openbao"), change) == ["binary:bao"]
    run_doctor(settings, seeded, groups=["RUNTIME", "HOST"], runner=base_runner())
    seeded.commit()
    n = doctor.invalidate(seeded, invalidator="component:python-runtime", reason="python diganti")
    seeded.commit()
    rows = latest_results(seeded, ["runtime.python", "runtime.node", "host.platform"])
    assert n == 1 and rows["runtime.python"].status == "STALE"
    assert rows["runtime.node"].status != "STALE" and rows["host.platform"].status != "STALE"
