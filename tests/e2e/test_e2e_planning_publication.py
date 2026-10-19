"""End to end through the HTTP API: PERSONAL DLY AEOS project -> 4-stage planning through the real Claude
bridge (scripted fake binary) -> human approval -> write enablement -> preview -> one PUBLICATION decision ->
interlock per operation -> Google Drive/Docs + GitHub Issues (respx) -> reconcile -> PUBLISHED.

Master §03/§05 steps 2-10, §12, §14, §26-§30, §42, §50 rows "Approval PlanVersion lama", "Revisi baru
muncul selama publish", "Google Docs direvisi manusia", "Provider timeout setelah create resource",
"Submit prompt tanpa domain"/OFFICE default deny; U-04, U-10.

Nothing leaves the process: the claude child process is a local script, every HTTP call is answered by
respx (unmocked requests fail), credentials are visibly fake values in the in-memory secret store.
"""

from __future__ import annotations

import copy

import pytest
import respx
from sqlalchemy import func, select

from aeos.core.enums import Domain
from aeos.models.audit import AuditEvent
from aeos.models.cost import UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.planning import PlanningRun
from aeos.models.publishing import (
    DocumentRecord,
    DocumentRevision,
    ExternalReference,
    OperationalWorkProjection,
    PublicationOperation,
)
from aeos.planning import runner
from aeos.publisher import worker as pub_worker
from tests.conftest import ADMIN_PW, AuthedClient, create_admin
from tests.e2e.fakes import RESOLVED, FakeClaude, FakeDrive, FakeGitHub, seed_fake_personal_secrets
from tests.unit.test_planning_support import challenge_output, final_output, sample_content, synthesis_output

BRIEF = (b"# Brief: AEOS publication slice\n\n- Publish Product 1 docs to Google Drive.\n"
         b"- Track tasks as GitHub Issues.\n- Every external write needs the owner's approval.\n")


@pytest.fixture
def world(settings, db, client: AuthedClient, secrets_memory, monkeypatch, tmp_path):
    from aeos.claude_bridge import facade as bridge_facade
    from aeos.policy import seed as policy_seed
    from aeos.portfolio import seed as portfolio_seed

    policy_seed.run(db)
    portfolio_seed.run(db)
    db.commit()
    create_admin(db)
    client.login("owner", ADMIN_PW)
    seed_fake_personal_secrets(secrets_memory)
    claude = FakeClaude(monkeypatch, tmp_path, outputs={
        "PRIMARY_PLANNING": sample_content("DLY AEOS"), "CHALLENGE_REVIEW": challenge_output(),
        "SYNTHESIS": synthesis_output(sample_content("DLY AEOS", note="v2")), "FINAL_REVIEW": final_output()})
    requests: list = []
    real = bridge_facade.run_structured

    def spy(request):
        requests.append(request)
        return real(request)

    monkeypatch.setattr(bridge_facade, "run_structured", spy)
    yield {"client": client, "db": db, "settings": settings, "claude": claude, "requests": requests,
           "drive": FakeDrive(), "github": FakeGitHub(settings.platform_repo_slug)}
    runner.join_all(30)


def _ok(r, code: int = 200):
    assert r.status_code == code, r.text
    return r.json()


def _plan(c: AuthedClient, plan_id: str) -> dict:
    return _ok(c.get(f"/api/plans/{plan_id}"))


def _publish_pass(world, router=None) -> None:
    """One executor pass (the app's background worker is not started in tests)."""
    pub_worker.run_once()


def _confirm(pub: dict) -> dict:
    return {"plan_version_id": pub["plan_version_id"], "approved_hash": pub["approved_hash"],
            "preview_hash": pub["preview_hash"]}


def _approve(c: AuthedClient, plan_id: str, detail: dict) -> dict:
    v = detail["current_version"]
    targets = [{"provider": t["provider"], "root_id": t["root_id"]}
               for t in detail["operation_catalog"]["configured_targets"]]
    return _ok(c.post(f"/api/plans/{plan_id}/decision", json={
        "action": "APPROVE_AND_PUBLISH", "version_id": v["id"], "version_hash": v["manifest_hash"],
        "targets": targets, "rationale": "Setuju versi ini dan target terkonfigurasinya",
        # §15: a human edit was not judged by the planning review; the owner acknowledges that explicitly
        "acknowledge_unreviewed": detail.get("current_version_reviewed") is False}))


def _human_edit(c: AuthedClient, plan_id: str, detail: dict, note: str) -> dict:
    v = detail["current_version"]
    content = copy.deepcopy(_ok(c.get(f"/api/plans/{plan_id}/versions/{v['id']}"))["content"])
    content["overview"]["overview"] = f"{content['overview'].get('overview', '')} {note}".strip()
    return _ok(c.post(f"/api/plans/{plan_id}/versions", json={
        "parent_version_id": v["id"], "content": content, "rationale": f"human edit: {note}"}), 201)


