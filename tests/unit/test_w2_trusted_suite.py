"""Owner-registered trusted acceptance suite for managed (non-platform) projects (ADR-0060 amendment
2026-10-18, §09/§19/§21: the grader lives outside every candidate and is read-only for workers).

The registration is a human SUPER_ADMIN action (CSRF, domain-scoped), copies exactly the reviewed
commit's ``tests/acceptance`` into the controller data dir and binds a POLICY decision to the suite hash;
a copy that no longer matches the hash is refused (fail closed), never used."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.errors import Blocked
from aeos.models.decisions import Decision
from aeos.models.portfolio import Project
from aeos.verification import suite
from tests.conftest import VIEWER_PW, create_viewer

SUITE_TEST = "def test_ok() -> None:\n    assert 1 + 1 == 2\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=Owner", "-c", "user.email=owner@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, timeout=60).stdout.strip()


def _repo(root: Path, *, with_suite: bool = True) -> tuple[Path, str]:
    repo = root / "proj"
    files = {"src/app.py": "VALUE = 1\n", "pyproject.toml": "[tool.ruff]\nline-length = 110\n"}
    if with_suite:
        files["tests/acceptance/test_app.py"] = SUITE_TEST
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _project(client, repo: Path, name: str = "Suite proj") -> str:
    r = client.post("/api/projects", json={
        "domain": "PERSONAL", "name": name, "idea": "proyek uji suite terpercaya",
        "repository": {"provider": "GITHUB", "owner": "o", "name": name.lower().replace(" ", "-"), "role": "project",
                       "local_path": str(repo), "default_branch": "main", "verify": False}})
    assert r.status_code == 200, r.text
    return r.json()["project"]["id"]


def _register(client, pid: str, **body):
    return client.post(f"/api/projects/{pid}/trusted-suite", json={"rationale": "suite ditinjau di main", **body})


@pytest.fixture
def git_required():
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git is required")


def test_owner_registers_suite_bound_to_a_policy_decision(personal_client, db, tmp_path, git_required):
    repo, head = _repo(tmp_path)
    pid = _project(personal_client, repo)
    r = _register(personal_client, pid, commit="main")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["source_commit"] == head and out["kind"] == "PROJECT" and out["files"] >= 1
    d = db.get(Decision, out["decision_id"])
    assert d.kind == "POLICY" and d.target_type == "trusted_suite" and d.target_id == pid
    assert d.target_hash == out["suite_hash"] and (d.actor_kind or "HUMAN") == "HUMAN"
    status = personal_client.get(f"/api/projects/{pid}/trusted-suite").json()
    assert status["status"] == "REGISTERED" and status["suite_hash"] == out["suite_hash"]
    # the copy lives in the controller data dir, outside the candidate/project repository
    ts = suite.resolve_trusted_suite(db, db.get(Project, pid))
    assert ts.kind == "PROJECT" and repo.resolve() not in ts.root.parents
    assert ts.root.joinpath("tests", "acceptance", "test_app.py").read_text(encoding="utf-8") == SUITE_TEST


def test_tampered_copy_is_refused_never_used(personal_client, db, tmp_path, git_required):
    repo, _head = _repo(tmp_path)
    pid = _project(personal_client, repo)
    assert _register(personal_client, pid).status_code == 201
    project = db.get(Project, pid)
    root = suite.resolve_trusted_suite(db, project).root
    root.joinpath("tests", "acceptance", "test_app.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    with pytest.raises(Blocked) as exc:
        suite.resolve_trusted_suite(db, project)
    assert exc.value.code == "TRUSTED_SUITE_TAMPERED"
    status = personal_client.get(f"/api/projects/{pid}/trusted-suite").json()
    assert status["status"] == "BLOCKED" and status["code"] == "TRUSTED_SUITE_TAMPERED"
    # a new owner registration supersedes the old decision and restores a matching copy
    again = _register(personal_client, pid)
    assert again.status_code == 201
    old = db.scalars(select(Decision).where(Decision.target_type == "trusted_suite",
                                            Decision.target_id == pid)).all()
    db.expire_all()
    assert sorted(x.status for x in old) == ["ACTIVE", "SUPERSEDED"]
    assert personal_client.get(f"/api/projects/{pid}/trusted-suite").json()["status"] == "REGISTERED"


def test_commit_without_acceptance_suite_is_blocked(personal_client, tmp_path, git_required):
    repo, _head = _repo(tmp_path, with_suite=False)
    pid = _project(personal_client, repo)
    r = _register(personal_client, pid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_TRUSTED_SUITE"
    status = personal_client.get(f"/api/projects/{pid}/trusted-suite").json()
    assert status["status"] == "BLOCKED" and status["code"] == "NO_TRUSTED_SUITE"


def test_registration_is_human_admin_only_with_csrf(personal_client, db, tmp_path, git_required):
    repo, _head = _repo(tmp_path)
    pid = _project(personal_client, repo)
    no_csrf = personal_client.client.post(f"/api/projects/{pid}/trusted-suite",
                                          json={"rationale": "tanpa csrf token"})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "CSRF_FAILED"
    create_viewer(db)
    from fastapi.testclient import TestClient

    from tests.conftest import AuthedClient

    with TestClient(personal_client.client.app, base_url="http://testserver") as tc:
        viewer = AuthedClient(tc)
        viewer.login("viewer", VIEWER_PW)
        viewer.select_domain(Domain.PERSONAL)
        r = _register(viewer, pid)
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"
    assert db.scalar(select(Decision).where(Decision.target_type == "trusted_suite")) is None


def test_platform_project_uses_the_controller_checkout(db):
    project = Project(project_key="dly-aeos", name="DLY AEOS", domain="PERSONAL", is_platform=True)
    db.add(project)
    db.flush()
    assert suite.resolve_trusted_suite(db, project).kind == "CONTROLLER"
