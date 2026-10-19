"""Incident records (master §28 incident documentation, §33 incident response, §16 Incident Analyst,
§05 step 15 operations feed the lawful change process).

* An emergency stop opens an incident with an owner that is never empty and a blast radius derived from
  records (projects, runs, side-effect targets, revoked capability kinds, components), domain-scoped.
* No root cause without trusted supporting evidence; RESOLVED needs root cause + corrective action +
  an existing regression test or eval case.
* A follow-up files a draft ChangeProposal for human review; the report renders INCIDENT_REPORT/POST_MORTEM.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from aeos.core.enums import Domain
from aeos.core.errors import NotFound, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.evidence import EvidenceArtifact
from aeos.models.operations_ext import OpsIncident
from aeos.models.research import ChangeProposal
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun
from aeos.operations import emergency, incident
from tests.conftest import VIEWER_PW, create_viewer
from tests.unit.test_operations_fakes import fake_broker, fake_verification, fake_workforce

THIS_TEST = "tests/unit/test_operations_incident.py::test_resolve_requires_evidenced_root_cause_action_and_regression"


def _world(db):
    runs = [WorkerRun(project_id="p1", domain="PERSONAL", execution_status="RUNNING", engine="opencode",
                      model_route_key="worker-route-a"),
            WorkerRun(project_id="p9", domain="OFFICE", execution_status="RUNNING", engine="opencode")]
    db.add_all(runs)
    db.flush()
    db.add(CapabilityGrant(token_hash="g1", run_id=runs[0].id, project_id="p1", domain="PERSONAL", kind="GIT_BRANCH",
                           expires_at=utcnow() + timedelta(hours=1)))
    db.add(CapabilityGrant(token_hash="g2", run_id=runs[1].id, project_id="p9", domain="OFFICE", kind="MODEL_PROXY",
                           expires_at=utcnow() + timedelta(hours=1)))
    db.add(BrokerSideEffect(run_id=runs[0].id, project_id="p1", domain="PERSONAL", kind="PUSH_BRANCH",
                            target={"repo": "o/r", "branch": "aeos/t1"}, idempotency_key="k1", status="SUCCEEDED"))
    db.commit()
    return runs


@pytest.fixture
def stopped(db, monkeypatch):
    fake_workforce(monkeypatch)
    fake_broker(monkeypatch)
    evidence = fake_verification(monkeypatch)
    runs = _world(db)
    report = emergency.execute(db, actor_type="user", actor_id="owner-1", reason="runaway worker pushes branches",
                               visible_domain="PERSONAL")
    db.commit()
    return {"report": report, "runs": runs, "evidence": evidence}


def test_emergency_stop_opens_incident_with_owner_and_derived_blast_radius(db, stopped):
    report, runs = stopped["report"], stopped["runs"]
    assert report["incident_owner"] == "user:owner-1"  # never empty: the human who ran the stop
    inc = db.get(OpsIncident, report["incident_record_id"])
    assert inc is not None and inc.status == "CONTAINED" and inc.owner == "user:owner-1"
    assert inc.source == "emergency_stop" and inc.source_ref == report["incident_id"]
    personal = inc.derived_blast_radius["PERSONAL"]
    assert personal["projects"] == ["p1"] and personal["runs"] == [runs[0].id]
    assert personal["revoked_capability_kinds"] == ["GIT_BRANCH"]
    assert personal["side_effect_targets"][0]["target"] == {"repo": "o/r", "branch": "aeos/t1"}
    assert personal["components"] == ["opencode", "worker-route-a"]
    assert inc.derived_blast_radius["OFFICE"]["runs"] == [runs[1].id]
    # the Personal view of the report and of the incident never carries Office details
    assert report["derived_blast_radius"]["runs"] == [runs[0].id]
    assert runs[1].id not in json.dumps(report)
    view = incident.get_incident(db, inc.id, domain="PERSONAL")
    assert set(view["derived_blast_radius"]) == {"PERSONAL"} and runs[1].id not in json.dumps(view)
    # each domain's containment evidence holds that domain's derived radius
    by_domain = {e.domain: e for e in stopped["evidence"]}
    assert by_domain["PERSONAL"].inline["derived_blast_radius"]["runs"] == [runs[0].id]
    assert sorted(inc.evidence_ids) == sorted(e.id for e in stopped["evidence"])


def test_root_cause_needs_trusted_supporting_evidence(db, stopped):
    inc_id = stopped["report"]["incident_record_id"]
    with pytest.raises(ValidationFailed) as exc:
        incident.record_analysis(db, inc_id, actor="owner-1", root_cause="lease heartbeat bug")
    assert exc.value.code == "ROOT_CAUSE_UNSUPPORTED"
    untrusted = EvidenceArtifact(kind="LOG", origin="worker", collector_identity="w", trusted=False,
                                 claim="worker said so", content_hash="sha256:x", evidence_level="E0")
    db.add(untrusted)
    db.flush()
    with pytest.raises(ValidationFailed) as exc:
        incident.record_analysis(db, inc_id, actor="owner-1", root_cause="lease heartbeat bug",
                                 root_cause_evidence_refs=[untrusted.id])
    assert exc.value.code == "ROOT_CAUSE_UNSUPPORTED"
    with pytest.raises(ValidationFailed):
        incident.record_analysis(db, inc_id, actor="owner-1", root_cause="x", root_cause_evidence_refs=["nope"])
    trusted = stopped["evidence"][0].id
    inc = incident.record_analysis(db, inc_id, actor="owner-1", impact="two runs pushed stale branches",
                                   hypotheses=["lease expiry race"], root_cause="lease heartbeat bug",
                                   root_cause_evidence_refs=[trusted])
    assert inc.root_cause == "lease heartbeat bug" and inc.root_cause_evidence_refs == [trusted]
    assert [e["kind"] for e in inc.timeline][-1] == "analysis"


def test_resolve_requires_evidenced_root_cause_action_and_regression(db, stopped):
    inc_id = stopped["report"]["incident_record_id"]
    with pytest.raises(ValidationFailed) as exc:
        incident.resolve(db, inc_id, actor="owner-1")
    assert exc.value.code == "INCIDENT_INCOMPLETE"
    assert set(exc.value.details["missing"]) == {"root_cause_with_evidence", "corrective_action",
                                                 "regression_test_or_eval_case"}
    incident.record_analysis(db, inc_id, actor="owner-1", root_cause="lease heartbeat bug",
                             root_cause_evidence_refs=[stopped["evidence"][0].id])
    incident.add_corrective_action(db, inc_id, actor="owner-1", description="renew leases before expiry", ref="T-1")
    with pytest.raises(ValidationFailed) as exc:
        incident.link_regression(db, inc_id, actor="owner-1", test_ids=["tests/unit/test_nope.py::test_missing"])
    assert exc.value.code == "REGRESSION_TEST_NOT_FOUND"
    with pytest.raises(ValidationFailed):
        incident.link_regression(db, inc_id, actor="owner-1", test_ids=["../etc/passwd::x"])
    with pytest.raises(ValidationFailed) as exc:
        incident.link_regression(db, inc_id, actor="owner-1", eval_case_refs=["no-suite#c1"])
    assert exc.value.code == "EVAL_CASE_NOT_FOUND"
    incident.link_regression(db, inc_id, actor="owner-1", test_ids=[THIS_TEST])
    inc = incident.resolve(db, inc_id, actor="owner-1")
    assert inc.status == "RESOLVED" and inc.resolved_by == "owner-1" and inc.regression_test_ids == [THIS_TEST]
    view = incident.get_incident(db, inc_id, domain="PERSONAL")
    md = incident.render_report(view, kind="POST_MORTEM")
    for needle in ("# Post-mortem", "## Timeline", "lease heartbeat bug", "renew leases before expiry", THIS_TEST,
                   "Diturunkan dari catatan (PERSONAL)"):
        assert needle in md, needle
    assert "OFFICE" not in md


def test_follow_up_files_a_draft_change_proposal_for_review(db, stopped):
    from tests.conftest import create_admin

    owner = create_admin(db)
    inc_id = stopped["report"]["incident_record_id"]
    result = incident.open_follow_up(db, inc_id, actor_user_id=owner.id, domain="PERSONAL",
                                     component_ids=["opencode"])
    proposal = db.get(ChangeProposal, result["proposal_id"])
    assert proposal is not None and proposal.kind == "UPGRADE" and inc_id in proposal.problem
    assert proposal.status not in ("TRIAL_APPROVED", "PROMOTION_APPROVED")  # a human review decides
    inc = db.get(OpsIncident, inc_id)
    assert inc.follow_ups[0] == {**inc.follow_ups[0], "kind": "change_proposal", "id": proposal.id}


def test_incident_api_is_scoped_and_admin_only(client, db, monkeypatch):
    from tests.conftest import ADMIN_PW, create_admin

    create_admin(db)
    create_viewer(db)
    office = incident.open_incident(db, title="Office outage", actor="op", domain="OFFICE")
    platform = incident.open_incident(db, title="Platform-wide stop", actor="op")
    db.commit()
    client.login("owner", ADMIN_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/ops/incidents", json={"title": "Sync conflicts", "note": "opened from an alert"})
    assert r.status_code == 200 and r.json()["owner"]
    personal_id = r.json()["id"]
    ids = {i["id"] for i in client.get("/api/ops/incidents").json()["items"]}
    assert {personal_id, platform.id} <= ids and office.id not in ids
    assert client.get(f"/api/ops/incidents/{office.id}").status_code == 404
    assert client.post(f"/api/ops/incidents/{office.id}/timeline", json={"note": "peek"}).status_code == 404
    rep = client.get(f"/api/ops/incidents/{personal_id}/report").json()
    assert rep["kind"] == "INCIDENT_REPORT" and "# Laporan insiden: Sync conflicts" in rep["markdown"]
    r = client.post(f"/api/ops/incidents/{personal_id}/resolve")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INCIDENT_INCOMPLETE"
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.post("/api/ops/incidents", json={"title": "nope"}).status_code == 403
    assert client.get(f"/api/ops/incidents/{personal_id}").status_code == 200


def test_get_unknown_incident_is_404(db):
    with pytest.raises(NotFound):
        incident.get_incident(db, "missing", domain="PERSONAL")
