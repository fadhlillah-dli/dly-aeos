"""Worker model routing (§23, §42, §43) — hard eligibility first, then evaluated selection.

Stage 1 (hard filters, each rejection carries a reason):
  status APPROVED with an ACTIVE human ROUTE_ACTIVATION decision bound to the current config hash,
  or TRIAL with an ACTIVE TRIAL decision bound to it; the runtime the activation was bound to
  (policy version, worker prompt revision, context strategy) is still the one in force; domain
  eligible (OFFICE additionally needs the office policy: external_worker_allowed + route listed in
  approved_worker_routes; policy unavailable -> deny); data classification allowed; identity
  UNVERIFIED -> PUBLIC data only; tool support proven when tools are needed and every TaskSpec tool
  inside the route's reviewed tool set; context window known and large enough; catalog availability
  observed (UNKNOWN and MISSING are rejected); region/residency reviewed where the provider restricts
  regions; quota not EXHAUSTED; pricing/terms freshness; paid (or free status unknown/expired) -> also
  an ACTIVE BUDGET decision referenced by the activation.
Stage 2 (selection): recorded eval results for the task role whose config hash matches the
  current configuration (a changed configuration never inherits an old baseline), otherwise the
  deterministic priority. Eval results are written onto the route only by the owner's APPROVED
  activation, which must reference completed, gate-passing EvalRuns of exactly this configuration
  (§23 "route produksi hanya menggunakan konfigurasi yang diizinkan dan telah diuji"). The model
  never selects itself; TaskSpec route policy can only narrow.
Fallback: only along the failed route's ``fallback_chain``, only to routes that are themselves
  eligible, same pool, free — never a paid fallback and never a supervisor downgrade.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import DataClassification, Domain
from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.cost import QuotaState
from aeos.models.decisions import Decision
from aeos.models.planning import TaskSpec
from aeos.models.routing import ModelRoute
from aeos.routing.facade import RouteDecision

log = logging.getLogger("aeos.routing")

TARGET_TYPE = "model_route"
WORKER_POOLS = ("WORKER", "COMPARATOR", "ARENA", "LOCAL", "WATCHLIST")
ALL_POOLS = ("SUPERVISOR", *WORKER_POOLS)
TERMS_MAX_AGE = timedelta(days=30)
TRIAL_DEFAULT_DAYS = 14
TRIAL_MAX_DAYS = 90
APPROVAL_MAX_DAYS = 365
RESTRICTED_TERMS_KEYS = ("may_use_inputs_for_training", "trial_only_no_confidential")
# Pools the worker runtime executes through the broker (the broker serves WORKER routes only) and whose
# activation is therefore bound to the worker runtime fingerprint (prompt revision, context strategy).
WORKER_EXECUTED_POOLS = ("WORKER",)
# TaskSpec tool vocabulary a route may be reviewed for (planning DEFAULT_TOOLS + the OpenCode adapter's
# explicit grants). The default reviewed set is the planning default; bash/webfetch need an explicit review.
REVIEWED_WORKER_TOOLS = ("fs.read", "fs.write:allowed_paths", "tests.run")
TOOL_VOCABULARY = frozenset({*REVIEWED_WORKER_TOOLS, "bash", "webfetch"})
PLACEHOLDER_TOOL_SETS = frozenset({"opencode-default"})
# Providers whose availability a catalog refresh observes (routing.catalog: OpenRouter models, `opencode models`,
# the OpenCode Go model list with the workers/opencode-go key - ADR-0041 amendment 2026-10-26).
CATALOG_OBSERVED_PROVIDERS = frozenset({"openrouter", "opencode-zen", "opencode-go"})
_REGION = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,31}$")
# TaskSpec model_route_policy keys: consumed by routing, or constraints routing always enforces (a TaskSpec
# asking to relax one of them is refused rather than silently ignored).
POLICY_KEYS_CONSUMED = frozenset({"pool", "requires_tools", "needs_tools", "min_context_tokens", "min_context",
                                  "routes", "primary"})
POLICY_KEYS_ENFORCED = {"eligible_routes_only": True, "paid_fallback": False, "supervisor_downgrade": False}
POLICY_KEYS_INFORMATIONAL = frozenset({"notes", "office_external_allowed"})
# ADR-0041 amendment 2026-10-25: a provider rule on WHICH CLIENT may call a route (S07: "Free-tier chat models require
# OpenCode; use paid models or Go from other clients") lives in ``data_terms.client_restriction``. The broker is its
# own HTTP client, so such a route is neither activated nor proxied until the owner records a decision for it.
CLIENT_TERMS_ACCEPTED = "OWNER_ACCEPTED_BROKER_CLIENT"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOCAL_BUILD_KEYS = ("quantization", "inference_backend", "hardware")  # §23: a LOCAL build is its own configuration

# Fields that make up the AIConfiguration identity (§43). Status/timestamps/eval are excluded so
# that refreshing a catalog without any change keeps the identity (and approvals) stable.
CONFIG_FIELDS = (
    "route_key", "pool", "provider", "endpoint", "model_id", "alias", "resolved_version", "mode_params", "harness",
    "harness_version", "prompt_revision", "tool_set", "policy_version", "context_strategy", "fallback_chain",
    "is_free", "pricing", "data_terms", "region", "eligible_domains", "allowed_data_classes", "context_window",
    "supports_tools", "identity_status", "free_until",
)


# ------------------------------------------------------------------------------ identity


_LIST_FIELDS = frozenset({"tool_set", "fallback_chain", "eligible_domains", "allowed_data_classes"})
_DICT_FIELDS = frozenset({"mode_params", "pricing", "data_terms"})
_SCALAR_DEFAULTS = {"supports_tools": "UNKNOWN", "is_free": "UNKNOWN", "identity_status": "PROVIDER_CLAIMED"}


def route_config(route: ModelRoute) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for f in CONFIG_FIELDS:
        v = getattr(route, f)
        # column defaults are applied only at flush: None and empty containers are the same identity
        if v is None:
            if f in _LIST_FIELDS:
                v = []
            elif f in _DICT_FIELDS:
                v = {}
            else:
                v = _SCALAR_DEFAULTS.get(f)
        if f == "data_terms" and isinstance(v, dict):
            v = {k: vv for k, vv in v.items() if not k.endswith("_at") and k not in {"catalog", "checked_by"}}
        if isinstance(v, datetime):
            v = ensure_aware(v).date().isoformat()  # type: ignore[union-attr]
        cfg[f] = v
    return cfg


def compute_config_hash(route: ModelRoute) -> str:
    return content_hash(route_config(route))


def refresh_config_hash(session: Session, route: ModelRoute, *, reason: str, readiness: bool = True) -> bool:
    """Recompute the identity. Returns True when a previously known identity changed: approvals
    bound to the old hash stop matching, eval baselines are not inherited, Doctor checks go STALE.

    ``readiness=False`` (supervisor prompt/policy/context binding only): the change does not touch the bridge, so the
    bridge capability proof is not invalidated; the configuration change itself is recorded the same way."""
    new = compute_config_hash(route)
    old = route.config_hash
    if old == new:
        return False
    route.config_hash = new
    if old is None:
        return False
    route.config_changed_at = utcnow()
    try:
        from aeos.policy.facade import invalidate_decisions

        invalidate_decisions(session, target_type=TARGET_TYPE, target_id=route.route_key,
                             reason=f"route configuration changed: {reason}")
    except (ImportError, ModuleNotFoundError):
        pass  # hash binding already makes old approvals ineffective
    if readiness:
        notify_readiness(session, route.route_key, reason=reason, pool=route.pool)
    # §47/§43 "re-evaluated when the model changes": roles that had a passing evaluation of the previous
    # configuration now need a new one; the result is never inherited (eval_score checks the hash).
    tested_roles = sorted(role for role, e in (route.eval_results or {}).items()
                          if isinstance(e, dict) and e.get("config_hash") == old)
    try:
        from aeos.events.service import emit

        emit(session, source="routing", kind="route.config_changed", entity_type=TARGET_TYPE,
             entity_id=route.route_key, entity_version=new, payload={"reason": reason, "previous_hash": old})
        if tested_roles:
            emit(session, source="routing", kind="route.needs_evaluation", entity_type=TARGET_TYPE,
                 entity_id=route.route_key, entity_version=new,
                 payload={"roles": tested_roles, "previous_hash": old, "reason": reason,
                          "remedy": "plan an EvalRun of the new configuration before an APPROVED re-activation"})
    except Exception:  # pragma: no cover
        log.exception("emit route.config_changed failed")
    return True


READINESS_INVALIDATORS = ("config:routes", "model:route")


def readiness_invalidators(pool: str | None) -> tuple[str, ...]:
    """``config:routes`` + ``model:route`` for every route change, plus ``model:route:<POOL>`` so a check can depend
    on one pool only (the Claude bridge proof on SUPERVISOR routes). Unknown pool -> wildcard (conservative)."""
    return (*READINESS_INVALIDATORS, f"model:route:{pool.upper()}" if pool else "model:route:*")


def notify_readiness(session: Session, route_key: str, *, reason: str, pool: str | None = None) -> int:
    """§08/§50 "Model alias/provider route berubah": a route status, pin (resolved model) or configuration
    change (eligible_domains, data terms, ...) marks the dependent Doctor checks STALE in the same
    transaction, so CAN_RUN_WORKER / review gates stop being READY until rechecked."""
    try:
        from aeos.doctor.facade import invalidate
    except (ImportError, ModuleNotFoundError):
        return 0
    if pool is None:
        pool = session.scalar(select(ModelRoute.pool).where(ModelRoute.route_key == route_key))
    return sum(invalidate(session, invalidator=inv, reason=f"{route_key}: {reason}"[:200])
               for inv in readiness_invalidators(pool))


def get_route(session: Session, route_key: str) -> ModelRoute:
    route = session.scalars(select(ModelRoute).where(ModelRoute.route_key == route_key)).first()
    if route is None:
        raise NotFound(f"route {route_key} not found")
    return route


# ------------------------------------------------------------------------------ decisions


def _decision_valid(d: Decision | None, *, kind: str, config_hash: str | None) -> bool:
    if d is None or d.kind != kind or d.status != "ACTIVE" or (d.actor_kind or "HUMAN") != "HUMAN":
        return False
    if d.expires_at is not None and ensure_aware(d.expires_at) <= utcnow():  # type: ignore[operator]
        return False
    return config_hash is None or d.target_hash == config_hash


def decision_effective(d: Decision, route: ModelRoute) -> bool:
    """Whether *d* currently authorises *route*: ACTIVE, human, unexpired and bound to the hash of the
    route's CURRENT configuration (what the dashboard may show as approved — nothing else)."""
    if d.kind not in {"ROUTE_ACTIVATION", "TRIAL"}:
        return False
    return _decision_valid(d, kind=d.kind, config_hash=compute_config_hash(route))


