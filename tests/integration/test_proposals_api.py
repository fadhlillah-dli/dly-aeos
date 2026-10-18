"""Upgrade Proposals API (§35, §39-§41; §50 "VIEWER memanggil write endpoint -> Ditolak backend",
"Dashboard Personal mencoba ... Office -> Backend menolak", "Baseline/candidate/target berubah
setelah approval -> Binding ditolak")."""

from __future__ import annotations

import pytest

from aeos.core.enums import Domain
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.inventory import ComponentReference
from tests.conftest import VIEWER_PW, AuthedClient, create_admin, create_viewer
from tests.unit.test_cost_fakes import install_fake_policy

SCOPE = {"target": "platform:dly-aeos", "environment": "sandbox", "provider": "opencode-zen",
         "data_class": "INTERNAL", "budget_usd": 0}


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _comp(db):
    db.add(ComponentReference(component_id="opencode", kind="worker-engine", name="OpenCode", approved_pin="1.3.0"))
    db.commit()


def _new(c: AuthedClient, **kw):
    body = {"kind": "REPLACE", "sources": ["https://github.com/anomalyco/opencode"], "problem": "stabilitas",
            "existing_component_ids": ["opencode"], "candidate_pin": "1.4.0"} | kw
    r = c.post("/api/proposals", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow_create_review_trial_promote_outcome_timeline(personal_client: AuthedClient, db, policy):
    _comp(db)
    p = _new(personal_client)
    assert p["status"] == "SUBMITTED" and p["guarantees"]["installs"] is False
    rv = personal_client.post(f"/api/proposals/{p['id']}/review", json={}).json()
    assert rv["status"] == "REVIEWED" and "TRIAL_APPROVE" in rv["allowed_actions"]
    bind = rv["review_package"]["human_decision"]["bind"]
    dec = personal_client.post(f"/api/proposals/{p['id']}/decision",
                               json={"action": "TRIAL_APPROVE", "scope": SCOPE, "rationale": "uji di sandbox", **bind})
    assert dec.status_code == 200, dec.text
    assert dec.json()["proposal"]["status"] == "TRIAL_APPROVED"
    ev = EvidenceArtifact(kind="TEST_REPORT", origin="verifier", collector_identity="verifier", trusted=True,
                          content_hash="sha256:" + "b" * 64, domain="PERSONAL", claim="suite pass")
    db.add(ev)
    db.commit()
    assert personal_client.post(f"/api/proposals/{p['id']}/evidence", json={"evidence_ids": [ev.id]}).status_code == 200
    promo = personal_client.post(f"/api/proposals/{p['id']}/decision",
                                 json={"action": "PROMOTE", "scope": SCOPE, "rationale": "lulus trial", **bind})
    assert promo.status_code == 200, promo.text
    out = personal_client.post(f"/api/proposals/{p['id']}/outcome",
                               json={"status": "ACCEPTED", "evidence_ids": [ev.id],
                                     "accepted_configuration": {"component_id": "opencode", "approved_pin": "1.4.0"}})
    assert out.status_code == 200 and out.json()["status"] == "ACCEPTED"
    tl = personal_client.get(f"/api/proposals/{p['id']}/timeline").json()
    assert tl["before_after"]["before"]["components"][0]["approved_pin"] == "1.3.0"
    assert tl["before_after"]["after"]["accepted_configuration"]["approved_pin"] == "1.4.0"
    assert [i["details"]["kind"] for i in tl["items"] if i["type"] == "decision"] == ["TRIAL", "PROMOTION"]
    detail = personal_client.get(f"/api/proposals/{p['id']}").json()
    assert len(detail["decisions"]) == 2 and detail["outcome"]["current"]["accepted"] is True


def test_stale_binding_rejected_and_invalidation_persisted(personal_client: AuthedClient, db, policy):
    _comp(db)
    p = _new(personal_client)
    rv = personal_client.post(f"/api/proposals/{p['id']}/review", json={}).json()
    bind = rv["review_package"]["human_decision"]["bind"]
    personal_client.post(f"/api/proposals/{p['id']}/decision",
                         json={"action": "TRIAL_APPROVE", "scope": SCOPE, "rationale": "trial", **bind})
    comp = db.get(ComponentReference, "opencode")
    comp.approved_pin = "1.3.9"
    db.commit()
    r = personal_client.post(f"/api/proposals/{p['id']}/decision",
                             json={"action": "PROMOTE", "scope": SCOPE, "rationale": "promote", **bind})
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_BINDING"
    db.expire_all()
    trial = db.query(Decision).filter(Decision.kind == "TRIAL").one()
    assert trial.status == "INVALIDATED"
    assert personal_client.get(f"/api/proposals/{p['id']}").json()["status"] == "UNDER_REVIEW"


def test_compare_trial_denied(personal_client: AuthedClient, db, policy):
    p = _new(personal_client, kind="COMPARE", existing_component_ids=[], candidate_pin=None)
    rv = personal_client.post(f"/api/proposals/{p['id']}/review", json={}).json()
    assert "TRIAL_APPROVE" not in rv["allowed_actions"]
    bind = rv["review_package"]["human_decision"]["bind"]
    r = personal_client.post(f"/api/proposals/{p['id']}/decision",
                             json={"action": "TRIAL_APPROVE", "scope": SCOPE, "rationale": "try", **bind})
    assert r.status_code == 403 and r.json()["error"]["code"] == "COMPARE_NO_INSTALL"


def test_list_filter_by_component_and_meta(personal_client: AuthedClient, db):
    _comp(db)
    _new(personal_client)
    _new(personal_client, kind="ADD", sources=["https://github.com/oraios/serena"], existing_component_ids=[])
    items = personal_client.get("/api/proposals", params={"component": "opencode"}).json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "REPLACE"
    meta = personal_client.get("/api/proposals/meta").json()
    assert len(meta["kinds"]) == 5 and "TRIAL_APPROVE" in meta["actions"] and meta["platform_project"] is None


def test_cross_domain_proposal_is_404(admin_client: AuthedClient, db):
    admin_client.select_domain(Domain.OFFICE)
    p = _new(admin_client, kind="ADD", existing_component_ids=[], candidate_pin=None)
    admin_client.select_domain(Domain.PERSONAL)
    assert admin_client.get(f"/api/proposals/{p['id']}").status_code == 404
    assert admin_client.get("/api/proposals").json()["items"] == []


def test_viewer_cannot_submit_or_decide(client: AuthedClient, db):
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/proposals", json={"kind": "ADD", "sources": ["https://github.com/a/b"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"
    assert client.get("/api/proposals").status_code == 200


def test_validation_errors(personal_client: AuthedClient):
    assert personal_client.post("/api/proposals", json={"kind": "INSTALL", "sources": ["x"]}).status_code == 422
    assert personal_client.post("/api/proposals", json={"kind": "ADD", "sources": []}).status_code == 422
    r = personal_client.post("/api/proposals", json={"kind": "ADD", "sources": ["https://u:p@github.com/a/b"]})
    assert r.status_code == 422
