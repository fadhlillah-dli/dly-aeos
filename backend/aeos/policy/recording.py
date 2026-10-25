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
  ``company_reference`` and explicit ``allowed_providers`` (read by ``aeos.policy.office``), plus the optional
  structured scope ``approved_targets`` / ``approved_data_classes`` / ``approved_identities``
  (``aeos.policy.office_scope``; absent = UNDECIDED, consumers fail closed).

The caller authenticates the human (session + CSRF in the API, password prompt in the CLI); the registry
still re-checks that the actor is an active SUPER_ADMIN whose scope covers the decision.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.models.decisions import Decision
from aeos.policy import catalog, enablement, office_scope
from aeos.policy import decisions as registry

GENERIC_KINDS = catalog.GENERIC_DECISION_KINDS
# Detail keys that carry policy meaning and are set only from the dedicated, validated fields. Smuggling
# them through free-form ``details`` (e.g. ``office_authorization: true`` without a company reference) is
# refused instead of silently granting or relaxing anything (fail closed).
RESERVED_DETAIL_KEYS = frozenset({"office_authorization", "company_reference", "allowed_providers", "office_policy",
                                  "enabled", "provider", "handled_by", "approved_targets", "approved_data_classes",
                                  "approved_identities"})


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


# Values that never bind evidence to a change (an UNKNOWN model/prompt matches nothing, §34).
_UNBOUND_VALUES = frozenset({"", "unknown", "none", "null", "n/a", "-"})
# Why a supervisor stage-model pin cannot be evidenced in this build (shown with every refused pin).
SUPERVISOR_EVAL_PATH_NOTE = (
    "this build has no evaluation path for a supervisor (Claude) model: EvalRuns execute only on broker-served "
    "WORKER/ARENA routes and a run must state exactly its route's model_id (EVAL_CONFIGURATION_MISMATCH), while the "
    "Claude Max supervisor is never served through the broker (master s12) - a stage-model pin stays refused until a "
    "supervisor evaluation path exists; planning keeps the baseline alias")
# Why a memory acceptance cannot be evidenced in this build (shown with every refused memory evaluation).
MEMORY_EVAL_PATH_NOTE = (
    "context packs carry ACCEPTED memory only, so a proposed record cannot reach a worker's context in this build: "
    "evaluating a memory acceptance needs the owner-authored memory/retrieval fixture suite (ADR-0060, ADR-0081); "
    "until then the acceptance is recorded NOT_EVALUATED (stated)")


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip().lower()


def _strings(value: Any) -> set[str]:
    """Every string leaf of a JSON value, normalised (context parameters may nest)."""
    if isinstance(value, dict):
        return {s for v in value.values() for s in _strings(v)}
    if isinstance(value, list):
        return {s for v in value for s in _strings(v)}
    return {_norm(value)} - _UNBOUND_VALUES if isinstance(value, (str, int, float)) and not isinstance(value, bool) \
        else set()


def _carries(cfg: dict[str, Any], field: str, wanted: str) -> bool:
    """Whether the evaluated AIConfiguration carries *wanted* in *field* (exact, normalised)."""
    if field == "tools":
        tools = {_norm(t) for t in cfg.get("tools") or [] if isinstance(t, str)} - _UNBOUND_VALUES
        given = {t for t in re.split(r"[\s,]+", wanted) if t} - _UNBOUND_VALUES
        return bool(tools) and tools == given
    if field == "context_params":
        return wanted in _strings(cfg.get("context_params"))
    if field == "harness_version":
        harness, version = _norm(cfg.get("harness")), _norm(cfg.get("harness_version"))
        return version not in _UNBOUND_VALUES and wanted in {version, f"{harness} {version}", f"{harness}@{version}"}
    return _norm(cfg.get(field)) == wanted


def change_of(*, target_type: str | None, target_id: str | None, target_version: str | None = None,
              target_hash: str | None = None, models: Any = None) -> dict[str, Any]:
    """The identity of a system change as the decision states it (what its evaluation must be evidence of)."""
    out: dict[str, Any] = {"target_type": target_type, "target_id": target_id, "target_version": target_version,
                           "target_hash": target_hash}
    if catalog.is_stage_model_pin(target_type, target_id):
        out["models"] = {str(k): str(v) for k, v in models.items()} if isinstance(models, dict) else {}
    return out


def _pinned(change: dict[str, Any]) -> set[str] | None:
    if not catalog.is_stage_model_pin(change.get("target_type"), change.get("target_id")):
        return None
    return {_norm(m) for m in (change.get("models") or {}).values()} - _UNBOUND_VALUES