def find_route_decision(session: Session, *, kind: str, route: ModelRoute) -> Decision | None:
    """ACTIVE human decision of *kind* bound to the route's CURRENT configuration, else None.

    The hash is recomputed from the live record, so a configuration edited without going through
    ``refresh_config_hash`` can never keep an older approval effective."""
    current = compute_config_hash(route)
    d: Decision | None = None
    try:
        from aeos.policy.facade import find_active_decision

        d = find_active_decision(session, kind=kind, target_type=TARGET_TYPE, target_id=route.route_key,
                                 target_hash=current)
    except (ImportError, ModuleNotFoundError):
        # Decision registry implementation not installed: strict read-only lookup of the canonical
        # table (still human-only, ACTIVE, unexpired, exact hash). Creation always goes via policy.
        rows = session.scalars(
            select(Decision).where(Decision.kind == kind, Decision.target_type == TARGET_TYPE,
                                   Decision.target_id == route.route_key, Decision.status == "ACTIVE")
            .order_by(Decision.created_at.desc())
        )
        d = next((r for r in rows if _decision_valid(r, kind=kind, config_hash=current)), None)
    return d if _decision_valid(d, kind=kind, config_hash=current) else None


def budget_decision_valid(session: Session, decision_id: str | None, route: ModelRoute) -> tuple[bool, str]:
    if not decision_id:
        return False, "an explicit BUDGET decision id is required"
    d = session.get(Decision, decision_id)
    if not _decision_valid(d, kind="BUDGET", config_hash=None):
        return False, "budget decision is missing, not ACTIVE, not human or expired"
    assert d is not None
    routes = (d.auth_scope or {}).get("routes") or []
    covers = (d.target_type == TARGET_TYPE and d.target_id == route.route_key) or route.route_key in routes
    if not covers:
        return False, "budget decision does not cover this route"
    return True, ""


# ------------------------------------------------------------------------------ helpers


def route_use_problem(session: Session, route: ModelRoute) -> str | None:
    """Per-request authorisation of an admitted route (broker, §23); see ``facade.route_use_problem``."""
    if route.status not in {"APPROVED", "TRIAL"}:
        return f"route status {route.status}"
    kind = "ROUTE_ACTIVATION" if route.status == "APPROVED" else "TRIAL"
    decision = find_route_decision(session, kind=kind, route=route)
    if decision is None:
        return (f"route {route.status} has no ACTIVE human {kind} decision bound to its current configuration "
                "(configuration changed, approval expired or revoked)")
    restriction = client_restriction_problem(route)
    if restriction:
        return restriction
    if needs_budget(route):
        ok, why = budget_decision_valid(session, (decision.details or {}).get("budget_decision_id"), route)
        if not ok:
            if is_subscription(route):
                return f"{budget_label(route)} has no valid budget decision: {why}"
            label = "free period ended" if route.is_free == "YES" else f"is_free={route.is_free}"
            return f"route is no longer free ({label}) and has no valid budget decision: {why}"
    return None


def is_effectively_free(route: ModelRoute, now: datetime | None = None) -> bool:
    if route.is_free != "YES":
        return False
    until = ensure_aware(route.free_until)
    return until is None or until > (now or utcnow())


def is_local_loopback(route: ModelRoute) -> bool:
    """A LOCAL route served by this host (provider ``local``, owner-set ``http://`` loopback endpoint, no credential):
    a call never leaves the machine and never spends money through the broker (§23 LOCAL; the probe's rule)."""
    if route.pool != "LOCAL" or route.provider != "local":
        return False
    u = urlparse(route.endpoint or "")
    return u.scheme == "http" and u.hostname in LOOPBACK_HOSTS


def needs_budget(route: ModelRoute, now: datetime | None = None) -> bool:
    """Whether using *route* needs an ACTIVE BUDGET decision: anything not effectively free, except a loopback
    LOCAL route (local compute is the owner's own hardware, not a paid upstream). A flat-fee SUBSCRIPTION route needs
    it too: its plan fee is paid (ADR-0080 amendment 2026-10-26)."""
    return not is_effectively_free(route, now) and not is_local_loopback(route)


def is_subscription(route: ModelRoute) -> bool:
    """The route's pricing declares a flat-fee subscription (``pricing.billing``; the cost module checks its terms)."""
    return isinstance(route.pricing, dict) and route.pricing.get("billing") == "SUBSCRIPTION"


def budget_label(route: ModelRoute) -> str:
    """Budget copy: a SUBSCRIPTION route's BUDGET decision covers its monthly plan fee, not per-token spend (its usage
    is plan allowance, counted per provider window by the cost module; ADR-0080 amendment 2026-10-26)."""
    if not is_subscription(route):
        return "paid route"
    pricing = route.pricing or {}
    fee = pricing.get("monthly_fee_usd")
    plan = pricing.get("plan") or "plan"
    return (f"subscription route ({plan}, {'$' + str(fee) if fee is not None else 'fee UNKNOWN'}/month plan fee; "
            "the BUDGET decision covers the fee, usage is plan allowance)")


def client_restriction(route: ModelRoute) -> dict[str, Any] | None:
    cr = (route.data_terms or {}).get("client_restriction")
    return cr if isinstance(cr, dict) and cr else None


def client_restriction_problem(route: ModelRoute) -> str | None:
    """Why the broker may not call *route* as its own client (None = no restriction, or the owner decided).

    The decision lives in ``data_terms.client_restriction.owner_decision`` - part of the configuration identity, so it
    is effective only with the human activation decision bound to that hash (never by editing the row)."""
    cr = client_restriction(route)
    if cr is None or cr.get("owner_decision") == CLIENT_TERMS_ACCEPTED:
        return None
    return (f"provider client restriction ({cr.get('source') or 'provider terms'}): "
            f"{cr.get('statement') or 'the provider restricts which client may call this route'} The AEOS broker "
            "calls the upstream as its own HTTP client, not OpenCode: no owner decision is recorded for this route "
            "(options: the seeded OpenCode Go routes opencode-go/* (owner decision A), a paid route with a BUDGET "
            "decision, or an explicit client-terms decision at activation; ADR-0041 amendments 2026-10-25/26)")


def request_shape_rejections(route: ModelRoute) -> list[str]:
    """The broker's verdict on the upstream request it would build (superseded seed endpoint, provider host pin,
    unsupported API, invalid reasoning level): a route the broker refuses is never selected (ADR-0041)."""
    if route.provider == "claude-native" or route.pool == "SUPERVISOR":
        return []
    try:
        from aeos.broker.facade import upstream_request_shape
    except (ImportError, ModuleNotFoundError):
        return ["broker unavailable: the upstream request shape cannot be checked (fail closed)"]
    shape = upstream_request_shape(route)
    return [f"{shape['code']}: {shape['problem']}"] if shape.get("code") else []


def _office_policy(session: Session) -> tuple[dict[str, Any] | None, str]:
    try:
        from aeos.policy.facade import office_policy

        return office_policy(session), ""
    except (ImportError, ModuleNotFoundError):
        return None, "office policy unavailable (aeos.policy not installed) - OFFICE denied"
    except Exception as exc:  # pragma: no cover - fail closed
        return None, f"office policy could not be evaluated ({type(exc).__name__}) - OFFICE denied"


def _office_data_class_rejections(session: Session, office: dict[str, Any], data_classification: str) -> list[str]:
    """§01 U-03 / §51 P1: OFFICE data of a class the company did not approve never reaches a model route."""
    try:
        from aeos.policy.facade import office_data_class_authorised

        ok, why = office_data_class_authorised(session, data_classification, policy=office)
    except (ImportError, ModuleNotFoundError):
        return ["office data-class policy unavailable - OFFICE denied"]
    return [] if ok else [f"OFFICE policy: {why}"]


def _quota_rows(session: Session, route: ModelRoute) -> list[QuotaState]:
    scopes = {"*", route.route_key, route.model_id}
    if route.alias:
        scopes.add(route.alias)
    if is_effectively_free(route):
        scopes.add("free-daily")
    scopes.add("credits")
    return list(session.scalars(select(QuotaState).where(QuotaState.provider == route.provider,
                                                         QuotaState.scope.in_(sorted(scopes)))))


def quota_exhausted(session: Session, route: ModelRoute) -> QuotaState | None:
    now = utcnow()
    for q in _quota_rows(session, route):
        if q.state == "EXHAUSTED" and (q.resets_at is None or ensure_aware(q.resets_at) > now):  # type: ignore[operator]
            return q
    return None


def credential_rejections(route: ModelRoute, *, domain: str) -> list[str]:
    """§26 credential paths (integration amendment, ADR-0041): a route the broker could not authenticate
    for *domain* is never eligible. OpenRouter routes may use the default worker refs; any other remote
    OpenAI-compatible upstream must name ``mode_params.credential_ref`` in a namespace the broker allows
    for the domain (loopback test endpoints may run without a key)."""
    if route.provider == "claude-native":
        return []
    ref = (route.mode_params or {}).get("credential_ref")
    if not ref:
        endpoint = urlparse(route.endpoint or "")
        # same test as the broker (scheme + parsed host): "http://localhost.example" is not loopback
        loopback = endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
        if route.provider == "openrouter":
            # the broker's default OpenRouter references are worker/PERSONAL ones; OFFICE has none
            return ["OFFICE routes need an office/ mode_params.credential_ref"] if domain == Domain.OFFICE.value else []
        if route.provider == "local" or loopback:
            return []
        return ["no mode_params.credential_ref: the broker has no credential for this upstream"]
    ref = str(ref)
    try:
        from aeos.broker.facade import credential_ref_problem

        # a projects/<id>/ reference is bound to its project; the broker re-checks the exact run project
        project_id = ref.split("/")[1] if ref.startswith("projects/") and ref.count("/") >= 2 else None
        problem = credential_ref_problem(domain, ref, project_id=project_id)
    except (ImportError, ModuleNotFoundError):
        return ["broker unavailable: credential reference cannot be checked (fail closed)"]
    return [f"credential_ref {ref}: {problem}"] if problem else []


