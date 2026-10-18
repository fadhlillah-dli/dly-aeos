"""Control Center projections (master §35 Overview/Readiness, History, extras: search, activity feed,
integration settings).

§50 rows: "Tool opsional atau Office belum ready -> Dashboard tetap terbuka" (missing modules degrade to
UNKNOWN, never crash); "failed test dan unverified outcome tidak hijau; cost/quota UNKNOWN ditampilkan";
"Fixture ... MOCK/DEMO jelas; tidak dihitung ... cost live"; "Dashboard Personal mencoba run/evidence
Office -> Backend menolak"; "Viewer mengakses detail/search/export inventory Office -> scope diterapkan".
"""

from __future__ import annotations

from sqlalchemy import select

from aeos.core.enums import Capability, Domain
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.cost import UsageLedgerEntry
from aeos.models.decisions import ArchitectureSnapshot, Decision, PolicySnapshot
from aeos.models.inventory import CheckResult
from aeos.models.planning import Plan, PlanVersion
from aeos.models.portfolio import Project, ProjectIntegration
from aeos.models.publishing import Publication, PublicationOperation
from tests.unit.test_operations_fakes import (
    block_module,
    fake_doctor,
    fake_planning,
    fake_queue,
    fake_workforce,
)
from tests.unit.test_operations_sli import make_task

ALL_IMPLS = ("aeos.doctor.capabilities", "aeos.planning.service", "aeos.policy.queue", "aeos.workforce.scheduler",
             "aeos.cost.service")


def _project(db, key="alpha", domain="PERSONAL", **kw):
    p = Project(project_key=key, name=key.title(), domain=domain, **kw)
    db.add(p)
    db.commit()
    return p


def _select(client, project_id=None):
    client.select_domain(Domain.PERSONAL, project_id)


