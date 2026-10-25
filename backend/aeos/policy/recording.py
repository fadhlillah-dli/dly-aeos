"""Generic human decisions (EXCEPTION, RISK_ACCEPTANCE, WRITE_ENABLEMENT, POLICY, BUDGET) — shared by the
decisions API, the Decision page and ``aeos policy decide`` (§19, §20, §26, ADR-0031 amendment).

Every kind is recorded with *exactly* the target shape its consumer reads, so a decision either changes
behaviour as the owner intends or is refused — it never sits in the registry looking effective while no
consumer honours it:

* ``WRITE_ENABLEMENT`` → ``provider_write/<provider>`` (+ the ``write_enabled.<provider>`` flag), read by the
  external-write interlock (``aeos.policy.enablement``);
* ``EXCEPTION`` presets (``catalog.EXCEPTION_PRESETS``): ``secret_store/interim-keyring`` with
  ``details.secret_store_interim_keyring = true`` (read by ``aeos.secrets.provider``) and
  ``worker_sandbox/process`` with ``details.allow_process_sandbox = true`` (+ optional ``allow_bash``)
  (read by ``aeos.worker_runtime.sandbox``), PERSONAL only, with an explicit expiry;
* OFFICE company authorisation → ``POLICY`` on ``office_policy/OFFICE`` with ``office_authorization``,
  ``company_reference`` and explicit ``allowed_providers`` (read by ``aeos.policy.office``).

The caller authenticates the human (session + CSRF in the API, password prompt in the CLI); the registry
still re-checks that the actor is an active SUPER_ADMIN whose scope covers the decision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.models.decisions import Decision
from aeos.policy import catalog, enablement
from aeos.policy import decisions as registry

GENERIC_KINDS = catalog.GENERIC_DECISION_KINDS
# Detail keys that carry policy meaning and are set only from the dedicated, validated fields. Smuggling
# them through free-form ``details`` (e.g. ``office_authorization: true`` without a company reference) is
# refused instead of silently granting or relaxing anything (fail closed).
RESERVED_DETAIL_KEYS = frozenset({"office_authorization", "company_reference", "allowed_providers", "office_policy",
                                  "enabled", "provider", "handled_by"})


def _require_target(kind: str, target_type: str | None, target_id: str | None) -> tuple[str, str]:
    if not target_type or not target_id:
        raise ValidationFailed(f"{kind} needs an explicit target_type and target_id")
    return target_type, target_id


def _exception_shape(domain: Domain, *, exception: str | None, target_type: str | None, target_id: str | None,
                     details: dict[str, Any], allow_bash: bool | None,
                     ops: list[str]) -> tuple[str, str, dict[str, Any], list[str]]:
    """Return (target_type, target_id, details, allowed_operations) for an EXCEPTION.

    A preset (or a free-form decision on a preset's target) gets the exact shape its consumer reads; the
    consumer-read detail keys are refused anywhere else so they can never look effective by accident."""
    preset = exception or catalog.exception_preset_for_target(target_type, target_id)
    if preset is None:
        stray = sorted(set(details) & catalog.EXCEPTION_DETAIL_KEYS)
        if stray:
            raise ValidationFailed(f"details {stray} are read only on their exception target; use exception="
                                   f"{sorted(catalog.EXCEPTION_PRESETS)}", code="EXCEPTION_TARGET_MISMATCH",
                                   details={"presets": {k: f"{v.target_type}/{v.target_id}"
                                                        for k, v in catalog.EXCEPTION_PRESETS.items()}})
        if allow_bash is not None:
            raise ValidationFailed("allow_bash applies to the worker_sandbox_process exception only",
                                   code="EXCEPTION_TARGET_MISMATCH")
        tt, tid = _require_target("EXCEPTION", target_type, target_id)
        return tt, tid, details, ops
    spec = catalog.EXCEPTION_PRESETS.get(preset)
    if spec is None:
        raise ValidationFailed(f"unknown exception preset {preset!r}", details={"allowed": sorted(catalog.EXCEPTION_PRESETS)})
    if (target_type and target_type != spec.target_type) or (target_id and target_id != spec.target_id):
        raise ValidationFailed(f"exception {preset} is bound to target {spec.target_type}/{spec.target_id}",
                               code="EXCEPTION_TARGET_MISMATCH")
    if domain.value not in spec.domains:
        # ADR-0042: OFFICE secrets never go to the interim store; ADR-0051: OFFICE is always BLOCKED.
        raise ValidationFailed(f"exception {preset} can only be recorded in {list(spec.domains)}",
                               code="EXCEPTION_DOMAIN_DENIED")
    extra = sorted((set(details) & catalog.EXCEPTION_DETAIL_KEYS) - set(spec.details) - set(spec.flags))
    if extra:
        raise ValidationFailed(f"details {extra} do not belong to exception {preset}", code="EXCEPTION_TARGET_MISMATCH")
    for k, want in spec.details.items():
        if k in details and details[k] is not want:
            raise ValidationFailed(f"exception {preset} needs details.{k} = {str(want).lower()} (boolean)",
                                   code="EXCEPTION_DETAILS_INVALID")
    out = {**details, **dict(spec.details)}
    if "allow_bash" in spec.flags:
        bash = details.get("allow_bash", False) if allow_bash is None else allow_bash
        if not isinstance(bash, bool):
            raise ValidationFailed("allow_bash must be true/false", code="EXCEPTION_DETAILS_INVALID")
        out["allow_bash"] = bash
    out["exception"] = preset
    out["consumer"] = spec.consumer
    return spec.target_type, spec.target_id, out, ops or list(spec.allowed_operations)


def system_change_evidence(session: Session, *, domain: str | None, eval_run_ids: Any,
                           rollback_target: Any) -> dict[str, Any]:
    """§41/§43: validate the evaluation and rollback of a system change (policy/model/tools/hooks/prompt/memory/
    fallback/context strategy). Every named run must be a COMPLETED evaluation whose gates all PASS, planned in this
    domain or the platform; the rollback target names what the change replaces. Returns the stored record."""
    from aeos.models.research import EvalRun

    ids = [str(i).strip() for i in (eval_run_ids if isinstance(eval_run_ids, list) else []) if str(i).strip()]
    rollback = str(rollback_target or "").strip()
    problems: list[str] = []
    if not ids:
        problems.append("no evaluation run (system_change.eval_run_ids)")
    runs = []
    for rid in dict.fromkeys(ids):
        run = session.get(EvalRun, rid)
        if run is None or run.submitted_domain not in (None, domain):
            problems.append(f"evaluation run {rid} not found in this domain")
            continue
        gates = {k: str(v).upper() for k, v in (run.gate_results or {}).items()}
        if run.status != "COMPLETED" or not gates or any(v != "PASS" for v in gates.values()):
            problems.append(f"evaluation run {rid} is {run.status} with gates {gates or 'none'} (needs COMPLETED, "
                            "every gate PASS)")
            continue
        runs.append({"id": run.id, "suite_key": run.suite_key, "configuration_hash": run.configuration_hash})
    if len(rollback) < 3:
        problems.append("no rollback target (system_change.rollback_target: the version/config/decision it replaces)")
    if problems:
        raise ValidationFailed("a system change (§41/§43) needs evaluation and a rollback target: "
                               + "; ".join(problems), code="SYSTEM_CHANGE_EVIDENCE_REQUIRED",
                               details={"problems": problems})
    return {"class": "SYSTEM_CHANGE", "evaluation": "EVALUATED", "eval_runs": runs, "rollback_target": rollback[:256]}


def record_generic_decision(
    session: Session, *, actor_user_id: str, domain: Domain, kind: str, rationale: str,
    auth_scope: dict[str, Any], project_id: str | None = None, target_type: str | None = None,
    target_id: str | None = None, target_version: str | None = None, target_hash: str | None = None,
    allowed_operations: list[str] | None = None, expires_at: datetime | None = None,
    details: dict[str, Any] | None = None, provider: str | None = None, enabled: bool | None = None,
    office_authorization: bool = False, company_reference: str | None = None,
    allowed_providers: list[str] | None = None, office_policy: dict[str, Any] | None = None,
    exception: str | None = None, allow_bash: bool | None = None,
) -> Decision:
    """Validate and record one human decision of a generic kind. Does not commit."""
    if kind not in GENERIC_KINDS:
        raise ValidationFailed(f"kind must be one of {list(GENERIC_KINDS)}")
    if not auth_scope:
        raise ValidationFailed("auth_scope must state the explicit scope of this decision")
    details = dict(details or {})
    reserved = sorted(set(details) & RESERVED_DETAIL_KEYS)
    if reserved:
        raise ValidationFailed(f"details may not set {reserved}; use the dedicated fields of this endpoint",
                               code="RESERVED_DETAIL_KEYS", details={"reserved": reserved})
    if exception is not None and kind != "EXCEPTION":
        raise ValidationFailed("exception presets are EXCEPTION decisions", code="EXCEPTION_TARGET_MISMATCH")
    dedicated = catalog.DEDICATED_TARGETS.get(str(target_type or ""))
    if dedicated:
        # office_route / workforce decisions are recorded only where their evidence is checked (company
        # authorisation + approved route, passing evaluation + DecisionRecord); never through this endpoint.
        raise ValidationFailed(f"decisions on {target_type} are recorded only through {dedicated}",
                               code="DEDICATED_ENDPOINT_REQUIRED", details={"endpoint": dedicated})
    ops = list(allowed_operations or [])
    supersede: list[Decision] = []

    if kind == "EXCEPTION":
        target_type, target_id, details, ops = _exception_shape(
            domain, exception=exception, target_type=target_type, target_id=target_id, details=details,
            allow_bash=allow_bash, ops=ops)
        if expires_at is None:
            raise ValidationFailed("EXCEPTION decisions need an explicit expiry (expires_at)")
        if exception is not None or catalog.exception_preset_for_target(target_type, target_id):
            supersede = list(session.scalars(select(Decision).where(
                Decision.kind == "EXCEPTION", Decision.target_type == target_type, Decision.target_id == target_id,
                Decision.domain == domain.value, Decision.status == "ACTIVE")))
    elif kind == "RISK_ACCEPTANCE":
        target_type, target_id = _require_target(kind, target_type, target_id)
        if expires_at is None:
            raise ValidationFailed("RISK_ACCEPTANCE decisions need an explicit expiry (expires_at)")
    elif kind == "WRITE_ENABLEMENT":
        provider = catalog.normalize_provider(provider or target_id)
        if provider not in catalog.providers_for(domain):
            raise ValidationFailed(f"provider {provider!r} is not allowed in domain {domain.value}",
                                   details={"allowed": list(catalog.providers_for(domain))})
        if enabled is None:
            raise ValidationFailed("WRITE_ENABLEMENT needs enabled=true|false")
        target_type, target_id = catalog.TARGET_PROVIDER_WRITE, provider
        details["enabled"] = bool(enabled)
        details["provider"] = provider
        ops = list(catalog.operations_for_provider(provider)) if enabled else []
        supersede = list(session.scalars(select(Decision).where(
            Decision.kind == "WRITE_ENABLEMENT", Decision.target_type == target_type,
            Decision.target_id == provider, Decision.status == "ACTIVE")))
    elif kind == "POLICY":
        if office_authorization or office_policy:
            if domain != Domain.OFFICE:
                raise ValidationFailed("OFFICE policy decisions are recorded in the OFFICE domain only")
            if office_policy:
                unknown = sorted(set(office_policy) - set(catalog.OFFICE_POLICY_KEYS))
                if unknown:
                    raise ValidationFailed(f"unknown office policy keys {unknown}",
                                           details={"allowed": list(catalog.OFFICE_POLICY_KEYS)})
                details["office_policy"] = office_policy
            if office_authorization:
                if not company_reference or len(company_reference.strip()) < 5:
                    raise ValidationFailed("company authorisation needs a company_reference (approval artefact)")
                given = [catalog.normalize_provider(p) for p in allowed_providers or []]
                providers = [p for p in given if p in catalog.providers_for(Domain.OFFICE)]
                if not providers or len(providers) != len(given):
                    raise ValidationFailed("allowed_providers must list OFFICE providers explicitly",
                                           details={"allowed": list(catalog.providers_for(Domain.OFFICE))})
                details.update({"office_authorization": True, "company_reference": company_reference.strip(),
                                "allowed_providers": providers})
            target_type, target_id = catalog.TARGET_OFFICE_POLICY, Domain.OFFICE.value
        else:
            target_type, target_id = _require_target(kind, target_type, target_id)
        given = details.pop("system_change", None)
        if target_type in catalog.SYSTEM_CHANGE_TARGET_TYPES:
            sc = given if isinstance(given, dict) else {}
            details["system_change"] = system_change_evidence(
                session, domain=domain.value, eval_run_ids=sc.get("eval_run_ids"),
                rollback_target=sc.get("rollback_target"))
        else:
            # governance/convention policy: not an agent/model configuration change; stated, never implied evaluated
            details["system_change"] = {"class": "NOT_A_SYSTEM_CHANGE", "evaluation": "NOT_APPLICABLE",
                                        "note": f"{target_type} is not a policy/model/tool/hook/prompt/memory/"
                                                "fallback/context-strategy target"}
    else:  # BUDGET: semantics owned by the cost module; the registry records the human decision.
        target_type, target_id = _require_target(kind, target_type, target_id)
        details.setdefault("handled_by", "aeos.cost")

    decision = registry.record_decision(
        session, kind=kind, actor_user_id=actor_user_id, target_type=target_type, target_id=target_id,
        target_version=target_version, target_hash=target_hash, allowed_operations=ops, auth_scope=auth_scope,
        project_id=project_id, domain=domain.value, rationale=rationale, expires_at=expires_at, details=details,
    )
    for old in supersede:
        registry.supersede_decision(session, old, new_decision_id=decision.id, reason=f"new {kind} decision",
                                    actor_type="user", actor_id=actor_user_id)
    if kind == "WRITE_ENABLEMENT":
        enablement.set_flag(session, target_id, bool(enabled))
    return decision