# ------------------------------------------------------------------------------ runtime binding (§23)


def current_policy_version() -> str | None:
    """Version of the canonical policy in force (hash of ``policy/*.yaml``). Decisions are deliberately not
    part of it: an owner EXCEPTION/BUDGET elsewhere is not a policy change of the route. None = unknown."""
    try:
        from aeos.policy.snapshot import load_policy_files
    except (ImportError, ModuleNotFoundError):
        return None
    try:
        files = load_policy_files()
    except Exception:  # pragma: no cover - fail closed (an unreadable policy is not a known version)
        log.exception("policy files could not be read")
        return None
    return "pol-" + content_hash({name: entry.get("sha256") for name, entry in sorted(files.items())})[7:23]


def current_worker_runtime() -> dict[str, str | None]:
    """Fingerprints of the worker runtime in this build (§23 "prompt revision", "context strategy").

    * ``prompt_revision``: the worker runtime's own revision (``executor.worker_prompt_revision``: the prompt
      template, ``build_prompt`` rendered for a fixed probe TaskSpec, the OpenCode permission template, the
      report schema). It is the same value the executor writes into every prompt and run and compares before a
      run starts, so activation, eligibility and execution agree on one identity; any change is a new revision.
    * ``context_strategy``: how worker context is assembled (TaskSpec file + worktree; attachment budgets and
      the default model context/output limits the adapter writes)."""
    try:
        from aeos.worker_runtime import executor, opencode
    except (ImportError, ModuleNotFoundError):
        return {"prompt_revision": None, "context_strategy": None}
    try:
        revision = executor.worker_prompt_revision()
        strategy = {"assembly": "taskspec-file+worktree", "max_attachment_chars": executor.MAX_ATTACHMENT_CHARS,
                    "max_attach_total": executor.MAX_PROMPT_ATTACH_TOTAL,
                    "default_context": opencode.DEFAULT_CONTEXT_LIMIT, "default_output": opencode.DEFAULT_OUTPUT_LIMIT}
    except Exception:  # pragma: no cover - fail closed
        log.exception("worker runtime fingerprint failed")
        return {"prompt_revision": None, "context_strategy": None}
    return {"prompt_revision": revision,
            "context_strategy": "taskspec-file+worktree:" + content_hash(strategy)[7:19]}


def observed_harness_version() -> str | None:
    """The OpenCode version installed now (cached probe), or None when it is not installed/observable."""
    try:
        from aeos.worker_runtime import opencode

        status = opencode.opencode_status()
    except Exception:
        return None
    return status.get("version") if status.get("status") == "INSTALLED" and status.get("version") else None


def runtime_context() -> dict[str, str | None]:
    """The runtime values approvals are bound to, computed once per eligibility pass. ``harness_version`` is the
    OpenCode version observable now (None = not installed/observable: the executor refuses ENGINE_MISSING)."""
    return {"policy_version": current_policy_version(), **current_worker_runtime(),
            "harness_version": observed_harness_version()}


def runs_on_worker_runtime(route: ModelRoute) -> bool:
    return route.pool in WORKER_EXECUTED_POOLS or route.harness == "opencode"


def binding_fields(route: ModelRoute) -> tuple[str, ...]:
    return ("policy_version", "prompt_revision", "context_strategy") if runs_on_worker_runtime(route) \
        else ("policy_version",)


def binding_drift(route: ModelRoute, runtime: dict[str, str | None]) -> list[str]:
    """Why an activated route no longer matches the runtime it was approved for (empty = still bound)."""
    reasons: list[str] = []
    for f in binding_fields(route):
        bound, current = getattr(route, f), runtime.get(f)
        if current is None:
            reasons.append(f"{f}: the value in force cannot be determined - fail closed")
        elif bound is None:
            reasons.append(f"{f} was not bound at activation: re-activate after review")
        elif bound != current:
            reasons.append(f"{f} changed since activation ({bound} -> {current}): re-review and re-activate")
    observed = runtime.get("harness_version")
    if runs_on_worker_runtime(route) and observed is not None:
        # §23 "harness version": an engine installed or upgraded after the activation is not the reviewed one
        # (a catalog refresh records it as a configuration change; until then the hard filter fails closed)
        if route.harness_version is None:
            reasons.append(f"harness_version was not bound at activation (OpenCode {observed} observed now): "
                           "refresh the catalog and re-activate after review")
        elif route.harness_version != observed:
            reasons.append(f"harness_version changed since activation ({route.harness_version} -> {observed}): "
                           "re-review and re-activate")
    return reasons


def _tool_covered(tool: str, reviewed: set[str]) -> bool:
    return tool in reviewed or (tool.startswith("bash:") and "bash" in reviewed)


def reviewed_tool_set(route: ModelRoute) -> list[str] | None:
    """The tool set the owner reviewed for this route, or None when it was never reviewed."""
    tools = [str(t) for t in (route.tool_set or [])]
    return tools if tools and not set(tools) <= PLACEHOLDER_TOOL_SETS else None


def tool_set_rejections(route: ModelRoute, tools: list[str] | None) -> list[str]:
    if not tools:
        return []
    reviewed = reviewed_tool_set(route)
    if reviewed is None:
        return ["tool set not reviewed for this route (the activation records the reviewed tool set)"]
    missing = [t for t in tools if not _tool_covered(t, set(reviewed))]
    return [f"TaskSpec tools {missing} outside the route's reviewed tool set {sorted(reviewed)}"] if missing else []


def _unavailable_regions(terms: dict[str, Any]) -> list[str]:
    return [str(x).strip().upper() for x in terms.get("unavailable_regions") or [] if str(x).strip()]


def region_restricted(route: ModelRoute) -> bool:
    terms = route.data_terms or {}
    return bool(terms.get("regions")) or bool(_unavailable_regions(terms))


def region_rejections(route: ModelRoute, *, domain: str, office: dict[str, Any] | None) -> list[str]:
    """§23 "Terms/kuota/promo/region tetap dinilai per route": a provider that restricts regions needs the
    owner-reviewed region/residency on the route; an OFFICE policy may name allowed regions."""
    reasons: list[str] = []
    region = (route.region or "").upper()
    if region_restricted(route) and not region:
        reasons.append("provider restricts regions/residency: region not reviewed (record it at activation)")
    if region and region in _unavailable_regions(route.data_terms or {}):
        reasons.append(f"route unavailable in region {region} per its data terms")
    if domain == Domain.OFFICE.value and office is not None and office.get("allowed_regions"):
        allowed = [str(x).upper() for x in office.get("allowed_regions") or []]
        if region not in allowed:
            reasons.append(f"OFFICE policy: region/residency {region or 'UNKNOWN'} not in allowed_regions")
    return reasons


def unknown_terms_keys(terms: dict[str, Any]) -> list[str]:
    """Training/trial flags whose value is UNKNOWN: a missing/None training flag, or an explicit None trial flag."""
    return [k for k in RESTRICTED_TERMS_KEYS
            if (k == "may_use_inputs_for_training" and terms.get(k) is None) or (k in terms and terms.get(k) is None)]


def terms_restriction(terms: dict[str, Any], identity_status: str | None) -> str | None:
    """Why only PUBLIC data may be sent on these terms, or None. Training/trial flags that are True
    restrict; a training flag that is missing or None is UNKNOWN and restricts too (§23 "klaim provider
    bukan bukti"), as does an explicit None trial flag."""
    if identity_status == "UNVERIFIED":
        return "model identity UNVERIFIED"
    hit = [k for k in RESTRICTED_TERMS_KEYS if terms.get(k) is True]
    if hit:
        return f"data terms restrict to PUBLIC ({', '.join(hit)})"
    unknown = unknown_terms_keys(terms)
    if unknown:
        return f"data terms UNKNOWN ({', '.join(unknown)}): record the reviewed terms (terms_record) first"
    return None


def terms_eligibility(route: ModelRoute, *, data_classification: str, now: datetime | None = None) -> list[str]:
    """Terms/data/provider eligibility of *route* for a workload of *data_classification* — the §40/§43
    evaluation gate ("terms/provider eligibility sebagai gate"); a subset of the routing hard filter."""
    now = now or utcnow()
    reasons: list[str] = []
    if route.mode in {"MOCK", "DEMO"}:
        reasons.append(f"mode {route.mode}: fixtures are never eligible")
    if route.status == "DISABLED":
        reasons.append("route DISABLED")
    if data_classification not in (route.allowed_data_classes or []):
        reasons.append(f"data classification {data_classification} not allowed (allowed: {route.allowed_data_classes or []})")
    restriction = terms_restriction(route.data_terms or {}, route.identity_status)
    if restriction and data_classification != DataClassification.PUBLIC.value:
        reasons.append(restriction)
    if region_restricted(route) and not route.region:
        reasons.append("provider restricts regions/residency: region not reviewed")
    checked = [ensure_aware(t) for t in (route.pricing_checked_at, route.terms_checked_at)]
    if any(t is None for t in checked):
        reasons.append("pricing/terms never checked")
    elif any(now - t > TERMS_MAX_AGE for t in checked if t is not None):
        reasons.append("pricing/terms older than 30 days")
    return reasons


def _validate_inputs(domain: str, data_classification: str, pool: str) -> None:
    if domain not in {d.value for d in Domain}:
        raise ValidationFailed("domain must be PERSONAL or OFFICE (explicit human choice)", code="DOMAIN_REQUIRED")
    if data_classification not in {c.value for c in DataClassification}:
        raise ValidationFailed(f"unknown data classification {data_classification!r}")
    if pool not in ALL_POOLS:
        raise ValidationFailed(f"unknown pool {pool!r}")


