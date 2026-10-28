"""Controlled route feasibility probe (master §23 "uji kelayakan route dan data", §48 arena; ADR-0111).

ARENA / COMPARATOR / LOCAL routes never serve worker tasks: the proxy serves the WORKER pool only and routing
refuses other pools at admission (ADR-0106). Their *feasibility* - does the route answer, with which model, how
fast, at what reported cost, is it rate limited - is tested only through this probe:

* owner-triggered (the API needs a SUPER_ADMIN; the CLI is the local owner), PERSONAL only - an OFFICE route is
  never probed (default deny, §23);
* the route is TRIAL/APPROVED on an ACTIVE human decision bound to its CURRENT configuration hash (the same
  per-request rule as the proxy: ``routing.route_use_problem``) - i.e. the owner reviewed its terms/region;
* no money: ``is_free=YES`` within its free period, a loopback LOCAL endpoint, or a flat-fee SUBSCRIPTION route whose
  terms are complete and whose state admits (ADR-0080 amendment 2026-10-26: the probe consumes plan allowance, not
  money - the plan fee is the activation's BUDGET decision). A subscription route is refused while its 5h window is
  used up (``SUBSCRIPTION_ALLOWANCE_EXHAUSTED``), while a call in it cannot be counted
  (``SUBSCRIPTION_ALLOWANCE_UNKNOWN``) and after a call billed beyond the plan until the owner re-activates it
  (``SUBSCRIPTION_BEYOND_PLAN``); its advisory week/month windows never refuse. A per-token paid route is refused
  (``ROUTE_NOT_FREE``): there is no budget path for a probe (no silent paid fallback, §42);
* the route accepts PUBLIC data and the request is ONE fixed synthetic PUBLIC prompt - no project, task, user or
  OFFICE data, no tools, ``max_tokens`` 16, no streaming, no retry and never another route;
* the credential comes from the route's own reference under the PERSONAL namespaces (same checks as the proxy).

The outcome is recorded as ``PROBE`` evidence (E3, subject ``model_route``), in the usage ledger and the audit
trail; a 429 records the quota state. It is DATA for the owner's activation/evaluation decision - it never
changes the route's status or eligibility.

Tool-call probe (``tools=True``; ADR-0125, owner decision 2026-10-28): the same gates, the same single request, no
retry and no other route, but the synthetic PUBLIC prompt carries ONE trivial function definition
(``TOOL_PROBE_TOOLS``, ``tool_choice`` auto) under its own output cap ``TOOL_PROBE_MAX_TOKENS``. PASS only for an HTTP
200 answer whose every tool call is well formed: ``id`` set, ``type`` function (or absent), the defined function name
and ``arguments`` a JSON string of exactly ``{"status": "OK"}``; anything else is FAIL with the reason. The verdict is
recorded as ``PROBE`` evidence marked ``probe: TOOL_CALL`` and bound to the route's configuration hash
(``routing.facade`` TOOL_PROBE_* marks). It never changes ``supports_tools`` (a CONFIG_FIELD, changing it would void
the TRIAL): routing reads the evidence instead (``routing.facade.tool_capable``).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.broker.facade import PROBE_POOLS
from aeos.core.errors import AeosError, Blocked, Conflict, NotFound, PolicyDenied
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.broker_ext import BrokerRequest
from aeos.models.routing import ModelRoute

log = logging.getLogger("aeos.broker.feasibility")

__all__ = ["PROBE_MAX_TOKENS", "PROBE_POOLS", "PROBE_PROMPT", "TOOL_PROBE_MAX_TOKENS", "TOOL_PROBE_PROMPT",
           "TOOL_PROBE_TOOLS", "probe_route", "tool_call_verdict"]
PROBE_PROMPT = "Feasibility check. Reply with the single word: OK"  # synthetic PUBLIC data, nothing else
PROBE_MAX_TOKENS = 16
# Upper bound of the probe's tokens when the upstream reports none (prompt characters >= its tokens, the output cap, a
# margin for the chat template): what a subscription route's allowance estimate counts (ADR-0080 amendment 2026-10-26).
PROBE_TOKEN_BOUND = len(PROBE_PROMPT) + PROBE_MAX_TOKENS + 64
# Tool-call probe (ADR-0125): one trivial function, synthetic PUBLIC data. Its own output cap leaves room for the short
# reasoning preamble of reasoning models (models.dev marks the Go candidates reasoning: true) before the call; 1024
# output tokens are at most about 0.015 USD of plan allowance at the highest seeded Go rate (15 USD per 1M tokens).
TOOL_PROBE_FUNCTION = "report_status"
TOOL_PROBE_PROMPT = (f"Tool-call check. Call the function {TOOL_PROBE_FUNCTION} with status set to OK. "
                     "Do not answer with text.")
TOOL_PROBE_TOOLS: list[dict[str, Any]] = [{"type": "function", "function": {
    "name": TOOL_PROBE_FUNCTION, "description": "Report the result of the tool-call check.",
    "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["OK"]}},
                   "required": ["status"], "additionalProperties": False}}}]
TOOL_PROBE_MAX_TOKENS = 1024
# upper bound when the upstream reports no usage: prompt and tool schema characters, the output cap and a margin for
# the provider's tool-calling template
TOOL_PROBE_TOKEN_BOUND = len(TOOL_PROBE_PROMPT) + len(json.dumps(TOOL_PROBE_TOOLS)) + TOOL_PROBE_MAX_TOKENS + 1024
PROBE_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
# Errors raised before the request left this host: such a probe never reached the upstream and is not counted against
# an OpenRouter free-model limit; every other outcome (an answer, a read timeout, a broken response) is counted.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
                   httpx.InvalidURL)
# A probe of an OpenRouter ``:free`` model is a broker request row like a worker call (decision C): recorded PENDING under
# the free-model count lock and committed before anything is sent, then settled. It has no grant and no project.
PROBE_MARK = "feasibility-probe"


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
        # a flat-fee plan: one synthetic request consumes allowance, not money - unless the route's state refuses it
        # (the same answer the scheduler and the cost module's reserve give; advisory windows never refuse)
        status = cost.allowance_status(session, route)
        block = cost.subscription_admission_block(status)
        if block:
            return block
        if status is None or status.get("state") not in ("OK", "WARN_EXHAUSTED"):
            return "SUBSCRIPTION_ALLOWANCE_UNKNOWN", (f"the subscription state cannot be read (state "
                                                      f"{(status or {}).get('state', 'UNKNOWN')}); fail closed")
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


def tool_call_verdict(data: dict[str, Any], *, status: int | None, outcome: str) -> tuple[str, str]:
    """(PASS|FAIL, reason) of a tool-call probe answer (ADR-0125): PASS only for a 200 answer whose every tool call is
    well formed and calls the one defined function with exactly ``{"status": "OK"}``."""
    from aeos.routing.facade import TOOL_PROBE_FAIL, TOOL_PROBE_PASS

    if status != 200:
        return TOOL_PROBE_FAIL, f"no answer to judge (outcome {outcome}, http {status})"
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else None
    if message is None:
        return TOOL_PROBE_FAIL, "the answer has no message"
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return TOOL_PROBE_FAIL, f"no tool call in the answer (finish_reason {first.get('finish_reason') or 'UNKNOWN'})"
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"].strip() \
                or call.get("type", "function") != "function" or not isinstance(fn, dict):
            return TOOL_PROBE_FAIL, "a malformed tool call (id, type or function missing)"
        if fn.get("name") != TOOL_PROBE_FUNCTION:
            return TOOL_PROBE_FAIL, "a tool call names a function that was not defined"
        try:
            args = json.loads(fn["arguments"]) if isinstance(fn.get("arguments"), str) else None
        except ValueError:
            args = None
        if args != {"status": "OK"}:
            return TOOL_PROBE_FAIL, 'the tool call arguments are not the JSON object {"status": "OK"}'
    return TOOL_PROBE_PASS, f"{len(calls)} well-formed tool call(s) of {TOOL_PROBE_FUNCTION}"


def probe_route(session: Session, route_key: str, *, actor: str, client: httpx.Client | None = None,
                tools: bool = False) -> dict[str, Any]:
    """Send the one synthetic probe to *route_key* and record the observation (caller commits). ``tools=True`` sends
    the tool-call probe instead (ADR-0125): the same gates, one function definition, PASS/FAIL evidence."""
    from aeos.broker.model_proxy import (
        ProxyError,
        _billing_of,
        _retry_after,
        _upstream_headers,
        _usage_of,
        rate_limit_info,
        resolve_upstream,
    )

    action = "broker.route_tool_probe" if tools else "broker.route_feasibility.probe"
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
                     action=action, result="DENIED", target_type="model_route",
                     target_id=route_key, domain="PERSONAL", reason=f"{exc.code}: {exc.message}"[:500])
        raise PolicyDenied(f"route cannot be probed: {exc.message}", code=exc.code) from exc
    spend = _spend_problem(session, route, upstream.base_url)
    if spend:
        raise PolicyDenied(spend[1], code=spend[0])
    # an OpenRouter :free probe counts against the account's free-model limit (decision C): checked and recorded under
    # the same count lock as worker requests, in its own committed transaction, before anything is sent. That needs the
    # caller's session to hold no uncommitted write (it would wait on its own lock): refused up front otherwise.
    if _is_free_model(route) and _holds_uncommitted_writes(session):
        raise Conflict("the probe needs a session without uncommitted writes (commit first): its free-model count is "
                       "recorded in its own transaction; nothing was sent", code="PROBE_SESSION_NOT_CLEAN")
    reservation = _reserve_free_probe(route, upstream.model_id) if _is_free_model(route) else None
    from aeos.cost import facade as cost_facade

    subscription = cost_facade.subscription_terms(route)[0] is not None
    # the route's own request shape (ADR-0041 amendment 2026-10-25): <endpoint>/chat/completions, the bare upstream
    # model id, the route's reasoning level when it states one - the probe tests the configuration that would serve
    prompt_text, max_tokens = (TOOL_PROBE_PROMPT, TOOL_PROBE_MAX_TOKENS) if tools else (PROBE_PROMPT, PROBE_MAX_TOKENS)
    body: dict[str, Any] = {"model": upstream.model_id, "messages": [{"role": "user", "content": prompt_text}],
                            "max_tokens": max_tokens, "temperature": 0, "stream": False, "n": 1}
    if tools:
        body["tools"] = TOOL_PROBE_TOOLS
        body["tool_choice"] = "auto"
    if upstream.reasoning_effort:
        body["reasoning_effort"] = upstream.reasoning_effort
    if upstream.is_openrouter:
        body["usage"] = {"include": True}
    started = time.monotonic()
    status: int | None = None
    error: str | None = None
    data: dict[str, Any] = {}
    retry_after: int | None = None
    rate_limit: dict[str, Any] = {}
    # whether the request reached the upstream (an OpenRouter :free probe counts against the free-model limit when it
    # did): False before the transport is entered and for an error raised before sending; None (unknown, counted) from
    # the moment the transport is entered until the outcome is classified; True once an answer or a sent-side error
    sent: bool | None = False
    settled = reservation is None
    try:
        try:
            own = client is None
            http = client or httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False, trust_env=False)
            try:
                # a probe is its own one-request conversation: its own opaque x-opencode-session on OpenCode Go
                headers = _upstream_headers(upstream, run_id=f"probe:{route_key}:{uuid.uuid4().hex}")
                sent = None  # the transport is entered: anything unclassified from here on may have reached OpenRouter
                resp = http.post(upstream.url, json=body, headers=headers)
                status = resp.status_code
                sent = True
                if status == 200:
                    try:
                        parsed = resp.json()
                        data = parsed if isinstance(parsed, dict) else {}
                    except ValueError:
                        error, status = "invalid upstream JSON", 502
                elif status == 429:
                    retry_after = _retry_after(resp)
                    rate_limit = rate_limit_info(resp)
            except httpx.TimeoutException as exc:
                error = "timeout"
                sent = not isinstance(exc, NOT_SENT_ERRORS)  # a read/write timeout: the request was sent
            except httpx.HTTPError as exc:
                error = type(exc).__name__
                sent = not isinstance(exc, NOT_SENT_ERRORS)
            except httpx.InvalidURL:  # not an HTTPError: the URL was refused before anything was sent
                sent = False
                raise
            finally:
                if own:
                    http.close()
        finally:
            # settled in its own transaction right after the attempt, before (and independent of) any other bookkeeping;
            # also for a BaseException (e.g. Ctrl-C while waiting for the answer), which then propagates unchanged
            if reservation is not None:
                settled = _settle_free_probe(reservation, sent=sent, status=status, outcome=_outcome(status, error),
                                             usage=_usage_of(data), latency_ms=int((time.monotonic() - started) * 1000))
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
            "probe": {"prompt": prompt_text, "data_class": "PUBLIC", "max_tokens": max_tokens, "fallback": "NONE"},
            "checked_at": utcnow().isoformat(),
            "note": "feasibility observation only: route status and eligibility are unchanged (owner decision)",
        }
        if tools:
            verdict, why = tool_call_verdict(data, status=status, outcome=outcome)
            result["probe"].update({"kind": "TOOL_CALL", "tools": [TOOL_PROBE_FUNCTION], "tool_choice": "auto"})
            result["tool_call"] = {"verdict": verdict, "reason": why}
            result["note"] = ("tool-call probe observation: supports_tools, the configuration hash and the route's status "
                              "are unchanged; a PASS makes the route tool-capable for this configuration only")
        billing = _billing_of(data)
        if cost and not subscription:
            result["note"] += "; a free route reported a cost - review its pricing before any activation"
        if prompt is None and completion is None:
            billing["charged_tokens"] = TOOL_PROBE_TOKEN_BOUND if tools else PROBE_TOKEN_BOUND  # own bound, no guess
        ledger, facts = _ledger(session, upstream, route_key, prompt, completion, cost, model_resolved, outcome, billing)
        if subscription:
            result["billing"] = {"billing": "SUBSCRIPTION", "allowance_usd": facts.get("allowance_usd") or "UNKNOWN",
                                 "allowance_basis": facts.get("allowance_basis") or "UNKNOWN",
                                 "billing_source": billing.get("billing_source") or "UNKNOWN",
                                 "beyond_plan_usd": facts.get("beyond_plan_usd")}
            if facts.get("beyond_plan_usd") is not None:
                result["note"] += ("; the subscription route billed this probe beyond the plan (provider-side paid "
                                   "fallback: is Extra Usage ON?) - the route is refused (SUBSCRIPTION_BEYOND_PLAN) until "
                                   "you re-activate it")
        if outcome == "RATE_LIMITED":
            _quota(session, upstream, route_key, retry_after, rate_limit)
        result["ledger"] = ledger
        ev = _evidence(session, route, result)
        result["evidence_id"] = ev
        success = outcome == "REACHABLE"
        if tools:
            from aeos.routing import facade as routing

            routing.tool_probe_recorded(session, route_key=route_key, verdict=result["tool_call"]["verdict"],
                                        evidence_id=ev)
            success = result["tool_call"]["verdict"] == routing.TOOL_PROBE_PASS
        audit.record(session, actor_type="user" if actor.startswith("user:") else "service", actor_id=actor,
                     action=action,
                     result="SUCCESS" if success else "FAILED",
                     target_type="model_route", target_id=route_key, domain="PERSONAL",
                     reason=f"{outcome} {result['tool_call']['verdict']}" if tools else outcome,
                     details={"pool": route.pool, "http_status": status, "latency_ms": latency_ms, "evidence_id": ev,
                              "ledger": ledger, "upstream": _upstream_mark(sent),
                              **({"tool_call": result["tool_call"]} if tools else {})})
        return result
    except AeosError:
        raise  # the free-model count was settled right after the attempt (or stays PENDING: still counted)
    except Exception as exc:
        # e.g. a failed flush (database locked, disk full): the session cannot be used before a rollback. The free-model
        # count does not depend on it: the probe's broker request row was committed before sending and settled after.
        session.rollback()
        what = _attempt_statement(sent=sent, counted=reservation is not None, settled=settled)
        try:
            audit.record(session, actor_type="user" if actor.startswith("user:") else "service", actor_id=actor,
                         action=action, result="FAILED", target_type="model_route",
                         target_id=route_key, domain="PERSONAL",
                         reason=f"PROBE_BOOKKEEPING_FAILED: {type(exc).__name__}: {what}"[:500],
                         details={"pool": route.pool, "http_status": status, "upstream": _upstream_mark(sent)})
            session.flush()
        except Exception:  # the database still refuses: the error below says what happened
            session.rollback()
        raise Blocked(f"the probe result could not be recorded ({type(exc).__name__}): {what}",
                      code="PROBE_BOOKKEEPING_FAILED") from exc


def _upstream_mark(sent: bool | None) -> str:
    return "SENT" if sent else ("UNKNOWN" if sent is None else "NOT_SENT")


def _holds_uncommitted_writes(session: Session) -> bool:
    """Pending ORM changes, or a database transaction with writes already open on the session's connection (pysqlite
    opens one only for a write: reads alone never count)."""
    if session.new or session.dirty or session.deleted:
        return True
    try:
        raw = session.connection().connection.dbapi_connection
    except Exception:  # no connection can be inspected: nothing is held
        return False
    return bool(getattr(raw, "in_transaction", False))


def _attempt_statement(*, sent: bool | None, counted: bool, settled: bool) -> str:
    """What happened to the probe's request, for the error the owner sees (never claims more than is known)."""
    if sent is None:
        return ("it is not known whether the request was sent; it is counted against the OpenRouter free-model limit"
                if counted else "it is not known whether the request was sent")
    if sent:
        return ("the request was sent and is counted against the OpenRouter free-model limit" if counted
                else "the request was sent")
    if not counted:
        return "the request was not sent"
    return ("the request was not sent (its count reservation was released)" if settled else
            "the request was not sent, but its count reservation could not be released and still counts")


