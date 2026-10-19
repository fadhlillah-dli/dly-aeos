"""Released controller: acceptance gating, running the ACTIVE release, config hash (master §09 zones, §31).

* Zone 3 "accepted release": a commit on main is built only with trusted provider-CI PASS for that exact
  commit, or an owner EXCEPTION decision bound to it (no acceptance -> RELEASE_NOT_ACCEPTED).
* "The active controller is the pinned released artifact": ``aeos serve`` runs the verified, extracted
  ACTIVE release; ``/api/health`` reports which code runs; the doctor flags a checkout controller.
* "Promotion uses the artifact/config hash": the runtime config + policy snapshot hash is recorded.
"""

from __future__ import annotations

import shutil
import subprocess
import time

import httpx
import pytest

from aeos.core.errors import Blocked
from aeos.operations import release
from aeos.operations._util import free_loopback_port
from tests.unit.test_operations_fakes import fake_verification
from tests.unit.test_operations_release import _commit_change, git, make_git_repo, no_server, ok_smoke

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def git_repo(tmp_path):
    return make_git_repo(tmp_path)


@pytest.fixture
def owner(db):
    from tests.conftest import create_admin

    return create_admin(db).id


def _head(repo) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def _ci(db, sha: str, conclusion: str):
    from aeos.delivery.provider import parse_check_runs, record_ci_evidence
    from aeos.models.portfolio import Project
    from aeos.portfolio import seed

    seed.run(db)
    project = db.query(Project).filter(Project.is_platform.is_(True)).one()
    payload = {"total_count": 2, "check_runs": [
        {"name": "backend (ubuntu-latest)", "status": "completed", "conclusion": "success", "head_sha": sha},
        {"name": "frontend", "status": "completed", "conclusion": conclusion, "head_sha": sha}]}
    ev = record_ci_evidence(db, project, parse_check_runs("o/r", sha, [payload]))
    db.commit()
    return ev


def test_build_refuses_commit_without_acceptance(settings, git_repo, db):
    with pytest.raises(Blocked) as exc:
        release.build_release(settings, repo=git_repo)  # no database session: cannot check -> fail closed
    assert exc.value.code == "RELEASE_ACCEPTANCE_UNVERIFIED"
    with pytest.raises(Blocked) as exc:
        release.build_release(settings, repo=git_repo, session=db)
    assert exc.value.code == "RELEASE_NOT_ACCEPTED"
    assert {c["basis"] for c in exc.value.details["checked"]} == {"provider_ci", "owner_exception"}
    assert not list(settings.layout.released_controller.glob("*.zip"))


def test_trusted_ci_pass_for_the_exact_commit_accepts_and_failure_does_not(settings, git_repo, db):
    sha = _head(git_repo)
    _ci(db, sha, "failure")
    with pytest.raises(Blocked) as exc:
        release.build_release(settings, repo=git_repo, session=db)
    assert exc.value.code == "RELEASE_NOT_ACCEPTED" and exc.value.details["checked"][0]["status"] == "FAIL"
    ev = _ci(db, sha, "success")
    prov = release.build_release(settings, repo=git_repo, session=db)
    assert prov["acceptance"]["status"] == "ACCEPTED" and prov["acceptance"]["basis"] == "provider_ci"
    assert prov["acceptance"]["evidence_id"] == ev.id
    # CI of another commit never accepts this one
    _commit_change(git_repo)
    with pytest.raises(Blocked):
        release.build_release(settings, repo=git_repo, session=db)


def test_owner_exception_is_bound_to_one_commit(settings, git_repo, db, owner):
    sha = _head(git_repo)
    did = release.record_acceptance_exception(db, commit=sha, operator_user_id=owner,
                                              reason="CI unavailable during the outage; reviewed locally")
    db.commit()
    prov = release.build_release(settings, repo=git_repo, session=db)
    assert prov["acceptance"] == {**prov["acceptance"], "status": "ACCEPTED", "basis": "owner_exception",
                                  "decision_id": did}
    _commit_change(git_repo)
    with pytest.raises(Blocked) as exc:
        release.build_release(settings, repo=git_repo, session=db)
    assert exc.value.code == "RELEASE_NOT_ACCEPTED"
    with pytest.raises(Exception):
        release.record_acceptance_exception(db, commit="HEAD", operator_user_id=owner, reason="x" * 20)


def _promote(settings, git_repo, db, owner, monkeypatch):
    fake_verification(monkeypatch)
    sha = _head(git_repo)
    release.record_acceptance_exception(db, commit=sha, operator_user_id=owner, reason="accepted for this test run")
    db.commit()
    prov = release.build_release(settings, repo=git_repo, session=db)
    db.commit()
    report = release.promote(settings, prov["release_id"], session=db, operator_user_id=owner,
                             smoke_runner=ok_smoke, server_check=no_server)
    assert report["status"] == "SUCCEEDED", report
    assert report["acceptance"]["basis"] == "owner_exception"
    return prov, report


