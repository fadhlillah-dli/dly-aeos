"""Cross-module contract for provider CI evidence (§31). Owned by ``aeos.delivery``.

Read-only: other modules ask for the trusted CI evidence of an exact commit and for the overall
conclusion of its check-runs; they never write delivery records through this facade.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.evidence import EvidenceArtifact


def latest_ci_evidence(session: Session, project_id: str, sha: str | None) -> EvidenceArtifact | None:
    """The newest trusted CI_STATUS evidence of *project_id* for the exact commit *sha* (None when absent)."""
    from aeos.delivery.gates import latest_ci_evidence as impl

    return impl(session, project_id, sha)


def summarize_check_runs(runs: list[dict[str, Any]]) -> str:
    """Overall conclusion (PASS | FAIL | PENDING | NOT_PROVEN | NO_CHECKS | UNKNOWN) of recorded check-runs,
    every run counting (the same rule the delivery gates use)."""
    from aeos.delivery.provider import CheckRunView, summarize_runs

    views = [CheckRunView(name=str(r.get("name") or ""), status=str(r.get("status") or "unknown"),
                          conclusion=r.get("conclusion"), app=r.get("app"), url=r.get("url"),
                          started_at=r.get("started_at"), completed_at=r.get("completed_at"),
                          gates=[str(g) for g in (r.get("gates") or [])])
             for r in runs if isinstance(r, dict)]
    return summarize_runs(views)