# ------------------------------------------------------------------------------ eligibility


def route_rejections(
    session: Session, route: ModelRoute, *, domain: str, data_classification: str, needs_tools: bool,
    min_context: int | None, office: dict[str, Any] | None = None, office_reason: str = "",
    tools: list[str] | None = None, runtime: dict[str, str | None] | None = None,
) -> list[str]:
    """All hard-filter reasons that make *route* ineligible (empty list = eligible)."""
    reasons: list[str] = []
    now = utcnow()
    if route.config_hash is None:
        route.config_hash = compute_config_hash(route)
    elif route.config_hash != compute_config_hash(route) and route.status in {"APPROVED", "TRIAL"}:
        reasons.append("configuration changed without a recorded re-review (stored hash differs)")
    if route.mode in {"MOCK", "DEMO"}:
        reasons.append(f"mode {route.mode}: fixtures are never routable")
    status = route.status
    if status == "APPROVED":
        if find_route_decision(session, kind="ROUTE_ACTIVATION", route=route) is None:
            reasons.append("APPROVED without an ACTIVE human ROUTE_ACTIVATION decision bound to the current "
                           "config hash (configuration changed or approval expired): re-activate after review")
    elif status == "TRIAL":
        if find_route_decision(session, kind="TRIAL", route=route) is None:
            reasons.append("TRIAL without an ACTIVE TRIAL decision bound to the current config hash")
    elif status == "DISABLED":
        reasons.append("route DISABLED")
    else:
        reasons.append(f"status {status}: not approved (registry entries are never routable by default)")
    if status in {"APPROVED", "TRIAL"}:
        reasons += binding_drift(route, runtime if runtime is not None else runtime_context())

    if domain not in (route.eligible_domains or []):
        reasons.append(f"domain {domain} not in eligible_domains {route.eligible_domains or []}")
    if domain == Domain.OFFICE.value:
        if office is None:
            reasons.append(office_reason or "office policy unavailable - OFFICE denied")
        else:
            if office.get("external_worker_allowed") is not True:
                reasons.append("OFFICE policy: external_worker_allowed=false")
            if route.route_key not in (office.get("approved_worker_routes") or []):
                reasons.append("OFFICE policy: route not in approved_worker_routes")
            reasons += _office_data_class_rejections(session, office, data_classification)

    if data_classification not in (route.allowed_data_classes or []):
        reasons.append(f"data classification {data_classification} not allowed by route data terms "
                       f"(allowed: {route.allowed_data_classes or []})")
    if route.identity_status == "UNVERIFIED" and data_classification != DataClassification.PUBLIC.value:
        reasons.append("model identity UNVERIFIED: only PUBLIC data may be sent")
    elif data_classification != DataClassification.PUBLIC.value:
        restriction = terms_restriction(route.data_terms or {}, route.identity_status)
        if restriction:
            reasons.append(f"{restriction}: only PUBLIC data may be sent")
    if needs_tools and route.supports_tools != "YES":
        reasons.append(f"tool support {route.supports_tools}: tool use not proven for this route")
    reasons += tool_set_rejections(route, tools)
    if min_context:
        if not route.context_window:
            reasons.append("context window UNKNOWN")
        elif route.context_window < min_context:
            reasons.append(f"context window {route.context_window} < required {min_context}")
    if route.availability == "MISSING":
        reasons.append("route missing from the provider catalog at the last refresh")
    elif route.availability != "AVAILABLE":
        # UNKNOWN is never OK (AGENTS.md #7): a route no catalog refresh has observed is not routable.
        remedy = ("run `aeos routes refresh`" if route.provider in CATALOG_OBSERVED_PROVIDERS
                  else f"no catalog source observes provider {route.provider} in this build")
        reasons.append(f"availability {route.availability or 'UNKNOWN'}: never observed in a provider catalog "
                       f"({remedy})")
    reasons += region_rejections(route, domain=domain, office=office)
    reasons += credential_rejections(route, domain=domain)
    reasons += request_shape_rejections(route)
    restriction = client_restriction_problem(route)
    if restriction:
        reasons.append(restriction)
    q = quota_exhausted(session, route)
    if q is not None:
        resets = q.resets_at.isoformat() if q.resets_at else "UNKNOWN"
        reasons.append(f"quota EXHAUSTED ({q.scope}, source {q.source}, resets {resets})")
    checked = [ensure_aware(t) for t in (route.pricing_checked_at, route.terms_checked_at)]
    if any(t is None for t in checked):
        reasons.append("pricing/terms never checked")
    elif any(now - t > TERMS_MAX_AGE for t in checked if t is not None):
        reasons.append("pricing/terms older than 30 days: refresh the catalog or re-review terms")
    if needs_budget(route):
        act = find_route_decision(session, kind="ROUTE_ACTIVATION", route=route) or find_route_decision(
            session, kind="TRIAL", route=route)
        budget_id = ((act.details or {}).get("budget_decision_id") if act else None)
        ok, why = budget_decision_valid(session, budget_id, route)
        if not ok:
            label = "free period expired" if route.is_free == "YES" else f"is_free={route.is_free}"
            reasons.append(f"{budget_label(route)}: {why}" if is_subscription(route) else f"paid route ({label}): {why}")
    return reasons


def _candidates(session: Session, pool: str) -> list[ModelRoute]:
    return list(session.scalars(select(ModelRoute).where(ModelRoute.pool == pool)
                                .order_by(ModelRoute.priority, ModelRoute.route_key)))


def eligible_routes(
    session: Session, *, domain: str, data_classification: str, needs_tools: bool = True,
    min_context: int | None = None, pool: str = "WORKER", tools: list[str] | None = None,
) -> RouteDecision:
    _validate_inputs(domain, data_classification, pool)
    office, office_reason = (_office_policy(session) if domain == Domain.OFFICE.value else (None, ""))
    eligible: list[str] = []
    rejected: dict[str, list[str]] = {}
    runtime = runtime_context()
    for r in _candidates(session, pool):
        reasons = route_rejections(session, r, domain=domain, data_classification=data_classification,
                                   needs_tools=needs_tools, min_context=min_context, office=office,
                                   office_reason=office_reason, tools=tools, runtime=runtime)
        if reasons:
            rejected[r.route_key] = reasons
        else:
            eligible.append(r.route_key)
    return RouteDecision(route=None, eligible=eligible, rejected=rejected,
                         reason=f"{len(eligible)} eligible, {len(rejected)} rejected (pool {pool}, {domain}, "
                                f"{data_classification})")


# ------------------------------------------------------------------------------ selection


@dataclass(frozen=True)
class RankedRoute:
    route_key: str
    score: float | None
    basis: str  # EVAL | PRIORITY
    priority: int


def eval_score(route: ModelRoute, role: str | None) -> float | None:
    """Score for *role* from recorded eval results, only when they belong to the current config."""
    if not role or not isinstance(route.eval_results, dict):
        return None
    entry = route.eval_results.get(role)
    if not isinstance(entry, dict):
        return None
    if entry.get("config_hash") != route.config_hash:
        return None  # changed configuration: baseline not inherited (§43, §50)
    score = entry.get("score")
    return float(score) if isinstance(score, (int, float)) else None


def rank(session: Session, route_keys: list[str], role: str | None) -> list[RankedRoute]:
    routes = {r.route_key: r for r in session.scalars(select(ModelRoute).where(ModelRoute.route_key.in_(route_keys)))}
    ranked = []
    for key in route_keys:
        r = routes[key]
        s = eval_score(r, role)
        ranked.append(RankedRoute(key, s, "EVAL" if s is not None else "PRIORITY", r.priority))
    ranked.sort(key=lambda x: (0 if x.score is not None else 1, -(x.score or 0.0), x.priority, x.route_key))
    return ranked


def _positive_int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _task_policy(task: TaskSpec) -> dict[str, Any]:
    """The TaskSpec route policy as routing consumes it. The planning compiler (planning.dag) writes
    ``requires_tools`` / ``min_context_tokens``; ``needs_tools`` / ``min_context`` are accepted too. A policy
    asking routing to relax a constraint it always enforces is refused, never silently ignored."""
    pol = task.model_route_policy if isinstance(task.model_route_policy, dict) else {}
    pool = str(pol.get("pool") or "WORKER")
    if pool == "SUPERVISOR":
        raise Blocked("worker tasks are never routed to the supervisor pool", code="SUPERVISOR_NOT_ROUTABLE")
    relaxed = [k for k, enforced in POLICY_KEYS_ENFORCED.items() if k in pol and pol[k] is not None
               and bool(pol[k]) != enforced]
    if relaxed:
        raise Blocked(f"TaskSpec route policy asks to relax {relaxed}: eligible routes only, no paid fallback, "
                      "no supervisor downgrade", code="ROUTE_POLICY_UNSUPPORTED", details={"keys": relaxed})
    needs = pol["requires_tools"] if "requires_tools" in pol else pol.get("needs_tools", True)
    min_context = _positive_int(pol.get("min_context_tokens")) or _positive_int(pol.get("min_context"))
    return {
        "pool": pool,
        "needs_tools": needs is not False,
        "min_context": min_context,
        "routes": [str(x) for x in pol.get("routes") or []],
        "primary": str(pol["primary"]) if pol.get("primary") else None,
    }


def _eval_serving(session: Session, task: TaskSpec) -> dict[str, Any]:
    try:
        from aeos.evaluation import facade as evaluation
    except ImportError:  # pragma: no cover - evaluation is part of the platform
        return {"status": "NOT_BOUND"}
    return evaluation.eval_serving(session, task=task)