def _is_free_model(route: ModelRoute) -> bool:
    return route.provider == "openrouter" and str(route.model_id or "").endswith(":free")


def _reserve_free_probe(route: ModelRoute, upstream_model: str) -> str:
    """Record an OpenRouter ``:free`` probe as a PENDING broker request under the free-model count lock, in its own
    transaction, committed before the request is sent (decision C): concurrent probes and worker requests at the limit
    cannot all pass, and the count never depends on the probe's later bookkeeping. Refused when the limit is reached
    (``FREE_TIER_LIMIT_REACHED``) or the count cannot be taken or recorded (fail closed, nothing is sent)."""
    from aeos.core.db import session_factory

    try:
        from aeos.routing import facade as routing

        lock, limit_of = routing.free_tier_count_lock, routing.free_tier_limit_problem
    except (ImportError, AttributeError) as exc:
        raise PolicyDenied("routing unavailable: the free-model limit cannot be checked; nothing was sent",
                           code="FREE_TIER_LIMIT_REACHED") from exc
    with session_factory()() as s:
        try:
            if not lock(s):
                raise Blocked("the free-model request count is busy; nothing was sent - retry",
                              code="FREE_TIER_LOCK_UNAVAILABLE")
            limit = limit_of(s, route)
            if limit:
                raise PolicyDenied(str(limit.get("reason") or "free-model limit reached"),
                                   code=str(limit.get("code") or "FREE_TIER_LIMIT_REACHED"))
            row = BrokerRequest(grant_id=PROBE_MARK, run_id=str(uuid.uuid4()), project_id=PROBE_MARK, domain="PERSONAL",
                                route_key=route.route_key, provider=route.provider, exposed_model=PROBE_MARK,
                                upstream_model=upstream_model, outcome="PENDING")
            s.add(row)
            s.commit()
            return row.id
        except AeosError:
            s.rollback()
            raise
        except SQLAlchemyError as exc:
            s.rollback()
            raise Blocked("the probe could not be counted before sending; nothing was sent - retry",
                          code="FREE_TIER_LOCK_UNAVAILABLE") from exc


