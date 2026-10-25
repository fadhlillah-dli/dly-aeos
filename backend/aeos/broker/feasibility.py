"""Controlled route feasibility probe (master §23 "uji kelayakan route dan data", §48 arena; ADR-0111).

ARENA / COMPARATOR / LOCAL routes never serve worker tasks: the proxy serves the WORKER pool only and routing
refuses other pools at admission (ADR-0106). Their *feasibility* - does the route answer, with which model, how
fast, at what reported cost, is it rate limited - is tested only through this probe:

* owner-triggered (the API needs a SUPER_ADMIN; the CLI is the local owner), PERSONAL only - an OFFICE route is
  never probed (default deny, §23);
* the route is TRIAL/APPROVED on an ACTIVE human decision bound to its CURRENT configuration hash (the same
  per-request rule as the proxy: ``routing.route_use_problem``) - i.e. the owner reviewed its terms/region;
* free only: ``is_free=YES`` within its free period, or a loopback LOCAL endpoint. There is no budget path for a
  probe (no silent paid fallback, §42);
* the route accepts PUBLIC data and the request is ONE fixed synthetic PUBLIC prompt - no project, task, user or
  OFFICE data, no tools, ``max_tokens`` 16, no streaming, no retry and never another route;
* the credential comes from the route's own reference under the PERSONAL namespaces (same checks as the proxy).

The outcome is recorded as ``PROBE`` evidence (E3, subject ``model_route``), in the usage ledger and the audit
trail; a 429 records the quota state. It is DATA for the owner's activation/evaluation decision - it never
changes the route's status or eligibility.
"""

from __future__ import annotations

import time
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
PROBE_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)


def _free_or_local(route: ModelRoute, base_url: str) -> str | None:
    from aeos.broker.model_proxy import _is_loopback_http

    if route.pool == "LOCAL" and _is_loopback_http(base_url):
        return None
    # The routing rule (is_free=YES and the free period not over) re-applied here: a probe has no budget path.
    until = ensure_aware(route.free_until)
    if route.is_free != "YES" or (until is not None and until <= utcnow()):
        return (f"route is not free (is_free={route.is_free}, free_until={route.free_until}); a feasibility probe "
                "never spends money (no budget path, §42)")
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
    paid = _free_or_local(route, upstream.base_url)
    if paid:
        raise PolicyDenied(paid, code="ROUTE_NOT_FREE")
    body: dict[str, Any] = {"model": upstream.model_id, "messages": [{"role": "user", "content": PROBE_PROMPT}],
                            "max_tokens": PROBE_MAX_TOKENS, "temperature": 0, "stream": False, "n": 1}
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
        resp = http.post(f"{upstream.base_url}/chat/completions", json=body, headers=_upstream_headers(upstream))
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
        "config_hash": route.config_hash, "outcome": outcome, "http_status": status, "latency_ms": latency_ms,
        "answered": answered, "model_resolved": model_resolved, "prompt_tokens": prompt,
        "completion_tokens": completion, "cost_usd": cost if cost is not None else "UNKNOWN",
        "retry_after_seconds": retry_after, "error": redact_text(error or "")[:200],
        "probe": {"prompt": PROBE_PROMPT, "data_class": "PUBLIC", "max_tokens": PROBE_MAX_TOKENS, "fallback": "NONE"},
        "checked_at": utcnow().isoformat(),
        "note": "feasibility observation only: route status and eligibility are unchanged (owner decision)",
    }
    if cost:
        result["note"] += "; a free route reported a cost - review its pricing before any activation"
    ledger = _ledger(session, upstream, route_key, prompt, completion, cost, model_resolved, outcome)
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
            cost: float | None, model: str | None, outcome: str) -> str:
    if outcome != "REACHABLE" and prompt is None and completion is None:
        return "NOT_RECORDED (no usage reported)"
    try:
        from aeos.cost import facade as cost_facade

        cost_facade.record_usage(session, provider=upstream.provider, category="WORKER_API",
                                 source="broker-feasibility", domain="PERSONAL", route_key=route_key,
                                 model=model or upstream.model_id, input_tokens=prompt, output_tokens=completion,
                                 requests=1, cost_usd=cost, is_estimate=False, mode="LIVE")
        return "RECORDED"
    except ImportError:
        return "LEDGER_UNAVAILABLE"
    except AeosError as exc:
        return f"LEDGER_REJECTED:{exc.code}"


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
                         "model_id": route.model_id, "provider": route.provider, "checked_at": result["checked_at"]})
    except AeosError:
        return None
    return ev.id
