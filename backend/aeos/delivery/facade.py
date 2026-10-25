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
    ``{"integration_id", "base_url", "health_path"}``, or None when none is registered (§04 Integration Registry).

    The same checked target the deployment observer uses: a stored ``health_path`` that is not an absolute URL
    path (e.g. ``@other.host/health``, which would send the health GET to another host) raises
    ``ValidationFailed`` HEALTH_PATH_INVALID instead of being used."""
    from aeos.delivery.deployment import ENVIRONMENTS, target_for, target_settings

    if environment not in ENVIRONMENTS:
        return None
    target = target_for(session, project, environment)
    if target is None or not target.root_id:
        return None
    checked = target_settings(target)
    return {"integration_id": checked["integration_id"], "base_url": checked["base_url"],
            "health_path": checked["health_path"]}


# deployment-target config keys owned by aeos.delivery (the Integration Registry validates them through
# validate_deployment_target_config); these keys are kept by a generic integration PUT whose config omits them
# (an explicit value, even an empty one, replaces them).
DEPLOYMENT_TARGET_CONFIG_KEYS: tuple[str, ...] = ("health_path", "smoke_checks", "sli_checks", "rollback_on",
                                                  "require_artifact_digest")
DEPLOYMENT_TARGET_CHECK_KEYS: tuple[str, ...] = ("health_path", "smoke_checks", "sli_checks", "rollback_on",
                                                 "require_artifact_digest")


def validate_deployment_target_config(config: dict[str, Any]) -> dict[str, Any]:
    """Additive: validate and normalise the deployment-target keys of an integration config (raises
    ``ValidationFailed`` HEALTH_PATH_INVALID / SMOKE_CHECKS_INVALID / SLI_CHECKS_INVALID /
    ROLLBACK_CRITERIA_INVALID / INVALID_CONFIG)."""
    from aeos.delivery.deployment import validate_target_config

    return validate_target_config(config)


def require_staging_gate(session: Session, project: Any) -> None:
    """Additive (§08): raise ``Blocked`` CAPABILITY_NOT_READY unless CAN_VALIDATE_STAGING is READY for *project*.
    Every staging validation operation (observation, screenshot capture) asks this first."""
    from aeos.delivery.deployment import require_staging_gate as impl

    impl(session, project)


def classify_deployment_health(sha: str, http_status: int | None, body: Any) -> tuple[str, str, str | None]:
    """(PASS|FAIL|NOT_PROVEN, reason, reported commit) of one health response for the expected commit - the same
    rule the deployment observer and gates G6/G8 use."""
    from aeos.delivery.deployment import classify

    return classify(sha, http_status, body)
