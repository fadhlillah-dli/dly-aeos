"""Adversarial review of the C2 (proposals/research) gap closure: regressions and holes found after the fix.

* A finished (ACCEPTED / ROLLED_BACK) proposal must not block every later proposal for the same source identity
  (the next UPGRADE of a component is a new change, §39 duplicates rule applies to OPEN proposals).
* ACCEPTED without an explicit pin must still prove the PROMOTED candidate pin is applied (§47 ENABLED only after
  a lawful configuration change) - an already-enabled component at its old pin is not the accepted upgrade.
* An accepted REMOVE retires the component: its registry entry never becomes ENABLED (§47), and a failed removal
  restores what it changed.
* REPLACE names when the old version may retire before PROMOTE (§39 REPLACE).
* A trial TaskSpec of another proposal cannot be linked (its evidence is about another candidate, §40).
* A ROLLED_BACK outcome needs something that was trialled/promoted, and the rollback after it (§40).
* Controller releases are platform (PERSONAL) records: an OFFICE proposal never gates one (§23).
* A shared registry row is never superseded by a domain row; goal-duplicate suggestions stay in the project scope.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.audit import service as audit
from aeos.core.errors import Blocked, Conflict, ValidationFailed
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.models.inventory import ComponentReference
from aeos.models.research import ChangeProposal, ResearchEntry
from aeos.proposals import links, plan, service
from aeos.proposals import review as R
from aeos.research import service as research
from tests.unit.proposal_fixtures import (
    ACCEPTANCE,
    SCOPE,
    apply_config,
    checkpoint_pass,
    component,
    create,
    decide,
    evidence,
    owner_plan,
    project,
    promoted,
    review,
    task,
    trialled,
)
from tests.unit.test_cost_fakes import install_fake_policy


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _accept(db, p, t, pin: str = "1.4.0"):
    ev = evidence(db, task_spec=t)
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                           accepted_configuration={"component_id": "opencode", "approved_pin": pin},
                           acceptance_results={"AC1": {"status": "PASS", "evidence_id": ev.id}})
    db.commit()
    return ev


def _entry(db, key: str = "tool:opencode") -> ResearchEntry:
    return db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == key))


# ---------------------------------------------------------------- §39 duplicates: finished changes are history


def test_a_new_proposal_for_an_accepted_then_rolled_back_identity_can_be_decided(db, policy):
    a, t, _ev = promoted(db)
    apply_config(db)
    _accept(db, a, t)
    db.get(ComponentReference, "opencode").approved_pin = "1.3.0"  # the reviewed manifest restores the old pin
    db.commit()
    reload = audit.record(db, actor_type="cli", actor_id="owner", action="inventory.manifest.load",
                          target_type="install_manifest", target_id="m-rollback")
    db.commit()
    service.record_outcome(db, a, actor_user_id="owner", status="ROLLED_BACK",
                           rollback={"reference": {"audit_event_id": reload.event_id}, "data_rollback": "NOT_POSSIBLE"})
    db.commit()
    rollback = [d for d in db.scalars(select(Decision).where(Decision.target_id == a.id)) if d.kind == "ROLLBACK"]
    assert len(rollback) == 1 and rollback[0].status == "ACTIVE", "the rollback is an ACTIVE Decision record"
    # the next attempt at the same source is a new proposal, linked as a duplicate, and it can be decided
    b = create(db, kind="UPGRADE", candidate_pin="1.5.0")
    assert b.duplicate_of == a.id
    owner_plan(db, b)
    review(db, b)
    d = decide(db, b, "TRIAL_APPROVE")
    assert d.kind == "TRIAL" and b.status == "TRIAL_APPROVED"


def test_an_open_duplicate_with_an_active_trial_still_blocks_a_conflicting_decision(db, policy):
    a, _t, _ev = trialled(db)  # TRIAL ACTIVE, proposal still open (PROMOTION_PENDING)
    b = review(db, create(db, kind="UPGRADE", candidate_pin="1.5.0"))
    assert b.duplicate_of == a.id
    with pytest.raises(Conflict) as e:
        decide(db, b, "REJECT", scope={})
    assert e.value.code == "DUPLICATE_DECISION_CONFLICT"


# ---------------------------------------------------------------- §47 lawful configuration: the promoted pin


def test_accepted_without_an_explicit_pin_still_requires_the_promoted_candidate_pin(db, policy):
    component(db, enablement="ENABLED")  # the component was already enabled at 1.3.0 before the upgrade
    p, t, _ev = promoted(db)
    ev = evidence(db, task_spec=t)  # evidence after the promotion
    params = {"evidence_ids": [ev.id], "accepted_configuration": {"component_id": "opencode"},
              "acceptance_results": {"AC1": {"status": "PASS", "evidence_id": ev.id}}}
    with pytest.raises(Blocked) as e:
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", **params)
    assert e.value.code == "CONFIG_NOT_APPLIED" and "1.4.0" in str(e.value.details["problems"])
    db.rollback()
    assert _entry(db).lifecycle != "ENABLED"
    apply_config(db)  # the reviewed manifest now pins the promoted 1.4.0
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", **params)
    db.commit()
    assert p.status == "ACCEPTED" and _entry(db).lifecycle == "ENABLED"


# ---------------------------------------------------------------- §47 an accepted removal never "enables"


def test_an_accepted_removal_retires_the_registry_entry_and_a_failure_restores_it(db, policy):
    component(db)
    oc = _entry(db)
    oc.lifecycle = "ENABLED"  # the component being removed was adopted earlier
    db.commit()
    p, t, _ev = promoted(db, kind="REMOVE")
    db.refresh(oc)
    assert oc.lifecycle == "ENABLED", "a removal trial is not a candidate evaluation stage"
    comp = db.get(ComponentReference, "opencode")
    comp.enablement, comp.lifecycle_final = "DISABLED", "RETIRED"
    db.add(ArchitectureDecisionRecord(adr_key="ADR-9201", title="Retire opencode", status="ACCEPTED"))
    db.commit()
    links.add_links(db, p, actor_user_id="owner", adr_keys=["ADR-9201"])
    items = R.removal_plan(p, p.review_package["baseline"]["components"])["items"]
    plan.update(db, p, {"removal": {i["id"]: {"status": "CLOSED", "note": "selesai"} for i in items}},
                actor_user_id="owner")
    db.commit()
    ev = evidence(db, task_spec=t)
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                           accepted_configuration={"component_id": "opencode"},
                           acceptance_results={"AC1": {"status": "PASS", "evidence_id": ev.id}})
    db.commit()
    db.refresh(oc)
    assert p.status == "ACCEPTED" and oc.lifecycle == "REFERENCE_ONLY"
    assert oc.history[-1]["event"] == "RETIRED" and oc.history[-1]["from"] == "ENABLED"
    service.record_outcome(db, p, actor_user_id="owner", status="FAILED", notes="consumer masih memakai opencode")
    db.commit()
    db.refresh(oc)
    assert oc.lifecycle == "ENABLED", "a failed removal restores what the accepted removal changed"


def test_a_binding_check_that_cannot_complete_blocks_only_that_trial_task(db, policy, monkeypatch):
    from aeos.proposals import facade as proposals
    from aeos.workforce import scheduler

    _p, t, _ev = trialled(db)
    scheduler.ensure_executions(db, t.plan_version_id, actor="system:test")
    scheduler.transition_task(db, t.id, "READY", reason="test: ready", actor="system:test")
    db.commit()

    def unavailable(session, task, *, route=None):
        raise Blocked("cannot invalidate stale decisions: decision registry unavailable",
                      code="DECISION_REGISTRY_UNAVAILABLE")

    monkeypatch.setattr(proposals, "trial_admission_problems", unavailable)
    adm = scheduler.admission_check(db, t.id)  # never raises out of the scheduler tick
    assert "TRIAL_BINDING_UNVERIFIED" in [c for c, _ in adm.blocks] and not adm.admitted


def test_a_proposal_whose_binding_went_stale_returns_to_the_decision_queue(db, policy):
    from aeos.policy.queue import queue_items

    component(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    decide(db, p, "TRIAL_APPROVE")
    t = task(db, p)
    db.get(ComponentReference, "opencode").approved_pin = "1.3.9"  # baseline drifts during the trial
    db.commit()
    with pytest.raises(service.StaleBinding):
        service.attach_evidence(db, p, evidence_ids=[evidence(db, task_spec=t).id], actor_user_id="owner")
    db.commit()
    assert p.status == "UNDER_REVIEW"
    items = [i for i in queue_items(db, "PERSONAL") if i["target"]["id"] == p.id]
    assert items and "kedaluwarsa" in items[0]["why"], "a stale proposal must stay visible to the human"


# ---------------------------------------------------------------- §40 canary: evidence before widening


def test_a_canary_trial_needs_bound_canary_evidence_before_accepted(db, policy):
    component(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p, canary={"target": "1 proyek PERSONAL", "cap": "5 run", "observation_window": "24 jam",
                              "stop_criteria": ["regresi apa pun"]})
    review(db, p)
    decide(db, p, "TRIAL_APPROVE", scope=SCOPE | {"environment": "canary"})
    t = task(db, p)  # evidence through a linked task: p.trial is never launched here
    service.attach_evidence(db, p, evidence_ids=[evidence(db, task_spec=t).id], actor_user_id="owner")
    db.commit()
    checkpoint_pass(db, p)
    decide(db, p, "PROMOTE")
    apply_config(db)
    ev = evidence(db, task_spec=t)
    params = {"evidence_ids": [ev.id], "accepted_configuration": {"component_id": "opencode", "approved_pin": "1.4.0"},
              "acceptance_results": {"AC1": {"status": "PASS", "evidence_id": ev.id}}}
    with pytest.raises(Blocked) as e:
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", **params)
    assert e.value.code == "CANARY_EVIDENCE_REQUIRED"
    db.rollback()
    unrelated = evidence(db, domain=None, claim="docs build ok")  # trusted, but not about this candidate
    with pytest.raises(ValidationFailed) as e2:
        plan.update(db, p, {"canary_evidence_ids": [unrelated.id]}, actor_user_id="owner")
    assert e2.value.code == "EVIDENCE_NOT_FOR_CANDIDATE"
    db.rollback()
    canary_ev = evidence(db, task_spec=t, claim="canary 5 run tanpa regresi")
    plan.update(db, p, {"canary_evidence_ids": [canary_ev.id]}, actor_user_id="owner")
    db.commit()
    assert p.status == "PROMOTION_APPROVED", "canary evidence is progress, never a re-binding"
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", **params)
    db.commit()
    assert p.status == "ACCEPTED"


# ---------------------------------------------------------------- §39 REPLACE: when may the old version retire


def test_replace_promotion_needs_the_retirement_point_of_the_old_version(db, policy):
    component(db)
    p = create(db, kind="REPLACE", candidate_pin="1.4.0", candidate_component_ids=["opencode-next"])
    plan.update(db, p, {"acceptance": ACCEPTANCE, "operations_owner": "owner",
                        "replacement": {"cutover_order": "pasang kandidat lalu pindahkan workforce",
                                        "rollback_or_forward_recovery": "kembalikan pin lama"}}, actor_user_id="owner")
    db.commit()
    review(db, p)
    decide(db, p, "TRIAL_APPROVE")
    t = task(db, p)
    service.attach_evidence(db, p, evidence_ids=[evidence(db, task_spec=t).id], actor_user_id="owner")
    db.commit()
    checkpoint_pass(db, p)
    with pytest.raises(Blocked) as e:
        decide(db, p, "PROMOTE")
    assert e.value.code == "REPLACEMENT_PLAN_INCOMPLETE" and "retire_old_version_after" in e.value.details["missing"]


# ---------------------------------------------------------------- §40 evidence about THIS candidate


def test_a_trial_taskspec_of_another_proposal_cannot_be_linked(db, policy):
    component(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    other = create(db, kind="ADD", sources=["https://github.com/acme/other"])
    foreign = task(db, other)  # created for the trial of `other`
    with pytest.raises(ValidationFailed) as e:
        links.add_links(db, p, actor_user_id="owner", task_ids=[foreign.id])
    assert e.value.code == "TASK_OF_OTHER_PROPOSAL"
    db.rollback()
    own = task(db, p)  # a task bound to this proposal links fine
    assert own.id in db.get(ChangeProposal, p.id).task_ids


def test_rolled_back_needs_something_trialled_or_promoted_and_a_rollback_after_it(db, policy):
    old = audit.record(db, actor_type="cli", actor_id="owner", action="inventory.manifest.load",
                       target_type="install_manifest", target_id="m-old")
    db.commit()
    component(db)
    never = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    with pytest.raises(ValidationFailed) as e:  # nothing ran under this proposal
        service.record_outcome(db, never, actor_user_id="owner", status="ROLLED_BACK",
                               rollback={"reference": {"audit_event_id": old.event_id}})
    assert e.value.code == "ROLLBACK_NOT_REFERENCED" and "nothing was trialled" in e.value.message
    db.rollback()
    p, _t, _ev = trialled(db)  # a trial without promotion: the rollback must postdate the TRIAL decision
    with pytest.raises(ValidationFailed) as e2:
        service.record_outcome(db, p, actor_user_id="owner", status="ROLLED_BACK",
                               rollback={"reference": {"audit_event_id": old.event_id}})
    assert "ran before the promotion/trial" in e2.value.message


# ---------------------------------------------------------------- §23 controller releases are platform records


def test_an_office_proposal_never_gates_a_controller_release(db, settings, monkeypatch):
    from aeos.operations import release
    from aeos.proposals import facade as proposals

    monkeypatch.setattr(release, "_verified_prov", lambda s, rid: {"release_id": rid, "artifact_sha256": "x"})
    monkeypatch.setattr(release, "refuse_worker_context", lambda what: None)
    monkeypatch.setattr(release, "_transition", lambda *a, **k: {"status": "SUCCEEDED", "steps": []})
    office = create(db, kind="ADD", sources=["https://github.com/acme/widget"], domain="OFFICE")
    seen: dict[str, object] = {}
    real = proposals.promotion_for_release

    def spy(session, proposal_id, *, domain=None):
        seen["domain"] = domain
        return real(session, proposal_id, domain=domain)

    monkeypatch.setattr(proposals, "promotion_for_release", spy)
    with pytest.raises(Blocked) as e:
        release.promote(settings, "rel-office", session=db, operator_user_id="owner", proposal_id=office.id)
    assert e.value.code == "PROPOSAL_PROMOTION_NOT_BOUND" and seen["domain"] == "PERSONAL"
    db.expire_all()
    assert "release_ids" not in (db.get(ChangeProposal, office.id).links or {})


# ---------------------------------------------------------------- §23/§35 scopes of shared rows and suggestions


def test_a_shared_registry_row_is_never_superseded_by_a_domain_row(db):
    research.seed_registry(db)
    db.commit()
    shared = _entry(db)
    personal, _dup = research.add_by_url(db, url="https://github.com/acme/widget", name=None, category=None,
                                         notes=None, actor_user_id="owner", domain="PERSONAL")
    db.commit()
    assert shared.submitted_domain is None and personal.submitted_domain == "PERSONAL"
    with pytest.raises(ValidationFailed):
        research.update_entry(db, shared, fields={"superseded_by": personal.id}, actor_user_id="owner", domain="PERSONAL")
    db.rollback()
    other_shared = _entry(db, "tool:litellm")
    research.update_entry(db, shared, fields={"superseded_by": other_shared.id}, actor_user_id="owner", domain="PERSONAL")
    db.commit()
    assert shared.superseded_by == other_shared.id


def test_goal_duplicate_suggestions_stay_within_the_proposal_project_scope(db):
    goal = {"title": "Retrieval dokumentasi berversi", "problem": "worker sering memakai dokumentasi usang tanpa versi"}
    a = create(db, kind="ADD", sources=["https://github.com/acme/alpha"], **goal)
    hidden_project = project(db).id
    b = create(db, kind="ADD", sources=["https://github.com/other/beta"], scope_type="PROJECT",
               project_id=hidden_project, **goal)
    c = create(db, kind="ADD", sources=["https://github.com/third/gamma"], **goal)
    suggested = [d["id"] for d in service.possible_duplicates(db, c)]
    assert a.id in suggested and b.id not in suggested, "a platform proposal never lists another project's titles"
    assert a.id in [d["id"] for d in service.possible_duplicates(db, b)], "a project proposal still sees platform ones"
