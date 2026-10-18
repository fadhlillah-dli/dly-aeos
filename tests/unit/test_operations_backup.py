"""Backup / verify / restore-test / live restore (master §26, §33).

§50 acceptance rows covered:
* "Restore backup belum pernah diuji -> Tidak mengklaim recovery siap": a backup is ready only after
  a restore test that recorded RESTORE_TEST evidence; corrupt backups never produce evidence.
* "VIEWER memanggil write endpoint langsung -> Ditolak backend": ops backup writes reject VIEWER.
* "Trace berisi secret/PII -> redaction/policy membatasi penyimpanan/ekspor": credential-like files are
  excluded from backups and never written into the manifest as values.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.models.audit import AuditEvent
from aeos.models.evidence import EvidenceArtifact
from aeos.models.portfolio import Project
from aeos.operations import backup
from tests.conftest import VIEWER_PW, create_admin, create_viewer
from tests.unit.test_operations_fakes import block_module, fake_verification

SECRET = "sk-or-v1-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4"


def _project(db, key: str, domain: Domain) -> Project:
    p = Project(project_key=key, name=key.upper(), domain=domain.value)
    db.add(p)
    db.commit()
    return p


def _seed_files(settings) -> dict[str, Path]:
    lay = settings.layout
    files = {
        "p_evidence": lay.evidence(Domain.PERSONAL, "alpha") / "run1" / "report.json",
        "p_attach": lay.attachments(Domain.PERSONAL, "alpha") / "idea.md",
        "p_secret_content": lay.attachments(Domain.PERSONAL, "alpha") / "notes.md",
        "p_secret_name": lay.attachments(Domain.PERSONAL, "alpha") / "client_secret_123.json",
        "o_pub": lay.publications(Domain.OFFICE, "omega") / "page.html",
        "openbao": lay.root / "openbao" / "data" / "vault.db",
        "task": lay.task_data(Domain.PERSONAL, "alpha") / "sandbox" / "x.txt",
    }
    for p in files.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    files["p_evidence"].write_text('{"ok": true}', encoding="utf-8")
    files["p_attach"].write_text("# Ide\nBangun fitur X.\n", encoding="utf-8")
    files["p_secret_content"].write_text(f"openrouter key {SECRET}\n", encoding="utf-8")
    files["p_secret_name"].write_text("{}", encoding="utf-8")
    files["o_pub"].write_text("<p>office</p>", encoding="utf-8")
    files["openbao"].write_bytes(b"vault-data")
    files["task"].write_text("worker scratch", encoding="utf-8")
    return files


def test_backup_manifest_partitions_and_exclusions(settings, db):
    """Per-domain archives, OpenBao/task-data never archived, credential files excluded w/o values."""
    _project(db, "alpha", Domain.PERSONAL)
    _seed_files(settings)
    result = backup.create_backup(settings, label="unit", actor="tester", session=db)
    db.commit()
    bdir = settings.layout.backups / result["backup_id"]
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    assert result["readiness"] == backup.READINESS_NOT_TESTED and result["ready"] is False
    archives = {a["domain"]: a for a in manifest["archives"]}
    assert set(archives) == {"PERSONAL", "OFFICE"}
    personal = {f["path"] for f in archives["PERSONAL"]["files"]}
    assert "projects/alpha/evidence/run1/report.json" in personal
    assert "projects/alpha/attachments/idea.md" in personal
    assert not any("notes.md" in p or "client_secret" in p for p in personal)
    assert {f["path"] for f in archives["OFFICE"]["files"]} == {"projects/omega/publications/page.html"}
    with zipfile.ZipFile(bdir / "personal.zip") as zf:
        names = zf.namelist()
    assert not any("omega" in n or "openbao" in n or "task-data" in n for n in names)
    excluded = {e["path"]: e["reason"] for e in manifest["excluded_files"]}
    assert "projects/alpha/attachments/notes.md" in excluded
    assert "projects/alpha/attachments/client_secret_123.json" in excluded
    whole = (bdir / "manifest.json").read_text(encoding="utf-8")
    assert SECRET not in whole
    assert any("OpenBao" in x for x in manifest["excluded_by_design"])
    assert manifest["database"]["sha256"].startswith("sha256:")
    assert manifest["app_version"] and manifest["schema_version"]
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.backup.create"))
    assert audit is not None and audit.target_id == result["backup_id"]


def test_backup_scrubs_login_sessions(admin_client, settings, db):
    result = backup.create_backup(settings, label="sess", actor="tester")
    copy = settings.layout.backups / result["backup_id"] / "aeos.sqlite3"
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("SELECT count(*) FROM auth_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        conn.close()


def test_restore_test_roundtrip_records_evidence_only_on_success(settings, db, monkeypatch):
    calls = fake_verification(monkeypatch)
    _project(db, "alpha", Domain.PERSONAL)
    _seed_files(settings)
    bid = backup.create_backup(settings, label="rt", actor="tester")["backup_id"]
    verify = backup.verify_backup(settings, bid)
    assert verify["ok"] and verify["files_checked"] >= 3
    assert backup.list_backups(settings)[0]["ready"] is False  # hash ok != restore tested
    result = backup.restore_test(settings, bid, session=db, actor="tester")
    db.commit()
    assert result["status"] == "PASSED", result["evidence_note"] or result["problems"]
    assert result["stats"]["integrity_check"] == "ok"
    assert result["stats"]["files_verified"] == 3
    ev = db.get(EvidenceArtifact, result["evidence_id"])
    assert ev.kind == "RESTORE_TEST" and ev.origin == "restore-test" and ev.trusted
    assert ev.evidence_level == "E2" and ev.subject_id == bid
    assert len(calls) == 1
    listed = backup.list_backups(settings)[0]
    assert listed["ready"] is True and listed["readiness"] == backup.READINESS_TESTED
    assert backup.latest_ready(settings)["backup_id"] == bid
    assert not list((settings.layout.root / "tmp").glob("restore-test-*"))  # scratch cleaned up


def test_restore_test_without_verification_module_is_not_ready(settings, db, monkeypatch):
    block_module(monkeypatch, "aeos.verification.collector")
    bid = backup.create_backup(settings, label="nov", actor="tester")["backup_id"]
    result = backup.restore_test(settings, bid, session=db, actor="tester")
    assert result["status"] == "PASSED_UNRECORDED"
    assert result["evidence_id"] is None and "verification module unavailable" in result["evidence_note"]
    summary = backup.list_backups(settings)[0]
    assert summary["ready"] is False and summary["readiness"] == backup.READINESS_UNRECORDED


def test_corrupt_backup_fails_and_produces_no_evidence(settings, db, monkeypatch):
    calls = fake_verification(monkeypatch)
    bid = backup.create_backup(settings, label="bad", actor="tester")["backup_id"]
    dbfile = settings.layout.backups / bid / "aeos.sqlite3"
    data = bytearray(dbfile.read_bytes())
    data[200:600] = b"\x00" * 400
    dbfile.write_bytes(bytes(data))
    assert backup.verify_backup(settings, bid)["ok"] is False
    result = backup.restore_test(settings, bid, session=db, actor="tester")
    assert result["status"] == "FAILED" and result["evidence_id"] is None
    assert "database copy hash mismatch" in result["problems"]
    assert calls == []
    assert db.scalar(select(EvidenceArtifact)) is None
    summary = backup.list_backups(settings)[0]
    assert summary["ready"] is False and summary["readiness"] == backup.READINESS_FAILED


def test_corruption_at_creation_caught_by_integrity_check(settings, db, monkeypatch):
    """Hashes consistent with a corrupt file (corrupted before hashing) -> integrity/readability fails."""
    calls = fake_verification(monkeypatch)
    _project(db, "alpha", Domain.PERSONAL)
    bid = backup.create_backup(settings, label="garbled", actor="tester")["backup_id"]
    bdir = settings.layout.backups / bid
    dbfile = bdir / "aeos.sqlite3"
    raw = bytearray(dbfile.read_bytes())
    page = 4096
    for off in range(page, len(raw), page * 2):  # garble every other page after the header
        raw[off + 8:off + 64] = b"\xff" * 56
    dbfile.write_bytes(bytes(raw))
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    manifest["database"]["sha256"] = backup.file_sha256(dbfile)
    (bdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    results = json.loads((bdir / "results.json").read_text(encoding="utf-8"))
    results["manifest_sha256"] = backup.file_sha256(bdir / "manifest.json")
    (bdir / "results.json").write_text(json.dumps(results), encoding="utf-8")
    assert backup.verify_backup(settings, bid)["ok"] is True
    result = backup.restore_test(settings, bid, session=db, actor="tester")
    assert result["status"] == "FAILED", result
    assert result["evidence_id"] is None and calls == []


def test_tampered_archive_detected(settings, db):
    _seed_files(settings)
    bid = backup.create_backup(settings, label="tamper", actor="tester")["backup_id"]
    arch = settings.layout.backups / bid / "personal.zip"
    with zipfile.ZipFile(arch, "a") as zf:
        zf.writestr("projects/alpha/evidence/injected.txt", "x")
    result = backup.verify_backup(settings, bid)
    assert not result["ok"]
    assert any("archive personal.zip" in p for p in result["problems"])


def _restore_tested(settings, db, monkeypatch) -> str:
    fake_verification(monkeypatch)
    bid = backup.create_backup(settings, label="live", actor="tester")["backup_id"]
    assert backup.restore_test(settings, bid, session=db, actor="tester")["status"] == "PASSED"
    db.commit()
    return bid


def test_restore_refuses_while_server_port_busy(settings, db, monkeypatch):
    bid = _restore_tested(settings, db, monkeypatch)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        busy = settings.model_copy(update={"port": sock.getsockname()[1]})
        with pytest.raises(Blocked) as exc:
            backup.restore_live(busy, bid, actor="tester")
        assert exc.value.code == "SERVER_RUNNING"
    finally:
        sock.close()


def test_restore_requires_restore_tested_backup(settings, db):
    bid = backup.create_backup(settings, label="untested", actor="tester")["backup_id"]
    with pytest.raises(Blocked) as exc:
        backup.restore_live(settings, bid, actor="tester", server_check=lambda s: {"running": False})
    assert exc.value.code == "RESTORE_NOT_TESTED"


def test_restore_refused_in_worker_context(settings, db, monkeypatch):
    bid = backup.create_backup(settings, label="w", actor="tester")["backup_id"]
    monkeypatch.setenv("AEOS_RUN_ID", "run-123")
    with pytest.raises(PolicyDenied):
        backup.restore_live(settings, bid, actor="worker", server_check=lambda s: {"running": False})


def test_restore_live_roundtrip_takes_pre_restore_backup(settings, db, monkeypatch):
    _project(db, "alpha", Domain.PERSONAL)
    files = _seed_files(settings)
    bid = _restore_tested(settings, db, monkeypatch)
    _project(db, "beta", Domain.PERSONAL)
    files["p_evidence"].unlink()
    newer = settings.layout.evidence(Domain.PERSONAL, "alpha") / "newer.json"
    newer.write_text("{}", encoding="utf-8")
    db.close()
    report = backup.restore_live(settings, bid, actor="tester", force_maintenance=True,
                                 server_check=lambda s: {"running": True})
    assert report["status"] == "RESTORED" and report["forced"] is True
    assert report["pre_restore_backup_id"]
    assert (settings.layout.backups / report["pre_restore_backup_id"] / "manifest.json").is_file()
    assert files["p_evidence"].is_file()  # restored
    assert newer.is_file()  # nothing deleted
    from aeos.core.db import session_factory

    s = session_factory()()
    try:
        keys = set(s.scalars(select(Project.project_key)))
        assert keys == {"alpha"}
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "ops.backup.restore")) is not None
    finally:
        s.close()


def test_restore_live_recovers_damaged_live_database(settings, db, monkeypatch):
    """A corrupt live database is kept as a raw copy (evidence preserved) and replaced by the backup."""
    _project(db, "alpha", Domain.PERSONAL)
    bid = _restore_tested(settings, db, monkeypatch)
    db.close()
    from aeos.core.db import get_engine

    get_engine().dispose()
    settings.layout.db_path.write_bytes(b"this is not a database" * 100)
    report = backup.restore_live(settings, bid, actor="tester", server_check=lambda s: {"running": False})
    assert report["status"] == "RESTORED" and report["pre_restore_backup_id"] is None
    raw = settings.layout.backups / report["pre_restore_raw_copy"] / "aeos.sqlite3"
    assert raw.read_bytes().startswith(b"this is not a database")
    from aeos.core.db import session_factory

    s = session_factory()()
    try:
        assert set(s.scalars(select(Project.project_key))) == {"alpha"}
    finally:
        s.close()


def test_retention_listing_never_deletes(settings, db, monkeypatch):
    fake_verification(monkeypatch)
    ids = [backup.create_backup(settings, label=f"r{i}", actor="t")["backup_id"] for i in range(3)]
    backup.restore_test(settings, ids[0], session=db, actor="t")
    db.commit()
    items = backup.list_backups(settings, retention={"keep_last": 1, "keep_days": 0})
    by_id = {i["backup_id"]: i["retention"] for i in items}
    assert by_id[ids[0]] == "KEEP"  # newest restore-tested backup is always kept
    assert sum(1 for v in by_id.values() if v.startswith("EXPIRED_BY_POLICY")) == 1
    assert all((settings.layout.backups / i).is_dir() for i in ids)
    assert all(i["retention"] == "NO_POLICY" for i in backup.list_backups(settings))


def test_partial_backup_listed_as_incomplete(settings, db):
    (settings.layout.backups / "20260101T000000Z-x-deadbeef.partial").mkdir(parents=True)
    items = backup.list_backups(settings)
    assert items and items[0]["readiness"] == backup.READINESS_INCOMPLETE and items[0]["ready"] is False


def test_api_backups_admin_flow_and_viewer_denied(client, db, settings, monkeypatch):
    fake_verification(monkeypatch)
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/ops/backups", json={"label": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"
    assert client.get("/api/ops/backups").status_code == 200
    from tests.conftest import ADMIN_PW

    client.login("owner", ADMIN_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/ops/backups", json={"label": "api"})
    assert r.status_code == 200, r.text
    bid = r.json()["backup_id"]
    listing = client.get("/api/ops/backups").json()
    assert listing["recovery_ready"] is False and listing["items"][0]["readiness"] == "NOT_RESTORE_TESTED"
    assert "files" not in json.dumps(listing["items"][0]["archives"])
    assert client.post(f"/api/ops/backups/{bid}/verify").json()["ok"] is True
    rt = client.post(f"/api/ops/backups/{bid}/restore-test").json()
    assert rt["status"] == "PASSED" and rt["evidence_id"]
    assert client.get("/api/ops/backups").json()["recovery_ready"] is True
    assert client.post("/api/ops/backups/..evil/verify").status_code == 422
    assert client.post("/api/ops/backups/..%2Fetc/verify").status_code in (404, 405, 422)


def test_api_backups_require_domain(admin_client):
    r = admin_client.get("/api/ops/backups")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_no_http_route_for_live_restore(app):
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.endswith("/restore") for p in paths)


def test_domain_scoped_views_hide_office_counts_and_problems(personal_client, db, settings, monkeypatch):
    """§35/§50 "Viewer/Personal mengakses ... Office -> scope diterapkan termasuk counts": a backup holds
    both partitions, but a PERSONAL session never sees Office file counts, totals or Office problem
    texts. The overall status stays global: a corrupt Office archive still makes the backup FAILED."""
    calls = fake_verification(monkeypatch)
    _project(db, "alpha", Domain.PERSONAL)
    lay = settings.layout
    for i in range(4):  # four Office files, one Personal file
        f = lay.publications(Domain.OFFICE, "omega") / f"page{i}.html"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"<p>{i}</p>", encoding="utf-8")
    pf = lay.evidence(Domain.PERSONAL, "alpha") / "r.json"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("{}", encoding="utf-8")
    created = personal_client.post("/api/ops/backups", json={"label": "scoped"})
    assert created.status_code == 200, created.text
    bid = created.json()["backup_id"]
    assert {a["domain"] for a in created.json()["archives"]} == {"PERSONAL"}
    rt = personal_client.post(f"/api/ops/backups/{bid}/restore-test").json()
    assert rt["status"] == "PASSED" and rt["scope"] == "PERSONAL"
    assert "files_verified" not in rt["stats"] and "rows_total" not in rt["stats"]
    assert rt["stats"]["files_verified_by_partition"] == {"PERSONAL": 1}
    ev = calls[-1]
    inline = json.dumps(ev.inline)  # platform evidence: no per-domain counts or totals
    assert '"files_verified"' not in inline and "files_verified_by_partition" not in inline
    assert '"rows_total"' not in inline and "OFFICE" not in inline
    assert "file(s)" not in ev.claim and not re.search(r"\b[45] archived", ev.claim)  # no Office volume
    listing = personal_client.get("/api/ops/backups").json()["items"][0]
    assert listing["last_restore_test"]["stats"]["files_verified_by_partition"] == {"PERSONAL": 1}
    office_size = (lay.backups / bid / "office.zip").stat().st_size
    full = backup.summarize(settings, bid)
    assert listing["size_bytes"] == full["size_bytes"] - office_size
    # corrupt the OFFICE archive: Personal sees FAILED with a neutral reason, Office sees the detail
    office_zip = lay.backups / bid / "office.zip"
    office_zip.write_bytes(office_zip.read_bytes() + b"x")
    v = personal_client.post(f"/api/ops/backups/{bid}/verify").json()
    assert v["ok"] is False and "files_checked" not in v
    assert v["problems"] == [backup.HIDDEN_PROBLEM]
    assert "office" not in json.dumps(v).lower()
    audit_row = db.scalars(select(AuditEvent).where(AuditEvent.action == "ops.backup.verify")).all()[-1]
    assert "office" not in json.dumps(audit_row.details).lower()  # platform audit row: no Office detail
    personal_client.select_domain(Domain.OFFICE)
    v_office = personal_client.post(f"/api/ops/backups/{bid}/verify").json()
    assert any("office.zip" in p for p in v_office["problems"])
    assert personal_client.get("/api/ops/backups").json()["items"][0]["readiness"] == backup.READINESS_FAILED


def test_restore_live_reports_locked_database_without_replacing(settings, db, monkeypatch):
    """Windows: a live DB held open by another process cannot be replaced -> Blocked, nothing changed."""
    _project(db, "alpha", Domain.PERSONAL)
    bid = _restore_tested(settings, db, monkeypatch)
    _project(db, "beta", Domain.PERSONAL)
    db.close()
    real_replace = backup.os.replace

    def locked(src, dst):
        if str(dst) == str(settings.layout.db_path):
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", locked)
    with pytest.raises(Blocked) as exc:
        backup.restore_live(settings, bid, actor="tester", server_check=lambda s: {"running": False})
    assert exc.value.code == "LIVE_DB_IN_USE" and exc.value.details["pre_restore_backup_id"]
    assert not list(settings.layout.db_path.parent.glob(".restore-*.sqlite3"))  # staged copy removed
    from aeos.core.db import init_engine, session_factory

    init_engine(settings)
    s = session_factory()()
    try:
        assert set(s.scalars(select(Project.project_key))) == {"alpha", "beta"}  # live data untouched
    finally:
        s.close()


def test_unexpected_restore_failure_is_recorded_as_failed(settings, db, monkeypatch):
    """Any unexpected error during the restore test is a FAILED test (recorded, no evidence), never a
    500 and never a pass (§33 "restore benar-benar diuji")."""
    calls = fake_verification(monkeypatch)
    bid = backup.create_backup(settings, label="boom", actor="tester")["backup_id"]

    def explode(_path):
        raise RuntimeError("model registry exploded")

    monkeypatch.setattr(backup, "_open_with_models", explode)
    result = backup.restore_test(settings, bid, session=db, actor="tester")
    assert result["status"] == "FAILED" and result["evidence_id"] is None and calls == []
    assert any("RuntimeError" in p for p in result["problems"])
    assert backup.list_backups(settings)[0]["readiness"] == backup.READINESS_FAILED
    assert not list((settings.layout.root / "tmp").glob("restore-test-*"))


def test_office_extraction_failure_detail_hidden_from_personal(settings, db, monkeypatch):
    """A broken Office archive fails the restore test for everyone, but the Personal view gets a neutral
    reason without Office member paths (§35, §50 scope)."""
    fake_verification(monkeypatch)
    f = settings.layout.publications(Domain.OFFICE, "omega") / "secret-plan.html"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<p>office</p>", encoding="utf-8")
    bid = backup.create_backup(settings, label="office-broken", actor="tester")["backup_id"]
    real_safe_member = backup._safe_member

    def refuse_office(name):
        if "omega" in name:
            raise ValidationFailed(f"unsafe archive member: {name!r}")
        return real_safe_member(name)

    monkeypatch.setattr(backup, "_safe_member", refuse_office)
    result = backup.restore_test(settings, bid, session=db, actor="tester")
    assert result["status"] == "FAILED"
    personal = backup.scope_result(result, Domain.PERSONAL)
    assert personal["status"] == "FAILED" and backup.HIDDEN_PROBLEM in personal["problems"]
    assert "omega" not in json.dumps(personal) and "office.zip" not in json.dumps(personal)
    office = backup.scope_result(result, Domain.OFFICE)
    assert any("office.zip" in p for p in office["problems"])


def test_backup_label_is_redacted_in_id_and_manifest(settings, db):
    """§50 "Trace berisi secret": a credential pasted into the free-text label never reaches the backup
    directory name, the manifest or the audit trail."""
    token = "ghp_" + "L" * 36
    summary = backup.create_backup(settings, label=f"x {token}", actor="tester", session=db)
    db.commit()
    bid = summary["backup_id"]
    assert token not in bid and token[:20] not in bid
    manifest = (settings.layout.backups / bid / "manifest.json").read_text(encoding="utf-8")
    assert token not in manifest
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.backup.create"))
    assert token not in json.dumps(row.details)