def _bind_memory(run: Any, change: dict[str, Any]) -> tuple[str | None, str]:
    """A memory acceptance (§41/§43 "prompt/memory ... system change") is evidenced only by a run whose executed tasks
    had exactly this record in their worker context: every collected sample's ``context_memory_ids`` (derived by the
    evaluation collector from the context pack each attempt used) names it. The run's stated ``context_params`` alone
    never bind - they describe an intention, not what reached the model."""
    record = _norm(change.get("target_id"))
    if record in _UNBOUND_VALUES:
        return None, "the change names no memory record"
    samples = [s for s in (run.samples or []) if isinstance(s, dict) and isinstance(s.get("context_memory_ids"), list)]
    if not samples:
        return None, ("no collected sample records which memory reached its tasks' context (collector "
                      "context_memory_ids); a stated context_params value is not evidence - " + MEMORY_EVAL_PATH_NOTE)
    missing = [str(s.get("sample_id") or s.get("task_id")) for s in samples
               if record not in {_norm(i) for i in s["context_memory_ids"]}]
    if missing:
        return None, (f"memory record {change.get('target_id')} did not reach the worker context of sample(s) "
                      f"{missing[:5]} - " + MEMORY_EVAL_PATH_NOTE)
    return f"context_memory_ids={record}", ""


def _bind(run: Any, change: dict[str, Any]) -> tuple[str | None, str]:
    """(how *run* is bound to *change*, or None + why not). See ``catalog.SYSTEM_CHANGE_CONFIG_FIELDS``."""
    if catalog.normalize_target_type(change.get("target_type")) in catalog.IDENTITY_BOUND_TARGETS:
        return _bind_memory(run, change)
    cfg = run.configuration if isinstance(run.configuration, dict) else {}
    pinned = _pinned(change)
    if pinned is not None:  # stage-model pin: only a run of exactly a pinned model is evidence of it
        evaluated = {_norm(cfg.get("model_id")), _norm(cfg.get("model_version"))} - _UNBOUND_VALUES
        hit = sorted(evaluated & pinned)
        if hit:
            return f"model_id={hit[0]}", ""
        return None, (f"evaluated model {cfg.get('model_id')!r} is none of the pinned models "
                      f"{sorted(pinned) or '(details.models names none)'}")
    want_hash = _norm(change.get("target_hash"))
    if want_hash not in _UNBOUND_VALUES and want_hash in {_norm(run.configuration_hash), _norm(run.route_config_hash)}:
        return f"configuration_hash={change.get('target_hash')}", ""
    ttype = catalog.normalize_target_type(change.get("target_type"))
    fields = catalog.SYSTEM_CHANGE_CONFIG_FIELDS.get(ttype, ())
    if not fields:
        return None, (f"{ttype or 'the target'} has no AIConfiguration field: bind it with target_hash = the run's "
                      f"configuration_hash ({run.configuration_hash})")
    wanted = [w for w in [_norm(change.get("target_version"))] if w not in _UNBOUND_VALUES]
    if not wanted:
        return None, (f"the decision names no new value: target_version = the new {'/'.join(fields)} (or "
                      f"target_hash = the run's configuration_hash {run.configuration_hash})")
    for field in fields:
        for value in wanted:
            if _carries(cfg, field, value):
                return f"{field}={value}", ""
    shown = ", ".join(f"{f}={cfg.get(f)!r}" for f in fields)
    return None, f"its configuration ({shown}) does not carry {wanted[0] if len(wanted) == 1 else wanted}"