def _select_eval_route(session: Session, task: TaskSpec, pol: dict[str, Any], bound: dict[str, Any]) -> RouteDecision:
    """§43 "konfigurasi yang dievaluasi persis" / §49 M6 arena: a TaskSpec bound to a slot of a RUNNING EvalRun runs on
    exactly the evaluated route - never ranked against others, never a fallback - and only when that route passes the
    same hard eligibility filter for this task (an eval-only ARENA route additionally the evaluation's own checks)."""
    key = str(bound.get("route_key") or "")
    label = f"evaluation {bound.get('eval_run_id')} (slot {bound.get('slot_id')})"
    if bound.get("status") != "SERVED":
        raise Blocked(f"the task is bound to {label}; its route {key or 'UNKNOWN'} cannot serve it: "
                      + "; ".join(bound.get("reasons") or []), code="EVAL_ROUTE_NOT_SERVED", details=bound)
    route = get_route(session, key)
    decision = eligible_routes(session, domain=task.domain, data_classification=task.data_classification,
                               needs_tools=pol["needs_tools"], min_context=pol["min_context"], pool=route.pool,
                               tools=[str(t) for t in task.tools_allowed or []])
    reasons = list(decision.rejected.get(key) or ([] if key in decision.eligible else ["not eligible"]))
    if pol["routes"] and key not in pol["routes"]:
        reasons.append("not in the approved TaskSpec route allow-list")
    if reasons:
        raise Blocked(f"the task is bound to {label}, but route {key} is not eligible for it: {'; '.join(reasons)}",
                      code="EVAL_ROUTE_NOT_ELIGIBLE", details={**bound, "rejected": {key: reasons}})
    return RouteDecision(route=route, eligible=[key], rejected={},
                         reason=f"controlled {label}: exactly route {key} ({route.pool}), no ranking, no fallback",
                         eval_run_id=str(bound.get("eval_run_id")))


def select_route(session: Session, *, task: TaskSpec) -> RouteDecision:
    pol = _task_policy(task)
    try:
        from aeos.broker.facade import SERVED_POOLS as served
    except ImportError:  # pragma: no cover - broker is part of the platform
        served = ("WORKER",)
    bound = _eval_serving(session, task)
    if bound.get("status") != "NOT_BOUND":
        return _select_eval_route(session, task, pol, bound)
    if pol["pool"] not in served:
        # §23: refuse at admission instead of failing at the worker's first model call (ADR-0106).
        raise Blocked(f"pool {pol['pool']} has no controlled execution path through the broker yet; "
                      f"only {', '.join(served)} routes are served", code="POOL_NOT_SERVED",
                      details={"pool": pol["pool"], "served_pools": list(served)})
    decision = eligible_routes(session, domain=task.domain, data_classification=task.data_classification,
                               needs_tools=pol["needs_tools"], min_context=pol["min_context"], pool=pol["pool"],
                               tools=[str(t) for t in task.tools_allowed or []])
    eligible = list(decision.eligible)
    rejected = dict(decision.rejected)
    if pol["routes"]:
        for key in [k for k in eligible if k not in pol["routes"]]:
            eligible.remove(key)
            rejected[key] = ["not in the approved TaskSpec route allow-list"]
    role = task.role_key
    if pol["primary"]:
        primary = pol["primary"]
        if primary in eligible:
            route = get_route(session, primary)
            return RouteDecision(route=route, eligible=[primary, *[k for k in eligible if k != primary]],
                                 rejected=rejected, reason=f"primary route {primary} eligible")
        fb = _walk_fallback(session, get_route(session, primary), eligible, rejected)
        if fb is not None:
            return RouteDecision(route=fb, eligible=eligible, rejected=rejected,
                                 reason=f"primary {primary} ineligible; fallback {fb.route_key} along its chain")
        raise Blocked("primary route ineligible and no eligible fallback along its chain - task waits",
                      code="NO_ELIGIBLE_ROUTE", details={"rejected": rejected, "primary": primary})
    if not eligible:
        raise Blocked("no approved route is eligible for this task", code="NO_ELIGIBLE_ROUTE",
                      details={"rejected": rejected, "domain": task.domain,
                               "classification": task.data_classification})
    ranked = rank(session, eligible, role)
    top = get_route(session, ranked[0].route_key)
    return RouteDecision(route=top, eligible=[r.route_key for r in ranked], rejected=rejected,
                         reason=f"selected by {ranked[0].basis.lower()} for role {role}")


def _walk_fallback(session: Session, failed: ModelRoute, eligible: list[str],
                   rejected: dict[str, list[str]]) -> ModelRoute | None:
    if failed.pool == "SUPERVISOR":
        raise Blocked("no supervisor downgrade: the queue waits for the approved supervisor",
                      code="NO_SUPERVISOR_DOWNGRADE")
    for key in failed.fallback_chain or []:
        if key == failed.route_key:
            continue
        target = session.scalars(select(ModelRoute).where(ModelRoute.route_key == key)).first()
        if target is None:
            rejected.setdefault(key, []).append("fallback target not registered")
            continue
        if key not in eligible:
            rejected.setdefault(key, []).append("fallback target is not itself eligible")
            continue
        if target.pool != failed.pool:
            rejected.setdefault(key, []).append(f"fallback crosses pools ({failed.pool} -> {target.pool})")
            continue
        if not is_effectively_free(target):
            rejected.setdefault(key, []).append("paid fallback is never automatic")
            continue
        return target
    return None


def fallback_route(session: Session, *, task: TaskSpec, failed_route_key: str) -> RouteDecision:
    """Next route after *failed_route_key* failed (e.g. 429): along its chain, eligible, free."""
    bound = _eval_serving(session, task)
    if bound.get("status") != "NOT_BOUND":
        # §43: another route would describe another configuration - the evaluation slot waits (back off) instead
        raise Blocked(f"the task is bound to evaluation {bound.get('eval_run_id')}: no fallback away from the "
                      f"evaluated route {bound.get('route_key')}", code="EVAL_NO_FALLBACK", details=bound)
    failed = get_route(session, failed_route_key)
    pol = _task_policy(task)
    decision = eligible_routes(session, domain=task.domain, data_classification=task.data_classification,
                               needs_tools=pol["needs_tools"], min_context=pol["min_context"], pool=failed.pool,
                               tools=[str(t) for t in task.tools_allowed or []])
    eligible = [k for k in decision.eligible if not pol["routes"] or k in pol["routes"]]
    rejected = dict(decision.rejected)
    target = _walk_fallback(session, failed, eligible, rejected)
    if target is None:
        raise Blocked("no eligible fallback along the route's chain - back off / queue (no paid fallback)",
                      code="NO_ELIGIBLE_FALLBACK", details={"failed": failed_route_key, "rejected": rejected})
    return RouteDecision(route=target, eligible=eligible, rejected=rejected,
                         reason=f"fallback from {failed_route_key} to {target.route_key}")


# ------------------------------------------------------------------------------ quota


def note_rate_limited(
    session: Session, *, provider: str, scope: str = "free-daily", retry_after_seconds: int | None = None,
    resets_at: datetime | None = None, source: str = "observed-429", details: dict[str, Any] | None = None,
) -> QuotaState:
    """Record an observed 429/limit: the route is excluded until the reset (§50 'Free quota habis')."""
    row = session.scalars(select(QuotaState).where(QuotaState.provider == provider, QuotaState.scope == scope)).first()
    if row is None:
        row = QuotaState(provider=provider, scope=scope)
        session.add(row)
    now = utcnow()
    if resets_at is None and retry_after_seconds is not None:
        resets_at = now + timedelta(seconds=max(1, int(retry_after_seconds)))
    if resets_at is None and scope == "free-daily":
        resets_at = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    row.state = "EXHAUSTED"
    row.source = source
    row.observed_at = now
    row.resets_at = resets_at
    row.details = {**(details or {}), "note": "observed limit; backoff/queue, approved free fallback only"}
    session.flush()
    try:
        from aeos.events.service import emit

        emit(session, source="routing", kind="quota.exhausted", entity_type="quota", entity_id=f"{provider}:{scope}",
             payload={"provider": provider, "scope": scope, "resets_at": resets_at.isoformat() if resets_at else None})
    except Exception:  # pragma: no cover
        log.exception("emit quota.exhausted failed")
    return row


# ------------------------------------------------------------------------------ observations


def observe_supervisor_resolution(session: Session, *, route_key: str, requested: str, resolved: str) -> bool:
    """Record the resolved model behind an alias (§12, §23). A change of the resolved model is a
    new configuration: identity UNKNOWN until observed, eval baseline not inherited."""
    route = session.scalars(select(ModelRoute).where(ModelRoute.route_key == route_key)).first()
    if route is None:
        return False
    previous = route.resolved_version
    if route.config_hash is None:
        route.config_hash = compute_config_hash(route)
    route.resolved_version = resolved
    route.resolved_observed_at = utcnow()
    route.identity_status = "VERIFIED"
    if previous and previous not in {"UNKNOWN", resolved}:
        route.notes = (route.notes + "\n" if route.notes else "") + (
            f"{utcnow().date().isoformat()}: alias {requested} now resolves to {resolved} (was {previous})")
        return refresh_config_hash(session, route, reason=f"alias {requested} resolved {previous} -> {resolved}")
    # The first observation (no previously known identity) is not a pin change: nothing was bound to an earlier
    # identity, and the bridge proof that observed it records the model itself. Invalidating here would mark
    # claude.capability_test STALE on the first call of every other supervisor alias (e.g. fable after an opus
    # test) and push reviews to the manual fallback. A real re-resolution goes through refresh_config_hash above.
    route.config_hash = compute_config_hash(route)
    return False


