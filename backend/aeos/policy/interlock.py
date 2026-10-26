"""External-write interlock (§14, §20, §26, §50 "Bootstrap belum selesai tetapi publish diminta").

Fail closed: every condition is evaluated and every failing reason is reported; the write is allowed
only when the list of reasons is empty. Conditions:

1. bootstrap complete;
2. actor is an active SUPER_ADMIN whose domain/project scope includes the target (§20: SUPER_ADMIN
   is not automatically AUTH over office resources);
3. project exists, is not archived/retired, and ``project.domain == intent.domain``;
4. provider is allowed for the domain and the operation is a known, non-human-only operation;
5. OFFICE: an ACTIVE company-authorisation decision listing the provider, the exact target inside its
   ``approved_targets`` (tenant/space/parent/project key/repository, ``aeos.policy.office_scope``), and a verified
   Atlassian deployment kind (Cloud vs Data Center is not assumed, §04);
6. write enablement: ``write_enabled.<provider>`` flag AND an ACTIVE human WRITE_ENABLEMENT decision;
7. an approved, immutable, LIVE (never MOCK/DEMO) PlanVersion whose stored content still hashes to
   its manifest hash and whose manifest hash equals ``intent.approved_hash``, bound by the ACTIVE
   APPROVAL decision that covers the operation and the target scope, and that is still the plan's
   approved version. Delivery writes reference that APPROVAL as ``intent.decision_id``; publication
   writes reference the run's human PUBLICATION decision (exact publication, preview hash, operation
   ids, targets, expiry), which in turn names its APPROVE_AND_PUBLISH approval (ADR-0074 amendment);
8. an unexpired capability (``capability_expires_at`` present and in the future).

Reason strings start with a stable code (``CODE: explanation``) so callers/tests can match them.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain, Role
from aeos.core.hashing import content_hash
from aeos.core.timeutil import ensure_aware, is_expired, utcnow
from aeos.models.decisions import Decision
from aeos.models.identity import User
from aeos.models.planning import Plan, PlanVersion
from aeos.models.portfolio import Project
from aeos.policy import catalog, enablement
from aeos.policy.facade import InterlockResult, WriteIntent

_INACTIVE_LIFECYCLES = {"ARCHIVED", "RETIRED"}
target_in_scope = catalog.target_in_scope  # re-exported (the rule lives with the static vocabularies)


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


def _publication_reasons(session: Session, intent: WriteIntent, d: Decision, reasons: list[str]) -> str | None:
    """Validate the run's human PUBLICATION decision (ADR-0074) and return the id of the APPROVE_AND_PUBLISH
    approval it was recorded under. One run decision covers exactly the previewed operations of one
    publication (U-10): any other publication, operation, target, preview or version is refused."""
    from aeos.models.publishing import Publication
    from aeos.policy.decisions import effective_status

    extra = intent.extra or {}
    scope = d.auth_scope or {}
    status = effective_status(d)
    if d.actor_kind != "HUMAN":
        reasons.append(f"DECISION_ACTOR_NOT_HUMAN: publication decision actor kind is {d.actor_kind}, not a human "
                       "SUPER_ADMIN")
    if status != "ACTIVE":
        reasons.append(f"PUBLICATION_DECISION_NOT_ACTIVE: publication decision is {status}")
    if d.expires_at is None:
        reasons.append("PUBLICATION_DECISION_NO_EXPIRY: a publication run decision must expire (capability, §26)")
    pub_id = extra.get("publication_id")
    if d.target_type != catalog.TARGET_PUBLICATION or not pub_id or d.target_id != str(pub_id):
        reasons.append("PUBLICATION_MISMATCH: the decision covers another publication run")
    if d.target_version != intent.approved_plan_version_id or d.target_hash != intent.approved_hash:
        reasons.append("PUBLICATION_BINDING_MISMATCH: the run decision is bound to another plan version/hash")
    if d.project_id != intent.project_id or d.domain != intent.domain:
        reasons.append("DECISION_SCOPE_MISMATCH: publication decision belongs to another project/domain")
    if intent.operation not in (d.allowed_operations or []):
        reasons.append(f"OPERATION_NOT_IN_RUN: the run decision does not include operation {intent.operation!r}")
    op_id = extra.get("operation_id")
    if not op_id or op_id not in (scope.get("operation_ids") or []):
        reasons.append("OPERATION_NOT_IN_RUN: this outbox operation was not part of the approved preview")
    if not extra.get("preview_hash") or extra.get("preview_hash") != scope.get("preview_hash"):
        reasons.append("PREVIEW_HASH_MISMATCH: the operation does not belong to the previewed run")
    if not target_in_scope(intent.target, scope.get("targets") or [], intent.provider):
        reasons.append("TARGET_NOT_IN_RUN: target is outside the targets of this publication run")
    pub = session.get(Publication, str(pub_id)) if pub_id else None
    if pub is None:
        reasons.append("PUBLICATION_UNKNOWN: publication run not found")
    elif (pub.plan_version_id != intent.approved_plan_version_id or pub.approved_hash != intent.approved_hash
          or pub.publication_decision_id != d.id or pub.preview_hash != scope.get("preview_hash")):
        reasons.append("PUBLICATION_MISMATCH: publication record does not match the run decision")
    approval_id = scope.get("approval_decision_id")
    if not approval_id:
        reasons.append("APPROVAL_LINK_MISSING: the run decision does not reference the plan approval it executes")
    return str(approval_id) if approval_id else None


def _approval_reasons(session: Session, intent: WriteIntent, reasons: list[str], *,
                      approval_id: str | None = None, publication: bool = False) -> Decision | None:
    """Approved immutable version + the human APPROVAL decision covering the operation and target.

    ``approval_id`` is the approval to check (the intent's own decision for delivery writes, the
    approval referenced by the run decision for publication writes)."""
    if approval_id is None and not publication:
        approval_id = intent.decision_id
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
    if content_hash(version.content) != version.manifest_hash:
        reasons.append("VERSION_INTEGRITY: stored plan content no longer matches its manifest hash")
    if (version.mode or "LIVE") != "LIVE":
        # §35/§49: MOCK/DEMO plan content is never written to an external system as if it were real.
        reasons.append(f"VERSION_NOT_LIVE: plan version is {version.mode}; fixtures are never published externally")
    plan = session.get(Plan, version.plan_id)
    if plan is None or plan.approved_version_id != version.id:
        reasons.append("VERSION_NOT_APPROVED: the plan's approved version is not this version (stale or revoked)")
    if not approval_id:
        if not publication:  # a publication write already reported the missing run decision/approval link
            reasons.append("DECISION_MISSING: no approval decision referenced")
        return None
    d = session.get(Decision, approval_id)
    if d is None:
        reasons.append("DECISION_UNKNOWN: approval decision not found")
        return None
    from aeos.policy.decisions import effective_status

    status = effective_status(d)
    if d.kind != "APPROVAL":
        reasons.append("DECISION_KIND: referenced decision is not an APPROVAL")
    if d.actor_kind != "HUMAN":
        # §19/§20: a publication approval comes from a live SUPER_ADMIN decision, never an ADR-recorded actor.
        reasons.append(f"DECISION_ACTOR_NOT_HUMAN: approval actor kind is {d.actor_kind}, not a human SUPER_ADMIN")
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
    if publication and (d.auth_scope or {}).get("action") != "APPROVE_AND_PUBLISH":
        reasons.append("APPROVAL_NOT_FOR_PUBLICATION: the plan was approved without publication (APPROVE); "
                       "publishing needs Approve & Publish of this version and its targets")
    return d


def _authority_reasons(session: Session, intent: WriteIntent, reasons: list[str]) -> Decision | None:
    """One model (§05 step 9-10, §14, U-10, ADR-0031/0074 amendments):

    * delivery writes (push_branch/create_pr) are authorised by the plan APPROVAL decision itself;
    * publication writes need the run's human PUBLICATION decision (exact publication + preview +
      operation ids + targets, short expiry) **and** the APPROVE_AND_PUBLISH approval it references,
      which must still be ACTIVE and bind the same version/hash/targets/operation kinds. A stale or
      revoked approval, or a newer unapproved version, therefore stops every write of the run."""
    if not catalog.is_publication_write(intent.provider, intent.operation):
        return _approval_reasons(session, intent, reasons)
    d = session.get(Decision, intent.decision_id) if intent.decision_id else None
    if d is None or d.kind != "PUBLICATION":
        reasons.append("PUBLICATION_DECISION_REQUIRED: publication writes need the human PUBLICATION decision of "
                       "this run (ADR-0074); a plan approval alone does not authorise a concrete run")
        _approval_reasons(session, intent, reasons, approval_id=d.id if d is not None and d.kind == "APPROVAL"
                          else None, publication=True)
        return d
    approval_id = _publication_reasons(session, intent, d, reasons)
    _approval_reasons(session, intent, reasons, approval_id=approval_id, publication=True)
    return d


def _office_target_reason(session: Session, intent: WriteIntent, office: dict) -> tuple[bool, str]:
    """§01 U-03 / §07 step 10: the exact OFFICE write target (tenant, space, parent/project key, repository) must lie
    within the company-approved targets. The tenant/space come from the project's registered integration for that
    exact root (the Integration Registry is the one source of the target identity, never the intent's own claim)."""
    from sqlalchemy import select

    from aeos.models.portfolio import ProjectIntegration
    from aeos.policy import office_scope

    target = intent.target if isinstance(intent.target, dict) else {}
    root = str(target.get("root_id") or target.get("repo") or "").strip()
    tenant = space = None
    if intent.project_id and root:
        integ = next((i for i in session.scalars(select(ProjectIntegration).where(
            ProjectIntegration.project_id == intent.project_id, ProjectIntegration.provider == intent.provider))
            if (i.root_id or "").strip().lower() == root.lower()), None)
        if integ is not None:
            tenant = integ.tenant_id or (integ.config or {}).get("base_url")
            space = (integ.config or {}).get("space_id")
    return office_scope.target_check(office, intent.provider, root_id=root or None, tenant=tenant, space_id=space)


def check_external_write(session: Session, intent: WriteIntent) -> InterlockResult:
    from aeos.identity.facade import bootstrap_complete

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
    elif intent.target.get("provider") not in (None, intent.provider):
        reasons.append(f"TARGET_PROVIDER_MISMATCH: target names provider {intent.target.get('provider')!r}, "
                       f"the write uses {intent.provider!r}")

    if domain == Domain.OFFICE.value:
        from aeos.policy.office import office_policy

        office = office_policy(session)
        if not office["office_authorization"]:
            reasons.append("OFFICE_NOT_AUTHORISED: no ACTIVE company authorisation decision for OFFICE writes")
        elif intent.provider not in office["authorized_providers"]:
            reasons.append(f"OFFICE_PROVIDER_NOT_AUTHORISED: company authorisation does not cover {intent.provider!r}")
        else:
            ok, why = _office_target_reason(session, intent, office)
            if not ok:
                reasons.append(f"OFFICE_TARGET_NOT_AUTHORISED: {why}")
        if intent.provider in catalog.ATLASSIAN_PROVIDERS and office["atlassian_deployment"] not in ("CLOUD", "DATA_CENTER"):
            reasons.append("ATLASSIAN_DEPLOYMENT_UNKNOWN: Cloud vs Data Center not verified for this tenant")

    if not enablement.flag(session, intent.provider):
        reasons.append(f"WRITE_DISABLED: write_enabled.{intent.provider} is not enabled")
    elif enablement.active_enablement_decision(session, intent.provider) is None:
        reasons.append(f"WRITE_ENABLEMENT_DECISION_MISSING: no ACTIVE human decision enables {intent.provider!r}")

    decision = _authority_reasons(session, intent, reasons)

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
                 "publication_id": (intent.extra or {}).get("publication_id"),
                 "operation_id": (intent.extra or {}).get("operation_id"),
                 "checked_at": utcnow().isoformat(), "reason_codes": [r.split(":", 1)[0] for r in reasons]},
    )
    return InterlockResult(allowed=allowed, reasons=reasons, decision=decision)
