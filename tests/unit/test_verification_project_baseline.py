"""W6-013 §05 step 6 "inventaris ... baseline tes": the project's own tests run at the inventoried commit through the
verifier runner (and its tamper window), recorded as a trusted TEST_REPORT (commit, suite identity, passing and
failing ids) that the inventory - and so PlanningContext.repo_inventory - references."""

from __future__ import annotations

from pathlib import Path

import pytest

from aeos.core.errors import Blocked
from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, project_baseline
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import commit_file, create_project, git, make_repo, needs_git

FILES = {
    "pyproject.toml": "[project]\nname = 'demo'\n",
    "src/demo/__init__.py": "",
    "src/demo/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": ("from demo.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                           "def test_known_bug():\n    assert add(0.1, 0.2) == 0.3\n"),
    "tests/test_tools.py": ("import pytest\n\n\n@pytest.mark.integration\ndef test_needs_docker():\n"
                            "    assert False\n"),
}


def _project(client: AuthedClient, tmp_path: Path, name: str = "Baseline Demo",
             files: dict[str, str] | None = None) -> tuple[dict, Path, str]:
    repo = make_repo(tmp_path / "base-demo", files or FILES, origin="https://github.com/octo/base-demo.git")
    p = create_project(client, name=name)
    r = client.post(f"/api/projects/{p['id']}/repositories",
                    json={"provider": "GITHUB", "owner": "octo", "name": "base-demo", "local_path": str(repo)})
    assert r.status_code == 200, r.text
    return p, repo, r.json()["repository"]["id"]


@needs_git
def test_project_baseline_runs_the_repos_tests_at_the_inventory_commit(personal_client: AuthedClient, db,
                                                                       tmp_path: Path):
    from aeos.portfolio.inventory import inventory_for_project

    p, repo, _rid = _project(personal_client, tmp_path)
    with pytest.raises(Blocked) as exc:
        project_baseline.run_project_baseline(db, p["id"], actor="tester")
    assert exc.value.code == "INVENTORY_REQUIRED"
    db.rollback()
    inv = inventory_for_project(db, p["id"])
    db.commit()
    commit = inv["commit"]
    assert inv["tests"]["baseline"]["status"] == "NOT_RUN" and "project test baseline" in inv["tests"]["baseline"][
        "reason"]
    # the live checkout moves on after the inventory: the baseline still runs the inventoried commit
    commit_file(repo, "tests/test_later.py", "def test_later():\n    assert False\n")
    out = project_baseline.run_project_baseline(db, p["id"], actor="tester")
    db.commit()
    assert out["status"] == "FAIL" and out["commit"] == commit, out  # a measured baseline with one failing test
    assert out["passed"] == 1 and out["failed"] == 1 and out["skipped"] == 0
    assert out["failing_sample"] == ["tests.test_calc::test_known_bug"]
    assert out["runner"] == "process" and out["isolation"] == "WEAK"
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert ev.trusted and ev.origin == "verifier" and ev.kind == "TEST_REPORT" and ev.evidence_level == "E2"
    assert ev.environment["project_baseline_commit"] == commit
    data = collector.read_json(ev)
    assert data["python"]["passing"] == ["tests.test_calc::test_add"]
    ident = data["suite_identity"]
    assert ident["selection"] == ["tests"] and ident["files"] == 2 and ident["files_hash"].startswith("sha256:")
    assert data["tamper"]["status"] in ("PASS", "UNKNOWN") and "residual_risks" in data["runner"]
    assert "test_later" not in str(data["python"])  # never the moved checkout
    # the same step discipline as the verifier: the pytest step is sealed and ran in its own kill group
    assert data["python"]["result_seal"]["status"] == "PASS", data["python"]["result_seal"]
    assert data["tamper"]["step_containment"]["survivors"] == []
    # the inventory (and so planning's repo_inventory) now cites the baseline for exactly this commit
    inv2 = inventory_for_project(db, p["id"], commit=commit)
    db.commit()
    base = inv2["tests"]["baseline"]
    assert base["status"] == "FAIL" and base["evidence_id"] == ev.id and base["commit"] == commit
    assert base["failing_sample"] == ["tests.test_calc::test_known_bug"]
    # the moved HEAD has no baseline yet: NOT_RUN, never the older commit's result
    inv3 = inventory_for_project(db, p["id"])
    db.commit()
    assert inv3["commit"] == git(repo, "rev-parse", "HEAD") != commit
    assert inv3["tests"]["baseline"]["status"] == "NOT_RUN"


@needs_git
def test_project_baseline_api(personal_client: AuthedClient, db, tmp_path: Path):
    p, _repo, _rid = _project(personal_client, tmp_path, name="Baseline Api")
    r = personal_client.get(f"/api/projects/{p['id']}/test-baseline")
    assert r.status_code == 200 and r.json()["status"] == "NOT_RUN" and r.json()["source"]
    r = personal_client.post(f"/api/projects/{p['id']}/test-baseline", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVENTORY_REQUIRED", r.text
    from aeos.portfolio.inventory import inventory_for_project

    inventory_for_project(db, p["id"])
    db.commit()
    r = personal_client.post(f"/api/projects/{p['id']}/test-baseline", json={"timeout_seconds": 600})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FAIL" and r.json()["evidence_id"]
    got = personal_client.get(f"/api/projects/{p['id']}/test-baseline").json()
    assert got["evidence_id"] == r.json()["evidence_id"] and got["checked_at"]
    # the verifier's own per-run TEST_REPORTs (several per verification) never push the baseline out of view
    for i in range(210):
        collector.collect_evidence(db, kind="TEST_REPORT", origin="verifier", collector_identity="aeos-verifier/test",
                                   claim=f"run report {i}", content={"suite": "trusted-acceptance"},
                                   evidence_level="E2", project_id=p["id"], subject_type="run", subject_id=f"run-{i}",
                                   environment={"project_baseline_commit": got["commit"]})
    db.commit()
    later = personal_client.get(f"/api/projects/{p['id']}/test-baseline").json()
    assert later["evidence_id"] == r.json()["evidence_id"] and later["status"] == "FAIL", later


# A project test that leaves a detached process behind: it outlives its step and could write the control plane after
# the tamper window closed.
LEAKY_TEST = (
    "import os, subprocess, sys\nfrom demo.calc import add\n\n\n"
    "def test_add_and_leave_a_process_behind():\n"
    "    kw = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}\n"
    "    if os.name == 'nt':\n"
    "        kw['creationflags'] = 0x00000008 | 0x00000200\n"
    "    else:\n"
    "        kw['start_new_session'] = True\n"
    "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kw)\n"
    "    assert add(1, 2) == 3\n")


@needs_git
def test_a_process_left_behind_by_the_project_tests_makes_the_baseline_tampered(personal_client: AuthedClient, db,
                                                                                tmp_path: Path):
    """Same kill-group discipline as the verifier: the survivor is killed and the baseline is never a measurement."""
    from aeos.models.audit import AuditEvent
    from aeos.portfolio.inventory import inventory_for_project

    p, _repo, _rid = _project(personal_client, tmp_path, name="Baseline Leak",
                              files={**FILES, "tests/test_calc.py": LEAKY_TEST})
    inventory_for_project(db, p["id"])
    db.commit()
    out = project_baseline.run_project_baseline(db, p["id"], actor="tester")
    db.commit()
    assert out["status"] == "TAMPERED", out
    data = collector.read_json(db.get(EvidenceArtifact, out["evidence_id"]))
    assert data["tamper"]["status"] == "FAIL"
    survivors = data["tamper"]["step_containment"]["survivors"]
    assert [s["step"] for s in survivors] == ["project_baseline"], survivors
    assert data["python"]["result_seal"]["status"] == "PASS"  # the result itself was honest: the leak is the finding
    alert = db.query(AuditEvent).filter(AuditEvent.action == "verification.controller_modified").one()
    assert "left processes running" in (alert.reason or "")


@needs_git
def test_a_project_shadowing_the_test_runner_or_stdlib_is_not_run_under_the_seal(personal_client: AuthedClient, db,
                                                                                 tmp_path: Path):
    from aeos.portfolio.inventory import inventory_for_project

    p, _repo, _rid = _project(personal_client, tmp_path, name="Baseline Shadow",
                              files={**FILES, "src/json.py": "# would replace the standard library for pytest\n"})
    inventory_for_project(db, p["id"])
    db.commit()
    out = project_baseline.run_project_baseline(db, p["id"], actor="tester")
    db.commit()
    assert out["status"] == "NOT_RUN", out
    data = collector.read_json(db.get(EvidenceArtifact, out["evidence_id"]))
    assert data["python"]["status"] == "NOT_RUN" and data["python"]["shadowed_modules"] == ["json"]
    assert "no sealed measurement" in data["python"]["summary"]
    assert project_baseline.shadowing_modules(tmp_path / "nothing-here") == []
    # the project's own conventional test packages at its import root are expected there; a fake pytest is not
    root = tmp_path / "layout"
    for rel in ("test/__init__.py", "tests/__init__.py", "conftest.py", "pytest.py", "app/__init__.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("", encoding="utf-8")
    assert project_baseline.shadowing_modules(root) == ["pytest"]