def test_overview_degrades_to_unknown_when_modules_missing(personal_client, db, monkeypatch):
    for name in ALL_IMPLS:
        block_module(monkeypatch, name)
    p = _project(db)
    _select(personal_client, p.id)
    r = personal_client.get("/api/control-center/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    s = body["sections"]
    assert s["capabilities"]["status"] == "UNKNOWN" and "modul belum tersedia" in s["capabilities"]["reason"]
    assert s["active_plan"]["status"] == "MISSING"  # no plans at all is a fact, not a module failure
    assert s["decisions"]["status"] == "UNKNOWN"
    assert s["secrets"]["status"] == "UNKNOWN"  # no secret store -> Blocked -> UNKNOWN
    assert s["blockers"]["status"] == "UNKNOWN"  # doctor never ran -> absence of blockers not proven
    assert s["cost"]["status"] == "UNKNOWN" and "modul cost" in s["cost"]["reason"]
    assert s["recovery"]["status"] == "NOT-PROVEN"
    assert s["policy"]["status"] == "MISSING"
    assert all(sec["status"] != "READY" for sec in s.values())
    assert body["project"]["id"] == p.id and body["mode"] == "LIVE"
    for sec in s.values():
        assert {"status", "source", "checked_at", "reason", "evidence_limit"} <= set(sec)


def test_ready_without_checks_is_not_ready(personal_client, db, monkeypatch):
    check = {"check_id": "claude.auth", "status": "VERIFIED", "last_checked_at": utcnow().isoformat()}
    fake_doctor(monkeypatch, {"CAN_OPEN_DASHBOARD": ("READY", [check]), "CAN_PLAN_PERSONAL": ("READY", [])})
    caps = personal_client.get("/api/control-center/overview").json()["sections"]["capabilities"]
    by = {c["capability"]: c for c in caps["data"]["items"]}
    assert by["CAN_OPEN_DASHBOARD"]["status"] == "READY"
    assert by["CAN_PLAN_PERSONAL"]["status"] == "UNKNOWN"  # READY claimed without evidence
    assert caps["status"] == "UNKNOWN"  # overall never READY while anything is unknown
    assert len(caps["data"]["items"]) == len(Capability)


def test_active_plan_policy_and_decisions(personal_client, db, monkeypatch):
    fake_planning(monkeypatch)
    p = _project(db)
    plan = Plan(project_id=p.id, domain="PERSONAL", title="Upgrade AEOS", state="APPROVED")
    db.add(plan)
    db.flush()
    pv = PlanVersion(plan_id=plan.id, project_id=p.id, domain="PERSONAL", version=2, content={}, manifest_hash="sha256:m")
    db.add(pv)
    db.flush()
    plan.approved_version_id = pv.id
    db.add(PolicySnapshot(version="1", content={}, content_hash="sha256:pol", active=True,
                          mci_reference={"status": "UNVERIFIED"}))
    db.commit()
    fake_queue(monkeypatch, [{"kind": "PLAN_APPROVAL", "title": "Approve plan", "domain": "PERSONAL", "project_id": p.id},
                             {"kind": "PUBLICATION", "title": "Office pub", "domain": "OFFICE"}])
    _select(personal_client, p.id)
    s = personal_client.get("/api/control-center/overview").json()["sections"]
    assert s["active_plan"]["status"] == "APPROVED"
    assert s["active_plan"]["data"]["approved"][0]["manifest_hash"] == "sha256:m"
    assert s["policy"]["data"]["content_hash"] == "sha256:pol" and "UNVERIFIED" in s["policy"]["reason"]
    assert s["decisions"]["data"]["count"] == 1 and s["decisions"]["data"]["top"][0]["title"] == "Approve plan"


def test_blockers_and_failed_critical_checks(personal_client, db, monkeypatch):
    fake_workforce(monkeypatch)
    project, _spec, te = make_task(db, Domain.PERSONAL, "alpha", "BLOCKED")
    te.reason = "waiting for credential"
    db.add(CheckResult(check_id="openbao.health", group="secret", status="FAILED", severity="CRITICAL",
                       summary="OpenBao sealed", scope={"domain": "PERSONAL"}))
    db.add(CheckResult(check_id="office.jira", group="adapter", status="FAILED", severity="CRITICAL",
                       summary="office", scope={"domain": "OFFICE"}))
    db.commit()
    _select(personal_client, project.id)
    b = personal_client.get("/api/control-center/overview").json()["sections"]["blockers"]
    assert b["status"] == "BLOCKED"
    kinds = {(e["kind"], e["id"]) for e in b["data"]["items"]}
    assert ("check", "openbao.health") in kinds and ("check", "office.jira") not in kinds
    assert any(e["kind"] == "task" and e["reason"] == "waiting for credential" for e in b["data"]["items"])


def test_cost_excludes_mock_and_shows_unknown(personal_client, db):
    db.add_all([
        UsageLedgerEntry(domain="PERSONAL", provider="openrouter", category="WORKER_API", source="broker", cost_usd=0.25),
        UsageLedgerEntry(domain="PERSONAL", provider="openrouter", category="WORKER_API", source="estimator",
                         cost_usd=1.0, is_estimate=True),
        UsageLedgerEntry(domain="PERSONAL", provider="mock", category="WORKER_API", source="fixture", cost_usd=99,
                         mode="MOCK"),
        UsageLedgerEntry(domain="PERSONAL", provider="openrouter", category="WORKER_API", source="broker", cost_usd=None),
        UsageLedgerEntry(domain="OFFICE", provider="x", category="WORKER_API", source="broker", cost_usd=50),
    ])
    db.commit()
    body = personal_client.get("/api/control-center/overview").json()
    c = body["sections"]["cost"]
    assert c["data"]["actual_usd"] == 0.25 and c["data"]["estimated_usd"] == 1.0
    assert c["data"]["mock_entries_excluded"] == 1 and c["data"]["unknown_cost_entries"] == 1
    assert c["status"] == "UNKNOWN"
    assert any(q["provider"] == "claude-native" and q["state"] == "UNKNOWN" for q in c["data"]["quotas"])
    assert body["contains_mock"] is True and body["mode"] == "MIXED"


def test_overview_requires_domain_and_rejects_cross_domain_project(admin_client, db):
    assert admin_client.get("/api/control-center/overview").status_code == 409
    office = _project(db, "omega", "OFFICE")
    admin_client.select_domain(Domain.PERSONAL)
    assert admin_client.get(f"/api/control-center/overview?project_id={office.id}").status_code == 404
    hint = admin_client.get("/api/control-center/overview").json()["sections"]["project_hint"]
    assert "aeos seed" in hint["reason"]


def test_activity_feed_scoped_paginated_and_filtered(personal_client, db):
    p = _project(db)
    o = _project(db, "omega", "OFFICE")
    for i in range(5):
        events.emit(db, source="controller", kind="task.updated", entity_id=f"t{i}", domain="PERSONAL", project_id=p.id)
    events.emit(db, source="controller", kind="task.updated", entity_id="office", domain="OFFICE", project_id=o.id)
    events.emit(db, source="doctor", kind="doctor.finished", entity_id="d", domain=None)
    db.commit()
    page = personal_client.get("/api/control-center/activity?limit=3").json()
    assert len(page["items"]) == 3 and page["next_before"]
    rest = personal_client.get(f"/api/control-center/activity?limit=10&before={page['next_before']}").json()
    ids = [e["entity_id"] for e in page["items"] + rest["items"]]
    assert "office" not in ids and "d" in ids and len(ids) == 6
    only = personal_client.get("/api/control-center/activity?kind=doctor.").json()["items"]
    assert [e["kind"] for e in only] == ["doctor.finished"]


def test_integrations_show_status_never_values(personal_client, db, secrets_memory):
    p = _project(db)
    secrets_memory.put("personal/github", "ghp_" + "Q" * 36, actor="test")
    db.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="github", purpose="code",
                              root_id="fadhlillah-dli/dly-aeos", credential_ref="personal/github",
                              config={"api_token": "should-not-show"}))
    db.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="google-docs", purpose="docs",
                              credential_ref="personal/google-oauth"))
    db.commit()
    body = personal_client.get("/api/control-center/integrations").json()
    text = str(body)
    assert "Q" * 36 not in text and "should-not-show" not in text
    by = {i["provider"]: i for i in body["items"]}
    assert by["github"]["credential"]["status"] == "CONFIGURED"
    assert by["google-docs"]["credential"]["status"] == "MISSING"
    assert by["github"]["verification"] == "NOT_TESTED"


