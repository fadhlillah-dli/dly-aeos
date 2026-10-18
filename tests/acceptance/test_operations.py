"""Operations and recovery (§09 zones, §33, §40; ADR-0090/0091). §50 rows:

* 1.7 "Upgrade controller gagal" -> the old release and data/evidence stay recoverable per the recovery test;
* 1.36 "Restore backup belum pernah diuji" -> no "recovery ready" claim;
* 3.21 "Migration gagal atau rollback data tidak mungkin" -> stop/decision; a code revert is never claimed
  as a data rollback;
* 3.25 "Snapshot export dibuka setelah runtime berubah" -> scope/time/hash explicit; never "live inventory";
* 1.37 "Trace berisi secret/PII" -> redaction before storage and export (secrets: PASS; PII: strict xfail,
  ``BUG_PII``).

The release build, promotion (PROMOTION decision, pre-upgrade backup + restore test, smoke test in a
child process on a loopback port) and rollback all run for real against a git repository built from
this controller's own backend.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from aeos.core.paths import repo_root
from tests.acceptance.world import owner_id
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

GIT_ID = ["-c", "user.name=AEOS Acceptance", "-c", "user.email=acc@example.invalid", "-c", "commit.gpgsign=false",
          "-c", "core.autocrlf=false", "-c", "core.hooksPath="]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True, capture_output=True,
                          text=True).stdout


def _platform_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "platform"
    (repo / "backend").mkdir(parents=True)
    shutil.copytree(Path(repo_root()) / "backend" / "aeos", repo / "backend" / "aeos",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release base")
    return repo


def _no_server(_settings):
    return {"running": False}


# ---------------------------------------------------------------------------------------------- 1.7
def test_failed_controller_upgrade_keeps_the_old_release_and_recoverable_data(personal_client: AuthedClient, db,
                                                                              settings, tmp_path):
    from aeos.operations import release

    repo = _platform_repo(tmp_path)
    operator = owner_id(db)
    kw = {"session": db, "operator_user_id": operator, "server_check": _no_server, "smoke_timeout": 180}
    good = release.build_release(settings, repo=repo)
    report = release.promote(settings, good["release_id"], **kw)
    assert report["status"] == "SUCCEEDED", report
    assert report["smoke"]["health"]["status_code"] == 200
    # the next release is broken: its app factory cannot serve /api/health
    system = repo / "backend" / "aeos" / "api" / "system.py"
    system.write_text(system.read_text(encoding="utf-8").replace('"status": "ok"', '"status": "broken"'),
                      encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "broken health")
    bad = release.build_release(settings, repo=repo)
    failed = release.promote(settings, bad["release_id"], **kw)
    assert failed["status"] == "FAILED" and failed["steps"][-1]["step"] == "smoke"
    pointer = release.read_pointer(settings)
    assert pointer["active"]["release_id"] == good["release_id"], "the old release stays active"
    pre = failed["pre_backup"]
    assert pre and pre["backup_id"]
    backups = {b["backup_id"]: b for b in personal_client.get("/api/ops/backups").json()["items"]}
    assert backups[pre["backup_id"]]["ready"] is True, "data/evidence recoverable: restore test passed"
    alerts = personal_client.get("/api/ops/alerts").json()["items"]
    assert any("failed" in a.get("title", "").lower() for a in alerts)
    releases = personal_client.get("/api/ops/releases").json()
    assert releases["notice"].startswith("Promote/rollback hanya lewat CLI")
    assert personal_client.post(f"/api/ops/releases/{bad['release_id']}/promote").status_code in (404, 405)


# ---------------------------------------------------------------------------------------------- 1.36
def test_untested_backup_is_never_reported_as_recovery_ready(personal_client: AuthedClient, db, settings, host):
    created = personal_client.post("/api/ops/backups", json={"label": "nightly"})
    assert created.status_code == 200, created.text
    backup_id = created.json().get("backup_id") or created.json()["backup"]["backup_id"]
    item = next(b for b in personal_client.get("/api/ops/backups").json()["items"] if b["backup_id"] == backup_id)
    assert item["ready"] is False and item["readiness"] != "TESTED"
    recovery = personal_client.get("/api/control-center/overview").json()["sections"]["recovery"]
    assert recovery["status"] == "NOT-PROVEN" and "belum pernah diuji" in recovery["reason"]
    personal_client.post("/api/doctor/run", json={"group": "RECOVERY"})
    checks = {c["check_id"]: c for c in personal_client.get("/api/doctor/checks").json()["items"]}
    assert checks["recovery.restore_test"]["effective_status"] != "VERIFIED"
    tested = personal_client.post(f"/api/ops/backups/{backup_id}/restore-test", json={})
    assert tested.status_code == 200, tested.text
    item = next(b for b in personal_client.get("/api/ops/backups").json()["items"] if b["backup_id"] == backup_id)
    assert item["ready"] is True and item["last_restore_test"]["evidence_id"]
    recovery = personal_client.get("/api/control-center/overview").json()["sections"]["recovery"]
    assert recovery["status"] == "VERIFIED" and "terisolasi" in recovery["evidence_limit"]


# ---------------------------------------------------------------------------------------------- 3.21
def test_code_rollback_is_refused_when_data_cannot_roll_back(personal_client: AuthedClient, db, settings, tmp_path):
    from aeos.operations import release

    repo = _platform_repo(tmp_path)
    operator = owner_id(db)

    def smoke(settings_, release_dir, *, db_source, timeout):  # the smoke of B is not the subject here
        import aeos

        return {"ok": True, "version": aeos.__version__, "health": {"status_code": 200}}

    kw = {"session": db, "operator_user_id": operator, "server_check": _no_server, "smoke_runner": smoke}
    a = release.build_release(settings, repo=repo)
    assert release.promote(settings, a["release_id"], **kw)["status"] == "SUCCEEDED"
    versions = repo / "backend" / "aeos" / "migrations" / "versions"
    heads = release.read_pointer(settings)["active"]
    (versions / "zz_acceptance_r2.py").write_text(
        f'revision = "zz_acc_r2"\ndown_revision = {a.get("schema_head")!r}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add migration")
    b = release.build_release(settings, repo=repo)
    assert b["schema_head"] == "zz_acc_r2"
    # the database was migrated by release B (its live migration ran)
    con = sqlite3.connect(str(settings.layout.db_path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        con.execute("DELETE FROM alembic_version")
        con.execute("INSERT INTO alembic_version VALUES ('zz_acc_r2')")
        con.commit()
    finally:
        con.close()
    assert release.promote(settings, b["release_id"], **kw)["status"] == "SUCCEEDED"
    report = release.rollback(settings, **kw)
    assert report["status"] == "FAILED"
    assert "aeos ops backup restore" in report["reason"] and "does not roll back data" in report["reason"]
    assert release.read_pointer(settings)["active"]["release_id"] == b["release_id"], "nothing switched"
    assert heads["release_id"] == a["release_id"]
    hist = release.read_pointer(settings)["history"]
    assert hist[-1]["pre_backup_id"], "the exact backup to restore is known before any data decision"


# ---------------------------------------------------------------------------------------------- 3.25
def test_export_snapshot_is_scoped_timed_hashed_and_never_live(personal_client: AuthedClient, db, settings, host):
    from tests.acceptance.world import seed_catalog

    seed_catalog(db)
    first = personal_client.get("/api/ops/export", params={"kind": "inventory"}).json()
    assert first["mode"] == "SNAPSHOT" and first["generated_at"] and first["content_hash"].startswith("sha256:")
    assert first["scope"]["domain"] == "PERSONAL" and first["hash_covers"]
    assert "live" not in first["notice"].lower() or "bukan" in first["notice"].lower()
    # the runtime changes after the export
    personal_client.post("/api/inventory/refresh", json={})
    second = personal_client.get("/api/ops/export", params={"kind": "inventory"}).json()
    assert second["snapshot_id"] != first["snapshot_id"] and second["generated_at"] >= first["generated_at"]
    listed = personal_client.get("/api/ops/exports").json()
    assert listed["mode"] == "SNAPSHOT"
    ids = {i["snapshot_id"]: i for i in listed["items"]}
    assert ids[first["snapshot_id"]]["content_hash"] == first["content_hash"], "the old snapshot keeps its hash"
    snap = personal_client.post("/api/inventory/snapshots", json={"label": "sebelum perubahan"}).json()
    assert snap["created_at"] and snap["snapshot_hash"].startswith("sha256:")
    assert personal_client.get("/api/inventory/snapshots").json()["mode"] == "DESIGN"


# ---------------------------------------------------------------------------------------------- 1.37
def _trace(db, project_id: str, text: str):
    from aeos.audit import service as audit
    from aeos.verification import facade as verification

    ev = verification.collect_evidence(db, kind="LOG", origin="worker", collector_identity="opencode-worker",
                                       claim=f"stderr: {text}", content=f"request trace\n{text}\n",
                                       project_id=project_id, domain="PERSONAL")
    audit.record(db, actor_type="service", actor_id="worker_runtime", action="worker.trace", reason=f"retry: {text}",
                 details={"trace": text}, project_id=project_id, domain="PERSONAL")
    db.commit()
    return ev


def test_trace_secret_is_redacted_before_storage_and_export(personal_client: AuthedClient, db, settings, host):
    from tests.acceptance.support import fake_token
    from tests.acceptance.world import create_project

    token = fake_token()
    project = create_project(personal_client, name="Trace App")
    ev = _trace(db, project["id"], f"Authorization: Bearer {token} OPENROUTER_API_KEY={token}")
    assert token not in ev.claim
    assert token not in (settings.layout.root / ev.location).read_text(encoding="utf-8"), "redacted before storage"
    assert token not in personal_client.get(f"/api/evidence/{ev.id}").text
    for kind in ("evidence", "audit"):
        export = personal_client.get("/api/ops/export", params={"kind": kind})
        assert export.status_code == 200 and token not in export.text, kind
        assert export.json()["redaction"]["engine"] == "aeos.core.redaction"
    assert ev.trusted is False, "a worker trace stays untrusted material"


BUG_PII = ("BUG: core redaction seam - aeos.core.redaction has no PII patterns (e-mail address, phone number) and "
           "no policy limits storing/exporting personal data found in traces: a worker trace with PII is persisted "
           "in the evidence store and exported verbatim (§50 1.37 'Trace berisi secret/PII').")


@pytest.mark.xfail(strict=True, reason=BUG_PII)
def test_trace_pii_is_redacted_or_limited_before_storage_and_export(personal_client: AuthedClient, db, settings,
                                                                    host):
    from tests.acceptance.world import create_project

    email, phone = "budi.santoso" + "@example.com", "+62 812-3456-7890"
    project = create_project(personal_client, name="Trace PII")
    ev = _trace(db, project["id"], f"pelanggan {email} telp {phone}")
    stored = (settings.layout.root / ev.location).read_text(encoding="utf-8")
    export = personal_client.get("/api/ops/export", params={"kind": "audit"}).text
    assert email not in stored and email not in export and phone not in export
