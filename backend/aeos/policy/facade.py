"""Cross-module contract for decisions and the external-write interlock (§14, §20, §26).

Owned by ``aeos.policy``. Signatures are frozen for other modules; the policy agent implements
the bodies in ``aeos.policy.decisions`` / ``aeos.policy.interlock``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.models.decisions import Decision


@dataclass(frozen=True)
class WriteIntent:
    """Everything the interlock needs to decide whether an external write may run (§26)."""

    actor_user_id: str
    project_id: str
    domain: str
    provider: str  # aeos.portfolio.providers: google-drive|github-issues|github-repo|confluence|jira|bitbucket
    operation: str  # create_folder|create_doc|update_doc|ensure_label|create_issue|push_branch|create_pr|...
    # target: {"provider": <provider>, "root_id": <exact approved target>, ...} for publication writes
    # decision_id: the run's PUBLICATION decision (publication writes) or the plan APPROVAL (delivery)
    target: dict[str, Any]
    approved_plan_version_id: str | None
    approved_hash: str | None
    decision_id: str | None
    capability_expires_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InterlockResult:
    allowed: bool
    reasons: list[str]
    decision: Decision | None = None


def record_decision(
    session: Session,
    *,
    kind: str,
    actor_user_id: str,
    target_type: str,
    target_id: str,
    target_version: str | None = None,
    target_hash: str | None = None,
    allowed_operations: list[str] | None = None,
    auth_scope: dict[str, Any] | None = None,
    project_id: str | None = None,
    domain: str | None = None,
    rationale: str = "",
    expires_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> Decision:
    # Capture arguments before the lazy import so the implementation is not passed as a kwarg.
    kwargs = dict(locals())
    from aeos.policy.decisions import record_decision as impl

    return impl(**kwargs)


def find_active_decision(
    session: Session,
    *,
    kind: str,
    target_type: str,
    target_id: str,
    target_hash: str | None = None,
    operation: str | None = None,
) -> Decision | None:
    kwargs = dict(locals())
    from aeos.policy.decisions import find_active_decision as impl

    return impl(**kwargs)


def invalidate_decisions(session: Session, *, target_type: str, target_id: str, reason: str) -> int:
    kwargs = dict(locals())
    from aeos.policy.decisions import invalidate_decisions as impl

    return impl(**kwargs)


def check_external_write(session: Session, intent: WriteIntent) -> InterlockResult:
    """Fail-closed interlock: bootstrap complete, actor AUTH, domain/provider/target valid,
    write enablement, approved immutable version/hash, unexpired capability (§26)."""
    from aeos.policy.interlock import check_external_write as impl

    return impl(session, intent)


def target_in_scope(target: dict[str, Any], approved_targets: list[Any], provider: str) -> bool:
    """Additive: True when *target* lies inside one approved target scope of *provider* (the exact rule the
    interlock applies), so callers can refuse before recording anything."""
    from aeos.policy.catalog import target_in_scope as impl

    return impl(target, approved_targets, provider)


def office_policy(session: Session) -> dict[str, Any]:
    """§23: OFFICE default external_worker_allowed=false and approved_worker_routes=[]."""
    from aeos.policy.office import office_policy as impl

    return impl(session)


def office_target_authorised(session: Session, provider: str, *, root_id: Any = None, tenant: Any = None,
                             space_id: Any = None, policy: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Additive (W7, 2026-10-25): (authorised, reason) for one exact OFFICE target against the company
    authorisation's ``approved_targets`` (§01 U-03, §07 step 10). No approved target -> False (fail closed)."""
    from aeos.policy import office_scope

    pol = policy if policy is not None else office_policy(session)
    return office_scope.target_check(pol, provider, root_id=root_id, tenant=tenant, space_id=space_id)


