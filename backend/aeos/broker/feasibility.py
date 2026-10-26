"""Controlled route feasibility probe (master §23 "uji kelayakan route dan data", §48 arena; ADR-0111).

ARENA / COMPARATOR / LOCAL routes never serve worker tasks: the proxy serves the WORKER pool only and routing
refuses other pools at admission (ADR-0106). Their *feasibility* - does the route answer, with which model, how
fast, at what reported cost, is it rate limited - is tested only through this probe:

* owner-triggered (the API needs a SUPER_ADMIN; the CLI is the local owner), PERSONAL only - an OFFICE route is
  never probed (default deny, §23);
* the route is TRIAL/APPROVED on an ACTIVE human decision bound to its CURRENT configuration hash (the same
  per-request rule as the proxy: ``routing.route_use_problem``) - i.e. the owner reviewed its terms/region;
* no money: ``is_free=YES`` within its free period, a loopback LOCAL endpoint, or a flat-fee SUBSCRIPTION route whose
  terms are complete and whose allowance windows are not used up (ADR-0080 amendment 2026-10-26: the probe consumes
  plan allowance, not money - the plan fee is the activation's BUDGET decision). A per-token paid route is refused
  (``ROUTE_NOT_FREE``): there is no budget path for a probe (no silent paid fallback, §42);
* the route accepts PUBLIC data and the request is ONE fixed synthetic PUBLIC prompt - no project, task, user or
  OFFICE data, no tools, ``max_tokens`` 16, no streaming, no retry and never another route;
* the credential comes from the route's own reference under the PERSONAL namespaces (same checks as the proxy).

The outcome is recorded as ``PROBE`` evidence (E3, subject ``model_route``), in the usage ledger and the audit
trail; a 429 records the quota state. It is DATA for the owner's activation/evaluation decision - it never
changes the route's status or eligibility.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.broker.facade import PROBE_POOLS
from aeos.core.errors import AeosError, NotFound, PolicyDenied
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.routing import ModelRoute

__all__ = ["PROBE_MAX_TOKENS", "PROBE_POOLS", "PROBE_PROMPT", "probe_route"]
PROBE_PROMPT = "Feasibility check. Reply with the single word: OK"  # synthetic PUBLIC data, nothing else
PROBE_MAX_TOKENS = 16
# Upper bound of the probe's tokens when the upstream reports none (prompt characters >= its tokens, the output cap, a
# margin for the chat template): what a subscription route's allowance estimate counts (ADR-0080 amendment 2026-10-26).
PROBE_TOKEN_BOUND = len(PROBE_PROMPT) + PROBE_MAX_TOKENS + 64
PROBE_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)


def _spend_problem(session: Session, route: ModelRoute, base_url: str) -> tuple[str, str] | None:
    """(code, reason) when the probe would spend money (or plan allowance that is used up); None when it may run."""
    from aeos.broker.model_proxy import _is_loopback_http
    from aeos.cost import facade as cost

    if route.pool == "LOCAL" and _is_loopback_http(base_url):
        return None
    terms, problem = cost.subscription_terms(route)
    if problem:
        return "ROUTE_NOT_FREE", f"subscription terms cannot be trusted ({problem}); a probe never spends money"
    if terms is not None:
        # a flat-fee plan: one synthetic request consumes allowance, not money - unless a window is used up
        status = cost.allowance_status(session, route) or {}
        if status.get("state") == "EXHAUSTED":
            return "SUBSCRIPTION_ALLOWANCE_EXHAUSTED", ("the subscription allowance is used up in a provider window: "
                                                        + "; ".join(w["window"] for w in status.get("windows") or []
                                                                    if w.get("state") == "EXHAUSTED"))
        if status.get("state") != "OK":
            return "SUBSCRIPTION_ALLOWANCE_UNKNOWN", ("the subscription allowance consumed cannot be counted: "
                                                      + "; ".join(status.get("problems") or ["state UNKNOWN"]))
        return None
    # The routing rule (is_free=YES and the free period not over) re-applied here: a probe has no budget path.
    until = ensure_aware(route.free_until)
    if route.is_free != "YES" or (until is not None and until <= utcnow()):
        return "ROUTE_NOT_FREE", (f"route is not free (is_free={route.is_free}, free_until={route.free_until}); a "
                                  "feasibility probe never spends money (no budget path, §42)")
    return None


def _outcome(status: int | None, error: str | None) -> str:
    if error == "timeout":
        return "TIMEOUT"
    if error:
        return "UNREACHABLE"
    if status == 200:
        return "REACHABLE"
    if status == 429:
        return "RATE_LIMITED"
    if status in (401, 403):
        return "AUTH_FAILED"
    if status is not None and 400 <= status < 500:
        return "REJECTED"
    return "UPSTREAM_ERROR"


def probe_route(session: Session, route_key: str, *, actor: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """Send the one synthetic probe to *route_key* and record the observation (caller commits)."""
    from aeos.broker.model_proxy import (
        ProxyError,
        _billing_of,
        _retry_after,
        _upstream_headers,
        _usage_of,
        resolve_upstream,
    )

    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    if route is None:
        raise NotFound(f"route {route_key} not found")
    if route.pool not in PROBE_POOLS:
        raise PolicyDenied(f"route pool {route.pool} is not probed (only {', '.join(PROBE_POOLS)})",
                           code="ROUTE_NOT_PROBEABLE")
    if "PERSONAL" not in (route.eligible_domains or []):
        raise PolicyDenied("only PERSONAL-eligible routes are probed; OFFICE routes stay default deny (§23)",
                           code="ROUTE_DOMAIN")
    if "PUBLIC" not in (route.allowed_data_classes or []):
        raise PolicyDenied("the route does not accept PUBLIC data; even the synthetic probe is not sent",
                           code="DATA_CLASS_NOT_ALLOWED")
    try:
        upstream = resolve_upstream(session, route_key, domain="PERSONAL", pools=PROBE_POOLS)
    except ProxyError as exc:
        audit.record(session, actor_type="user" if actor.startswith("user:") else "service", actor_id=actor,
                     action="broker.route_feasibility.probe", result="DENIED", target_type="model_route",
                     target_id=route_key, domain="PERSONAL", reason=f"{exc.code}: {exc.message}"[:500])
        raise PolicyDenied(f"route cannot be probed: {exc.message}", code=exc.code) from exc
    spend = _spend_problem(session, route, upstream.base_url)
    if spend:
        raise PolicyDenied(spend[1], code=spend[0])
    from aeos.cost import facade as cost_facade

    subscription = cost_facade.subscription_terms(route)[0] is not None
    # the route's own request shape (ADR-0041 amendment 2026-10-25): <endpoint>/chat/completions, the bare upstream
    # model id, the route's reasoning level when it states one - the probe tests the configuration that would serve
    body: dict[str, Any] = {"model": upstream.model_id, "messages": [{"role": "user", "content": PROBE_PROMPT}],
                            "max_tokens": PROBE_MAX_TOKENS, "temperature": 0, "stream": False, "n": 1}
    if upstream.reasoning_effort:
        body["reasoning_effort"] = upstream.reasoning_effort
    if upstream.is_openrouter:
        body["usage"] = {"include": True}
    started = time.monotonic()
    status: int | None = None
    error: str | None = None
    data: dict[str, Any] = {}
    retry_after: int | None = None
    own = client is None
    http = client or httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False, trust_env=False)
    try:
        # a probe is its own one-request conversation: its own opaque x-opencode-session on OpenCode Go
        resp = http.post(upstream.url, json=body,
                         headers=_upstream_headers(upstream, run_id=f"probe:{route_key}:{uuid.uuid4().hex}"))
        status = resp.status_code
        if status == 200:
            try:
                parsed = resp.json()
                data = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                error, status = "invalid upstream JSON", 502
        elif status == 429:
            retry_after = _retry_after(resp)
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPError as exc:
        error = type(exc).__name__
    finally:
        if own:
            http.close()
    latency_ms = int((time.monotonic() - started) * 1000)
    outcome = _outcome(status, error if error != "invalid upstream JSON" else None)
    prompt, completion, cost, model_resolved = _usage_of(data)
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    answered = bool(choices and isinstance(choices[0], dict)
                    and str(((choices[0].get("message") or {}).get("content")) or "").strip())
    result: dict[str, Any] = {
        "route_key": route_key, "pool": route.pool, "provider": upstream.provider, "model_id": upstream.model_id,
        "catalog_model_id": route.model_id, "upstream_url": upstream.url,
        "reasoning_effort": upstream.reasoning_effort or "NOT_SENT",
        "config_hash": route.config_hash, "outcome": outcome, "http_status": status, "latency_ms": latency_ms,
        "answered": answered, "model_resolved": model_resolved, "prompt_tokens": prompt,
        "completion_tokens": completion, "cost_usd": cost if cost is not None else "UNKNOWN",
        "retry_after_seconds": retry_after, "error": redact_text(error or "")[:200],
        "probe": {"prompt": PROBE_PROMPT, "data_class": "PUBLIC", "max_tokens": PROBE_MAX_TOKENS, "fallback": "NONE"},
        "checked_at": utcnow().isoformat(),
        "note": "feasibility observation only: route status and eligibility are unchanged (owner decision)",
    }
    if cost and subscription:
        result["note"] += ("; a subscription route reported a marginal charge - paid usage beyond the plan (is the "
                           "provider's Extra Usage ON?)")
    elif cost:
        result["note"] += "; a free route reported a cost - review its pricing before any activation"
    billing = _billing_of(data)
    if prompt is None and completion is None:
        billing["charged_tokens"] = PROBE_TOKEN_BOUND  # the probe's own upper bound, never a guess
    ledger, facts = _ledger(session, upstream, route_key, prompt, completion, cost, model_resolved, outcome, billing)
    if subscription:
        result["billing"] = {"billing": "SUBSCRIPTION", "allowance_usd": facts.get("allowance_usd") or "UNKNOWN",
                             "allowance_basis": facts.get("allowance_basis") or "UNKNOWN",
                             "billing_source": billing.get("billing_source") or "UNKNOWN"}
    if outcome == "RATE_LIMITED":
        _quota(session, upstream, route_key, retry_after)
    result["ledger"] = ledger
    ev = _evidence(session, route, result)
    result["evidence_id"] = ev
    audit.record(session, actor_type="user" if actor.startswith("user:") else "service", actor_id=actor,
                 action="broker.route_feasibility.probe", result="SUCCESS" if outcome == "REACHABLE" else "FAILED",
                 target_type="model_route", target_id=route_key, domain="PERSONAL", reason=outcome,
                 details={"pool": route.pool, "http_status": status, "latency_ms": latency_ms, "evidence_id": ev,
                          "ledger": ledger})
    return result


def _ledger(session: Session, upstream: Any, route_key: str, prompt: int | None, completion: int | None,
            cost: float | None, model: str | None, outcome: str,
            billing: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    if outcome != "REACHABLE" and prompt is None and completion is None:
        return "NOT_RECORDED (no usage reported)", {}
    try:
        from aeos.cost import facade as cost_facade

        entry = cost_facade.record_usage(session, provider=upstream.provider, category="WORKER_API",
                                         source="broker-feasibility", domain="PERSONAL", route_key=route_key,
                                         model=model or upstream.model_id, input_tokens=prompt,
                                         output_tokens=completion, requests=1, cost_usd=cost, is_estimate=False,
                                         mode="LIVE", details=billing or None)
        info = entry.details or {}
        return "RECORDED", {k: info[k] for k in ("allowance_usd", "allowance_basis") if k in info}
    except ImportError:
        return "LEDGER_UNAVAILABLE", {}
    except AeosError as exc:
        return f"LEDGER_REJECTED:{exc.code}", {}


def _quota(session: Session, upstream: Any, route_key: str, retry_after: int | None) -> None:
    from datetime import timedelta

    try:
        from aeos.cost import facade as cost_facade

        now = utcnow()
        wait = retry_after or 60
        cost_facade.set_quota_state(session, provider=upstream.provider, state="EXHAUSTED", source="observed-429",
                                    scope=route_key, details={"status": 429, "retry_after_seconds": wait,
                                                              "note": "observed by the feasibility probe; no fallback"},
                                    observed_at=now, resets_at=now + timedelta(seconds=wait))
    except ImportError:
        pass


def _evidence(session: Session, route: ModelRoute, result: dict[str, Any]) -> str | None:
    try:
        from aeos.verification import facade as verification
    except ImportError:
        return None
    try:
        ev = verification.collect_evidence(
            session, kind="PROBE", origin="collector", collector_identity="aeos-broker/route-feasibility",
            claim=f"Route feasibility probe {route.route_key} ({route.pool}): {result['outcome']}", content=result,
            evidence_level="E3", subject_type="model_route", subject_id=route.route_key, domain="PERSONAL",
            classification="PUBLIC",
            environment={"route_key": route.route_key, "pool": route.pool, "config_hash": route.config_hash,
                         "model_id": route.model_id, "upstream_model": result["model_id"],
                         "upstream_url": result["upstream_url"], "provider": route.provider,
                         "checked_at": result["checked_at"]})
    except AeosError:
        return None
    return ev.id
