"""§05 step 15 / §03 tail: operations results feed backlog, documentation and evaluation through the lawful
change process - an alert opens (one) incident, the incident files a CANDIDATE eval case for human review (the
eval side gets a draft case, not only a reference to an existing one), and the incident is documented in
Product 1 09-incidents (tests/unit/test_docs_engine_operations_evidence.py)."""

from __future__ import annotations

from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.models.evidence import EvidenceArtifact
from aeos.models.research import EvalCaseCandidate
from aeos.operations import alerts, incident
from tests.conftest import AuthedClient


def _alert(db, domain: str = "PERSONAL"):
    a, _new = alerts.raise_alert(db, dedup_key=f"sli:stale_leases:{domain}", kind="sli.stale_leases", severity="HIGH",
                                 title="Lease kedaluwarsa masih ACTIVE", domain=domain)
    db.commit()
    return a


def test_an_alert_opens_one_incident_which_files_a_candidate_eval_case(personal_client: AuthedClient, db):
    a = _alert(db)
    r = personal_client.post(f"/api/ops/alerts/{a.id}/incident", json={"owner": "Ops Lead"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] and body["source"] == "alert" and body["source_ref"] == a.id
    assert body["owner"] == "Ops Lead" and body["domain"] == "PERSONAL"
    again = personal_client.post(f"/api/ops/alerts/{a.id}/incident", json={}).json()
    assert again["id"] == body["id"] and again["created"] is False, "one incident per alert"

    r = personal_client.post(f"/api/ops/incidents/{body['id']}/eval-case",
                             json={"title": "Lease basi tidak dipakai ulang",
                                   "expected": "Scheduler menandai lease kedaluwarsa sebelum menjadwal ulang"})
    assert r.status_code == 200, r.text
    cand = db.get(EvalCaseCandidate, r.json()["candidate_id"])
    assert cand is not None and cand.status == "CANDIDATE" and cand.source_type == "alert" and cand.source_id == a.id
    view = personal_client.get(f"/api/ops/incidents/{body['id']}").json()
    assert any(f["kind"] == "eval_case_candidate" and f["id"] == cand.id for f in view["follow_ups"])
    assert any(e["kind"] == "follow_up" and cand.id in e["note"] for e in view["timeline"])


def test_another_domains_alert_is_not_found(personal_client: AuthedClient, db):
    office = _alert(db, "OFFICE")
    assert personal_client.post(f"/api/ops/alerts/{office.id}/incident", json={}).status_code == 404


def test_an_eval_case_needs_an_alert_or_evidence_of_the_incident(personal_client: AuthedClient, db):
    inc = incident.open_incident(db, title="Manual report", actor="op", domain="PERSONAL")
    db.commit()
    r = personal_client.post(f"/api/ops/incidents/{inc.id}/eval-case",
                             json={"title": "Kasus regresi", "expected": "Perilaku yang benar tercatat"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "EVAL_CASE_NO_SOURCE"
    ev = EvidenceArtifact(kind="LOG", origin="collector", collector_identity="c", trusted=True,
                          content_hash="sha256:l", domain="PERSONAL", claim="log menunjukkan kegagalan")
    db.add(ev)
    db.commit()
    incident.add_timeline(db, inc.id, actor="op", note="log ditautkan", evidence_ids=[ev.id])
    db.commit()
    r = personal_client.post(f"/api/ops/incidents/{inc.id}/eval-case",
                             json={"title": "Kasus regresi", "expected": "Perilaku yang benar tercatat"})
    assert r.status_code == 200, r.text
    cand = db.scalar(select(EvalCaseCandidate).where(EvalCaseCandidate.id == r.json()["candidate_id"]))
    assert cand.source_type == "evidence" and cand.source_id == ev.id


def test_viewer_cannot_open_incidents_from_alerts(client: AuthedClient, db):
    from tests.conftest import VIEWER_PW, create_viewer

    create_viewer(db)
    a = _alert(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.post(f"/api/ops/alerts/{a.id}/incident", json={}).status_code == 403