def test_integrations_list_the_doctor_checks_of_their_provider(personal_client, db):
    """W1 seam: one provider vocabulary - the github-issues integration shows adapter.github_token (no substring
    of "github-issues" matches it) and google-drive shows adapter.google_oauth (aeos.portfolio.providers)."""
    p = _project(db)
    db.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="github-issues", purpose="issues",
                              root_id="fadhlillah-dli/dly-aeos", credential_ref="personal/github"))
    db.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="google-drive", purpose="docs",
                              credential_ref="personal/google-oauth-token"))
    db.add(CheckResult(check_id="adapter.github_token", group="PROJECT_ADAPTERS", component_id="github",
                       status="CONNECTED", severity="INFO", summary="token ok", scope={"domain": "PERSONAL"}))
    db.add(CheckResult(check_id="adapter.google_oauth", group="PROJECT_ADAPTERS", component_id="google-drive",
                       status="MISSING", severity="HIGH", summary="token missing", scope={"domain": "PERSONAL"}))
    db.add(CheckResult(check_id="adapter.office_jira", group="PROJECT_ADAPTERS", component_id="jira",
                       status="NOT_APPLICABLE", severity="INFO", summary="office"))
    db.commit()
    by = {i["provider"]: i for i in personal_client.get("/api/control-center/integrations").json()["items"]}
    assert [c["check_id"] for c in by["github-issues"]["checks"]] == ["adapter.github_token"]
    assert [c["check_id"] for c in by["google-drive"]["checks"]] == ["adapter.google_oauth"]
    assert by["google-drive"]["checks"][0]["status"] == "MISSING"  # shown as it is, never as a pass


def test_search_is_domain_scoped(personal_client, db):
    _project(db, "alpha-app")
    _project(db, "alpha-office", "OFFICE")
    hits = personal_client.get("/api/control-center/search?q=alpha").json()["items"]
    assert {h["title"] for h in hits} == {"Alpha-App"}
    assert personal_client.get("/api/control-center/search?q=a").status_code == 422
    assert personal_client.get("/api/control-center/search?q=%25%25").json()["items"] == []  # LIKE wildcard escaped