def system_change_evidence(session: Session, *, domain: str | None, eval_run_ids: Any,
                           rollback_target: Any, change: dict[str, Any] | None = None) -> dict[str, Any]:
    """§41/§43: validate the evaluation and rollback of a system change (policy/model/tools/hooks/prompt/memory/
    fallback/context strategy). Every named run must be a COMPLETED evaluation whose gates all PASS, planned in this
    domain or the platform, and bound to exactly this *change* (``change_of``: target type/id/version/hash and, for a
    stage-model pin, every pinned model id); the rollback target names what the change replaces. Returns the stored
    record. Missing/unfinished evidence -> SYSTEM_CHANGE_EVIDENCE_REQUIRED; evidence of another configuration ->
    SYSTEM_CHANGE_EVIDENCE_UNRELATED (an unstated change is never bound, fail closed)."""
    from aeos.models.research import EvalRun

    ids = [str(i).strip() for i in (eval_run_ids if isinstance(eval_run_ids, list) else []) if str(i).strip()]
    rollback = str(rollback_target or "").strip()
    problems: list[str] = []
    if not ids:
        problems.append("no evaluation run (system_change.eval_run_ids)")
    valid = []
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
        valid.append(run)
    if len(rollback) < 3:
        problems.append("no rollback target (system_change.rollback_target: the version/config/decision it replaces)")
    if problems:
        raise ValidationFailed("a system change (§41/§43) needs evaluation and a rollback target: "
                               + "; ".join(problems), code="SYSTEM_CHANGE_EVIDENCE_REQUIRED",
                               details={"problems": problems})
    unrelated: list[str] = []
    runs: list[dict[str, Any]] = []
    if change is None:
        unrelated.append("the change this evaluation is evidence of is not stated")
    else:
        for run in valid:
            how, why = _bind(run, change)
            if how is None:
                unrelated.append(f"evaluation run {run.id} ({run.suite_key}) is not evidence of this change: {why}")
                continue
            cfg = run.configuration if isinstance(run.configuration, dict) else {}
            runs.append({"id": run.id, "suite_key": run.suite_key, "configuration_hash": run.configuration_hash,
                         "model_id": cfg.get("model_id"), "binding": how})
        pinned = _pinned(change)
        if pinned is not None:
            covered = {r["binding"].split("=", 1)[1] for r in runs}
            unrelated += [f"pinned model {m} has no passing evaluation run of exactly that model"
                          for m in sorted(pinned - covered)]
            if pinned - covered:
                # stated, not implied (§02): a run's model is the model its route executes (evaluation start refuses
                # a mismatch), and no broker-served route runs a supervisor model (§12)
                unrelated.append(SUPERVISOR_EVAL_PATH_NOTE)
            if not pinned:
                unrelated.append("a stage-model pin names its models (details.models {STAGE: model id})")
    if unrelated:
        raise ValidationFailed("the evaluation runs of a system change (§41/§43) must be evidence of exactly this "
                               "change: " + "; ".join(unrelated), code="SYSTEM_CHANGE_EVIDENCE_UNRELATED",
                               details={"problems": unrelated, "change": change,
                                        "binding": "target_hash = the run's configuration_hash, or target_version = "
                                                   "the new value of the changed AIConfiguration field; a stage-model "
                                                   "pin needs a run of every pinned model"})
    return {"class": "SYSTEM_CHANGE", "evaluation": "EVALUATED", "eval_runs": runs, "rollback_target": rollback[:256],
            "bound_to": {k: v for k, v in (change or {}).items() if v not in (None, "", {})}}


def record_generic_decision(
    session: Session, *, actor_user_id: str, domain: Domain, kind: str, rationale: str,
    auth_scope: dict[str, Any], project_id: str | None = None, target_type: str | None = None,
    target_id: str | None = None, target_version: str | None = None, target_hash: str | None = None,
    allowed_operations: list[str] | None = None, expires_at: datetime | None = None,
    details: dict[str, Any] | None = None, provider: str | None = None, enabled: bool | None = None,
    office_authorization: bool = False, company_reference: str | None = None,
    allowed_providers: list[str] | None = None, office_policy: dict[str, Any] | None = None,
    exception: str | None = None, allow_bash: bool | None = None,
    approved_targets: list[Any] | None = None, approved_data_classes: list[str] | None = None,
    approved_identities: dict[str, str] | list[str] | None = None,
) -> Decision:
    """Validate and record one human decision of a generic kind. Does not commit.

    ``approved_targets`` / ``approved_data_classes`` / ``approved_identities`` belong to the OFFICE company
    authorisation only (``aeos.policy.office_scope``): what the company approved, never inferred."""
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
    scoped = bool(approved_targets or approved_data_classes or approved_identities)
    if scoped and not (kind == "POLICY" and office_authorization):
        raise ValidationFailed("approved_targets / approved_data_classes / approved_identities are part of the OFFICE "
                               "company authorisation (office_authorization=true)", code="OFFICE_SCOPE_WITHOUT_AUTHORISATION")
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
                # §01 U-03 / §07 step 10 / §51 P1: the company's approved targets, data classes and identities.
                # Optional at recording; absent stays UNDECIDED and every consumer fails closed (office_scope).
                details.update({
                    "approved_targets": office_scope.normalize_targets(approved_targets, allowed_providers=providers),
                    "approved_data_classes": office_scope.normalize_data_classes(approved_data_classes),
                    "approved_identities": office_scope.normalize_identities(approved_identities),
                })
            target_type, target_id = catalog.TARGET_OFFICE_POLICY, Domain.OFFICE.value
        else:
            target_type, target_id = _require_target(kind, target_type, target_id)
        given = details.pop("system_change", None)
        if catalog.system_change_target(target_type):
            sc = given if isinstance(given, dict) else {}
            ids = list(sc.get("eval_run_ids") or []) if isinstance(sc.get("eval_run_ids"), list) else []
            if details.get("eval_run_id"):
                # a consumer that reads details.eval_run_id (planning_baseline/stage_models) is held to the same
                # validated run: it is part of the system-change evidence, never a second unchecked reference
                ids.append(str(details["eval_run_id"]))
            details["system_change"] = system_change_evidence(
                session, domain=domain.value, eval_run_ids=ids, rollback_target=sc.get("rollback_target"),
                change=change_of(target_type=target_type, target_id=target_id, target_version=target_version,
                                 target_hash=target_hash, models=details.get("models")))
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
