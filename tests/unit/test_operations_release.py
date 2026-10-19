"""Released controller build / promote / rollback (master §09, §40).

§50 acceptance rows covered:
* "Upgrade controller gagal -> Rilis lama dan data/evidence tetap dapat dipulihkan": a failed smoke
  test (or failed decision/compatibility) keeps the previous release active; a pre-upgrade backup with
  a restore test exists.
* "Candidate DLY AEOS mengubah aktif policy/verifier/controller -> Ditolak boundary": promote/rollback
  have no HTTP route and refuse inside worker contexts; only main commits from a clean tree are built.
* "Migration gagal atau rollback data tidak mungkin -> jangan klaim rollback berhasil dari revert code":
  rollback is refused when the database is ahead of the previous release.
"""

from __future__ import annotations

import io
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked, PolicyDenied
from aeos.core.paths import repo_root
from aeos.models.audit import AuditEvent
from aeos.models.operations_ext import OpsAlert
from aeos.operations import release
from tests.conftest import create_admin
from tests.unit.test_operations_fakes import block_module, fake_policy, fake_verification

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

GIT_ID = ["-c", "user.name=AEOS Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
          "-c", "core.autocrlf=false"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True, capture_output=True, text=True).stdout


def _real_head() -> str:
    """Head of the real migration history (not hardcoded: new revisions are added by later work)."""
    from alembic.script import ScriptDirectory

    from aeos.core.migrate import alembic_config

    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, heads
    return heads[0]


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "platform"
    (repo / "backend").mkdir(parents=True)
    shutil.copytree(repo_root() / "backend" / "aeos", repo / "backend" / "aeos",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "release base")
    return repo


def _commit_change(repo: Path, name: str = "CHANGELOG.txt", text: str = "next\n") -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"change {name}")


def ok_smoke(settings, release_dir, *, db_source, timeout):
    import aeos

    return {"ok": True, "version": aeos.__version__, "health": {"status_code": 200, "body": {"status": "ok"}}}


def bad_smoke(settings, release_dir, *, db_source, timeout):
    return {"ok": False, "error": "health check failed", "health": {"status_code": 500}}


def no_server(_settings):
    return {"running": False}


@pytest.fixture
def operator(db):
    return create_admin(db).id


def test_build_refuses_dirty_tree(settings, git_repo):
    (git_repo / "untracked.txt").write_text("x", encoding="utf-8")
    with pytest.raises(Blocked) as exc:
        release.build_release(settings, repo=git_repo)
    assert exc.value.code == "RELEASE_DIRTY"


def test_build_refuses_commit_not_on_main(settings, git_repo):
    git(git_repo, "checkout", "-q", "-b", "feature/x")
    _commit_change(git_repo, "feature.txt")
    with pytest.raises(Blocked) as exc:
        release.build_release(settings, repo=git_repo)
    assert exc.value.code == "RELEASE_NOT_ON_MAIN"


def test_build_creates_artifact_with_provenance(settings, git_repo, db):
    prov = release.build_release(settings, repo=git_repo, builder="tester", session=db)
    head = git(git_repo, "rev-parse", "HEAD").strip()
    assert prov["commit"] == head and prov["release_id"].endswith(head[:12])
    assert prov["tree_hash"] == git(git_repo, "rev-parse", "HEAD^{tree}").strip()
    artifact = settings.layout.released_controller / prov["artifact"]
    assert artifact.is_file() and prov["artifact_sha256"] == release.file_sha256(artifact)
    head_rev = _real_head()
    assert prov["schema_head"] == head_rev and prov["schema_heads"] == [head_rev]  # copied real history
    assert prov["builder"]["actor"] == "tester" and prov["built_at"]
    assert prov["source"]["on_main"] and prov["source"]["clean_tree"]
    again = release.build_release(settings, repo=git_repo)
    assert again["already_built"] is True
    listed = release.list_releases(settings)
    assert listed["items"][0]["integrity"] == "OK" and listed["items"][0]["role"] == "AVAILABLE"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.release.build")) is not None


def test_parse_migrations_is_static():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("backend/aeos/migrations/versions/a_base.py", 'revision = "a1"\ndown_revision = None\n')
        zf.writestr("backend/aeos/migrations/versions/b_next.py",
                    'revision: str = "b2"\ndown_revision: str | None = "a1"\nraise SystemExit("never imported")\n')
    with zipfile.ZipFile(buf) as zf:
        graph = release.parse_migrations(zf)
    assert graph["heads"] == ["b2"] and graph["revisions"] == {"a1": [], "b2": ["a1"]}


