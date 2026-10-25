"""Cross-module contract for evaluation results (§43). Owned by ``aeos.evaluation``.

The workforce/verifier records results here once an EvalRun has executed. Results are stored as
per-sample observations (failures included) and never change a route.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.research import EvalRun


def record_result(session: Session, run_id: str, *, samples: list[dict[str, Any]], gate_results: dict[str, Any],
                  result_source: str, status: str = "COMPLETED") -> EvalRun:
    """``result_source`` must be a trusted collector identity (verifier/collector/...)."""
    from aeos.evaluation.service import record_result as impl

    return impl(session, run_id, samples=samples, gate_results=gate_results, result_source=result_source, status=status)


def leaderboard(session: Session, *, role: str | None = None) -> dict[str, Any]:
    """Internal (unscoped) leaderboard for trusted consumers; viewer endpoints scope runs by domain."""
    from aeos.evaluation.service import leaderboard as impl

    return impl(session, role=role)


# --- additive contracts (existing signatures above are unchanged) ------------------------------


def activation_evidence(session: Session, *, route_key: str, route_config_hash: str,
                        run_ids: list[str]) -> dict[str, Any]:
    """Used by routing for an APPROVED activation (§23 "telah diuji"): ``{"ok", "problems", "roles"}`` —
    ok only when every referenced EvalRun is COMPLETED by a trusted collector on a protected suite, made on
    exactly *route_config_hash* of *route_key*, gate-passing and without critical regression."""
    from aeos.evaluation.service import activation_evidence as impl

    return impl(session, route_key=route_key, route_config_hash=route_config_hash, run_ids=run_ids)


def propose_case_from_failure(session: Session, *, source_type: str, source_id: str, actor_user_id: str,
                              title: str, expected: str, kind: str = "incident", viewer_domain: str | None = None
                              ) -> Any:
    """§28/§33 an observed failure (evidence / worker_run / alert) files a CANDIDATE eval case for human review;
    no evaluation uses it until a PROMOTION decision adopts it. Returns the EvalCaseCandidate."""
    from aeos.evaluation.service import propose_case_from_failure as impl

    return impl(session, source_type=source_type, source_id=source_id, actor_user_id=actor_user_id, title=title,
                expected=expected, kind=kind, viewer_domain=viewer_domain)