def office_data_class_authorised(session: Session, classification: Any, *,
                                 policy: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Additive (W7, 2026-10-25): (authorised, reason) for sending OFFICE data of *classification* to an approved
    model route / Claude Max (``approved_data_classes``, §51 P1). None approved -> False (fail closed)."""
    from aeos.policy import office_scope

    pol = policy if policy is not None else office_policy(session)
    return office_scope.data_class_check(pol, classification)


# ---- additive (non-breaking) contract: raise a request for a human decision -----------------------


def request_decision(
    session: Session,
    *,
    kind: str,
    title: str,
    reason: str,
    requested_by: str,
    requested_by_kind: str = "SYSTEM",
    project_id: str | None = None,
    domain: str | None = None,
    target_type: str = "",
    target_id: str = "",
    target_hash: str | None = None,
    requested_operations: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    """File a request into the single Human Decision Queue (U-12) and return its id.

    A request is DATA and grants nothing; only a human Decision resolves it (§19, §21 blocker/decision
    request). Kinds: EXCEPTION, RISK_ACCEPTANCE, POLICY, BUDGET, WRITE_ENABLEMENT, ROUTE_ACTIVATION,
    TRIAL, PROMOTION."""
    kwargs = dict(locals())
    from aeos.policy.requests import create_request as impl

    return impl(**kwargs).id


def decision_record_fields() -> tuple[str, ...]:
    """§44 DecisionRecord fields a routing/role/mode adoption decision carries (options, goal, evidence, cost/quality
    trade-off, invalidation condition)."""
    from aeos.policy.catalog import DECISION_RECORD_FIELDS

    return tuple(DECISION_RECORD_FIELDS)


def system_change_evidence(session: Session, *, domain: str | None, eval_run_ids: Any,
                           rollback_target: Any, change: dict[str, Any] | None = None) -> dict[str, Any]:
    """§41/§43: validate the evaluation runs (COMPLETED, every gate PASS, this domain or the platform) and the
    rollback target of a system change; raises ValidationFailed (SYSTEM_CHANGE_EVIDENCE_REQUIRED) otherwise.

    ``change`` (additive, 2026-10-25): the change the runs must be evidence of - ``{"target_type", "target_id",
    "target_version", "target_hash"}`` (+ ``models`` for a stage-model pin). Each run must be bound to it (its
    configuration hash, or the changed AIConfiguration field carrying the new value); otherwise
    SYSTEM_CHANGE_EVIDENCE_UNRELATED. Without ``change`` no run can be bound (fail closed)."""
    from aeos.policy.recording import system_change_evidence as impl

    return impl(session, domain=domain, eval_run_ids=eval_run_ids, rollback_target=rollback_target, change=change)


def mci_reference(*, repo: Path | None = None, session: Session | None = None,
                  show_decision_ids: bool = True, settings: Settings | None = None) -> dict[str, Any]:
    """Additive: the MCI_V2 reference exactly as the policy module verifies it (§02, §24, §34): ``status`` is VERIFIED
    only for a declared VERIFIED reference whose LOCAL source (``<data dir>/policy/canonical/`` or aeos.yaml
    ``mci_source_path``; never inside the public repository) matches the recorded sha256; ``verification_code`` says
    why not (NOT_SUPPLIED, REFERENCED_NOT_VERIFIED, UNKNOWN_DECLARED_STATUS, INCOMPLETE_REFERENCE,
    LOCATION_NOT_LOCAL, LOCATION_OUTSIDE_LOCAL_DIR, SOURCE_IN_REPOSITORY, SOURCE_MISSING, HASH_MISMATCH);
    ``compliance`` is always NOT-PROVEN. ``repo`` reads ``<repo>/policy`` instead of the controller's own checkout;
    ``settings`` selects the data dir / aeos.yaml (default: the controller's); ``session`` evaluates the owner's
    per-pack mapping decisions (UNVERIFIED without it)."""
    from aeos.policy.snapshot import mci_reference as impl

    return impl(repo=repo, session=session, show_decision_ids=show_decision_ids, settings=settings)


def mci_source_path(*, repo: Path | None = None, settings: Settings | None = None) -> Path | None:
    """Additive: the declared MCI source file on this machine, or None (nothing declared, or the location is refused:
    not ``local:<file>``, escaping the local directory, or inside the repository)."""
    from aeos.policy.snapshot import mci_source_path as impl

    return impl(repo, settings)


def decision_effective_status(session: Session, decision_id: str) -> str | None:
    """Additive: ACTIVE / EXPIRED / INVALIDATED / SUPERSEDED of a decision as consumers must judge it - a decision past
    its ``expires_at`` is EXPIRED even before the expiry is written back. None when the decision does not exist."""
    from aeos.policy.decisions import effective_status

    d = session.get(Decision, decision_id)
    return effective_status(d) if d is not None else None


def adr_register_status(session: Session, adr_key: str) -> dict[str, Any] | None:
    """Additive (§02 change control): the ADR's builder-written file status vs the owner's recorded ADR decision
    (``effective_status``, ``status_source``, ``owner_status``, ``owner_status_state`` RECORDED|STALE|NOT_RECORDED).
    None when the ADR is not loaded."""
    from sqlalchemy import select

    from aeos.models.decisions import ArchitectureDecisionRecord
    from aeos.policy import adrs

    row = session.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == adr_key))
    if row is None:
        return None
    return adrs.register_status(row, adrs.owner_decisions(session, [adr_key]).get(adr_key, []))


def claude_extra_usage_attestation(session: Session | None) -> dict[str, Any]:
    """Additive (§12): the owner's attestation of the claude.ai paid extra-usage setting, which AEOS cannot read.

    ``state`` is ATTESTED_OFF / ATTESTED_ON from the newest ACTIVE, unexpired human POLICY decision on
    ``claude_extra_usage/PERSONAL`` (preset ``claude_extra_usage_off``), else UNKNOWN. Evidence level E0 (a claim):
    a consumer shows it as CONFIGURED at most, never VERIFIED."""
    from aeos.policy import catalog
    from aeos.policy.decisions import find_active_decision

    spec = catalog.POLICY_PRESETS["claude_extra_usage_off"]
    base: dict[str, Any] = {"state": "UNKNOWN", "decision_id": None, "expires_at": None, "recorded_at": None,
                            "evidence_level": "E0", "target": f"{spec.target_type}/{spec.target_id}",
                            "preset": "claude_extra_usage_off"}
    if session is None:
        return {**base, "reason": "no database: the owner attestation cannot be read"}
    d = find_active_decision(session, kind="POLICY", target_type=spec.target_type, target_id=spec.target_id)
    if d is None or d.actor_kind != "HUMAN" or d.domain != spec.domains[0]:
        return {**base, "reason": "no ACTIVE owner attestation recorded"}
    value = (d.details or {}).get(spec.detail_key)
    if not isinstance(value, bool):
        return {**base, "reason": "attestation without a boolean extra_usage_disabled (ignored, fail closed)"}
    return {**base, "state": "ATTESTED_OFF" if value else "ATTESTED_ON", "decision_id": d.id,
            "expires_at": d.expires_at.isoformat() if d.expires_at else None,
            "recorded_at": d.created_at.isoformat() if d.created_at else None, "reason": "owner attestation (E0)"}


def write_enablement(session: Session, provider: str, *, domain: str) -> dict[str, Any]:
    """§26 external write enablement of *provider* exactly as the interlock evaluates it: effective only with the
    ``write_enabled.<provider>`` flag AND an ACTIVE human WRITE_ENABLEMENT decision on ``provider_write/<provider>``
    (per provider, for every target of it). A provider that is not a write provider of *domain* is NOT_APPLICABLE."""
    from aeos.policy import catalog, enablement

    canonical = catalog.normalize_provider(provider)
    if canonical not in catalog.providers_for(domain):
        return {"provider": canonical, "status": "NOT_APPLICABLE", "decision_id": None, "flag": False,
                "scope": "provider"}
    decision = enablement.active_enablement_decision(session, canonical)
    flag = enablement.flag(session, canonical)
    return {"provider": canonical, "status": "ENABLED" if flag and decision is not None else "NOT_ENABLED",
            "decision_id": decision.id if decision is not None else None, "flag": flag, "scope": "provider"}
