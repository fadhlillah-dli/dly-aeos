"""Model Evaluation API (§43, §44). Suites/experiments are definitions; results come only from
recorded EvalRuns. Planning a run never executes it and never changes a route."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal
from aeos.core.timeutil import utcnow
from aeos.evaluation import definitions as D
from aeos.evaluation import service

router = APIRouter(tags=["evaluation"])


@router.get("/api/eval/suites")
def list_suites(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    return {"items": service.list_suites(db), "source": "eval_suites (protected, held-out; definisi tanpa hasil)",
            "checked_at": utcnow().isoformat(), "mode": "DESIGN", "protocol": D.PROTOCOL}


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
def list_runs(db: DB, principal: CurrentPrincipal, suite_key: str | None = None, role: str | None = None) -> dict[str, Any]:
    items = service.list_runs(db, suite_key=suite_key, role=role, domain=_viewer_domain(principal), scoped=True)
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


@router.post("/api/eval/runs")
def plan_run(body: RunIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    run = service.plan_run(db, suite_key=body.suite_key, configuration=body.configuration, role_key=body.role_key,
                           topology=body.topology, repetitions=body.repetitions, limits=body.limits,
                           actor_user_id=principal.user.id, notes=body.notes, domain=_viewer_domain(principal))
    db.commit()
    view = service.run_view(run)
    view["execution"] = {"status": "NOT_STARTED",
                         "note": "Eksekusi melalui workforce scheduler (admission, budget, sandbox, verifier); "
                                 "planning tidak menjalankan model dan tidak mengubah route."}
    return view


@router.get("/api/eval/leaderboard")
def leaderboard(db: DB, principal: CurrentPrincipal, role: str | None = Query(None, max_length=64)) -> dict[str, Any]:
    return service.leaderboard(db, role=role, domain=_viewer_domain(principal), scoped=True)


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