def _alembic_db(settings, revision: str | None) -> None:
    conn = sqlite3.connect(settings.layout.db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("DELETE FROM alembic_version")
        if revision:
            conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        conn.commit()
    finally:
        conn.close()


def test_compatibility_rules(settings, db):
    prov = {"schema_heads": ["b2"], "migrations": {"a1": [], "b2": ["a1"]}}
    _alembic_db(settings, "a1")
    assert release.compatibility(settings, prov)["action"] == "upgrade"
    _alembic_db(settings, "b2")
    assert release.compatibility(settings, prov)["action"] == "none"
    _alembic_db(settings, "zz9")
    c = release.compatibility(settings, prov)
    assert not c["compatible"] and "newer" in c["reason"]
    old = {"schema_heads": [], "migrations": {}}
    assert not release.compatibility(settings, old)["compatible"]
    assert not release.compatibility(settings, {"schema_heads": ["x", "y"], "migrations": {"x": [], "y": []}})["compatible"]


def test_promote_keeps_previous_and_rollback_restores_it(settings, git_repo, db, operator, monkeypatch):
    fake_verification(monkeypatch)
    policy_log = fake_policy(monkeypatch)
    a = release.build_release(settings, repo=git_repo)
    _commit_change(git_repo)
    c = release.build_release(settings, repo=git_repo)
    assert a["release_id"] != c["release_id"]
    kw = {"session": db, "operator_user_id": operator, "smoke_runner": ok_smoke, "server_check": no_server}
    r1 = release.promote(settings, a["release_id"], **kw)
    assert r1["status"] == "SUCCEEDED", r1
    assert r1["pre_backup"]["backup_id"] and r1["pre_backup"]["restore_test"] == "PASSED"
    r2 = release.promote(settings, c["release_id"], **kw)
    assert r2["status"] == "SUCCEEDED"
    pointer = release.read_pointer(settings)
    assert pointer["active"]["release_id"] == c["release_id"]
    assert pointer["previous"]["release_id"] == a["release_id"]
    rb = release.rollback(settings, **kw)
    assert rb["status"] == "SUCCEEDED"
    pointer = release.read_pointer(settings)
    assert pointer["active"]["release_id"] == a["release_id"]
    assert pointer["previous"]["release_id"] == c["release_id"]
    assert [h["action"] for h in pointer["history"]] == ["promote", "promote", "rollback"]
    kinds = [d.kind for d in policy_log["recorded"]]
    assert kinds == ["PROMOTION"] * 3
    assert all(d.target_hash and d.actor_user_id == operator for d in policy_log["recorded"])
    roles = {i["release_id"]: i["role"] for i in release.list_releases(settings)["items"]}
    assert roles[a["release_id"]] == "ACTIVE" and roles[c["release_id"]] == "PREVIOUS"


def test_failed_smoke_keeps_old_release_active(settings, git_repo, db, operator, monkeypatch):
    fake_verification(monkeypatch)
    policy_log = fake_policy(monkeypatch)
    a = release.build_release(settings, repo=git_repo)
    kw = {"session": db, "operator_user_id": operator, "server_check": no_server}
    assert release.promote(settings, a["release_id"], smoke_runner=ok_smoke, **kw)["status"] == "SUCCEEDED"
    _commit_change(git_repo)
    b = release.build_release(settings, repo=git_repo)
    report = release.promote(settings, b["release_id"], smoke_runner=bad_smoke, **kw)
    assert report["status"] == "FAILED" and "smoke test failed" in report["reason"]
    assert report["active_after"] == a["release_id"]
    assert release.read_pointer(settings)["active"]["release_id"] == a["release_id"]
    assert report["pre_backup"]["backup_id"]  # recovery point exists
    assert policy_log["invalidated"] and policy_log["invalidated"][-1][1] == b["release_id"]
    alert = db.scalar(select(OpsAlert).where(OpsAlert.kind == "release_failure"))
    assert alert is not None and alert.status == "OPEN"
    failed = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.release.promote",
                                                AuditEvent.result == "FAILED"))
    assert failed is not None


def test_promote_fails_closed_without_policy_module(settings, git_repo, db, operator, monkeypatch):
    fake_verification(monkeypatch)
    block_module(monkeypatch, "aeos.policy.decisions")
    a = release.build_release(settings, repo=git_repo)
    report = release.promote(settings, a["release_id"], session=db, operator_user_id=operator,
                             smoke_runner=ok_smoke, server_check=no_server)
    assert report["status"] == "FAILED" and "fail closed" in report["reason"]
    assert release.read_pointer(settings)["active"] is None


