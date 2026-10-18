"""External-write interlock (§14, §20, §26, §50 "Bootstrap belum selesai tetapi publish diminta").

Fail closed: every condition is evaluated and every failing reason is reported; the write is allowed
only when the list of reasons is empty. Conditions:

1. bootstrap complete;
2. actor is an active SUPER_ADMIN whose domain/project scope includes the target (§20: SUPER_ADMIN
   is not automatically AUTH over office resources);
3. project exists, is not archived/retired, and ``project.domain == intent.domain``;
4. provider is allowed for the domain and the operation is a known, non-human-only operation;
5. OFFICE: an ACTIVE company-authorisation decision listing the provider, and a verified Atlassian
   deployment kind (Cloud vs Data Center is not assumed, §04);
6. write enablement: ``write_enabled.<provider>`` flag AND an ACTIVE human WRITE_ENABLEMENT decision;
7. an approved, immutable PlanVersion whose manifest hash equals ``intent.approved_hash``, bound by
   the ACTIVE APPROVAL decision ``intent.decision_id`` that covers the operation and the target scope,
   and that is still the plan's approved version;
8. an unexpired capability (``capability_expires_at`` present and in the future).

Reason strings start with a stable code (``CODE: explanation``) so callers/tests can match them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain, Role
from aeos.core.timeutil import ensure_aware, is_expired, utcnow
from aeos.models.decisions import Decision
from aeos.models.identity import User
from aeos.models.planning import Plan, PlanVersion
from aeos.models.portfolio import Project
from aeos.policy import catalog, enablement
from aeos.policy.facade import InterlockResult, WriteIntent

_INACTIVE_LIFECYCLES = {"ARCHIVED", "RETIRED"}
_TARGET_META_KEYS = {"provider", "label", "title", "kind", "display_name", "name"}


def target_in_scope(target: dict[str, Any], approved_targets: list[Any], provider: str) -> bool:
    """True when *target* lies within one approved target scope of the same provider.

    An approved target is a scope (e.g. ``{"provider": "google-drive", "root_id": "F1"}``); every
    identifying key it names must be present in the intent target with an equal value."""
    if not isinstance(target, dict) or not target:
        return False
    for scope in approved_targets or []:
        if not isinstance(scope, dict) or scope.get("provider") != provider:
            continue
        keys = [k for k in scope if k not in _TARGET_META_KEYS]
        if not keys:
            continue  # a provider-only scope is too broad to bind a write
        if all(k in target and str(target[k]) == str(scope[k]) for k in keys):
            return True
    return False


def _actor_reasons(session: Session, intent: WriteIntent, reasons: list[str]) -> None:
    user = session.get(User, intent.actor_user_id) if intent.actor_user_id else None
    if user is None or not user.is_active:
        reasons.append("ACTOR_NOT_ACTIVE: actor is not an active user")
        return
    if user.role != Role.SUPER_ADMIN.value:
        reasons.append("ACTOR_NOT_AUTHORISED: only SUPER_ADMIN may authorise external writes")
    if user.must_change_password:
        reasons.append("ACTOR_PASSWORD_CHANGE_REQUIRED: actor must change the bootstrap password first")
    if user.domain_scope is not None and intent.domain not in user.domain_scope:
        reasons.append("ACTOR_DOMAIN_SCOPE: actor's domain scope does not include " + str(intent.domain))
    if user.project_scope is not None and intent.project_id not in user.project_scope:
        reasons.append("ACTOR_PROJECT_SCOPE: actor's project scope does not include the project")


def _approval_reasons(session: Session, intent: WriteIntent, reasons: list[str]) -> Decision | None:
    if not intent.approved_plan_version_id or not intent.approved_hash:
        reasons.append("APPROVED_VERSION_MISSING: no approved plan version/hash bound to the write")
        return None
    version = session.get(PlanVersion, intent.approved_plan_version_id)
    if version is None:
        reasons.append("APPROVED_VERSION_UNKNOWN: plan version not found")
        return None
    if version.project_id != intent.project_id or version.domain != intent.domain:
        reasons.append("APPROVED_VERSION_SCOPE: plan version belongs to another project/domain")
    if version.manifest_hash != intent.approved_hash:
        reasons.append("APPROVED_HASH_MISMATCH: approved_hash does not match the immutable version manifest hash")
    plan = session.get(Plan, version.plan_id)
    if plan is None or plan.approved_version_id != version.id:
        reasons.append("VERSION_NOT_APPROVED: the plan's approved version is not this version (stale or revoked)")
    if not intent.decision_id:
        reasons.append("DECISION_MISSING: no approval decision referenced")
        return None
    d = session.get(Decision, intent.decision_id)
    if d is None:
        reasons.append("DECISION_UNKNOWN: approval decision not found")
        return None
    from aeos.policy.decisions import effective_status

    status = effective_status(d)
    if d.kind != "APPROVAL":
        reasons.append("DECISION_KIND: referenced decision is not an APPROVAL")
    if status != "ACTIVE":
        reasons.append(f"DECISION_NOT_ACTIVE: approval decision is {status}")
    if d.target_type != catalog.TARGET_PLAN_VERSION or d.target_id != version.id:
        reasons.append("DECISION_TARGET_MISMATCH: approval does not bind this plan version")
    if d.target_hash != version.manifest_hash or d.target_hash != intent.approved_hash:
        reasons.append("DECISION_HASH_MISMATCH: approval is bound to a different manifest hash")
    if d.project_id != intent.project_id or d.domain != intent.domain:
        reasons.append("DECISION_SCOPE_MISMATCH: approval belongs to another project/domain")
    if intent.operation not in (d.allowed_operations or []):
        reasons.append(f"OPERATION_NOT_APPROVED: approval does not include operation {intent.operation!r}")
    if not target_in_scope(intent.target, (d.auth_scope or {}).get("targets") or [], intent.provider):
        reasons.append("TARGET_NOT_APPROVED: target is outside the approved target scope")
    return d


def check_external_write(session: Session, intent: WriteIntent) -> InterlockResult:
    from aeos.identity.service import bootstrap_complete

    reasons: list[str] = []
    if not bootstrap_complete(session):
        reasons.append("BOOTSTRAP_INCOMPLETE: local bootstrap (first SUPER_ADMIN) is not complete")
    try:
        domain = Domain(intent.domain).value
    except ValueError:
        domain = None
        reasons.append(f"DOMAIN_INVALID: {intent.domain!r} is not PERSONAL/OFFICE")
    _actor_reasons(session, intent, reasons)

    project = session.get(Project, intent.project_id) if intent.project_id else None
    if project is None:
        reasons.append("PROJECT_UNKNOWN: project not found")
    else:
        if domain is not None and project.domain != domain:
            reasons.append("DOMAIN_MISMATCH: project domain differs from the write intent domain")
        if (project.lifecycle or "").upper() in _INACTIVE_LIFECYCLES:
            reasons.append(f"PROJECT_INACTIVE: project lifecycle is {project.lifecycle}")

    provider_ok = domain is not None and intent.provider in catalog.providers_for(domain)
    if not provider_ok:
        reasons.append(f"PROVIDER_NOT_ALLOWED: provider {intent.provider!r} is not allowed for domain {intent.domain}")
    if intent.operation in catalog.HUMAN_ONLY_OPERATIONS:
        reasons.append(f"HUMAN_ONLY_OPERATION: {intent.operation!r} is a human-only action (U-09)")
    elif not catalog.known_operation(intent.provider, intent.operation):
        reasons.append(f"OPERATION_UNKNOWN: {intent.operation!r} is not a known operation for {intent.provider!r}")
    if not isinstance(intent.target, dict) or not intent.target:
        reasons.append("TARGET_MISSING: write target is empty")

    if domain == Domain.OFFICE.value:
        from aeos.policy.office import office_policy

        office = office_policy(session)
        if not office["office_authorization"]:
            reasons.append("OFFICE_NOT_AUTHORISED: no ACTIVE company authorisation decision for OFFICE writes")
        elif intent.provider not in office["authorized_providers"]:
            reasons.append(f"OFFICE_PROVIDER_NOT_AUTHORISED: company authorisation does not cover {intent.provider!r}")
        if intent.provider in catalog.ATLASSIAN_PROVIDERS and office["atlassian_deployment"] not in ("CLOUD", "DATA_CENTER"):
            reasons.append("ATLASSIAN_DEPLOYMENT_UNKNOWN: Cloud vs Data Center not verified for this tenant")

    if not enablement.flag(session, intent.provider):
        reasons.append(f"WRITE_DISABLED: write_enabled.{intent.provider} is not enabled")
    elif enablement.active_enablement_decision(session, intent.provider) is None:
        reasons.append(f"WRITE_ENABLEMENT_DECISION_MISSING: no ACTIVE human decision enables {intent.provider!r}")

    decision = _approval_reasons(session, intent, reasons)

    cap_exp = ensure_aware(intent.capability_expires_at)
    if cap_exp is None:
        reasons.append("CAPABILITY_MISSING: no capability expiry supplied")
    elif is_expired(cap_exp):
        reasons.append("CAPABILITY_EXPIRED: capability expired at " + cap_exp.isoformat())

    allowed = not reasons
    audit.record(
        session, actor_type="user", actor_id=intent.actor_user_id or "", action="policy.interlock.check",
        result="SUCCESS" if allowed else "DENIED", target_type=intent.provider, target_id=str(intent.target)[:120],
        project_id=intent.project_id if project else None, domain=domain,
        reason="; ".join(reasons)[:2000],
        details={"operation": intent.operation, "plan_version_id": intent.approved_plan_version_id,
                 "approved_hash": intent.approved_hash, "decision_id": intent.decision_id,
                 "checked_at": utcnow().isoformat(), "reason_codes": [r.split(":", 1)[0] for r in reasons]},
    )
    return InterlockResult(allowed=allowed, reasons=reasons, decision=decision)