def _settle_free_probe(row_id: str, *, sent: bool | None, status: int | None, outcome: str,
                       usage: tuple[int | None, int | None, float | None, str | None] = (None, None, None, None),
                       latency_ms: int | None = None) -> bool:
    """Settle the probe's broker request row in its own transaction: NOT_SENT (uncounted) only when nothing can have
    left this host; UNKNOWN (counted) when that is not known; else the observed outcome with the usage the upstream
    reported. False when it cannot be written: the row then stays PENDING and keeps counting (the count is never lowered
    by a bookkeeping failure)."""
    from aeos.core.db import session_factory

    prompt, completion, cost, model_resolved = usage
    try:
        with session_factory()() as s:
            row = s.get(BrokerRequest, row_id)
            if row is not None:
                if sent is None:
                    row.outcome = "UNKNOWN"
                elif not sent:
                    row.outcome = "NOT_SENT"
                else:
                    row.outcome = {"REACHABLE": "OK", "RATE_LIMITED": "RATE_LIMITED", "TIMEOUT": "TIMEOUT"}.get(
                        outcome, "UPSTREAM_ERROR")
                row.status_code = status
                row.prompt_tokens, row.completion_tokens, row.cost_usd = prompt, completion, cost
                row.model_resolved = model_resolved[:160] if model_resolved else None
                row.latency_ms = latency_ms
                s.commit()
        return True
    except Exception:  # a database that refuses the write: the PENDING row still counts (conservative)
        log.warning("feasibility probe %s could not be settled; it stays PENDING and counted", row_id)
        return False


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
        return "RECORDED", {k: info[k] for k in ("allowance_usd", "allowance_basis", "beyond_plan_usd") if k in info}
    except ImportError:
        return "LEDGER_UNAVAILABLE", {}
    except AeosError as exc:
        return f"LEDGER_REJECTED:{exc.code}", {}


