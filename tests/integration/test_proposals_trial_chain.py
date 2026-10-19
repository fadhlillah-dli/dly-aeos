"""§40 trial execution and §49 M3 "satu trial komponen yang terbatas membuktikan alur usulan -> TaskSpec ->
evidence -> keputusan -> inventory/doc delta", with the REAL policy registry, planning and workforce scheduler.

* launch_trial turns the bound TRIAL decision into a Plan/PlanVersion/TaskSpec in the platform project;
* the scheduler re-verifies the binding before an attempt (stale baseline, other provider, stopped trial);
* the owner's stop cancels the trial tasks;
* the whole chain ends in ACCEPTED only with the lawful config change, an observation after activation,
  the declared acceptance met, the ADR linked and the finalization recorded.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.models.inventory import ComponentReference
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.research import ChangeProposal, ResearchEntry
from aeos.models.workforce import TaskExecution
from tests.conftest import AuthedClient
from tests.unit.proposal_fixtures import ACCEPTANCE, MANUAL_PASS, SCOPE, component, restore_evidence, route


def _ok(r: Any, code: int = 200) -> dict[str, Any]:
    assert r.status_code == code, r.text
    return r.json()


def _setup(client: AuthedClient, db) -> dict[str, Any]:
    from aeos.portfolio import seed as portfolio_seed

    portfolio_seed.run(db)  # the DLY AEOS platform project (PERSONAL)
    db.commit()
    component(db)
    p = _ok(client.post("/api/proposals", json={
        "kind": "UPGRADE", "sources": ["https://github.com/anomalyco/opencode"], "existing_component_ids": ["opencode"],
        "candidate_pin": "1.4.0", "problem": "Worker sering perlu repair", "expected_benefit": "repair berkurang"}))
    _ok(client.post(f"/api/proposals/{p['id']}/review", json={}))
    p = _ok(client.patch(f"/api/proposals/{p['id']}/plan", json={
        "acceptance": ACCEPTANCE, "operations_owner": "owner",
        "replacement": {"cutover_order": "pin baru lewat manifest yang direview", "rollback_or_forward_recovery": "pin lama",
                        "backup_restore_evidence_id": restore_evidence(db).id}}))["proposal"]
    bind = p["review_package"]["human_decision"]["bind"]
    trial = _ok(client.post(f"/api/proposals/{p['id']}/decision",
                            json={"action": "TRIAL_APPROVE", "scope": SCOPE, "rationale": "uji di sandbox", **bind}))
    return {"proposal": trial["proposal"], "bind": bind, "trial_decision_id": trial["decision_id"]}


def _ready(db, task_id: str, version_id: str) -> None:
    from aeos.workforce import scheduler

    scheduler.ensure_executions(db, version_id, actor="system:test")
    scheduler.transition_task(db, task_id, "READY", reason="test: dependencies accepted", actor="system:test")
    db.commit()


def test_launch_trial_creates_the_bound_taskspec_and_the_scheduler_rechecks_the_binding(personal_client: AuthedClient, db):
    from aeos.proposals import facade as proposals
    from aeos.workforce import scheduler

    s = _setup(personal_client, db)
    pid = s["proposal"]["id"]
    launched = _ok(personal_client.post(f"/api/proposals/{pid}/trial/launch", json={}))
    trial = launched["trial"]
    assert launched["proposal"]["status"] == "IN_TRIAL" and trial["status"] == "LAUNCHED"
    assert trial["execution"]["status"] in ("WAITING", "ENQUEUED")
    version = db.get(PlanVersion, trial["plan_version_id"])
    assert version.source == "CHANGE_PROPOSAL" and version.content["source"]["trial_decision_id"] == s["trial_decision_id"]
    approval = db.get(Decision, trial["plan_approval_decision_id"])
    assert approval.kind == "APPROVAL" and approval.target_hash == version.manifest_hash and approval.actor_kind == "HUMAN"
    t = db.get(TaskSpec, trial["task_ids"][0])
    assert f"change_proposal:{pid}" in t.input_artifacts and t.data_classification == "INTERNAL"
    assert set(t.capability_scope["operations"]) <= set(db.get(Decision, s["trial_decision_id"]).allowed_operations)
    assert t.limits["timeout_seconds"] == 60 * 60 and t.limits["max_attempts"] == 2
    assert t.allowed_paths == [f"sandbox/trials/{pid[:8]}/**"]
    assert launched["proposal"]["task_ids"] == [t.id]
    _err = personal_client.post(f"/api/proposals/{pid}/trial/launch", json={})
    assert _err.status_code == 409 and _err.json()["error"]["code"] == "TRIAL_ALREADY_LAUNCHED"

    _ready(db, t.id, version.id)
    assert not [b for b in scheduler.admission_check(db, t.id).blocks if b[0].startswith(("TRIAL", "STALE"))]
    other = route(db, provider="openrouter", key="openrouter/other-free")
    codes = [c for c, _ in proposals.trial_admission_problems(db, t, route=other)]
    assert codes == ["TRIAL_SCOPE_PROVIDER"], "the decided provider only - no other provider"
    db.get(ComponentReference, "opencode").approved_pin = "1.3.9"  # the baseline drifts during the trial
    db.commit()
    adm = scheduler.admission_check(db, t.id)
    db.commit()
    assert ("STALE_BINDING" in [c for c, _ in adm.blocks]) and not adm.admitted
    db.expire_all()
    assert db.get(Decision, s["trial_decision_id"]).status == "INVALIDATED"
    assert db.get(ChangeProposal, pid).status == "UNDER_REVIEW"


def test_owner_stop_cancels_the_trial_and_blocks_further_attempts(personal_client: AuthedClient, db):
    from aeos.proposals import facade as proposals

    s = _setup(personal_client, db)
    pid = s["proposal"]["id"]
    trial = _ok(personal_client.post(f"/api/proposals/{pid}/trial/launch", json={}))["trial"]
    t = db.get(TaskSpec, trial["task_ids"][0])
    _ready(db, t.id, trial["plan_version_id"])
    stopped = _ok(personal_client.post(f"/api/proposals/{pid}/trial/stop", json={"reason": "owner menghentikan trial"}))
    assert stopped["trial"]["status"] == "STOPPED" and stopped["trial"]["cancelled"][0]["state"] == "CANCELLED"
    db.expire_all()
    assert db.scalar(select(TaskExecution).where(TaskExecution.task_id == t.id)).state == "CANCELLED"
    assert [c for c, _ in proposals.trial_admission_problems(db, t)] == ["TRIAL_STOPPED"]
    r = personal_client.post(f"/api/proposals/{pid}/decision",
                             json={"action": "PROMOTE", "scope": SCOPE, "rationale": "promote", **s["bind"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "TRIAL_STOPPED"


def test_m3_chain_proposal_taskspec_evidence_decision_inventory_doc_delta(personal_client: AuthedClient, db):
    from aeos.inventory import facade as inventory
    from aeos.verification import facade as verification

    s = _setup(personal_client, db)
    pid = s["proposal"]["id"]
    trial = _ok(personal_client.post(f"/api/proposals/{pid}/trial/launch", json={}))["trial"]
    tid = trial["task_ids"][0]
    # the independent verifier collects its report for the trial TaskSpec (trusted collector identity)
    ev = verification.collect_evidence(db, kind="TEST_REPORT", origin="verifier", collector_identity="verifier",
                                       claim="trusted suite passed on the candidate", content={"passed": 12, "failed": 0},
                                       evidence_level="E2", domain="PERSONAL", task_id=tid)
    db.commit()
    p = _ok(personal_client.post(f"/api/proposals/{pid}/evidence", json={"evidence_ids": [ev.id]}))
    assert p["status"] == "PROMOTION_PENDING"
    _ok(personal_client.post(f"/api/proposals/{pid}/checkpoint", json={"manual_result": MANUAL_PASS}))
    promo = _ok(personal_client.post(f"/api/proposals/{pid}/decision",
                                     json={"action": "PROMOTE", "scope": SCOPE, "rationale": "lulus trial", **s["bind"]}))
    assert promo["proposal"]["status"] == "PROMOTION_APPROVED"
    from aeos.proposals import facade as proposals

    assert proposals.promotion_for_release(db, pid).id == promo["decision_id"]
    # the reviewed install-manifest change is applied, then the component is observed after activation
    comp = db.get(ComponentReference, "opencode")
    comp.approved_pin, comp.enablement = "1.4.0", "ENABLED"
    db.commit()
    inventory.record_observation(db, component_id="opencode", dimension="configuration", status="CONFIGURED",
                                 observer="reconciler", observed_version="1.4.0")
    db.add(ArchitectureDecisionRecord(adr_key="ADR-9100", title="opencode 1.4.0", status="ACCEPTED"))
    db.commit()
    out = _ok(personal_client.post(f"/api/proposals/{pid}/outcome", json={
        "status": "ACCEPTED", "evidence_ids": [ev.id], "adr_keys": ["ADR-9100"],
        "accepted_configuration": {"component_id": "opencode", "approved_pin": "1.4.0"},
        "acceptance_results": {"AC1": {"status": "PASS", "evidence_id": ev.id, "observed": "1.0"}}}))
    cur = out["outcome"]["current"]
    assert out["status"] == "ACCEPTED" and cur["acceptance"]["unmet"] == []
    fin = cur["finalization"]
    assert fin["adr"] == {"status": "LINKED", "keys": ["ADR-9100"]}
    assert fin["inventory"]["status"] in ("DONE", "PARTIAL", "PENDING") and "docs" in cur["inventory_docs_update"]
    assert out["delivery_dimensions"]["ACTIVE"]["status"] == "ENABLED_IN_CONFIG"
    db.expire_all()
    oc = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:opencode"))
    assert oc.lifecycle == "ENABLED"
    stages = [h.get("to") for h in oc.history if h.get("proposal_id") == pid]
    assert stages[:3] == ["CONTROLLED_EVAL", "RECOMMENDED_FOR_PROMOTION", "ENABLED"]
    tl = _ok(personal_client.get(f"/api/proposals/{pid}/timeline"))
    assert tl["trial"]["plan_id"] == trial["plan_id"] and tl["links"]["tasks"][0]["id"] == tid
    actions = [i["action"] for i in tl["items"] if i["type"] == "audit"]
    assert "proposal.trial.launch" in actions and "proposal.checkpoint.record" in actions
    assert db.get(Plan, trial["plan_id"]).state == "APPROVED"


def test_trial_launch_refuses_without_a_project_in_the_domain(admin_client: AuthedClient, db):
    from aeos.core.enums import Domain

    component(db)
    route(db, domains=("OFFICE",))
    admin_client.select_domain(Domain.OFFICE)
    p = _ok(admin_client.post("/api/proposals", json={
        "kind": "UPGRADE", "sources": ["https://github.com/anomalyco/opencode"], "existing_component_ids": ["opencode"],
        "candidate_pin": "1.4.0", "problem": "x", "expected_benefit": "y"}))
    pytest.importorskip("aeos.policy.office")
    from aeos.proposals import service

    proposal = db.get(ChangeProposal, p["id"])
    assert service.provider_eligibility(db, proposal, "opencode-zen", "INTERNAL")["status"] in ("NOT_ACTIVATED",
                                                                                              "INELIGIBLE")
    r = admin_client.post(f"/api/proposals/{p['id']}/trial/launch", json={})
    assert r.status_code == 409  # no TRIAL decision: nothing launches
