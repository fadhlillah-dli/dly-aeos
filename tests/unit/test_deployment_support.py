"""Shared helpers for the deployment-target tests (delivery gates G6/G8, staging gate, post-release response).

These are CONTROLLED PRECONDITIONS for unit tests: Doctor results are labelled ``fixture = unit`` and owner
decisions are written as the HUMAN Decision rows the product reads (as ``record_release_approvers`` does).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aeos.core.timeutil import utcnow

HEALTHY_RELEASE = {"mode": "release", "release_id": "rel-1", "dirty": False, "active_release_id": "rel-1",
                   "matches_active": True, "status": "ACTIVE_RELEASE"}


def deploy_target(db, project, purpose: str, url: str = "https://staging.example.org",
                  config: dict[str, Any] | None = None):
    from aeos.models.portfolio import ProjectIntegration

    integ = ProjectIntegration(project_id=project.id, domain=project.domain, provider="deployment-target",
                               purpose=purpose, root_id=url, config=dict(config or {}))
    db.add(integ)
    db.commit()
    return integ


def make_staging_ready(db, project) -> list[str]:
    """Record passing Doctor results for every check of CAN_VALIDATE_STAGING: the platform checks and the
    project-scoped ``delivery.staging_target`` of *project* (bound to its current staging target)."""
    from aeos.core.enums import Capability
    from aeos.delivery import deployment
    from aeos.doctor.capabilities import checks_for_capability
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.models.inventory import CheckResult

    load_all()
    now = utcnow()
    ids = checks_for_capability(Capability.CAN_VALIDATE_STAGING)
    for cid in ids:
        spec = REGISTRY[cid]
        status = "VERIFIED" if "VERIFIED" in spec.ok_statuses else (sorted(spec.ok_statuses) or ["UNKNOWN"])[0]
        scope = dict(spec.scope)
        evidence: dict[str, Any] = {"fixture": "unit"}
        if cid == "delivery.staging_target":
            target = deployment.target_for(db, project, "staging")
            scope.update(project_id=project.id, domain=project.domain)
            evidence["targets"] = [{"root_id": target.root_id}] if target is not None else []
            status = "CONFIGURED"
        db.add(CheckResult(check_id=cid, title=spec.title, group=spec.group, component_id=spec.component_id,
                           scope=scope, status=status, severity="INFO", summary="unit fixture: controlled precondition",
                           observed_evidence=evidence, last_checked_at=now, expires_at=now + timedelta(hours=1),
                           invalidators=list(spec.invalidators), fingerprint=None, error_code=None, remediation={},
                           requires_destructive=False, requires_paid_write=False))
    db.commit()
    return ids


def environment_decision(db, project, environment: str, **scope: Any):
    """The owner's HUMAN POLICY decision about an environment (``staging_environment`` /
    ``production_environment``), e.g. ``staging="AUTHORISED", target=<url>`` or ``staging="NONE"``."""
    from aeos.models.decisions import Decision

    d = Decision(kind="POLICY", actor_user_id="owner", actor_kind="HUMAN", target_type=f"{environment}_environment",
                 target_id=project.id, project_id=project.id, domain=project.domain, auth_scope=dict(scope),
                 rationale=f"owner decision about the {environment} environment")
    db.add(d)
    db.commit()
    return d


def authorise_staging(db, project, url: str = "https://staging.example.org"):
    return environment_decision(db, project, "staging", staging="AUTHORISED", target=url)