def test_promote_refused_in_worker_context(settings, git_repo, db, operator, monkeypatch):
    a = release.build_release(settings, repo=git_repo)
    monkeypatch.setenv("AEOS_CAPABILITY_TOKEN", "x")
    with pytest.raises(PolicyDenied):
        release.promote(settings, a["release_id"], session=db, operator_user_id=operator, smoke_runner=ok_smoke)


def test_tampered_artifact_is_refused(settings, git_repo, db, operator, monkeypatch):
    fake_policy(monkeypatch)
    a = release.build_release(settings, repo=git_repo)
    artifact = settings.layout.released_controller / a["artifact"]
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(Blocked) as exc:
        release.promote(settings, a["release_id"], session=db, operator_user_id=operator, smoke_runner=ok_smoke)
    assert exc.value.code == "RELEASE_INTEGRITY"


def test_rollback_refused_when_database_is_ahead(settings, git_repo, db, operator, monkeypatch):
    fake_verification(monkeypatch)
    fake_policy(monkeypatch)
    kw = {"session": db, "operator_user_id": operator, "smoke_runner": ok_smoke, "server_check": no_server}
    a = release.build_release(settings, repo=git_repo)
    assert release.promote(settings, a["release_id"], **kw)["status"] == "SUCCEEDED"
    mig = git_repo / "backend" / "aeos" / "migrations" / "versions" / "r1_init.py"
    mig.write_text(f'revision = "r1"\ndown_revision = "{_real_head()}"\n', encoding="utf-8")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "add migration")
    b = release.build_release(settings, repo=git_repo)
    assert b["schema_head"] == "r1"
    _alembic_db(settings, "r1")  # database migrated by release b (simulated)
    assert release.promote(settings, b["release_id"], **kw)["status"] == "SUCCEEDED"
    report = release.rollback(settings, **kw)
    assert report["status"] == "FAILED"
    assert "aeos ops backup restore" in report["reason"]
    assert release.read_pointer(settings)["active"]["release_id"] == b["release_id"]


def test_no_http_route_for_promote_or_rollback(app, admin_client):
    paths = " ".join(getattr(r, "path", "") for r in app.routes)
    assert "promote" not in paths and "rollback" not in paths
    assert admin_client.post("/api/ops/releases/x/promote").status_code in (404, 405)
    assert admin_client.get("/api/ops/releases").json()["notice"].startswith("Promote/rollback hanya lewat CLI")


def test_real_smoke_subprocess_promotes_good_and_rejects_broken_release(settings, git_repo, db, operator, monkeypatch):
    """End-to-end: the extracted artifact's app factory runs in a subprocess on a temporary loopback
    port against a copy of the database; a release whose /api/health is not ok never becomes active."""
    fake_verification(monkeypatch)
    fake_policy(monkeypatch)
    kw = {"session": db, "operator_user_id": operator, "server_check": no_server, "smoke_timeout": 120}
    good = release.build_release(settings, repo=git_repo)
    report = release.promote(settings, good["release_id"], **kw)
    assert report["status"] == "SUCCEEDED", report
    assert report["smoke"]["health"]["status_code"] == 200
    system = git_repo / "backend" / "aeos" / "api" / "system.py"
    system.write_text(system.read_text(encoding="utf-8").replace('"status": "ok"', '"status": "broken"'),
                      encoding="utf-8")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "break health")
    broken = release.build_release(settings, repo=git_repo)
    report = release.promote(settings, broken["release_id"], **kw)
    assert report["status"] == "FAILED", report
    assert release.read_pointer(settings)["active"]["release_id"] == good["release_id"]


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "-n", "HEAD main", ""])
def test_build_rejects_option_like_refs(settings, git_repo, ref):
    """A ref is passed to git as an argument: option-like or malformed refs are refused up front."""
    from aeos.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed) as exc:
        release.build_release(settings, repo=git_repo, ref=ref)
    assert exc.value.code == "RELEASE_BAD_REF"


def test_smoke_error_text_is_redacted(settings, tmp_path, monkeypatch):
    """Child-process output is untrusted text: a credential-shaped string never reaches the report."""
    secret = "ghp_" + "R" * 36

    class Proc:
        returncode = 4
        stdout = '{"ok": false, "error": "boom ' + secret + '"}\n'
        stderr = "trace " + secret

    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: Proc())
    out = release._run_release_python(tmp_path, "print(1)", [], {}, tmp_path, 5)
    assert out["ok"] is False and secret not in str(out)
