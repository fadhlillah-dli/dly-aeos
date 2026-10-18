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