def test_planning_to_publication_end_to_end(world):
    c, db, claude, drive, gh = world["client"], world["db"], world["claude"], world["drive"], world["github"]

    # ---- §05 step 2-3: explicit PERSONAL domain, the seeded DLY AEOS project (§09)
    c.select_domain(Domain.PERSONAL)
    projects = _ok(c.get("/api/projects"))["items"]
    project = next(p for p in projects if p["project_key"] == "dly-aeos")
    pid = project["id"]
    integrations = _ok(c.get(f"/api/projects/{pid}/integrations"))["items"]
    assert {(i["provider"], i["purpose"]) for i in integrations} == {("google-drive", "docs"),
                                                                     ("github-issues", "issues")}

    # ---- §05 step 4-5: idea + .md attachment through intake (U-11)
    att = _ok(c.post(f"/api/projects/{pid}/attachments", files={"file": ("brief.md", BRIEF, "text/markdown")}))
    att = att.get("attachment", att)
    assert att["scan_status"] in ("CLEAN", "FLAGGED") and att["trusted"] is False

    plan = _ok(c.post(f"/api/projects/{pid}/plans", json={
        "title": "Slice publikasi", "attachment_ids": [att["id"]],
        "prompt": "Rencanakan slice publikasi Product 1 ke Google Drive dan GitHub Issues untuk DLY AEOS."}), 201)
    plan_id = plan["id"]
    ctx = plan["context"]
    assert plan["state"] == "CONTEXT_READY" and plan["domain"] == "PERSONAL"
    assert ctx["attachments"][0]["status"] == "READ"
    # §05 step 6: existing project -> read-only inventory of the registered checkout (correct signature)
    assert ctx["repo_inventory"]["status"] == "AVAILABLE", ctx["repo_inventory"]
    assert ctx["repo_inventory"]["commit"] and ctx["repo_inventory"]["inventory"]["files"] > 0

    # ---- §12/§15: four bounded stages through the real bridge (fake binary)
    _ok(c.post(f"/api/plans/{plan_id}/run"), 202)
    runner.join_all(120)
    detail = _plan(c, plan_id)
    assert detail["state"] == "READY_FOR_HUMAN_REVIEW", detail["state_reason"]
    calls = claude.calls()
    assert [x["stage"] for x in calls] == ["PRIMARY_PLANNING", "CHALLENGE_REVIEW", "SYNTHESIS", "FINAL_REVIEW"]
    assert [x["model"] for x in calls] == ["opus", "fable", "opus", "fable"]
    for x in calls:
        assert "--bare" not in x["argv"] and "ANTHROPIC_API_KEY" not in x["env"]
        assert "Brief: AEOS publication slice" not in " ".join(x["argv"])  # prompt only via stdin
    assert "Brief: AEOS publication slice" in calls[0]["prompt"]  # the .md attachment as fenced DATA
    for req in world["requests"]:  # every request carries the human-selected domain and the project
        assert req.domain == "PERSONAL" and req.project_id == pid and req.context_files == []
    runs = list(db.scalars(select(PlanningRun).where(PlanningRun.plan_id == plan_id)))
    assert {r.model_resolved for r in runs} == set(RESOLVED.values()) and {r.mode for r in runs} == {"NATIVE"}
    usage = list(db.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.category == "NATIVE_CLAUDE")))
    assert len(usage) == 4
    assert all(u.domain == "PERSONAL" and u.project_id == pid and u.cost_usd is None for u in usage)  # §42

    # ---- §14/§50 "Approval PlanVersion lama": stale hash and stale version are refused
    current = detail["current_version"]
    assert current["mode"] == "LIVE" and current["source"] == "PLANNING_RUN"
    stale = c.post(f"/api/plans/{plan_id}/decision", json={
        "action": "APPROVE_AND_PUBLISH", "version_id": current["id"], "version_hash": "sha256:" + "0" * 64,
        "targets": [{"provider": "google-drive", "root_id": "root"}], "rationale": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "STALE_PLAN_VERSION"
    older = next(v for v in detail["versions"] if v["id"] != current["id"])
    stale = c.post(f"/api/plans/{plan_id}/decision", json={
        "action": "APPROVE_AND_PUBLISH", "version_id": older["id"], "version_hash": older["manifest_hash"],
        "targets": [{"provider": "google-drive", "root_id": "root"}], "rationale": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["details"]["latest_version_id"] == current["id"]

    # ---- §05 step 9: Approve & Publish of the exact version + its configured targets
    configured = detail["operation_catalog"]["configured_targets"]
    assert {(t["provider"], t["root_id"]) for t in configured} == {
        ("google-drive", "root"), ("github-issues", world["settings"].platform_repo_slug)}
    approval = _approve(c, plan_id, detail)["decision"]
    assert approval["auth_scope"]["action"] == "APPROVE_AND_PUBLISH"
    assert {"create_folder", "create_doc", "update_doc", "ensure_label", "create_issue"} <= set(
        approval["allowed_operations"])
    assert _plan(c, plan_id)["state"] == "APPROVED"

    # ---- §26: without write enablement the interlock refuses the run (nothing is written)
    pub = _ok(c.post(f"/api/plans/{plan_id}/publications/preview", json={"render_visuals": False}))
    denied = c.post(f"/api/publications/{pub['id']}/execute", json=_confirm(pub))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "INTERLOCK_DENIED"
    assert any("WRITE_DISABLED" in r for r in denied.json()["error"]["details"]["reasons"])

    base = {"auth_scope": {"scope": "Product 1 publication of DLY AEOS"}, "rationale": "aktifkan tulis untuk publikasi"}
    for provider in ("google-drive", "github-issues"):
        _ok(c.post("/api/decisions", json={**base, "kind": "WRITE_ENABLEMENT", "provider": provider,
                                           "enabled": True}), 201)
    status = {i["provider"]: i for i in _ok(c.get("/api/policy/write-enablement"))["items"]}
    assert status["google-drive"]["effective"] and status["github-issues"]["effective"]

    # ---- §05 step 10: preview -> one human decision for the concrete operations of this run (U-10)
    pub = _ok(c.post(f"/api/plans/{plan_id}/publications/preview", json={"render_visuals": False}))
    assert pub["status"] == "PREVIEW" and pub["plan_version_id"] == current["id"]
    assert {t["provider"]: t["status"] for t in pub["targets"]} == {"google-drive": "READY", "github-issues": "READY"}
    ops = pub["operations"]
    assert {o["provider"] for o in ops} == {"google-drive", "github-issues"}
    assert all(o["target"]["root_id"] for o in ops)
    gh.lose_response_for.add("issue:task:T1")  # §50 "Provider timeout setelah create resource"
    with respx.mock(assert_all_called=False) as router:
        drive.install(router)
        gh.install(router)
        started = _ok(c.post(f"/api/publications/{pub['id']}/execute", json=_confirm(pub)))
        assert started["status"] == "RUNNING"
        runs_decisions = list(db.scalars(select(Decision).where(Decision.kind == "PUBLICATION",
                                                                Decision.target_id == pub["id"])))
        assert len(runs_decisions) == 1  # one approval for the whole run, no per-API re-prompt
        run_decision = runs_decisions[0]
        assert run_decision.auth_scope["approval_decision_id"] == approval["id"]
        assert set(run_decision.auth_scope["operation_ids"]) == {o["id"] for o in ops if o["status"] == "PENDING"}
        assert _plan(c, plan_id)["state"] == "PUBLISHING"

        _publish_pass(world)
        view = _ok(c.get(f"/api/publications/{pub['id']}"))
        t1 = next(o for o in view["operations"] if o["logical_id"] == "issue:task:T1")
        assert t1["status"] == "UNKNOWN" and view["status"] == "RUNNING"  # never assumed failed/created
        rec = _ok(c.post(f"/api/publications/{pub['id']}/reconcile"))
        assert rec["result"]["found"] >= 1
        _publish_pass(world)
    view = _ok(c.get(f"/api/publications/{pub['id']}"))
    assert view["status"] == "PUBLISHED", view["status_reason"]
    assert len(gh.issues_for("issue:task:T1")) == 1  # reconciled by marker, no duplicate
    assert _plan(c, plan_id)["state"] == "PUBLISHED"
    assert drive.permission_calls == 0  # no public/anyone-with-link sharing (ADR-0072)

    db.expire_all()
    records = list(db.scalars(select(DocumentRecord).where(DocumentRecord.project_id == pid)))
    published = [r for r in records if r.status == "PUBLISHED"]
    assert published and all(r.links.get("external_id") for r in published)
    refs = {(r.provider, r.logical_id): r for r in db.scalars(select(ExternalReference).where(
        ExternalReference.project_id == pid))}
    assert ("google-drive", "root") in refs and any(p == "github-issues" and lid.startswith("issue:")
                                                    for p, lid in refs)
    issues = list(db.scalars(select(OperationalWorkProjection).where(OperationalWorkProjection.project_id == pid)))
    assert {i.logical_id for i in issues} == {"issue:task:T1", "issue:task:T2"}
    assert all(len(drive.by_logical(r.logical_doc_id)) == 1 for r in published)  # one file per document
    checks = db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action == "policy.interlock.check", AuditEvent.result == "SUCCESS"))
    assert checks >= len([o for o in ops if o["status"] == "PENDING"])

    # ---- §50 "Revisi baru muncul": a version created after approval is never published on that approval
    detail = _plan(c, plan_id)
    v3 = _human_edit(c, plan_id, detail, "Revisi pertama.")
    detail = _plan(c, plan_id)
    assert detail["state"] == "READY_FOR_HUMAN_REVIEW" and detail["approved_version"] is None
    refused = c.post(f"/api/plans/{plan_id}/publications/preview", json={"render_visuals": False})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "PLAN_NOT_APPROVED"
    _approve(c, plan_id, detail)
    p2 = _ok(c.post(f"/api/plans/{plan_id}/publications/preview", json={"render_visuals": False}))
    assert p2["plan_version_id"] == v3["id"]
    v4 = _human_edit(c, plan_id, _plan(c, plan_id), "Revisi kedua.")  # newer version after the approval
    updates_before = len(drive.batch_updates)
    with respx.mock(assert_all_called=False) as router:
        drive.install(router)
        gh.install(router)
        late = c.post(f"/api/publications/{p2['id']}/execute", json=_confirm(p2))
        assert late.status_code == 409 and late.json()["error"]["code"] in ("PLAN_NOT_APPROVED", "APPROVAL_STALE")
        refused = c.post(f"/api/plans/{plan_id}/publications/preview", json={"render_visuals": False})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "PLAN_NOT_APPROVED"
    assert len(drive.batch_updates) == updates_before  # neither v3 (no longer approved) nor v4 was written

    # ---- §29/§50 "Google Docs direvisi manusia": requiredRevisionId conflict, never overwritten
    detail = _plan(c, plan_id)
    assert detail["current_version"]["id"] == v4["id"]
    _approve(c, plan_id, detail)
    p3 = _ok(c.post(f"/api/plans/{plan_id}/publications/preview", json={"render_visuals": False}))
    updates = [o for o in p3["operations"] if o["operation"] == "update_doc" and o["status"] == "PENDING"]
    assert updates, "the edited overview must change at least one published document"
    edited = updates[0]
    doc_id = refs[("google-drive", edited["logical_id"])].external_id
    drive.human_edit(doc_id, "Teks yang ditulis manusia langsung di Google Docs.\n")
    with respx.mock(assert_all_called=False) as router:
        drive.install(router)
        gh.install(router)
        _ok(c.post(f"/api/publications/{p3['id']}/execute", json=_confirm(p3)))
        _publish_pass(world)
    view = _ok(c.get(f"/api/publications/{p3['id']}"))
    by_lid = {o["logical_id"]: o for o in view["operations"]}
    assert by_lid[edited["logical_id"]]["status"] == "CONFLICT"
    assert drive.files[doc_id]["text"].startswith("Teks yang ditulis manusia")  # human content kept
    assert all(u["doc_id"] != doc_id for u in drive.batch_updates)  # no write was even attempted on it
    others = [o for o in updates[1:]]
    assert all(by_lid[o["logical_id"]]["status"] == "SUCCEEDED" for o in others)
    assert view["status"] in ("PARTIAL", "CONFLICT") and _plan(c, plan_id)["state"] == "PUBLICATION_PARTIAL"
    db.expire_all()
    revision_input = db.scalar(select(DocumentRevision).where(DocumentRevision.kind == "HUMAN_EDIT_INPUT",
                                                              DocumentRevision.logical_doc_id == edited["logical_id"]))
    assert revision_input is not None and revision_input.status == "OPEN"
    created = list(db.scalars(select(PublicationOperation.logical_id).where(
        PublicationOperation.operation.in_(("create_doc", "create_folder")),
        PublicationOperation.status == "SUCCEEDED")))
    assert created and len(created) == len(set(created))  # every document/folder was created exactly once
    assert all(len(drive.by_logical(lid)) == 1 for lid in created)

    # ---- §23/§50: an OFFICE plan is blocked before any bridge call (default deny)
    before = len(claude.calls())
    c.select_domain(Domain.OFFICE)
    office = _ok(c.post("/api/projects", json={"name": "Aplikasi Kantor", "domain": "OFFICE", "project_kind": "NEW",
                                               "idea": "Portal internal kantor"}))
    office = office.get("project", office)
    oplan = _ok(c.post(f"/api/projects/{office['id']}/plans", json={
        "title": "Rencana kantor", "prompt": "Rencanakan portal internal kantor dengan Jira dan Confluence."}), 201)
    blocked = c.post(f"/api/plans/{oplan['id']}/run")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "OFFICE_PLANNING_BLOCKED"
    runner.join_all(30)
    assert len(claude.calls()) == before
    assert _plan(c, oplan["id"])["state"] == "NEEDS_DECISION"