def test_history_endpoints_scoped(personal_client, db):
    p = _project(db)
    o = _project(db, "omega", "OFFICE")
    from aeos.audit import service as audit

    audit.record(db, actor_type="user", actor_id="u", action="plan.approve", result="SUCCESS", project_id=p.id,
                 domain="PERSONAL")
    audit.record(db, actor_type="user", actor_id="u", action="plan.approve", result="DENIED", project_id=p.id,
                 domain="PERSONAL")
    audit.record(db, actor_type="user", actor_id="u", action="office.x", project_id=o.id, domain="OFFICE")
    db.add(Decision(kind="APPROVAL", actor_user_id="u", target_type="plan_version", target_id="pv1",
                    project_id=p.id, domain="PERSONAL"))
    db.add(Decision(kind="APPROVAL", actor_user_id="u", target_type="plan_version", target_id="pv-office",
                    project_id=o.id, domain="OFFICE"))
    db.add(ArchitectureSnapshot(label="v1", refs={"manifest": "sha256:a", "adrs": {"ADR-0001": "h1"}}, snapshot_hash="s1"))
    db.commit()
    db.add(ArchitectureSnapshot(label="v2", refs={"manifest": "sha256:b", "adrs": {"ADR-0001": "h1", "ADR-0090": "h2"}},
                                snapshot_hash="s2"))
    pub = Publication(project_id=p.id, domain="PERSONAL", plan_version_id="pv1", approved_hash="h", decision_id="d",
                      status="PARTIAL")
    db.add(pub)
    db.flush()
    db.add(PublicationOperation(publication_id=pub.id, provider="google-docs", operation="create_doc", logical_id="00",
                                idempotency_key="k1", payload_hash="h", status="SUCCEEDED"))
    db.add(PublicationOperation(publication_id=pub.id, provider="github-issues", operation="create_issue",
                                logical_id="i1", idempotency_key="k2", payload_hash="h", status="FAILED"))
    db.commit()
    denied = personal_client.get("/api/control-center/history/audit?action=plan.&result=DENIED").json()["items"]
    assert len(denied) == 1 and denied[0]["result"] == "DENIED"
    all_audit = personal_client.get("/api/control-center/history/audit?limit=500").json()["items"]
    assert all(a["action"] != "office.x" for a in all_audit)
    decisions = personal_client.get("/api/control-center/history/decisions").json()["items"]
    assert [d["target_id"] for d in decisions] == ["pv1"]
    snaps = personal_client.get("/api/control-center/history/snapshots").json()["items"]
    assert snaps[0]["label"] == "v2"
    changes = {(c["path"], c["change"]) for c in snaps[0]["diff"]}
    assert ("manifest", "changed") in changes and ("adrs.ADR-0090", "added") in changes
    pubs = personal_client.get("/api/control-center/history/publications").json()["items"]
    assert pubs[0]["status"] == "PARTIAL" and pubs[0]["operations"] == {"SUCCEEDED": 1, "FAILED": 1}
    assert len(db.scalars(select(Publication)).all()) == 1


def test_ready_with_stale_or_failed_supporting_check_is_not_ready(personal_client, db, monkeypatch):
    """§08/§50 "Config/model/provider berubah setelah doctor -> check terkait kedaluwarsa": a READY claim
    whose supporting check is STALE/FAILED/UNKNOWN is shown as UNKNOWN with the check named."""
    now = utcnow().isoformat()
    fake_doctor(monkeypatch, {
        "CAN_OPEN_DASHBOARD": ("READY", [{"check_id": "core.db", "status": "VERIFIED", "last_checked_at": now}]),
        "CAN_PLAN_PERSONAL": ("READY", [{"check_id": "claude.auth", "status": "STALE", "last_checked_at": now}]),
        "CAN_RUN_WORKER": ("READY", [{"check_id": "sandbox.kill", "status": "FAILED", "last_checked_at": now}]),
    })
    by = {c["capability"]: c for c in
          personal_client.get("/api/control-center/overview").json()["sections"]["capabilities"]["data"]["items"]}
    assert by["CAN_OPEN_DASHBOARD"]["status"] == "READY"
    assert by["CAN_PLAN_PERSONAL"]["status"] == "UNKNOWN" and "claude.auth" in by["CAN_PLAN_PERSONAL"]["reasons"][-1]
    assert by["CAN_RUN_WORKER"]["status"] == "UNKNOWN" and "sandbox.kill" in by["CAN_RUN_WORKER"]["reasons"][-1]


