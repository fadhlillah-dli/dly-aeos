"""Upgrade Proposals API (§35, §39-§41; §50 "VIEWER memanggil write endpoint -> Ditolak backend",
"Dashboard Personal mencoba ... Office -> Backend menolak", "Baseline/candidate/target berubah
setelah approval -> Binding ditolak")."""

from __future__ import annotations

import pytest

from aeos.core.enums import Domain
from aeos.models.decisions import Decision
from aeos.models.inventory import ComponentReference
from aeos.research import service as research
from tests.conftest import VIEWER_PW, AuthedClient, create_admin, create_viewer
from tests.unit.proposal_fixtures import (
    ACCEPTANCE,
    MANUAL_PASS,
    SCOPE,
    evidence,
    restore_evidence,
    route,
    task,
)
from tests.unit.test_cost_fakes import install_fake_policy


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _comp(db):
    db.add(ComponentReference(component_id="opencode", kind="worker-engine", name="OpenCode", approved_pin="1.3.0",
                              owner="owner"))
    db.commit()
    research.seed_registry(db)  # github:anomalyco/opencode is stated by the master -> passes the identity gate
    db.commit()
    route(db)


def _new(c: AuthedClient, **kw):
    body = {"kind": "UPGRADE", "sources": ["https://github.com/anomalyco/opencode"], "problem": "stabilitas",
            "expected_benefit": "lebih sedikit repair", "existing_component_ids": ["opencode"],
            "candidate_pin": "1.4.0"} | kw
    r = c.post("/api/proposals", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _plan(c: AuthedClient, db, p):
    r = c.patch(f"/api/proposals/{p['id']}/plan", json={
        "acceptance": ACCEPTANCE, "operations_owner": "owner",
        "replacement": {"cutover_order": "pin baru lewat manifest", "rollback_or_forward_recovery": "pin lama",
                        "backup_restore_evidence_id": restore_evidence(db).id}})
    assert r.status_code == 200, r.text
    return r.json()["proposal"]


def test_full_flow_create_review_trial_promote_outcome_timeline(personal_client: AuthedClient, db, policy):
    from aeos.models.research import ChangeProposal

    _comp(db)
    p = _new(personal_client)
    assert p["status"] == "SUBMITTED" and p["guarantees"]["installs"] is False
    personal_client.post(f"/api/proposals/{p['id']}/review", json={})
    rv = _plan(personal_client, db, p)
    assert rv["status"] == "REVIEWED" and "TRIAL_APPROVE" in rv["allowed_actions"]
    assert rv["review_package"]["experiment_plan"]["acceptance_status"] == "DECLARED"
    bind = rv["review_package"]["human_decision"]["bind"]
    dec = personal_client.post(f"/api/proposals/{p['id']}/decision",
                               json={"action": "TRIAL_APPROVE", "scope": SCOPE, "rationale": "uji di sandbox", **bind})
    assert dec.status_code == 200, dec.text
    assert dec.json()["proposal"]["status"] == "TRIAL_APPROVED"
    t = task(db, db.get(ChangeProposal, p["id"]), link=False)
    linked = personal_client.post(f"/api/proposals/{p['id']}/links", json={"task_ids": [t.id]})
    assert linked.status_code == 200 and linked.json()["proposal"]["task_ids"] == [t.id]
    ev = evidence(db, task_spec=t, claim="suite pass")
    assert personal_client.post(f"/api/proposals/{p['id']}/evidence", json={"evidence_ids": [ev.id]}).status_code == 200
    blocked = personal_client.post(f"/api/proposals/{p['id']}/decision",
                                   json={"action": "PROMOTE", "scope": SCOPE, "rationale": "lulus trial", **bind})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "CHECKPOINT_REVIEW_REQUIRED"
    cp = personal_client.post(f"/api/proposals/{p['id']}/checkpoint", json={"manual_result": MANUAL_PASS})
    assert cp.status_code == 200 and cp.json()["checkpoint"]["verdict"] == "PASS"
    promo = personal_client.post(f"/api/proposals/{p['id']}/decision",
                                 json={"action": "PROMOTE", "scope": SCOPE, "rationale": "lulus trial", **bind})
    assert promo.status_code == 200, promo.text
    comp = db.get(ComponentReference, "opencode")  # the reviewed manifest change applies the pin
    comp.approved_pin, comp.enablement = "1.4.0", "ENABLED"
    db.commit()
    ev2 = evidence(db, task_spec=t, claim="post-activation probe")
    out = personal_client.post(f"/api/proposals/{p['id']}/outcome",
                               json={"status": "ACCEPTED", "evidence_ids": [ev2.id],
                                     "accepted_configuration": {"component_id": "opencode", "approved_pin": "1.4.0"},
                                     "acceptance_results": {"AC1": {"status": "PASS", "evidence_id": ev2.id}}})
    assert out.status_code == 200, out.text
    assert out.json()["status"] == "ACCEPTED"
    tl = personal_client.get(f"/api/proposals/{p['id']}/timeline").json()
    assert tl["before_after"]["before"]["components"][0]["approved_pin"] == "1.3.0"
    assert tl["before_after"]["after"]["accepted_configuration"]["approved_pin"] == "1.4.0"
    assert [i["details"]["kind"] for i in tl["items"] if i["type"] == "decision"] == ["TRIAL", "PROMOTION"]
    assert tl["links"]["tasks"][0]["id"] == t.id
    detail = personal_client.get(f"/api/proposals/{p['id']}").json()
    assert len(detail["decisions"]) == 2 and detail["outcome"]["current"]["accepted"] is True


def test_stale_binding_rejected_and_invalidation_persisted(personal_client: AuthedClient, db, policy):
    _comp(db)
    p = _new(personal_client)
    personal_client.post(f"/api/proposals/{p['id']}/review", json={})
    rv = _plan(personal_client, db, p)
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
    assert len(items) == 1 and items[0]["kind"] == "UPGRADE"
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


def test_links_are_validated_per_domain_and_suggestions_enter_review(personal_client: AuthedClient, db):
    from aeos.models.research import ChangeProposal

    _comp(db)
    meta = personal_client.get("/api/proposals/meta").json()
    assert "max_minutes" in meta["scope_required"] and meta["trial_environments"] == ["sandbox", "staging", "canary"]
    p = _new(personal_client)
    office_task = task(db, db.get(ChangeProposal, p["id"]), domain="OFFICE", link=False)
    r = personal_client.post(f"/api/proposals/{p['id']}/links", json={"task_ids": [office_task.id]})
    assert r.status_code == 422, "an OFFICE task is never linked to a PERSONAL proposal"
    r = personal_client.post(f"/api/proposals/{p['id']}/links", json={"adr_keys": ["ADR-9999"]})
    assert r.status_code == 422
    r = personal_client.post(f"/api/proposals/{p['id']}/links", json={})
    assert r.status_code == 422
    s = personal_client.post("/api/proposals/suggestions", json={
        "source_kind": "hermes", "source_id": "hermes-run-7", "kind": "COMPARE", "title": "Skill memori baru",
        "sources": ["https://github.com/NousResearch/hermes-agent"], "rationale": "saran skill"})
    assert s.status_code == 200, s.text
    body = s.json()
    assert body["status"] == "SUBMITTED" and body["submitted_by"] == "hermes:hermes-run-7" and body["decisions"] == []
    bad = personal_client.post("/api/proposals/suggestions", json={"source_kind": "worker", "source_id": "no-such-run"})
    assert bad.status_code == 422


def test_viewer_cannot_use_the_new_proposal_and_research_writes(client: AuthedClient, db):
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    for method, path, body in (("patch", "/api/proposals/x/plan", {"operations_owner": "x"}),
                               ("post", "/api/proposals/x/links", {"task_ids": ["t"]}),
                               ("post", "/api/proposals/x/trial/launch", {}),
                               ("post", "/api/proposals/x/checkpoint", {}),
                               ("patch", "/api/research/x", {"maintainer": "x"}),
                               ("post", "/api/research/x/confirm-source", None)):
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE", (path, r.text)
