"""Retirement workflow (master §10 retirement workflow, §33 retirement, §16 Retirement Assistant).

The checklist maps consumers, jobs, credentials, repository/integration/infra closure, data retention,
legal hold and backups; the owner's decision is bound to the checklist hash; RETIRED needs it; a legal
hold blocks disposal; nothing is ever deleted.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.decisions import Decision
from aeos.models.portfolio import Project, ProjectIntegration, RepositoryReference
from aeos.models.publishing import ExternalReference, Publication
from tests.conftest import VIEWER_PW, create_viewer


def _project(db, key="alpha", lifecycle="RETIRING") -> Project:
    p = Project(project_key=key, name=key.title(), domain="PERSONAL", lifecycle=lifecycle)
    db.add(p)
    db.flush()
    return p


def _items(client, pid):
    body = client.get(f"/api/ops/retirement/{pid}").json()
    return body, {i["key"]: i for i in body["items"]}


def test_checklist_maps_consumers_repositories_integrations_infra_and_jobs(personal_client, db, settings):
    p = _project(db)
    db.add(RepositoryReference(project_id=p.id, provider="GITHUB", full_name="o/alpha"))
    integ = ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="github-actions", purpose="ci")
    db.add(integ)
    db.add(ExternalReference(project_id=p.id, domain="PERSONAL", provider="google-docs", logical_id="doc:prd",
                             external_id="x1"))
    db.add(Publication(project_id=p.id, domain="PERSONAL", plan_version_id="pv", approved_hash="h", decision_id="d",
                       status="PENDING", targets=[{"provider": "github-issues"}]))
    db.add(BrokerSideEffect(run_id="r1", project_id=p.id, domain="PERSONAL", kind="PUSH_BRANCH",
                            target={"repo": "o/alpha", "branch": "aeos/t1"}, idempotency_key="k", status="SUCCEEDED"))
    db.flush()
    db.add(Decision(kind="WRITE_ENABLEMENT", actor_user_id="u", target_type="integration", target_id=integ.id,
                    status="ACTIVE", allowed_operations=[], auth_scope={}, details={}))
    ws = settings.layout.task_data(Domain.PERSONAL, "alpha") / "run-1"
    ws.mkdir(parents=True)
    db.commit()
    body, items = _items(personal_client, p.id)
    consumers = items["consumers"]["detail"]
    assert consumers["external_targets"] == ["google-docs:doc:prd"]
    assert consumers["publication_targets"] == ["github-issues"]
    assert items["jobs"]["status"] == "BLOCKING" and items["jobs"]["detail"]["pending_publications"] == 1
    assert items["repositories"]["status"] == "OWNER_ACTION"
    assert items["repositories"]["detail"]["aeos_branches"] == [{"repo": "o/alpha", "branch": "aeos/t1"}]
    assert items["integrations"]["status"] == "BLOCKING"  # an ACTIVE WRITE_ENABLEMENT still targets it
    assert items["infra"]["status"] == "OWNER_ACTION"
    assert items["infra"]["detail"] == {"worker_workspaces": 1, "ci_or_staging_integrations": ["github-actions:ci"]}
    assert set(body["blocking"]) == {"jobs", "integrations"}
    assert ws.is_dir()  # read-only: nothing was removed


def test_backup_closure_counts_backups_with_project_files(personal_client, db, settings):
    from aeos.operations import backup

    p = _project(db, key="beta")
    db.commit()
    f = settings.layout.attachments(Domain.PERSONAL, "beta") / "spec.md"
    f.parent.mkdir(parents=True)
    f.write_text("# spec\n", encoding="utf-8")
    backup.create_backup(settings, label="pre-retire", session=db)
    db.commit()
    _, items = _items(personal_client, p.id)
    assert items["backups"]["status"] == "OWNER_DECISION_REQUIRED"
    assert items["backups"]["detail"]["backups_containing_project_files"] == 1


def test_legal_hold_is_recorded_cleared_audited_and_blocks_disposal(personal_client, client, db, settings):
    from aeos.operations import backup

    p = _project(db, key="gamma")
    db.commit()
    f = settings.layout.attachments(Domain.PERSONAL, "gamma") / "a.txt"
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")
    backup.create_backup(settings, label="b", session=db)
    db.commit()
    _, items = _items(personal_client, p.id)
    assert items["legal_hold"]["status"] == "UNKNOWN"
    r = personal_client.put(f"/api/ops/retirement/{p.id}/legal-hold", json={"active": True, "reason": "litigation hold"})
    assert r.status_code == 200 and r.json()["legal_hold"]["active"] is True
    _, items = _items(personal_client, p.id)
    assert items["legal_hold"]["status"] == "RECORDED"
    assert items["data"]["status"] == "BLOCKING" and items["backups"]["status"] == "BLOCKING"
    assert personal_client.put(f"/api/ops/retirement/{p.id}/legal-hold",
                               json={"active": False, "reason": "hold released"}).status_code == 200
    _, items = _items(personal_client, p.id)
    assert items["data"]["status"] == "OWNER_DECISION_REQUIRED" and items["legal_hold"]["status"] == "RECORDED"
    actions = [a.details["active"] for a in db.scalars(select(AuditEvent).where(
        AuditEvent.action == "ops.retirement.legal_hold").order_by(AuditEvent.seq))]
    assert actions == [True, False]
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.put(f"/api/ops/retirement/{p.id}/legal-hold",
                      json={"active": True, "reason": "viewer try"}).status_code == 403


def test_data_item_shows_the_retention_rule_and_never_claims_expiry(personal_client, db):
    from aeos.operations import retention

    p = _project(db, key="delta")
    db.commit()
    _, items = _items(personal_client, p.id)
    rules = items["data"]["detail"]["rules"]
    assert rules["evidence"]["rule"] == "RETAIN" and rules["evidence"]["retain_days"] is None
    policy = retention.load_policy()
    assert policy["status"] == "OWNER_DECISION_REQUIRED"
    rule = retention.resolve(policy, "evidence", "PERSONAL")
    assert retention.is_expired(rule, utcnow() - timedelta(days=10_000)) is False  # no rule -> never expired
    decided = {"rules": {"evidence": {"PERSONAL": 30}}}
    r30 = retention.resolve(decided, "evidence", "PERSONAL")
    assert retention.is_expired(r30, utcnow() - timedelta(days=31)) is True
    assert retention.is_expired(r30, utcnow() - timedelta(days=31), legal_hold=True) is False
    api = personal_client.get("/api/ops/retention").json()
    assert {i["data_class"] for i in api["items"]} == set(retention.DATA_CLASSES) and api["checked_at"]


def test_retired_needs_owner_decision_bound_to_the_current_checklist(personal_client, db):
    p = _project(db, key="epsilon")
    pub = Publication(project_id=p.id, domain="PERSONAL", plan_version_id="pv", approved_hash="h", decision_id="d",
                      status="PENDING")
    db.add(pub)
    db.commit()
    r = personal_client.patch(f"/api/projects/{p.id}", json={"lifecycle": "RETIRED"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_NOT_VERIFIED"
    body, _ = _items(personal_client, p.id)
    r = personal_client.post(f"/api/ops/retirement/{p.id}/decision",
                             json={"checklist_hash": body["checklist_hash"], "rationale": "owner reviewed it"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_BLOCKING_ITEMS"
    pub.status = "SUCCEEDED"
    db.commit()
    r = personal_client.post(f"/api/ops/retirement/{p.id}/decision",
                             json={"checklist_hash": body["checklist_hash"], "rationale": "owner reviewed it"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_CHECKLIST_STALE"
    body, _ = _items(personal_client, p.id)
    assert personal_client.put(f"/api/ops/retirement/{p.id}/steps/communication",
                               json={"status": "DONE", "note": "stakeholders told"}).status_code == 200
    r = personal_client.post(f"/api/ops/retirement/{p.id}/decision",
                             json={"checklist_hash": body["checklist_hash"], "rationale": "owner reviewed it"})
    assert r.status_code == 201, r.text  # recording a step does not change the reviewed checklist
    body, items = _items(personal_client, p.id)
    assert items["final_verification"]["status"] == "VERIFIED"
    assert items["communication"]["recorded"]["status"] == "DONE"
    # a checklist change after the decision invalidates it
    assert personal_client.put(f"/api/ops/retirement/{p.id}/legal-hold",
                               json={"active": False, "reason": "no hold exists"}).status_code == 200
    _, items = _items(personal_client, p.id)
    assert items["final_verification"]["status"] == "STALE"
    r = personal_client.patch(f"/api/projects/{p.id}", json={"lifecycle": "RETIRED"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_NOT_VERIFIED"
    body, _ = _items(personal_client, p.id)
    assert personal_client.post(f"/api/ops/retirement/{p.id}/decision",
                                json={"checklist_hash": body["checklist_hash"],
                                      "rationale": "owner reviewed it again"}).status_code == 201
    r = personal_client.patch(f"/api/projects/{p.id}", json={"lifecycle": "RETIRED"})
    assert r.status_code == 200 and r.json()["project"]["lifecycle"] == "RETIRED"
    # one decision mechanism (aeos.policy.retirement): a RETIREMENT decision on project/<id> bound to the hash
    d = db.scalar(select(Decision).where(Decision.target_type == "project", Decision.target_id == p.id,
                                         Decision.target_hash == body["checklist_hash"]))
    assert d is not None and d.kind == "RETIREMENT" and d.allowed_operations == ["retire_project"]
    assert d.status == "ACTIVE"  # the earlier, now stale verification was superseded by this one
    body, _ = _items(personal_client, p.id)
    assert body["workflow"] == {"status": "RETIRED", "decision_id": d.id}


def test_a_step_cannot_cite_the_other_domains_evidence(personal_client, db):
    """Domain-first: a PERSONAL project's retirement step only accepts PERSONAL or platform evidence; the OFFICE
    domain's evidence id is refused like an unknown one (review finding)."""
    from aeos.models.evidence import EvidenceArtifact

    p = _project(db, key="zeta")
    ev = {d: EvidenceArtifact(kind="LOG", origin="collector", collector_identity="c", trusted=True, claim=d,
                              content_hash="sha256:" + d.lower(), evidence_level="E2", domain=d)
          for d in ("PERSONAL", "OFFICE")}
    db.add_all(ev.values())
    db.commit()
    r = personal_client.put(f"/api/ops/retirement/{p.id}/steps/data",
                            json={"status": "DONE", "evidence_ids": [ev["OFFICE"].id]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"
    r = personal_client.put(f"/api/ops/retirement/{p.id}/steps/data",
                            json={"status": "DONE", "evidence_ids": [ev["PERSONAL"].id]})
    assert r.status_code == 200 and r.json()["recorded"]["evidence_ids"] == [ev["PERSONAL"].id]


def test_cli_records_a_step_and_show_prints_it(personal_client, db, settings):
    """W6-197: the owner records the communication (and data) step from the local CLI; it is audited, shows in
    the checklist ('recorded') and in 'aeos ops retirement show'. Evidence of the other domain is refused."""
    from typer.testing import CliRunner

    from aeos.models.evidence import EvidenceArtifact
    from aeos.operations import cli as ops_cli

    p = _project(db, key="gamma")
    office_ev = EvidenceArtifact(kind="NOTE", origin="manual", collector_identity="owner", trusted=False,
                                 content_hash="sha256:" + "d" * 64, domain="OFFICE", claim="office note")
    db.add(office_ev)
    db.commit()
    runner = CliRunner()
    r = runner.invoke(ops_cli.app, ["retirement", "step", "gamma", "communication", "--status", "done",
                                    "--note", "stakeholders and integration owners informed"])
    assert r.exit_code == 0, r.output
    assert "Step communication of gamma: DONE" in r.output and r.output.isascii()
    db.expire_all()
    _, items = _items(personal_client, p.id)
    rec = items["communication"]["recorded"]
    assert rec["status"] == "DONE" and rec["note"] == "stakeholders and integration owners informed"
    assert rec["by"].startswith("cli:")
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.retirement.step",
                                               AuditEvent.target_id == p.id))
    assert audit is not None and audit.actor_type == "cli" and audit.details["step"] == "communication"
    shown = runner.invoke(ops_cli.app, ["retirement", "show", "gamma"])
    assert shown.exit_code == 0 and "step DONE by" in shown.output
    assert "note: stakeholders and integration owners informed" in shown.output
    # fail closed: unknown step, bad status, other-domain evidence
    bad = runner.invoke(ops_cli.app, ["retirement", "step", "gamma", "final_verification", "--status", "DONE"])
    assert bad.exit_code == 1 and "unknown retirement step" in bad.output
    bad = runner.invoke(ops_cli.app, ["retirement", "step", "gamma", "data", "--status", "FINISHED"])
    assert bad.exit_code == 1 and "status must be one of" in bad.output
    bad = runner.invoke(ops_cli.app, ["retirement", "step", "gamma", "data", "--status", "DONE",
                                      "--evidence", office_ev.id])
    assert bad.exit_code == 1 and "EVIDENCE_NOT_FOUND" in bad.output