def test_secret_refs_budgets_and_quotas_are_domain_scoped(personal_client, db, secrets_memory):
    """§35/§50 "Viewer mengakses ... Office -> scope diterapkan termasuk counts": OFFICE secret refs,
    Office project budgets and Office-scoped quotas never appear in (or count towards) a PERSONAL view."""
    from aeos.models.cost import Budget, QuotaState
    from aeos.models.workforce import WorkerRun

    p = _project(db)
    o = _project(db, "omega", "OFFICE")
    for ref in ("personal/github", "office/atlassian", "workers/openrouter", f"projects/{p.id}/x",
                f"projects/{o.id}/y", "projects/omega/z"):
        secrets_memory.put(ref, "value-" + "Z" * 30, actor="test")
    office_run = WorkerRun(project_id=o.id, domain="OFFICE", task_id="t", role_key="impl", execution_status="DONE")
    db.add(office_run)
    db.flush()
    db.add_all([
        Budget(scope_type="GLOBAL", scope_id="*", limit_usd=10),
        Budget(scope_type="PROJECT", scope_id=p.id, limit_usd=5),
        Budget(scope_type="PROJECT", scope_id=o.id, limit_usd=5),
        Budget(scope_type="RUN", scope_id=office_run.id, limit_usd=1),
        QuotaState(provider="openrouter", scope="*", state="OK"),
        QuotaState(provider="jira", scope="office/acme", state="LOW"),
        QuotaState(provider="x", scope="*", state="OK", details={"domain": "OFFICE"}),
    ])
    db.commit()
    _select(personal_client, p.id)
    s = personal_client.get("/api/control-center/overview").json()["sections"]
    refs = {r["ref"] for r in s["secrets"]["data"]["items"]}
    assert refs == {"personal/github", "workers/openrouter", f"projects/{p.id}/x"}
    assert "Z" * 30 not in str(s["secrets"])
    assert s["cost"]["data"]["budgets"] == 2  # GLOBAL + the Personal project budget
    providers = {q["provider"] for q in s["cost"]["data"]["quotas"]}
    assert "openrouter" in providers and "jira" not in providers and "x" not in providers
    personal_client.select_domain(Domain.OFFICE, o.id)
    s_office = personal_client.get("/api/control-center/overview").json()["sections"]
    assert "office/atlassian" in {r["ref"] for r in s_office["secrets"]["data"]["items"]}
    assert "personal/github" not in {r["ref"] for r in s_office["secrets"]["data"]["items"]}
    assert s_office["cost"]["data"]["budgets"] == 3  # GLOBAL + Office project + Office run


def test_search_hides_components_scoped_to_other_domain_or_project(personal_client, db):
    from aeos.models.inventory import ComponentReference

    p = _project(db)
    o = _project(db, "omega", "OFFICE")
    db.add_all([
        ComponentReference(component_id="tool-shared", kind="tool", name="Toolbox shared", scope={}),
        ComponentReference(component_id="tool-office", kind="tool", name="Toolbox office", scope={"domain": "OFFICE"}),
        ComponentReference(component_id="tool-office-proj", kind="tool", name="Toolbox omega",
                           scope={"project_id": o.id}),
        ComponentReference(component_id="tool-alpha", kind="tool", name="Toolbox alpha", scope={"project_id": p.id}),
    ])
    db.commit()
    hits = personal_client.get("/api/control-center/search?q=toolbox").json()["items"]
    assert {h["id"] for h in hits if h["type"] == "component"} == {"tool-shared", "tool-alpha"}


def test_overview_header_carries_environment(personal_client, db):
    body = personal_client.get("/api/control-center/overview").json()
    assert body["environment"] and "dev_mode" in body


def test_pending_decisions_without_domain_field_are_scoped_by_project(personal_client, db, monkeypatch):
    """A queue item that carries only a project_id is shown only when that project is in the active
    domain (an admin is unrestricted by project, but never across domains)."""
    p = _project(db)
    o = _project(db, "omega", "OFFICE")
    fake_queue(monkeypatch, [{"kind": "PLAN_APPROVAL", "title": "Personal item", "project_id": p.id},
                             {"kind": "PLAN_APPROVAL", "title": "Office item", "project_id": o.id},
                             {"kind": "PROMOTION", "title": "Platform item"}])
    _select(personal_client, p.id)
    top = personal_client.get("/api/control-center/overview").json()["sections"]["decisions"]["data"]["top"]
    assert {t["title"] for t in top} == {"Personal item", "Platform item"}
