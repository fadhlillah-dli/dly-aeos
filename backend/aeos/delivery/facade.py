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


def deployment_target(session: Session, project: Any, environment: str) -> dict[str, Any] | None:
    """The observe-only deployment target registered for *project* and *environment* (staging|production):
    ``{"integration_id", "base_url", "health_path"}``, or None when none is registered (§04 Integration Registry)."""
    from aeos.delivery.deployment import DEFAULT_HEALTH_PATH, ENVIRONMENTS, target_for

    if environment not in ENVIRONMENTS:
        return None
    target = target_for(session, project, environment)
    if target is None or not target.root_id:
        return None
    return {"integration_id": target.id, "base_url": target.root_id.rstrip("/"),
            "health_path": str((target.config or {}).get("health_path") or DEFAULT_HEALTH_PATH)}


def classify_deployment_health(sha: str, http_status: int | None, body: Any) -> tuple[str, str, str | None]:
    """(PASS|FAIL|NOT_PROVEN, reason, reported commit) of one health response for the expected commit - the same
    rule the deployment observer and gates G6/G8 use."""
    from aeos.delivery.deployment import classify

    return classify(sha, http_status, body)