def _quota(session: Session, upstream: Any, route_key: str, retry_after: int | None,
           rate_limit: dict[str, Any] | None = None) -> None:
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
    if upstream.is_openrouter and str(upstream.model_id).endswith(":free"):
        # the free-model limits are per OpenRouter account (decision C): every free route waits
        info = rate_limit or {}
        try:
            from aeos.routing import facade as routing

            routing.note_free_tier_rate_limited(session, route_key=route_key,
                                                retry_after_seconds=info.get("retry_after"),
                                                headers=info.get("headers"), source="observed-429-probe")
        except (ImportError, AttributeError):
            pass


def _evidence(session: Session, route: ModelRoute, result: dict[str, Any]) -> str | None:
    try:
        from aeos.verification import facade as verification
    except ImportError:
        return None
    environment = {"route_key": route.route_key, "pool": route.pool, "config_hash": route.config_hash,
                   "model_id": route.model_id, "upstream_model": result["model_id"],
                   "upstream_url": result["upstream_url"], "provider": route.provider,
                   "checked_at": result["checked_at"]}
    collector = "aeos-broker/route-feasibility"
    claim = f"Route feasibility probe {route.route_key} ({route.pool}): {result['outcome']}"
    if result.get("tool_call"):
        # the marks routing reads back (routing.facade.tool_capable): bound to the configuration hash probed
        from aeos.routing import facade as routing

        collector = routing.TOOL_PROBE_COLLECTOR
        environment.update({"probe": routing.TOOL_PROBE_MARK, "verdict": result["tool_call"]["verdict"],
                            "outcome": result["outcome"]})
        claim = (f"Route tool-call probe {route.route_key} ({route.pool}): {result['tool_call']['verdict']} "
                 f"({result['outcome']})")
    try:
        ev = verification.collect_evidence(
            session, kind="PROBE", origin="collector", collector_identity=collector, claim=claim, content=result,
            evidence_level="E3", subject_type="model_route", subject_id=route.route_key, domain="PERSONAL",
            classification="PUBLIC", environment=environment)
    except AeosError:
        return None
    return ev.id