def test_promotion_records_config_hash_and_drift_is_flagged(settings, git_repo, db, owner, monkeypatch):
    prov, report = _promote(settings, git_repo, db, owner, monkeypatch)
    active = release.read_pointer(settings)["active"]
    assert active["config_hash"] == report["config"]["config_hash"] and active["config_hash"].startswith("sha256:")
    ctl = release.controller_report(settings, db, probe=lambda s: None)
    assert ctl["config_drift"] is False
    changed = settings.model_copy(update={"session_idle_minutes": settings.session_idle_minutes + 1})
    assert release.controller_report(changed, db, probe=lambda s: None)["config_drift"] is True


def test_checkout_controller_is_not_the_active_release_and_doctor_fails(settings, git_repo, db, owner, monkeypatch):
    from aeos.core.timeutil import utcnow
    from aeos.doctor.checks.recovery import recovery_config_rollback
    from aeos.doctor.model import CheckContext

    prov, _ = _promote(settings, git_repo, db, owner, monkeypatch)
    ctl = release.running_controller(settings)  # this test process imports the editable checkout
    assert ctl["mode"] == "checkout" and ctl["matches_active"] is False and ctl["status"] == "NOT_ACTIVE_RELEASE"
    monkeypatch.setattr(release, "probe_server_controller", lambda s: None)
    ctx = CheckContext(settings=settings, session=db, runner=None, repo=git_repo, now=utcnow())  # type: ignore[arg-type]
    out = recovery_config_rollback(ctx)
    assert out.status == "FAILED" and out.error_code == "CONTROLLER_NOT_ACTIVE_RELEASE"
    assert "serve --release active" in (out.remedy.command or "")
    # the server's own report wins when one answers on host:port
    monkeypatch.setattr(release, "probe_server_controller",
                        lambda s: {"mode": "release", "release_id": prov["release_id"], "matches_active": True,
                                   "active_release_id": prov["release_id"]})
    assert recovery_config_rollback(ctx).status == "CONFIGURED"


def test_extracted_release_is_verified_and_repaired(settings, git_repo, db, owner, monkeypatch):
    prov, _ = _promote(settings, git_repo, db, owner, monkeypatch)
    prepared = release.prepare_active_release(settings)
    root = prepared["dir"]
    target = root / "backend" / "aeos" / "api" / "system.py"
    target.write_text("raise SystemExit('tampered')\n", encoding="utf-8")
    (root / "backend" / "aeos" / "planted.py").write_text("x = 1\n", encoding="utf-8")
    assert {p.split(":")[0] for p in release.verify_extracted(settings, release.load_provenance(
        settings, prov["release_id"]), root)} == {"modified", "unexpected"}
    again = release.prepare_active_release(settings)  # re-extracted from the verified artifact
    assert "tampered" not in (again["dir"] / "backend" / "aeos" / "api" / "system.py").read_text(encoding="utf-8")
    assert not (again["dir"] / "backend" / "aeos" / "planted.py").exists()
    artifact = settings.layout.released_controller / prov["artifact"]
    artifact.write_bytes(artifact.read_bytes() + b"x")
    with pytest.raises(Blocked) as exc:
        release.prepare_active_release(settings)
    assert exc.value.code == "RELEASE_INTEGRITY"


def test_no_active_release_refuses_release_mode(settings):
    with pytest.raises(Blocked) as exc:
        release.prepare_active_release(settings)
    assert exc.value.code == "NO_ACTIVE_RELEASE"


def test_serve_runs_the_promoted_release_and_health_reports_it(settings, git_repo, db, owner, monkeypatch):
    """Real promote -> the controller process imports AEOS from the extracted, verified release."""
    prov, _ = _promote(settings, git_repo, db, owner, monkeypatch)
    db.close()
    prepared = release.prepare_active_release(settings)
    argv, env = release.release_serve_command(settings, prepared)
    assert env["PYTHONPATH"] == str(prepared["dir"] / "backend")
    assert argv[1] == "-P"  # the cwd (normally the checkout) is never prepended to sys.path
    port = free_loopback_port()
    env.update({"AEOS_PORT": str(port), "AEOS_HOST": "127.0.0.1"})
    # started from the checkout's backend/ directory: the editable aeos package sits in the cwd, yet the
    # controller must import the release (``-P``), never the checkout
    from aeos.core.paths import repo_root

    proc = subprocess.Popen(argv, env=env, cwd=str(repo_root() / "backend"), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    try:
        body = None
        deadline = time.time() + 90
        while time.time() < deadline and proc.poll() is None:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0)
                body = r.json()
                break
            except httpx.HTTPError:
                time.sleep(0.3)
        assert body is not None, proc.stdout.read().decode("utf-8", "replace") if proc.poll() is not None else "timeout"
        assert body["status"] == "ok" and body["version"] == prov["version"]
        ctl = body["controller"]
        assert ctl["mode"] == "release" and ctl["release_id"] == prov["release_id"]
        assert ctl["commit"] == prov["commit"] and ctl["matches_active"] is True and ctl["status"] == "ACTIVE_RELEASE"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