def eval_summary(r: ModelRoute) -> dict[str, Any]:
    """Evaluation state of the route's CURRENT configuration: DIUJI (results bound to this config hash),
    BELUM_DIUJI, or NEEDS_EVALUATION (the configuration changed and the old results are not inherited)."""
    roles: dict[str, Any] = {}
    stale: list[str] = []
    for role, entry in (r.eval_results or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("config_hash") != r.config_hash:
            stale.append(role)
            continue
        roles[role] = {k: entry.get(k) for k in ("score", "score_basis", "runs", "accepted", "suite", "at",
                                                  "sample_size", "ci95", "run_ids", "decision_id")}
    state = "DIUJI" if roles else "BELUM_DIUJI"
    note = "Hasil konfigurasi lama tidak diwariskan." if stale else ""
    if not roles and (stale or (r.pool == "SUPERVISOR" and r.config_changed_at is not None)):
        # §02 "a version change is never silent and needs evaluation": the changed configuration is untested.
        state = "NEEDS_EVALUATION"
        note = ("Konfigurasi berubah (mis. alias kini me-resolve ke versi model lain); hasil lama tidak diwariskan "
                "dan konfigurasi baru belum dievaluasi.")
        if r.pool == "SUPERVISOR":
            note += (" Planning tetap memakai versi baru (uji penerimaan 1.31 menetapkan perilaku ini); evaluasi "
                     "baseline planning untuk versi ini belum tercatat - keputusan pemilik diperlukan.")
    return {"state": state, "roles": roles, "stale_roles": stale, "note": note}


def supervisor_configuration(session: Session, model: str) -> dict[str, Any] | None:
    """§02 "a version change is never silent": the supervisor route configuration a planning/review call ran on and
    its evaluation state, recorded on that call so the plan reviewer sees a NEEDS_EVALUATION configuration (this
    build keeps planning on the new version - trusted acceptance 1.31 - so the owner's decision is the gate)."""
    route = supervisor_route_for(session, model)
    if route is None:
        return None
    summary = eval_summary(route)
    return {"route_key": route.route_key, "config_hash": route.config_hash, "resolved_version": route.resolved_version,
            "harness_version": route.harness_version, "evaluation": summary["state"],
            "config_changed_at": route.config_changed_at.isoformat() if route.config_changed_at else None,
            "note": summary["note"] or None}


SUPERVISOR_MODE_DEFAULT = "CLI_DEFAULT"  # no --effort / thinking flag passed: the Claude Code default applies


def supervisor_route_for(session: Session, model: str) -> ModelRoute | None:
    """The SUPERVISOR route a bridge call with *model* (alias or pinned id) ran on."""
    m = (model or "").strip().lower()
    if not m:
        return None
    routes = list(session.scalars(select(ModelRoute).where(ModelRoute.pool == "SUPERVISOR")))
    for r in routes:
        if m in {str(x).lower() for x in (r.alias, r.model_id, r.resolved_version, r.route_key) if x}:
            return r
    return next((r for r in routes if r.alias and r.alias.lower() in m.split("-")), None)


def mode_thinking(route: ModelRoute) -> dict[str, Any]:
    """§23 "mode/thinking params" as the route's configuration identity carries it (stated, never guessed)."""
    mp = route.mode_params or {}
    if route.pool == "SUPERVISOR":
        return {"effort": mp.get("effort") or "UNKNOWN", "thinking": mp.get("thinking") or "UNKNOWN",
                "bound_in": ["mode_params"],
                "note": "Bridge memakai default Claude Code bila tidak ada --effort; perubahan = konfigurasi baru"}
    if route.harness == "opencode":
        # ADR-0041 amendment 2026-10-25: the broker sets the route's level and never forwards the worker's
        return {"reasoning": mp.get("reasoning") or "PROVIDER_DEFAULT",
                "bound_in": ["mode_params", "harness_version", "context_strategy"],
                "note": "Level reasoning ditetapkan rute (mode_params.reasoning), bukan pekerja: broker membuang "
                        "reasoning_effort kiriman pekerja dan mencatatnya di BrokerRequest. PROVIDER_DEFAULT = broker "
                        "tidak mengirim level apa pun, default provider berlaku; mengubahnya = konfigurasi baru."}
    keys = {k: v for k, v in mp.items() if k in ("effort", "thinking", "reasoning", "quantization", "backend")}
    return {**keys, "bound_in": ["mode_params"]} if keys else {"status": "UNKNOWN", "bound_in": []}


def current_supervisor_runtime() -> dict[str, str | None]:
    """§23 "prompt revision, policy version, context strategy" of the native supervisor routes: the planning prompt
    revision (``planning.facade.supervisor_prompt_runtime``), the canonical policy version and a fingerprint of how
    planning context is assembled. None = cannot be determined (never bound)."""
    out: dict[str, str | None] = {"prompt_revision": None, "policy_version": current_policy_version(),
                                  "context_strategy": None}
    try:
        from aeos.planning.facade import supervisor_prompt_runtime

        planning = supervisor_prompt_runtime()
    except (ImportError, ModuleNotFoundError, AttributeError):
        return out
    revision = planning.get("prompt_revision")
    out["prompt_revision"] = str(revision)[:64] if revision else None
    context = planning.get("context")
    if isinstance(context, dict) and context:
        out["context_strategy"] = "planning-fenced-data:" + content_hash(context)[7:19]
    return out


def observe_supervisor_call(session: Session, *, model: str, engine_version: str | None,
                            effort: str | None = None) -> dict[str, Any]:
    """§23 "ModelRoute menyimpan ... mode/thinking params, harness version, prompt revision, policy version, context
    strategy" for the native supervisor routes: the Claude Code version a planning/review call ran with is the route's
    ``harness_version``, the effort/thinking setting it passed (``CLI_DEFAULT`` when none) is ``mode_params.effort``,
    and the planning prompt revision, policy version and planning context strategy in force are ``prompt_revision``,
    ``policy_version`` and ``context_strategy`` (``current_supervisor_runtime``). All are part of the configuration
    identity: a later change (a Claude Code upgrade, a caller passing an effort level, a prompt or policy change) is a
    new configuration - approvals bound to the old hash stop matching, readiness goes STALE, eval results are not
    inherited (NEEDS_EVALUATION). The first observation binds without invalidating (nothing was bound to an earlier
    value), like the resolved model; a value that cannot be determined is never bound."""
    route = supervisor_route_for(session, model)
    if route is None:
        return {"route_key": None, "changed": False}
    if route.config_hash is None:
        route.config_hash = compute_config_hash(route)
    reasons: list[str] = []  # changes of a value that was already bound (a first binding is not a change)
    version = (engine_version or "").strip()[:64] or None
    if version and route.harness_version != version:
        if route.harness_version:
            reasons.append(f"harness claude-code {route.harness_version} -> {version}")
        route.harness_version = version
    mode = dict(route.mode_params or {})
    wanted = (effort or SUPERVISOR_MODE_DEFAULT).strip()[:32]
    if mode.get("effort") != wanted:
        if mode.get("effort"):
            reasons.append(f"effort {mode.get('effort')} -> {wanted}")
        route.mode_params = {**mode, "effort": wanted, "thinking": mode.get("thinking") or SUPERVISOR_MODE_DEFAULT}
    bridge_changed = bool(reasons)  # engine/effort: what the bridge capability proof was made with
    for field_name, value in current_supervisor_runtime().items():
        bound = getattr(route, field_name)
        if value and bound != value:
            if bound:
                reasons.append(f"{field_name} {bound} -> {value}")
            setattr(route, field_name, value)
    if not reasons:
        route.config_hash = compute_config_hash(route)  # first binding: no earlier identity was bound to it
        return {"route_key": route.route_key, "changed": False}
    # a prompt/policy/context change is a new configuration (NEEDS_EVALUATION) but does not touch the bridge, so the
    # bridge capability proof (model:route:SUPERVISOR) stays valid - reviews are not pushed to the manual fallback
    changed = refresh_config_hash(session, route, reason="; ".join(reasons), readiness=bridge_changed)
    if changed:
        route.notes = (route.notes + "\n" if route.notes else "") + f"{utcnow().date().isoformat()}: " + "; ".join(reasons)
    return {"route_key": route.route_key, "changed": changed, "reasons": reasons}


# ------------------------------------------------------------------------------ activation


def _validated_classes(allowed_data_classes: list[str] | None, route: ModelRoute) -> list[str]:
    if allowed_data_classes is None:
        return list(route.allowed_data_classes or [])
    valid = {c.value for c in DataClassification}
    if not allowed_data_classes or any(c not in valid for c in allowed_data_classes):
        raise ValidationFailed(f"allowed_data_classes must be a non-empty subset of {sorted(valid)}")
    # canonical (enum) order: re-confirming the same classes must not change the reviewed identity
    chosen = set(allowed_data_classes)
    return [c.value for c in DataClassification if c.value in chosen]


def _validated_terms_record(terms_record: dict[str, Any] | None, *, terms_reviewed: bool) -> dict[str, Any]:
    """The owner's own reading of the training/trial terms (§23: a provider claim is not proof)."""
    if not terms_record:
        return {}
    if not terms_reviewed:
        raise ValidationFailed("a terms record is the result of a terms review: set terms_reviewed=true",
                               code="TERMS_REVIEW_REQUIRED")
    unknown_keys = sorted(set(terms_record) - {*RESTRICTED_TERMS_KEYS, "note"})
    if unknown_keys:
        raise ValidationFailed(f"terms_record accepts {sorted(RESTRICTED_TERMS_KEYS)} and note only",
                               details={"unknown": unknown_keys})
    out: dict[str, Any] = {}
    for k in RESTRICTED_TERMS_KEYS:
        if k not in terms_record:
            raise ValidationFailed(f"terms_record must state {k} (true/false) as read in the provider terms",
                                   code="TERMS_RECORD_INCOMPLETE")
        if not isinstance(terms_record[k], bool):
            raise ValidationFailed(f"terms_record.{k} must be true or false")
        out[k] = terms_record[k]
    note = str(terms_record.get("note") or "").strip()
    if len(note) < 10:
        raise ValidationFailed("terms_record.note must say what was read (min 10 characters)")
    out["owner_terms_note"] = redact_text(note)[:500]
    out["owner_terms_record"] = True
    return out


def _validated_region(region: str | None, route: ModelRoute, terms: dict[str, Any]) -> str | None:
    if region is None or not str(region).strip():
        if (bool(terms.get("regions")) or bool(_unavailable_regions(terms))) and not route.region:
            raise ValidationFailed("the provider restricts regions/residency: state the region you reviewed "
                                   "(e.g. ID) before activation", code="REGION_REVIEW_REQUIRED")
        return route.region
    code = str(region).strip().upper()
    if not _REGION.match(code):
        raise ValidationFailed("region must be a region/residency code such as ID, EU or GLOBAL")
    if code in _unavailable_regions(terms):
        raise ValidationFailed(f"the route's data terms list it as unavailable in {code}", code="REGION_UNAVAILABLE")
    return code


def _validated_client_terms(client_terms: dict[str, Any] | None, route: ModelRoute) -> dict[str, Any] | None:
    """The owner's decision on a provider client restriction (ADR-0041 amendment 2026-10-25) to write into the data
    terms, or None when the route has none or the owner already decided it in the configuration being re-activated
    (that decision stays as recorded). Without it a restricted route is not activated (fail closed)."""
    cr = client_restriction(route)
    if cr is None:
        if client_terms:
            raise ValidationFailed("client_terms answers a provider client restriction; this route has none to decide",
                                   code="CLIENT_TERMS_NOT_APPLICABLE")
        return None
    if cr.get("owner_decision") == CLIENT_TERMS_ACCEPTED:
        return None  # already part of the reviewed configuration (and of the hash the new decision binds to)
    if not client_terms:
        raise ValidationFailed(
            f"{client_restriction_problem(route)}. Record your decision explicitly (client_terms: accepted + "
            "what you read/confirmed) or use another route", code="CLIENT_TERMS_DECISION_REQUIRED",
            details={"client_restriction": {k: cr.get(k) for k in ("statement", "source", "applies_to")}})
    unknown = sorted(set(client_terms) - {"accepted", "note"})
    if unknown:
        raise ValidationFailed("client_terms accepts accepted and note only", details={"unknown": unknown})
    if client_terms.get("accepted") is not True:
        raise ValidationFailed("client_terms.accepted must be true to activate a client-restricted route; otherwise "
                               "keep it inactive and use an OpenCode Go route (opencode-go/*) or a paid route with "
                               "a BUDGET decision",
                               code="CLIENT_TERMS_DECISION_REQUIRED")
    note = str(client_terms.get("note") or "").strip()
    if len(note) < 10:
        raise ValidationFailed("client_terms.note must say what you read/confirmed (min 10 characters)")
    return {**cr, "owner_decision": CLIENT_TERMS_ACCEPTED, "owner_note": redact_text(note)[:500]}


def _client_terms_record(route: ModelRoute, *, recorded_now: bool) -> dict[str, Any] | None:
    """The client-restriction decision the activation is bound to (stored on the immutable decision details)."""
    cr = client_restriction(route)
    if cr is None:
        return None
    return {"decision": cr.get("owner_decision"), "note": cr.get("owner_note"), "statement": cr.get("statement"),
            "source": cr.get("source"), "recorded_by_this_activation": recorded_now}


def _validated_tool_set(tool_set: list[str] | None, route: ModelRoute) -> list[str]:
    if tool_set is None:
        return reviewed_tool_set(route) or list(REVIEWED_WORKER_TOOLS)
    tools = list(dict.fromkeys(str(t).strip() for t in tool_set if str(t).strip()))
    bad = [t for t in tools if t not in TOOL_VOCABULARY and not (t.startswith("bash:") and len(t) > 5)]
    if not tools or bad:
        raise ValidationFailed(f"tool_set must be a non-empty subset of {sorted(TOOL_VOCABULARY)} (or bash:<pattern>)",
                               details={"invalid": bad})
    return sorted(tools)


def rollback_target(session: Session, route: ModelRoute, *, new_hash: str) -> dict[str, Any]:
    """§41/§43 "memiliki rollback": the configuration to return to if this activation goes wrong — the most
    recent earlier ROUTE_ACTIVATION of this route for another configuration (with its config snapshot), or
    disabling the route when no other configuration was ever approved."""
    rows = session.scalars(select(Decision).where(
        Decision.kind == "ROUTE_ACTIVATION", Decision.target_type == TARGET_TYPE, Decision.target_id == route.route_key,
    ).order_by(Decision.created_at.desc()))
    for d in rows:
        if d.target_hash and d.target_hash != new_hash:
            return {"action": "RESTORE_PREVIOUS_CONFIGURATION", "decision_id": d.id, "config_hash": d.target_hash,
                    "config_snapshot_in_decision": bool((d.details or {}).get("config_snapshot")),
                    "runbook": "docs/runbooks/model-rollback.md"}
    return {"action": "DISABLE_ROUTE", "decision_id": None, "config_hash": None,
            "note": "no earlier approved configuration of this route: rollback = disable it",
            "runbook": "docs/runbooks/model-rollback.md"}


def _activation_evidence(session: Session, route: ModelRoute, eval_run_ids: list[str] | None) -> dict[str, Any]:
    try:
        from aeos.evaluation.facade import activation_evidence
    except (ImportError, ModuleNotFoundError):
        return {"ok": False, "problems": ["evaluation module unavailable - test evidence cannot be checked"],
                "roles": {}}
    assert route.config_hash is not None
    return activation_evidence(session, route_key=route.route_key, route_config_hash=route.config_hash,
                               run_ids=list(eval_run_ids or []))


_ACTIVATION_FIELDS = ("allowed_data_classes", "data_terms", "terms_checked_at", "pricing_checked_at", "region",
                      "tool_set", "policy_version", "prompt_revision", "context_strategy", "harness_version",
                      "config_hash")


def activate_route(
    session: Session, route_key: str, *, actor_user_id: str, mode: str, rationale: str,
    expected_config_hash: str, allowed_data_classes: list[str] | None = None, budget_decision_id: str | None = None,
    expires_in_days: int | None = None, terms_reviewed: bool = False, terms_note: str = "",
    terms_record: dict[str, Any] | None = None, region: str | None = None, tool_set: list[str] | None = None,
    eval_run_ids: list[str] | None = None, decision_record: dict[str, Any] | None = None,
    client_terms: dict[str, Any] | None = None,
) -> tuple[ModelRoute, Decision]:
    """Human activation: records a ROUTE_ACTIVATION (APPROVED) or TRIAL decision bound to the exact
    route config hash. Never touches OFFICE.

    * Pricing/terms are re-read by the owner (``terms_reviewed``) whenever the last check is missing or
      older than 30 days (§51 P2, §45) — a seeded research date is not a terms review.
    * Data classes beyond PUBLIC need known, unrestricted training/trial terms (``terms_record`` states the
      owner's reading when the catalog left them UNKNOWN) and a verified identity.
    * The decision binds the runtime in force (policy version, worker prompt revision, context strategy,
      observed harness version), the reviewed tool set and, where the provider restricts regions, the
      reviewed region; a change of any of them makes the route ineligible until re-activated.
    * Paid routes also need an explicit BUDGET decision; for a flat-fee SUBSCRIPTION route it covers the plan fee
      (ADR-0080 amendment 2026-10-26).
    * APPROVED (production) additionally needs ``eval_run_ids``: COMPLETED, gate-passing EvalRuns made on
      exactly this configuration (§23 "telah diuji", §43). Their per-role result is written to
      ``eval_results`` by this decision — the only writer — and a rollback target is recorded. TRIAL needs no
      evaluation (it is time-limited).
    * APPROVED adopts a routing result, so it carries the §44 DecisionRecord (options, goal, evidence,
      cost/quality trade-off, invalidation condition; ``policy.catalog.DECISION_RECORD_FIELDS``).
    * A provider client restriction (``data_terms.client_restriction``, e.g. S07 free-tier models require the
      OpenCode client) needs the owner's explicit ``client_terms`` decision; it is written into the data terms (so
      the decision binds to it) and onto the decision. Without it: ``CLIENT_TERMS_DECISION_REQUIRED``.
    * The broker must be able to build the request (``broker.facade.upstream_request_shape``): a superseded seed
      endpoint, an unsupported API or a LOCAL route without its loopback endpoint is refused before any decision."""
    if mode not in {"APPROVED", "TRIAL"}:
        raise ValidationFailed("mode must be APPROVED or TRIAL")
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("rationale is required (min 10 characters)")
    route = get_route(session, route_key)
    if route.pool == "SUPERVISOR":
        raise ValidationFailed("supervisor routes are governed by the planning policy and the native bridge, "
                               "not by worker activation")
    if route.mode in {"MOCK", "DEMO"}:
        raise ValidationFailed("fixture routes cannot be activated")
    current = compute_config_hash(route)
    if route.config_hash != current:
        refresh_config_hash(session, route, reason="unrecorded configuration change detected at activation")
    if expected_config_hash != route.config_hash:
        raise Conflict("route configuration changed since it was reviewed; reload and review again",
                       code="CONFIG_HASH_MISMATCH", details={"current": route.config_hash})
    if Domain.OFFICE.value in (route.eligible_domains or []):
        # §23: OFFICE eligibility exists only through the policy module's controlled path (company authorisation
        # + company-approved route + owner decision bound to the route identity); anything else is refused.
        try:
            from aeos.policy.office_routes import office_route_authorised

            authorised = office_route_authorised(session, route) is not None
        except ImportError:  # policy module absent: fail closed
            authorised = False
        if not authorised:
            raise ValidationFailed("OFFICE eligibility needs a recorded company authorisation (policy module)")
    now = utcnow()
    # ---- validate everything first; nothing on the route changes until all checks passed
    terms = dict(route.data_terms or {})
    terms.update(_validated_terms_record(terms_record, terms_reviewed=terms_reviewed))
    stale = [ensure_aware(t) for t in (route.terms_checked_at, route.pricing_checked_at)]
    if not terms_reviewed and any(t is None or now - t > TERMS_MAX_AGE for t in stale):
        raise ValidationFailed("pricing/data terms were never reviewed or are older than 30 days: re-read the "
                               "official pricing and data terms and confirm (terms_reviewed)",
                               code="TERMS_REVIEW_REQUIRED")
    classes = _validated_classes(allowed_data_classes, route)
    restriction = terms_restriction(terms, route.identity_status)
    if restriction and set(classes) - {DataClassification.PUBLIC.value}:
        raise ValidationFailed(f"{restriction}: this route may receive PUBLIC data only",
                               code="DATA_TERMS_RESTRICTED", details={"requested": classes})
    reviewed_region = _validated_region(region, route, terms)
    tools = _validated_tool_set(tool_set, route) if runs_on_worker_runtime(route) else list(route.tool_set or [])
    days: int | None
    if mode == "TRIAL":
        days = expires_in_days or TRIAL_DEFAULT_DAYS
        if not 1 <= days <= TRIAL_MAX_DAYS:
            raise ValidationFailed(f"trial expiry must be 1..{TRIAL_MAX_DAYS} days")
    else:
        days = expires_in_days
        if days is not None and not 1 <= days <= APPROVAL_MAX_DAYS:
            raise ValidationFailed(f"approval expiry must be 1..{APPROVAL_MAX_DAYS} days")
    shape_problems = request_shape_rejections(route)
    if shape_problems:
        local_hint = (" - set the loopback endpoint of the local server first (aeos routes set-endpoint)"
                      if route.pool == "LOCAL" and not route.endpoint else "")
        raise ValidationFailed(f"the broker cannot build the upstream request for this route: {shape_problems[0]}"
                               f"{local_hint}", code="ROUTE_REQUEST_SHAPE_REFUSED", details={"problems": shape_problems})
    if needs_budget(route):
        ok, why = budget_decision_valid(session, budget_decision_id, route)
        if not ok:
            raise ValidationFailed(f"{budget_label(route)}: {why}", code="BUDGET_DECISION_REQUIRED")
    client_decision = _validated_client_terms(client_terms, route)
    if client_decision is not None:
        terms["client_restriction"] = client_decision
    runtime = runtime_context()
    unknown_runtime = [f for f in binding_fields(route) if runtime.get(f) is None]
    if unknown_runtime:
        raise Blocked(f"runtime binding cannot be determined ({unknown_runtime}) - activation refused (fail closed)",
                      code="RUNTIME_BINDING_UNKNOWN")
    # ---- apply (reverted when the test-evidence check below refuses)
    saved = {f: getattr(route, f) for f in _ACTIVATION_FIELDS}
    route.allowed_data_classes = classes
    if terms_reviewed:
        route.terms_checked_at = now
        route.pricing_checked_at = now  # the owner re-read pricing and terms now
        terms["checked_by"] = "owner"
    route.data_terms = terms
    route.region = reviewed_region
    route.tool_set = tools
    for f in binding_fields(route):
        setattr(route, f, runtime[f])
    if runs_on_worker_runtime(route):
        route.harness_version = runtime.get("harness_version") or route.harness_version
    route.config_hash = compute_config_hash(route)
    evidence: dict[str, Any] = {"ok": True, "problems": [], "roles": {}}
    if mode == "APPROVED":
        evidence = _activation_evidence(session, route, eval_run_ids)
        if not evidence["ok"]:
            to_evaluate = route.config_hash
            for f, v in saved.items():
                setattr(route, f, v)
            raise ValidationFailed(
                "APPROVED (production) needs completed, gate-passing EvalRuns of exactly this configuration: "
                "activate it as TRIAL (binds the runtime), evaluate that configuration, then approve",
                code="TEST_EVIDENCE_REQUIRED",
                details={"problems": evidence["problems"], "config_hash_to_evaluate": to_evaluate})
    from aeos.policy import facade as policy

    record_fields = policy.decision_record_fields()
    record = {k: redact(v) for k, v in (decision_record or {}).items()
              if k in record_fields and v not in (None, "", [], {})}
    if mode == "APPROVED":
        # §44 "Hasil routing/role/mode menjadi DecisionRecord": adopting the evaluated route records the options
        # weighed, the goal, the evidence, the cost/quality trade-off and when the decision stops applying.
        record.setdefault("evidence_ids", sorted({rid for r in evidence["roles"].values() for rid in r["run_ids"]}))
        missing = [k for k in record_fields if not record.get(k)]
        if missing:
            for f, v in saved.items():
                setattr(route, f, v)
            raise ValidationFailed(f"decision record incomplete: {missing}", code="DECISION_RECORD_INCOMPLETE",
                                   details={"required": list(record_fields)})
    rollback = rollback_target(session, route, new_hash=route.config_hash)
    terms_review = ({"checked_by": actor_user_id, "checked_at": now.isoformat(), "data_classes": classes,
                     "note": redact_text(terms_note or "")[:500], "terms_record": bool(terms_record)}
                    if terms_reviewed else None)
    try:
        from aeos.policy.facade import record_decision

        decision = record_decision(
            session, kind="ROUTE_ACTIVATION" if mode == "APPROVED" else "TRIAL", actor_user_id=actor_user_id,
            target_type=TARGET_TYPE, target_id=route.route_key, target_version=route.config_hash[7:23],
            target_hash=route.config_hash, allowed_operations=["route.use"],
            auth_scope={"domains": [Domain.PERSONAL.value], "data_classes": route.allowed_data_classes,
                        "pool": route.pool},
            domain=Domain.PERSONAL.value, rationale=rationale.strip(),
            expires_at=(now + timedelta(days=days)) if days else None,
            details={"mode": mode, "budget_decision_id": budget_decision_id, "terms_reviewed": terms_reviewed,
                     "terms_review": terms_review, "previous_status": route.status, "is_free": route.is_free,
                     "previous_config_hash": saved["config_hash"], "rollback_target": rollback,
                     "runtime_binding": {f: getattr(route, f) for f in (*binding_fields(route), "harness_version")},
                     "tool_set": route.tool_set, "region": route.region,
                     "client_terms": _client_terms_record(route, recorded_now=client_decision is not None),
                     "eval_run_ids": sorted({rid for r in evidence["roles"].values() for rid in r["run_ids"]}),
                     "eval_results": evidence["roles"], "config_snapshot": route_config(route),
                     "decision_record": record or None},
        )
    except (ImportError, ModuleNotFoundError) as exc:
        for f, v in saved.items():
            setattr(route, f, v)
        raise Blocked("decision registry (aeos.policy) unavailable - activation cannot be recorded",
                      code="DECISION_REGISTRY_UNAVAILABLE") from exc
    previous_status = route.status
    route.status = mode
    if mode == "APPROVED":
        results = dict(route.eval_results or {})
        for role, r in evidence["roles"].items():
            results[role] = {**r, "config_hash": route.config_hash, "decision_id": decision.id, "at": now.isoformat()}
        route.eval_results = results
    session.flush()
    notify_readiness(session, route.route_key, reason=f"status {previous_status} -> {mode}", pool=route.pool)
    return route, decision


def validated_local_endpoint(endpoint: str) -> str:
    """An owner-set LOCAL endpoint: ``http://`` on a loopback host with an explicit port (the OpenAI-compatible base,
    e.g. ``http://127.0.0.1:8080/v1``; the broker appends ``/chat/completions``). Never another host (§06)."""
    raw = str(endpoint or "").strip()
    u = urlparse(raw)
    if u.scheme != "http" or u.hostname not in LOOPBACK_HOSTS:
        raise ValidationFailed("a LOCAL endpoint must be http on a loopback host (127.0.0.1, localhost or [::1]), "
                               "e.g. http://127.0.0.1:8080/v1", code="ENDPOINT_NOT_LOOPBACK")
    try:
        port = u.port
    except ValueError as exc:
        raise ValidationFailed("the LOCAL endpoint port is invalid", code="ENDPOINT_INVALID") from exc
    if port is None or u.username or u.password or u.query or u.fragment or u.params or len(raw) > 512:
        raise ValidationFailed("the LOCAL endpoint needs an explicit port and no credentials, query or fragment",
                               code="ENDPOINT_INVALID")
    return raw.rstrip("/")


def _short_value(value: Any, name: str, *, limit: int, spaces: bool = False) -> str:
    text = " ".join(str(value or "").split()) if spaces else str(value or "").strip()
    if not text or len(text) > limit or (not spaces and any(c.isspace() for c in text)) or not text.isprintable():
        raise ValidationFailed(f"{name} must be 1..{limit} printable characters"
                               f"{'' if spaces else ' without whitespace'}")
    return redact_text(text)


def set_local_endpoint(
    session: Session, route_key: str, *, endpoint: str, rationale: str, upstream_model: str | None = None,
    build: dict[str, str] | None = None,
) -> tuple[ModelRoute, dict[str, Any]]:
    """Owner path (ADR-0111 amendment 2026-10-25): point a LOCAL route at the OpenAI-compatible server the owner runs
    on this host. Endpoint, served model name and build parameters are part of the configuration identity, so a change
    is a new configuration: decisions bound to the old hash stop matching (a new TRIAL is needed), eval results are
    not inherited and readiness goes STALE. Caller authenticates the human, audits and commits."""
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("rationale is required (min 10 characters)")
    route = get_route(session, route_key)
    if route.pool != "LOCAL" or route.provider != "local":
        raise ValidationFailed("only LOCAL routes (provider local) take an owner-set loopback endpoint",
                               code="ROUTE_NOT_LOCAL")
    if route.mode in {"MOCK", "DEMO"}:
        raise ValidationFailed("fixture routes cannot be configured")
    base = validated_local_endpoint(endpoint)
    mode_params = dict(route.mode_params or {})
    if upstream_model is not None and str(upstream_model).strip():
        mode_params["upstream_model"] = _short_value(upstream_model, "upstream_model", limit=160)
    for key, value in (build or {}).items():
        if key not in LOCAL_BUILD_KEYS:
            raise ValidationFailed(f"build parameters are {list(LOCAL_BUILD_KEYS)}", details={"unknown": key})
        if value is not None and str(value).strip():
            mode_params[key] = _short_value(value, key, limit=64, spaces=True)
    if route.config_hash is None:
        route.config_hash = compute_config_hash(route)
    previous = {"endpoint": route.endpoint, "config_hash": route.config_hash, "status": route.status}
    route.endpoint = base
    route.mode_params = mode_params
    changed = refresh_config_hash(session, route, reason=f"owner set LOCAL endpoint {previous['endpoint'] or 'none'} "
                                                          f"-> {base}: {redact_text(rationale.strip())[:120]}")
    session.flush()
    return route, {"previous": previous, "config_changed": changed, "config_hash": route.config_hash,
                   "needs_new_trial": changed and previous["status"] in {"APPROVED", "TRIAL"}}


def disable_route(session: Session, route_key: str, *, reason: str) -> ModelRoute:
    route = get_route(session, route_key)
    previous_status = route.status
    route.status = "DISABLED"
    try:
        from aeos.policy.facade import invalidate_decisions

        invalidate_decisions(session, target_type=TARGET_TYPE, target_id=route.route_key,
                             reason=f"route disabled: {reason}")
    except (ImportError, ModuleNotFoundError):
        pass  # DISABLED status alone already excludes the route
    session.flush()
    notify_readiness(session, route.route_key, reason=f"status {previous_status} -> DISABLED: {reason}",
                     pool=route.pool)
    return route
