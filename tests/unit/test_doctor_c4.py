"""Readiness Doctor gaps closed by the C4 cluster (§07, §08, §45, §51). No network, no real tools: every
probe goes through the fake runner, in-process ASGI or a throw-away in-memory database.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

from sqlalchemy import select

from aeos.core.enums import Capability
from aeos.core.timeutil import utcnow
from aeos.doctor import capabilities as caps
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.checks import adapters as adapters_mod
from aeos.doctor.checks import evidence as evidence_mod
from aeos.doctor.checks import host as host_mod
from aeos.doctor.runner import latest_project_results, make_context, run_check, run_doctor
from aeos.inventory.probes import HttpResult, set_runner_override
from aeos.models.inventory import CheckResult
from tests.conftest import create_admin
from tests.unit.test_inventory_fakes import base_runner, git_handler, ok

DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"


def _ctx(settings, db, runner, *, functional=False, repo=None):
    return make_context(settings, db, runner=runner, functional=functional, repo=repo)


def check(cid, ctx):
    load_all()
    return run_check(REGISTRY[cid], ctx)


def _row(db, cid, status="VERIFIED", **kw):
    load_all()
    spec = REGISTRY[cid]
    now = utcnow()
    r = CheckResult(check_id=cid, title=spec.title, group=spec.group, component_id=spec.component_id,
                    scope=kw.pop("scope", dict(spec.scope)), status=status, last_checked_at=kw.pop("at", now),
                    expires_at=now + timedelta(hours=1), invalidators=list(spec.invalidators), **kw)
    db.add(r)
    db.flush()
    return r


# ------------------------------------------------------------------ §08 Local auth: probed, not asserted


def test_http_guard_probes_the_real_app_and_fails_when_csrf_is_not_enforced(settings, db, monkeypatch):
    res = check("auth.http_guard", _ctx(settings, db, base_runner()))
    assert res.status == "VERIFIED", res.observed_evidence
    sc = res.observed_evidence["scenarios"]
    assert sc["foreign_host"]["status"] == 421 and sc["foreign_origin"]["status"] == 403
    assert sc["viewer_write"]["code"] == "READ_ONLY_ROLE" and sc["write_without_csrf"]["code"] == "CSRF_FAILED"
    assert sc["session_cookie_flags"]["ok"] and sc["legitimate_logout"]["ok"]
    from aeos.identity import service as identity

    monkeypatch.setattr(identity, "verify_csrf", lambda principal, header: True)
    broken = check("auth.http_guard", _ctx(settings, db, base_runner()))
    assert broken.status == "FAILED" and broken.error_code == "HTTP_GUARD_FAILED"
    assert "write_without_csrf" in broken.summary
    from aeos.models.identity import User

    assert db.scalar(select(User).where(User.username == "doctor-probe-admin")) is None, "operator DB untouched"


def test_sessions_are_configured_not_verified_and_interlock_is_self_tested(settings, db, monkeypatch):
    assert check("auth.sessions", _ctx(settings, db, base_runner())).status == "CONFIGURED"
    res = check("auth.interlock", _ctx(settings, db, base_runner()))
    assert res.status == "VERIFIED" and res.observed_evidence["self_test"]["allowed"] is False
    assert "APPROVED_VERSION_MISSING" in res.observed_evidence["self_test"]["reason_codes"]
    from aeos.policy import interlock
    from aeos.policy.facade import InterlockResult

    monkeypatch.setattr(interlock, "check_external_write", lambda s, i: InterlockResult(True, []))
    bad = check("auth.interlock", _ctx(settings, db, base_runner()))
    assert bad.status == "FAILED" and bad.error_code == "INTERLOCK_ALLOWED_UNBOUND_WRITE"


# ------------------------------------------------------------------ §08 Secret: AppRole/policy/TTL, full scan


def _bao(monkeypatch, *, backend="openbao", policies=("aeos-personal", "aeos-platform", "aeos-projects",
                                                      "aeos-workers", "default"), ttl=3600, verify=None):
    from aeos.secrets import health, provider

    monkeypatch.setattr(provider, "select_backend", lambda settings=None: provider.Selection(
        object() if backend == "openbao" else None, backend, ["login failed"] if backend == "none" else []))
    monkeypatch.setattr(health, "store_health", lambda settings: {
        "backend": backend, "openbao": {"login": "OK", "approle_in_keyring": True,
                                        "token": {"present": True, "ttl_seconds": ttl, "renewable": True,
                                                  "policies": list(policies)}},
        "functional_check": verify or {"status": "NOT_TESTED"}})


def test_openbao_auth_checks_policies_ttl_and_the_roundtrip(settings, db, monkeypatch):
    _bao(monkeypatch)
    assert check("secret.openbao_auth", _ctx(settings, db, base_runner())).status == "CONNECTED"
    _bao(monkeypatch, verify={"status": "VERIFIED", "checked_at": utcnow().isoformat(), "stale": False})
    assert check("secret.openbao_auth", _ctx(settings, db, base_runner())).status == "VERIFIED"
    _bao(monkeypatch, policies=("aeos-personal", "aeos-office", "aeos-platform", "aeos-projects", "aeos-workers"))
    bad = check("secret.openbao_auth", _ctx(settings, db, base_runner()))
    assert bad.status == "FAILED" and "aeos-office" in bad.summary
    _bao(monkeypatch, ttl=999999)
    assert check("secret.openbao_auth", _ctx(settings, db, base_runner())).status == "FAILED"
    _bao(monkeypatch, backend="none")
    assert check("secret.openbao_auth", _ctx(settings, db, base_runner())).status == "MISSING"


def test_openbao_auth_is_not_applicable_for_another_provider(settings, db, secrets_memory):
    assert check("secret.openbao_auth", _ctx(settings, db, base_runner())).status == "NOT_APPLICABLE"


def _old_secret_scan(settings, db) -> str:
    from aeos.models.audit import Event

    raw = "ghp_" + "Q1w2E3r4" * 4
    db.add(Event(source="test", kind="x", payload={"note": raw}))  # oldest row, bypassing emit() redaction
    for i in range(520):  # more than the 500-row recent window the old check scanned
        db.add(Event(source="test", kind="y", payload={"i": i}))
    db.commit()
    res = check("secret.no_raw_exposure", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and res.observed_evidence["recent_findings"]["events"] == 0
    assert res.observed_evidence["findings"]["events"] == 1 and raw not in json.dumps(res.as_dict())
    db.commit()
    # the finding is remembered: a second run (old row already swept) still fails
    again = check("secret.no_raw_exposure", _ctx(settings, db, base_runner()))
    assert again.status == "FAILED"
    db.commit()
    return again.observed_evidence["finding_ids"][0]


def test_raw_secret_scan_covers_rows_older_than_the_recent_window(settings, db):
    assert _old_secret_scan(settings, db).startswith("events:")


def test_accepting_a_rotated_exposure_is_a_human_risk_acceptance(settings, db, personal_client):
    fid = _old_secret_scan(settings, db)
    r = personal_client.post(f"/api/doctor/secret-exposures/{fid}/accept",
                             json={"rationale": "credential rotated; row is append-only"})
    assert r.status_code == 201, r.text
    db.expire_all()
    assert check("secret.no_raw_exposure", _ctx(settings, db, base_runner())).status == "VERIFIED"


# ------------------------------------------------------------------ §08 Project adapters per project


def _projects_with_repos(db):
    from aeos.models.portfolio import Project, RepositoryReference

    plat = Project(project_key="aeos", name="AEOS", domain="PERSONAL", is_platform=True)
    other = Project(project_key="other", name="Other", domain="PERSONAL")
    office = Project(project_key="corp", name="Corp", domain="OFFICE")
    db.add_all([plat, other, office])
    db.flush()
    db.add_all([RepositoryReference(project_id=plat.id, provider="GITHUB", full_name="fadhlillah-dli/dly-aeos",
                                    provider_repo_id="1", role="platform"),
                RepositoryReference(project_id=other.id, provider="GITHUB", full_name="me/other",
                                    provider_repo_id="77"),
                RepositoryReference(project_id=office.id, provider="GITHUB", full_name="corp/repo")])
    db.commit()
    return plat, other, office


def test_project_repo_results_are_per_project_and_gate_the_project(settings, db, secrets_memory):
    plat, other, office = _projects_with_repos(db)
    secrets_memory.put("personal/github", "t" * 40, actor="test")
    r = base_runner()
    r.http["https://api.github.com/repos/me/other"] = HttpResult(404, {"message": "Not Found"})
    r.http["https://api.github.com/repos/fadhlillah-dli/dly-aeos"] = HttpResult(
        200, {"id": 1, "permissions": {"push": True}}, headers={"x-oauth-scopes": "repo"})
    run_doctor(settings, db, checks=["adapter.project_repo", "adapter.github_token"], functional=True, runner=r)
    db.commit()
    rows = latest_project_results(db, ["adapter.project_repo"])
    by_project = {pid: row for (_cid, pid), row in rows.items()}
    assert by_project[other.id].status == "FAILED" and by_project[other.id].scope["domain"] == "PERSONAL"
    assert by_project[plat.id].status == "CONNECTED"
    assert by_project[plat.id].observed_evidence["token_scopes"] == ["repo"]
    assert by_project[office.id].status == "UNKNOWN" and by_project[office.id].scope["domain"] == "OFFICE"
    st = caps.capability_state(db, Capability.CAN_PREPARE_PR, domain="PERSONAL", project_id=other.id)
    assert st.status == "BLOCKED" and any("adapter.project_repo" in x for x in st.reasons)
    no_project = caps.capability_state(db, Capability.CAN_PREPARE_PR, domain="PERSONAL")
    assert any("dievaluasi per proyek" in x for x in no_project.reasons)
    from aeos.doctor import views

    personal_items = views.checks_view(db, domain="PERSONAL")["project_items"]
    assert {i["project_id"] for i in personal_items} == {plat.id, other.id}
    assert "adapter.project_repo" not in {i["check_id"] for i in views.checks_view(db, domain="PERSONAL")["items"]}


def test_platform_repo_project_is_covered_by_the_platform_probe():
    load_all()
    rows = {cid: {"check_id": cid, "status": "VERIFIED", "summary": "", "expires_at": None, "last_checked_at": None,
                  "fingerprint": None, "remediation": {}, "observed_evidence": {}}
            for cid in caps.checks_for_capability(Capability.CAN_PREPARE_PR)}
    rows.pop("adapter.project_repo")
    project = {"id": "p1", "domain": "PERSONAL", "is_platform_repo": True}
    st = caps.evaluate(Capability.CAN_PREPARE_PR, rows, now=utcnow(), fps={}, domain="PERSONAL", session=None,
                       project=project, project_rows={})
    entry = next(c for c in st.checks if c["check_id"] == "adapter.project_repo")
    assert entry["covered_by"] == "adapter.github_token" and entry["ok"]
    other = caps.evaluate(Capability.CAN_PREPARE_PR, rows, now=utcnow(), fps={}, domain="PERSONAL", session=None,
                          project={**project, "is_platform_repo": False}, project_rows={})
    assert other.status == "UNKNOWN" and any("adapter.project_repo" in x for x in other.reasons)


def test_project_drive_root_is_probed_read_only_with_the_stored_token(settings, db, secrets_memory):
    from aeos.models.portfolio import Project, ProjectIntegration

    p = Project(project_key="docs", name="Docs", domain="PERSONAL")
    db.add(p)
    db.flush()
    db.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="google-drive", purpose="docs",
                              root_id="FOLDER1"))
    db.commit()
    token = {"token": "ya29." + "x" * 30, "refresh_token": "1//" + "y" * 30, "client_id": "c",
             "expiry": (utcnow() + timedelta(hours=1)).isoformat(), "scopes": [DRIVE_FILE]}
    secrets_memory.put("personal/google-oauth-client", '{"installed": {"client_id": "c"}}', actor="test")
    secrets_memory.put("personal/google-oauth-token", json.dumps(token), actor="test")
    url = (f"{adapters_mod.DRIVE_API}/FOLDER1?fields=id,name,mimeType,trashed,capabilities(canAddChildren)"
           "&supportsAllDrives=true")
    r = base_runner()
    r.http[url] = HttpResult(200, {"id": "FOLDER1", "mimeType": adapters_mod.FOLDER_MIME, "trashed": False,
                                   "capabilities": {"canAddChildren": True}})
    target = adapters_mod.project_drive_targets(_ctx(settings, db, r))[0]
    ctx = _ctx(settings, db, r, functional=True)
    ctx.project = target
    res = run_check(REGISTRY["adapter.project_drive_root"], ctx)
    assert res.status == "CONNECTED" and res.scope["project_id"] == p.id
    assert token["token"] not in json.dumps(res.as_dict()) and r.http_calls[-1]["allow_remote"] is True
    r.http[url] = HttpResult(404, {"error": {"message": "File not found"}})
    res = run_check(REGISTRY["adapter.project_drive_root"], ctx)
    assert res.status == "FAILED" and res.observed_evidence["results"][0]["error"] == "DRIVE_ROOT_NOT_VISIBLE"
    ctx.functional = False
    assert run_check(REGISTRY["adapter.project_drive_root"], ctx).status == "CONFIGURED"


def test_office_project_targets_stay_unknown_behind_the_office_gate(settings, db):
    from aeos.models.portfolio import Project, ProjectIntegration

    office = Project(project_key="corp", name="Corp", domain="OFFICE")
    personal = Project(project_key="mine", name="Mine", domain="PERSONAL")
    db.add_all([office, personal])
    db.flush()
    db.add_all([ProjectIntegration(project_id=office.id, domain="OFFICE", provider="jira", purpose="issues",
                                   root_id="CORP"),
                ProjectIntegration(project_id=personal.id, domain="PERSONAL", provider="jira", purpose="issues",
                                   root_id="MINE")])
    db.commit()
    targets = adapters_mod.project_office_targets(_ctx(settings, db, base_runner()))
    assert [t["project_id"] for t in targets] == [office.id], "only OFFICE projects are OFFICE targets"
    run_doctor(settings, db, checks=["adapter.project_office_targets"], functional=True, runner=base_runner())
    db.commit()
    rows = latest_project_results(db, ["adapter.project_office_targets"])
    (row,) = rows.values()
    assert row.status == "UNKNOWN" and row.error_code == "OFFICE_DEFAULT_DENY"
    assert row.scope["project_id"] == office.id and row.scope["domain"] == "OFFICE"
    assert row.observed_evidence["targets"][0]["root_id"] == "CORP"


def test_google_token_scopes_are_read_and_excess_scope_fails(settings, db, secrets_memory):
    secrets_memory.put("personal/google-oauth-client", '{"installed": {"client_id": "c"}}', actor="test")
    secrets_memory.put("personal/google-oauth-token", json.dumps({"token": "t", "scopes": [DRIVE_FILE]}), actor="test")
    res = check("adapter.google_oauth", _ctx(settings, db, base_runner()))
    assert res.status == "CONFIGURED" and res.observed_evidence["token_scopes_read"] is True
    secrets_memory.put("personal/google-oauth-token", json.dumps(
        {"token": "t", "scopes": [DRIVE_FILE, "https://www.googleapis.com/auth/drive"]}), actor="test")
    res = check("adapter.google_oauth", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and res.error_code == "GOOGLE_SCOPE_NOT_LEAST_PRIVILEGE"


def test_integration_target_change_marks_adapter_checks_stale(settings, db):
    rows = [_row(db, cid) for cid in ("adapter.github_token", "adapter.google_oauth", "delivery.branch_protection")]
    db.commit()
    n = caps.invalidate(db, invalidator="integration:abc-123", reason="integration target changed")
    db.commit()
    assert n == 3 and all(r.status == "STALE" for r in rows)


def test_observed_permission_change_invalidates_dependent_checks(settings, db, secrets_memory):
    secrets_memory.put("personal/github", "t" * 40, actor="test")
    url = f"https://api.github.com/repos/{settings.platform_repo_slug}"
    r = base_runner()
    r.http[url] = HttpResult(200, {"id": 1, "permissions": {"push": True, "pull": True}})
    run_doctor(settings, db, checks=["adapter.github_token"], functional=True, runner=r)
    db.commit()
    other = _row(db, "delivery.branch_protection")
    db.commit()
    r.http[url] = HttpResult(200, {"id": 1, "permissions": {"push": False, "pull": True}})
    run_doctor(settings, db, checks=["adapter.github_token"], functional=True, runner=r)
    db.commit()
    db.refresh(other)
    assert other.status == "STALE" and "izin/scope" in other.observed_evidence["stale_reason"]


# ------------------------------------------------------------------ §08 Evidence/review


def test_still_valid_acceptance_with_a_changed_head_fails(settings, db):
    from aeos.models.evidence import AcceptanceRecord
    from aeos.models.workforce import WorkerRun

    assert check("evidence.acceptance_invalidation", _ctx(settings, db, base_runner())).status == "NOT_APPLICABLE"
    run = WorkerRun(project_id="p", domain="PERSONAL", head_commit="b" * 40)
    db.add(run)
    db.flush()
    db.add(AcceptanceRecord(task_id="t", run_id=run.id, project_id="p", domain="PERSONAL", artifact_hash="h",
                            head_commit="a" * 40, acceptance_status="ACCEPTED", verifier_evidence_refs=[], reasons=[]))
    db.commit()
    res = check("evidence.acceptance_invalidation", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and res.error_code == "ACCEPTANCE_NOT_INVALIDATED"
    assert "head changed" in json.dumps(res.observed_evidence)


def test_evidence_dir_permissions_runs_and_reads_windows_acls(settings, db, monkeypatch):
    load_all()
    assert REGISTRY["evidence.dir_permissions"].fn is evidence_mod.evidence_dir_permissions
    monkeypatch.setattr(evidence_mod, "_windows", lambda: True)
    r = base_runner()
    r.add_binary("powershell")
    r.outputs["powershell"] = ok(json.dumps([{"sid": "S-1-1-0", "rights": "FullControl", "type": "Allow",
                                              "inherited": True}]))
    res = check("evidence.dir_permissions", _ctx(settings, db, r))
    assert res.status == "FAILED" and res.error_code == "EVIDENCE_DIR_ACL_TOO_OPEN"
    r.outputs["powershell"] = ok(json.dumps({"sid": "S-1-5-18", "rights": "FullControl", "type": "Allow",
                                             "inherited": True}))
    res = check("evidence.dir_permissions", _ctx(settings, db, r))
    assert res.status == "VERIFIED" and res.observed_evidence["acl_checked"] is True
    res = check("evidence.dir_permissions", _ctx(settings, db, base_runner()))
    assert res.status == "CONFIGURED" and res.observed_evidence["acl_checked"] is False


# ------------------------------------------------------------------ §08 Host: clock, ACL, VS Code, budget


def test_clock_compares_with_os_time_sync_when_functional(settings, db, monkeypatch):
    monkeypatch.setattr("aeos.doctor.checks.host.sys.platform", "win32")
    r = base_runner()
    r.add_binary("w32tm")
    r.outputs["w32tm"] = ok("Leap Indicator: 3(not synchronized)\nSource: Local CMOS Clock\n"
                            "Last Successful Sync Time: unspecified\n")
    res = check("host.clock", _ctx(settings, db, r, functional=True))
    assert res.status == "FAILED" and res.error_code == "CLOCK_NOT_SYNCED"
    r.outputs["w32tm"] = ok("Source: time.windows.com,0x9\nLast Successful Sync Time: 10/19/2026 1:02:03 AM\n")
    res = check("host.clock", _ctx(settings, db, r, functional=True))
    assert res.status == "VERIFIED" and res.observed_evidence["ntp_compared"] is True
    plain = check("host.clock", _ctx(settings, db, r))
    assert plain.observed_evidence["ntp_compared"] is False


def test_data_dir_acl_is_checked_on_windows(settings, db, monkeypatch):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(host_mod, "_posix", lambda: False)
    r = base_runner()
    r.add_binary("powershell")
    r.outputs["powershell"] = ok(json.dumps([{"sid": "S-1-5-32-545", "rights": "Modify, Synchronize", "type": "Allow",
                                              "inherited": False}]))
    res = check("host.data_dir", _ctx(settings, db, r))
    assert res.status == "FAILED" and res.error_code == "DATA_DIR_ACL_TOO_OPEN"


def test_port_conflict_names_the_owning_process(monkeypatch):
    import types

    import psutil

    conn = types.SimpleNamespace(laddr=types.SimpleNamespace(port=8787), status=psutil.CONN_LISTEN, pid=4242)
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": [conn])
    monkeypatch.setattr(psutil, "Process", lambda pid: types.SimpleNamespace(name=lambda: "other-server"))
    assert host_mod.port_owner(8787) == {"status": "IDENTIFIED", "pid": 4242, "name": "other-server"}

    def denied(kind="tcp"):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", denied)
    assert host_mod.port_owner(8787)["status"] == "UNKNOWN"


def test_vscode_identity_is_recorded(settings, db):
    r = base_runner()
    r.add_binary("code")
    r.outputs["code"] = ok("1.105.1\nabcdef0123456789\nx64\n")
    res = check("host.vscode", _ctx(settings, db, r))
    assert res.status == "VERIFIED" and res.component_version == "1.105.1"
    assert res.observed_evidence["commit"] == "abcdef0123456789" and res.observed_evidence["channel"] == "stable"
    assert res.observations[0]["component_id"] == "vscode"
    assert check("host.vscode", _ctx(settings, db, base_runner())).status == "MISSING"


def test_owner_resource_budget_is_audited_and_compared(settings, db, personal_client):
    from aeos.models.audit import AuditEvent

    assert check("host.platform", _ctx(settings, db, base_runner())).observed_evidence["resource_budget"].startswith(
        "UNKNOWN")
    r = personal_client.put("/api/doctor/resource-budget", json={"cpu_cores": 1024, "disk_gib": 1_000_000,
                                                                 "reason": "test budget on purpose"})
    assert r.status_code == 200, r.text
    db.expire_all()
    res = check("host.platform", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and res.error_code == "RESOURCE_BELOW_BUDGET"
    assert check("host.disk", _ctx(settings, db, base_runner())).status == "FAILED"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "doctor.resource_budget.set"))
    from aeos.doctor import facade

    assert facade.resource_budget(db)["cpu_cores"] == 1024
    assert personal_client.put("/api/doctor/resource-budget", json={"cpu_cores": 0, "reason": "zero"}).status_code == 422


# ------------------------------------------------------------------ §08 Repository domain


def test_repo_domain_is_personal_and_never_an_office_host(settings, db):
    res = check("repo.domain", _ctx(settings, db, base_runner()))
    assert res.status == "CONFIGURED"
    _projects_with_repos(db)
    assert check("repo.domain", _ctx(settings, db, base_runner())).status == "VERIFIED"
    r = base_runner()
    r.outputs["git"] = git_handler(remote="git@bitbucket.org:corp/dly-aeos.git")
    bad = check("repo.domain", _ctx(settings, db, r))
    assert bad.status == "FAILED" and bad.error_code == "PLATFORM_REPO_OFFICE_HOST"


# ------------------------------------------------------------------ §08 Containment protected paths


def test_protected_paths_are_compared_with_enforced_lists_and_self_tested(settings, db, monkeypatch):
    res = check("containment.protected_paths", _ctx(settings, db, base_runner()))
    assert res.status == "VERIFIED" and res.observed_evidence["self_test"]["missed"] == []
    from aeos.worker_runtime import scope as worker_scope

    monkeypatch.setattr(worker_scope, "PLATFORM_PROTECTED_PATHS", worker_scope.PLATFORM_PROTECTED_PATHS[:-1])
    res = check("containment.protected_paths", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and res.error_code == "PROTECTED_PATHS_DIVERGED"
    monkeypatch.undo()
    from aeos.verification import scope as verifier_scope

    real = verifier_scope.check_scope
    monkeypatch.setattr(verifier_scope, "check_scope",
                        lambda changed, **kw: real(changed, **{**kw, "protected": ["policy/**"]}))
    res = check("containment.protected_paths", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and res.error_code == "PROTECTED_PATH_NOT_QUARANTINED"


# ------------------------------------------------------------------ §08 paid-write flag, claude auth fingerprint


def test_route_registry_flags_paid_remedies(settings, db):
    from aeos.models.routing import ModelRoute

    db.add(ModelRoute(route_key="cheap", pool="WORKER", provider="openrouter", model_id="a:free", is_free="YES"))
    db.commit()
    assert check("route.registry", _ctx(settings, db, base_runner())).requires_paid_write is False
    db.add(ModelRoute(route_key="paid", pool="WORKER", provider="openrouter", model_id="b", is_free="NO"))
    db.commit()
    res = check("route.registry", _ctx(settings, db, base_runner()))
    assert res.status == "MISSING" and res.requires_paid_write is True


def test_claude_auth_fingerprint_follows_credential_metadata_without_reading_it(settings, db, tmp_path):
    cfg = tmp_path / "claude-config"
    cfg.mkdir()
    cred = cfg / ".credentials.json"
    cred.write_text('{"a": 1}', encoding="utf-8")
    r = base_runner(env={"CLAUDE_CONFIG_DIR": str(cfg)})
    from aeos.doctor.runner import compute_fingerprint

    load_all()
    fp1 = compute_fingerprint(REGISTRY["claude.auth"], _ctx(settings, db, r))
    cred.write_text('{"a": 1, "refreshed": true}', encoding="utf-8")
    fp2 = compute_fingerprint(REGISTRY["claude.auth"], _ctx(settings, db, r))
    assert fp1 and fp2 and fp1 != fp2


# ------------------------------------------------------------------ §08 STALE -> targeted recheck


def test_invalidation_triggers_a_targeted_background_recheck(settings, db, monkeypatch):
    monkeypatch.setattr(caps, "RECHECK_DELAY_SECONDS", 0.05)
    row = _row(db, "host.disk", at=utcnow() - timedelta(minutes=5))
    db.commit()
    set_runner_override(base_runner())
    caps.enable_auto_recheck(settings)
    try:
        caps.invalidate(db, invalidator="config:resource_budget", reason="budget changed")
        db.commit()  # the recheck is queued only after the invalidating transaction commits
        from aeos.models.doctor_ext import DoctorRun

        deadline = time.monotonic() + 20
        fresh = None
        while time.monotonic() < deadline:
            db.expire_all()
            fresh = db.scalar(select(DoctorRun).where(DoctorRun.trigger == "recheck"))
            if fresh is not None:
                break
            time.sleep(0.1)
        assert fresh is not None, "a targeted recheck ran without a manual Doctor run"
        db.expire_all()
        latest = db.scalars(select(CheckResult).where(CheckResult.check_id == "host.disk")
                            .order_by(CheckResult.last_checked_at.desc())).first()
        assert latest.id != row.id and latest.status != "STALE" and latest.run_id == fresh.id
    finally:
        caps.disable_auto_recheck()
        set_runner_override(None)


def test_no_recheck_without_the_server_hook(settings, db):
    assert caps.auto_recheck_enabled() is False
    _row(db, "host.disk")
    db.commit()
    caps.invalidate(db, invalidator="config:resource_budget", reason="x")
    assert "aeos_doctor_recheck" not in db.info
    db.commit()


def test_verifier_independence_is_self_tested_with_functional(settings, db, monkeypatch):
    assert check("evidence.verifier", _ctx(settings, db, base_runner())).status == "CONFIGURED"
    res = check("evidence.verifier", _ctx(settings, db, base_runner(), functional=True))
    assert res.status == "VERIFIED", res.observed_evidence
    test = res.observed_evidence["independence_self_test"]
    assert test["shadow_detected"] and test["overlap_refused"] and test["sibling_allowed"] and test["trusted_suite_hash"]
    from aeos.verification import verifier

    monkeypatch.setattr(verifier, "shadowed_modules", lambda cand, trusted: [])
    bad = check("evidence.verifier", _ctx(settings, db, base_runner(), functional=True))
    assert bad.status == "FAILED" and bad.error_code == "VERIFIER_NOT_INDEPENDENT"


def test_claude_review_is_e0_advisory_and_sends_platform_rows_only(settings, db, monkeypatch):
    from aeos.claude_bridge import facade as bridge
    from aeos.doctor import review, views

    sent: list = []

    def fake(req):
        sent.append(req)
        return bridge.ClaudeResult(ok=True, structured={"summary": "disk penuh", "findings": [
            {"check_id": "host.disk", "observation": "ruang kecil", "suggestion": "kosongkan"}]}, text="",
            model_requested="opus", model_resolved="claude-opus", engine_version="2.1", session_id=None, usage={},
            duration_ms=1)

    monkeypatch.setattr(bridge, "run_structured", fake)
    before = _row(db, "host.disk", status="FAILED")
    _row(db, "adapter.github_token", status="MISSING", scope={"domain": "PERSONAL"})
    db.commit()
    rows = [{"check_id": r.check_id, "status": r.status, "scope": r.scope, "summary": "x"} for r in
            db.scalars(select(CheckResult))]
    rec = review.review_with_claude(db, rows)
    db.commit()
    assert rec["evidence_level"] == "E0" and rec["advisory"] and rec["findings"][0]["check_id"] == "host.disk"
    req = sent[0]
    assert req.purpose == "doctor.review" and req.allowed_tools == [] and req.domain is None
    assert "adapter.github_token" not in req.prompt, "domain-scoped rows never leave the controller"
    db.refresh(before)
    assert before.status == "FAILED", "the review never changes a status"
    assert views.checks_view(db, domain="PERSONAL")["claude_review"]["summary"] == "disk penuh"


def test_doctor_cli_budget_and_inventory_cli_poll_and_sbom(settings, db, tmp_path):
    from typer.testing import CliRunner

    from aeos.cli import app

    runner = CliRunner()
    out = runner.invoke(app, ["doctor", "budget"])
    assert out.exit_code == 0 and "UNKNOWN" in out.output
    out = runner.invoke(app, ["doctor", "budget", "--cpu", "4", "--ram", "8", "--disk", "20", "--max-workers", "2",
                              "--reason", "owner sets the real machine budget"])
    assert out.exit_code == 0, out.output
    assert "cpu_cores=4" in runner.invoke(app, ["doctor", "budget"]).output
    out = runner.invoke(app, ["inventory", "poll"])
    assert out.exit_code == 0 and "DISABLED" in out.output
    target = tmp_path / "bom.cdx.json"
    out = runner.invoke(app, ["inventory", "sbom", "--out", str(target)])
    assert out.exit_code == 0 and json.loads(target.read_text(encoding="utf-8"))["bomFormat"] == "CycloneDX"


def test_project_items_hide_other_domains_and_owner_creation(settings, db):
    create_admin(db)
    _projects_with_repos(db)
    out = run_doctor(settings, db, checks=["adapter.project_repo"], runner=base_runner())
    db.commit()
    assert {r.scope.get("domain") for r in out.results} == {"PERSONAL", "OFFICE"}
    from aeos.doctor import views

    office = views.checks_view(db, domain="OFFICE")["project_items"]
    assert office and all(i["scope"]["domain"] == "OFFICE" for i in office)
