"""Model Evaluation API (§43, §44). Suites/experiments are definitions; results come only from
recorded EvalRuns. Planning a run never executes it and never changes a route. Execution: an owner
starts a run, binds executed TaskSpecs to its case slots and the trusted collector records samples from
the canonical task records (``aeos.evaluation.executor``)."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal
from aeos.core.timeutil import utcnow
from aeos.evaluation import definitions as D
from aeos.evaluation import executor, service

router = APIRouter(tags=["evaluation"])


@router.get("/api/eval/suites")
def list_suites(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    return {"items": service.list_suites(db), "source": "eval_suites (protected, held-out; definisi tanpa hasil)",
            "checked_at": utcnow().isoformat(), "mode": "DESIGN", "protocol": D.PROTOCOL, "grader": D.GRADER_NOTE}


@router.get("/api/eval/experiments")
def list_experiments(principal: CurrentPrincipal) -> dict[str, Any]:
    return {"items": D.EXPERIMENTS, "common_gates": D.COMMON_GATES, "measures": D.MEASURES,
            "not_measured": D.NOT_MEASURED, "dimensions": D.DIMENSIONS, "source": "§44 experiment definitions",
            "checked_at": utcnow().isoformat(), "mode": "DESIGN",
            "note": "Keempat eksperimen memakai gate policy/authority, review dan security yang sama; belum ada hasil."}


def _viewer_domain(principal: Any) -> str | None:
    d = principal.active_domain
    return d.value if d is not None and principal.can_access_domain(d) else None


@router.get("/api/eval/runs")
def list_runs(db: DB, principal: CurrentPrincipal, suite_key: str | None = None, role: str | None = None,
              proposal_id: str | None = None) -> dict[str, Any]:
    items = service.list_runs(db, suite_key=suite_key, role=role, domain=_viewer_domain(principal), scoped=True,
                              proposal_id=proposal_id)
    db.rollback()  # read-only
    return {"items": items, "source": "eval_runs",
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


class RunIn(BaseModel):
    suite_key: str = Field(min_length=3, max_length=96)
    configuration: dict[str, Any]
    role_key: str | None = Field(default=None, max_length=64)
    topology: str | None = Field(default=None, pattern="^[ABCD]$")
    repetitions: int = Field(default=3, ge=1, le=50)
    limits: dict[str, Any]
    notes: str = Field(default="", max_length=2000)
    is_baseline: bool = False
    proposal_id: str | None = Field(default=None, max_length=36)


@router.post("/api/eval/runs")
def plan_run(body: RunIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    run = service.plan_run(db, suite_key=body.suite_key, configuration=body.configuration, role_key=body.role_key,
                           topology=body.topology, repetitions=body.repetitions, limits=body.limits,
                           actor_user_id=principal.user.id, notes=body.notes, domain=_viewer_domain(principal),
                           is_baseline=body.is_baseline, proposal_id=body.proposal_id)
    db.commit()
    view = service.run_view(run, db)
    view["execution"] = {**executor.execution_view(run), "status": "NOT_STARTED",
                         "note": "Eksekusi melalui workforce scheduler (admission, budget, sandbox, verifier); "
                                 "planning tidak menjalankan model dan tidak mengubah route. " + executor.FIXTURES_NOTE}
    return view


def _run_out(db: Any, run: Any) -> dict[str, Any]:
    view = service.run_view(run, db)
    view["slots"] = executor.slots(db, run)
    return view


@router.get("/api/eval/runs/{run_id}")
def get_run(run_id: str, db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    run = executor.get_run(db, run_id, viewer_domain=_viewer_domain(principal), scoped=True)
    out = _run_out(db, run)
    db.rollback()
    return out


@router.post("/api/eval/runs/{run_id}/start")
def start_run(run_id: str, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    run = executor.start_run(db, run_id, actor_user_id=principal.user.id, viewer_domain=_viewer_domain(principal),
                             scoped=True)
    db.commit()
    return _run_out(db, run)


class BindIn(BaseModel):
    slot_id: str = Field(min_length=3, max_length=128)
    task_id: str = Field(min_length=1, max_length=36)
    # §44 B/C/D: append one more TaskSpec to an already bound slot (an ordered task set aggregated as one sample)
    append: bool = False


@router.post("/api/eval/runs/{run_id}/bind")
def bind_task(run_id: str, body: BindIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    if body.append:
        from aeos.evaluation import topology

        run = topology.bind_additional_task(db, run_id, slot_id=body.slot_id, task_id=body.task_id,
                                            actor_user_id=principal.user.id, viewer_domain=_viewer_domain(principal),
                                            scoped=True)
        db.commit()
        return _run_out(db, run)
    run = executor.bind_task(db, run_id, slot_id=body.slot_id, task_id=body.task_id, actor_user_id=principal.user.id,
                             viewer_domain=_viewer_domain(principal), scoped=True)
    db.commit()
    return _run_out(db, run)


@router.post("/api/eval/runs/{run_id}/collect")
def collect(run_id: str, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    out = executor.collect(db, run_id, viewer_domain=_viewer_domain(principal), scoped=True)
    db.commit()
    return out


class FinishIn(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


@router.post("/api/eval/runs/{run_id}/finish")
def finish(run_id: str, body: FinishIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    run = executor.finish_run(db, run_id, actor_user_id=principal.user.id, reason=body.reason,
                              viewer_domain=_viewer_domain(principal), scoped=True)
    db.commit()
    return _run_out(db, run)


@router.get("/api/eval/leaderboard")
def leaderboard(db: DB, principal: CurrentPrincipal, role: str | None = Query(None, max_length=64)) -> dict[str, Any]:
    out = service.leaderboard(db, role=role, domain=_viewer_domain(principal), scoped=True)
    db.rollback()
    return out


@router.get("/api/eval/topology")
def topology(db: DB, principal: CurrentPrincipal, suite_key: str = Query(..., max_length=96),
             configuration_hash: str = Query(..., max_length=80)) -> dict[str, Any]:
    out = service.topology_comparison(db, suite_key=suite_key, configuration_hash=configuration_hash,
                                      domain=_viewer_domain(principal), scoped=True)
    db.rollback()
    return out


class ComparisonIn(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=36)
    suite_keys: list[str] = Field(min_length=1, max_length=8)
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    limits: dict[str, Any]
    repetitions: int = Field(default=3, ge=1, le=50)


@router.post("/api/eval/comparisons")
def plan_comparison(body: ComparisonIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    runs = service.plan_comparison(db, proposal_id=body.proposal_id, suite_keys=body.suite_keys,
                                   baseline=body.baseline, candidate=body.candidate, limits=body.limits,
                                   repetitions=body.repetitions, actor_user_id=principal.user.id,
                                   domain=_viewer_domain(principal))
    db.commit()
    return {"proposal_id": body.proposal_id, "run_ids": [r.id for r in runs],
            "comparison": service.comparison(db, proposal_id=body.proposal_id, domain=_viewer_domain(principal),
                                             scoped=True)}


@router.get("/api/eval/comparisons/{proposal_id}")
def get_comparison(proposal_id: str, db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    out = service.comparison(db, proposal_id=proposal_id, domain=_viewer_domain(principal), scoped=True)
    db.rollback()
    return out


class WeightsIn(BaseModel):
    role: str = Field(min_length=2, max_length=64)
    weights: dict[str, float]
    rationale: str = Field(min_length=10, max_length=4000)


@router.post("/api/eval/weights")
def declare_weights(body: WeightsIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    d = service.declare_weights(db, role=body.role, weights=body.weights, actor_user_id=principal.user.id,
                                rationale=body.rationale)
    db.commit()
    return {"decision_id": d.id, "role": body.role, "weights": (d.details or {}).get("weights")}


class CaseIn(BaseModel):
    source_type: Literal["evidence", "worker_run", "alert"]
    source_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=5, max_length=200)
    expected: str = Field(min_length=10, max_length=1000)
    kind: str = Field(default="incident", max_length=16)
    related_suite_key: str | None = Field(default=None, max_length=96)


@router.get("/api/eval/cases")
def list_cases(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    return {"items": service.list_case_candidates(db, domain=_viewer_domain(principal)), "source": "eval_case_candidates",
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.post("/api/eval/cases")
def propose_case(body: CaseIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    c = service.propose_case_from_failure(db, source_type=body.source_type, source_id=body.source_id,
                                          actor_user_id=principal.user.id, title=body.title, expected=body.expected,
                                          kind=body.kind, related_suite_key=body.related_suite_key,
                                          viewer_domain=_viewer_domain(principal))
    db.commit()
    return service.case_candidate_view(c)


class AdoptIn(BaseModel):
    expected_case_hash: str = Field(min_length=10, max_length=80)
    rationale: str = Field(min_length=10, max_length=4000)


@router.post("/api/eval/cases/{candidate_id}/adopt")
def adopt_case(candidate_id: str, body: AdoptIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    suite = service.adopt_case(db, candidate_id, actor_user_id=principal.user.id,
                               expected_case_hash=body.expected_case_hash, rationale=body.rationale,
                               viewer_domain=_viewer_domain(principal))
    db.commit()
    return {"suite": service.suite_view(suite)}


class RejectIn(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


@router.post("/api/eval/cases/{candidate_id}/reject")
def reject_case(candidate_id: str, body: RejectIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    c = service.reject_case(db, candidate_id, actor_user_id=principal.user.id, reason=body.reason,
                            viewer_domain=_viewer_domain(principal))
    db.commit()
    return service.case_candidate_view(c)


@router.get("/api/eval/blind-review")
def blind_review(db: DB, principal: AdminPrincipal, suite_key: str = Query(..., max_length=96)) -> dict[str, Any]:
    out = service.blind_review_items(db, suite_key=suite_key, domain=_viewer_domain(principal), scoped=True)
    db.rollback()
    return out


class BlindScoreIn(BaseModel):
    suite_key: str = Field(min_length=3, max_length=96)
    item_id: str = Field(min_length=3, max_length=40)
    scores: dict[str, float]


@router.post("/api/eval/blind-review")
def record_blind_review(body: BlindScoreIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    out = service.record_blind_review(db, suite_key=body.suite_key, item_id=body.item_id, scores=body.scores,
                                      reviewer_user_id=principal.user.id, domain=_viewer_domain(principal), scoped=True)
    db.commit()
    return out


class ScorecardIn(BaseModel):
    role: str = Field(min_length=2, max_length=64)
    rationale: str = Field(default="", max_length=4000)


@router.post("/api/eval/scorecard")
def scorecard(body: ScorecardIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    """Open a decision request (COMPARE proposal) from the current scorecard. Never changes a route."""
    from aeos.proposals import service as proposals

    snap = service.scorecard_snapshot(db, role=body.role, domain=domain.value, scoped=True)
    summary = json.dumps(snap, ensure_ascii=False, sort_keys=True)[:6000]
    p = proposals.create_proposal(
        db, actor_user_id=principal.user.id, domain=domain.value, kind="COMPARE",
        title=f"Scorecard evaluasi peran {body.role}", sources=[f"eval scorecard {body.role} {snap['scorecard_hash'][7:19]}"],
        problem=(body.rationale or "Tinjau hasil evaluasi tercatat sebelum keputusan route/role (§43, §44).")
        + f"\n\nScorecard ({snap['scorecard_hash']}): {summary}",
        expected_benefit="Keputusan routing berbasis bukti; tidak ada perubahan route otomatis.",
    )
    db.commit()
    return {"proposal_id": p.id, "scorecard": snap, "route_changed": False}
