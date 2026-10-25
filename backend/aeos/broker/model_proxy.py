"""OpenAI-compatible model proxy for workers (§25, §26, §42, §50).

Mounted at ``/broker/v1``. Authenticated ONLY by ``Authorization: Bearer <capability token>`` of an
ACTIVE MODEL_PROXY grant; a request carrying the dashboard session cookie is rejected (the proxy
is not a browser API and must never be reachable through ambient cookie auth).

Guarantees:
* the worker only names models from its grant scope; the broker maps them to the route's exact
  upstream model id and strips any field that could make the upstream pick another model;
* the request shape is the route's own configuration (ADR-0041 amendment 2026-10-25): ``<endpoint>/chat/completions``
  with the bare upstream model id (``mode_params.upstream_model``; for OpenCode Zen the catalog id without its
  ``opencode/`` config prefix), the provider's credential only towards the provider's own host, a superseded seed
  endpoint or a non-chat-completions API refused; the reasoning level is the route's (``mode_params.reasoning``),
  never the worker's - a worker-sent ``reasoning_effort`` is dropped and recorded on the BrokerRequest;
* request/token/cost caps and expiry are enforced before forwarding; ``max_tokens`` is clamped;
* upstream 429 -> 429 + ``Retry-After`` to the worker, QuotaState recorded, and NO fallback to any
  other (paid or unapproved) model (§42, §50 "Free quota habis / 429");
* the real provider key is fetched in-process from the secrets facade, registered for redaction,
  and never appears in any response, log, event or audit record;
* usage (prompt/completion tokens, cost when the upstream reports it, else UNKNOWN) goes to the
  cost ledger via ``aeos.cost.facade.record_usage`` and to the grant counters.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from aeos.audit import service as audit
from aeos.broker import grants
from aeos.core.db import session_factory
from aeos.core.errors import AeosError, Blocked, Conflict, PolicyDenied
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.broker_ext import BrokerRequest
from aeos.models.routing import CapabilityGrant, ModelRoute
from aeos.models.workforce import WorkerRun

log = logging.getLogger("aeos.broker.proxy")

router = APIRouter(prefix="/broker/v1", tags=["broker-proxy"])

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
SESSION_COOKIE = "aeos_session"
MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_RETRY_AFTER = 60
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
# Fields forwarded upstream. Anything else (models[], route, provider, transforms, plugins, user,
# ...) is dropped so the worker cannot request routing to another model. ``reasoning_effort`` is NOT forwarded
# from the worker: it is a route configuration value (``mode_params.reasoning``, §23 "mode/thinking params").
ALLOWED_FIELDS = frozenset({
    "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stream", "stream_options",
    "tools", "tool_choice", "response_format", "stop", "seed", "presence_penalty", "frequency_penalty",
    "parallel_tool_calls",
})
REASONING_NOT_SENT = "NOT_SENT"  # BrokerRequest.reasoning_effort when nothing was sent (provider default)
ZEN_CONFIG_PREFIX = "opencode/"  # OpenCode config/catalog format; the Zen API takes the bare model id (S07)


class ProxyError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def _error(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    kind = "rate_limit_error" if status == 429 else "invalid_request_error" if status < 500 else "upstream_error"
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"message": redact_text(message), "type": kind, "code": code}})


@dataclass
class Upstream:
    base_url: str
    api_key: str | None
    provider: str
    route_key: str
    model_id: str  # the upstream (API) model id sent in the request body
    is_openrouter: bool
    reasoning_effort: str | None = None  # set by the route's configuration, None = not sent (provider default)
    catalog_model_id: str | None = None  # ModelRoute.model_id (e.g. ``opencode/<id>``) when it differs

    @property
    def url(self) -> str:
        return f"{self.base_url}/chat/completions"


@dataclass
class Prepared:
    grant_id: str
    run_id: str
    project_id: str
    domain: str
    exposed: str
    upstream: Upstream
    body: dict[str, Any]
    request_id: str
    stream: bool


def _is_loopback_http(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost", "::1"}


# §26 "Jalur terpisah untuk OFFICE, PERSONAL, model worker, dan per proyek": the namespaces a worker
# model credential may come from, per domain. ``platform/`` (controller's own) is never a worker key.
CREDENTIAL_NAMESPACES = {"OFFICE": ("office",), "PERSONAL": ("personal", "workers", "projects")}


def credential_namespace_problem(domain: str, ref: str, *, project_id: str | None = None) -> str | None:
    """Why *ref* may not serve a worker model call of *domain* (None = allowed). Shared with routing
    eligibility so a route whose credential the broker would refuse is never selected."""
    ns, _, name = str(ref or "").partition("/")
    if not ns or not name:
        return "credential reference must be <namespace>/<name>"
    allowed = CREDENTIAL_NAMESPACES.get(domain, ())
    if ns not in allowed:
        if domain == "OFFICE":
            return "OFFICE routes need an office/ credential reference"
        if ns == "office":
            return "office/ credentials are never used outside OFFICE"
        return f"{ns}/ credentials are not worker-model credentials for {domain} (allowed: {', '.join(allowed)})"
    if ns == "projects" and (not project_id or not name.startswith(f"{project_id}/")):
        return "a projects/ credential serves only its own project"
    return None


def _check_credential_domain(domain: str, ref: str, project_id: str | None = None) -> None:
    """§26 separate credential paths: an OFFICE run only ever uses an ``office/`` credential and a
    PERSONAL run never touches one — whatever the route's configuration says."""
    from aeos.secrets.facade import validate_ref

    try:
        validate_ref(str(ref))
    except ValueError as exc:  # malformed reference in the route configuration: fail closed
        raise ProxyError(503, "UPSTREAM_CREDENTIAL_MISSING", "route credential reference is invalid") from exc
    problem = credential_namespace_problem(domain, ref, project_id=project_id)
    if problem:
        raise ProxyError(403, "CREDENTIAL_DOMAIN", problem)


def secret_purpose(ref: str, use: str = "broker.model-proxy") -> str:
    """Purpose string of the secrets contract: ``<namespace>:<use>`` (``projects:<id>:<use>``)."""
    ns, _, name = str(ref).partition("/")
    if ns == "projects":
        return f"projects:{name.split('/', 1)[0]}:{use}"
    return f"{ns}:{use}"


def _fetch_key(secrets: Any, ref: str, purpose: str) -> str:
    # ADR-0042 purpose contract: the purpose scope must equal the reference namespace
    # ("workers:..." for workers/*, "projects:<id>:..." for projects/<id>/*) - see secret_purpose().
    try:
        return secrets.get_secret(ref, purpose=secret_purpose(ref), actor="service:broker")
    except ValueError as exc:  # malformed reference in the route configuration: fail closed
        raise ProxyError(503, "UPSTREAM_CREDENTIAL_MISSING", "route credential reference is invalid") from exc
    except PolicyDenied as exc:  # namespace/purpose scope refused by the secret provider
        raise ProxyError(403, "CREDENTIAL_DOMAIN", f"secret scope refused for {purpose}: {exc.code}") from exc


def route_authorisation_problem(session: Any, route: ModelRoute) -> str | None:
    """§23 "per request: only routes still allowed (hash-bound, unexpired decision, still free or budgeted)".

    Re-checked on every proxied request, not only at admission: a grant can live for hours while the route's
    configuration changes (e.g. the catalog shows a price), its TRIAL expires, its free period ends or the
    owner revokes the approval. The decision lookup recomputes the route's *current* configuration hash."""
    try:
        from aeos.routing import facade as routing

        return routing.route_use_problem(session, route)
    except (ImportError, AttributeError):  # routing unavailable: the approval cannot be checked -> fail closed
        return "routing module unavailable: route approval cannot be checked"


def client_restriction_problem(session: Any, route: ModelRoute) -> str | None:
    """A provider rule on which *client* may call the route (S07: "Free-tier chat models require OpenCode"), not
    decided by the owner: the broker is its own HTTP client, so it never proxies such a route silently."""
    try:
        from aeos.routing import facade as routing

        return routing.client_restriction_problem(route)
    except (ImportError, AttributeError):  # routing unavailable: the restriction cannot be checked -> fail closed
        return "routing module unavailable: the provider client restriction cannot be checked"


def upstream_model_id(route: ModelRoute) -> str:
    """The model id sent upstream: ``mode_params.upstream_model`` when the route states it, else the route's model
    id - for OpenCode Zen without the ``opencode/`` OpenCode-config prefix (S07: API requests use the bare id)."""
    explicit = (route.mode_params or {}).get("upstream_model")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    model = route.model_id or ""
    if route.provider == "opencode-zen" and model.startswith(ZEN_CONFIG_PREFIX):
        return model[len(ZEN_CONFIG_PREFIX):]
    return model


def route_reasoning_effort(route: ModelRoute) -> tuple[str | None, str | None]:
    """(level the broker sends as ``reasoning_effort``, problem). The route's ``mode_params.reasoning`` is the only
    source: a default marker (or nothing) sends no level, so the provider's default applies; an unknown value is
    refused rather than forwarded."""
    from aeos.broker.facade import REASONING_DEFAULT_MARKERS, REASONING_LEVELS

    value = (route.mode_params or {}).get("reasoning")
    if value is None or value in REASONING_DEFAULT_MARKERS:
        return None, None
    if isinstance(value, str) and value in REASONING_LEVELS:
        return value, None
    return None, (f"mode_params.reasoning {str(value)[:32]!r} is neither a reasoning level "
                  f"({', '.join(REASONING_LEVELS)}) nor a default marker ({', '.join(REASONING_DEFAULT_MARKERS)})")


REASONING_UNRECOGNISED = "UNRECOGNISED"  # BrokerRequest.reasoning_requested for a value that is not a known level


def requested_reasoning(body: dict[str, Any]) -> str | None:
    """The reasoning level the worker asked for (``reasoning_effort``, or the OpenRouter-style ``reasoning.effort``);
    neither is ever forwarded. The worker is untrusted, so its free text is never persisted: a known level is
    recorded as such, anything else as ``UNRECOGNISED``. None = the worker asked for nothing."""
    from aeos.broker.facade import REASONING_LEVELS

    value: Any = body.get("reasoning_effort")
    if value is None and isinstance(body.get("reasoning"), dict):
        value = body["reasoning"].get("effort", REASONING_UNRECOGNISED)
    elif value is None and body.get("reasoning") is not None:
        value = REASONING_UNRECOGNISED
    if value is None:
        return None
    return value if isinstance(value, str) and value in REASONING_LEVELS else REASONING_UNRECOGNISED


def request_shape(route: ModelRoute) -> dict[str, Any]:
    """The upstream request the broker would build for *route* (no secret, no DB), with the refusal code/reason
    when it would not build one. The route view shows the same verdict the boundary applies."""
    from aeos.broker.facade import PROVIDER_ENDPOINT_HOSTS, SUPERSEDED_ENDPOINTS, UPSTREAM_API_CHAT

    mp = route.mode_params or {}
    api = str(mp.get("upstream_api") or UPSTREAM_API_CHAT)
    if route.provider == "openrouter":
        base = (route.endpoint or OPENROUTER_BASE).rstrip("/")
    else:
        base = (route.endpoint or "").rstrip("/")
    effort, effort_problem = route_reasoning_effort(route)
    out: dict[str, Any] = {"url": f"{base}/chat/completions" if base else None, "model": upstream_model_id(route),
                           "catalog_model_id": route.model_id, "api": api,
                           "reasoning_effort": effort or REASONING_NOT_SENT, "code": None, "problem": None}

    def refuse(code: str, problem: str) -> dict[str, Any]:
        return {**out, "code": code, "problem": problem}

    if api != UPSTREAM_API_CHAT:
        return refuse("ROUTE_API_UNSUPPORTED", f"the provider documents the {api} API for this model; the broker "
                                                f"speaks {UPSTREAM_API_CHAT} only")
    u = urlparse(base)
    if route.provider == "openrouter":
        if not base.startswith("https://openrouter.ai/"):
            return refuse("ROUTE_ENDPOINT", "openrouter route endpoint must be https://openrouter.ai/...")
    else:
        if base in SUPERSEDED_ENDPOINTS:
            return refuse("ROUTE_ENDPOINT_SUPERSEDED",
                          f"endpoint {base} is a superseded seed value; the provider documents "
                          f"{SUPERSEDED_ENDPOINTS[base]} (E1, ADR-0041 amendment 2026-10-25): run `aeos seed` "
                          "(CANDIDATE rows are corrected; an activated row first needs `aeos routes disable`) and "
                          "activate the corrected configuration")
        if not base or not (u.scheme == "https" or _is_loopback_http(base)) or u.username or u.password:
            return refuse("ROUTE_ENDPOINT", "route endpoint must be https (or loopback http) without credentials")
        hosts = PROVIDER_ENDPOINT_HOSTS.get(route.provider)
        if hosts and (u.scheme != "https" or u.hostname not in hosts):
            return refuse("ROUTE_ENDPOINT", f"the {route.provider} credential is only sent to https://"
                                            f"{' / https://'.join(hosts)}")
        if route.provider == "local" and not _is_loopback_http(base):
            return refuse("ROUTE_ENDPOINT", "a local route is served from this host only (loopback http)")
    if not out["model"]:
        return refuse("ROUTE_MODEL_MISSING", "route has no upstream model id")
    if effort_problem:
        return refuse("ROUTE_MODE_PARAMS", effort_problem)
    return out


def resolve_upstream(session: Any, route_key: str, *, domain: str, project_id: str | None = None,
                     pools: tuple[str, ...] | None = None) -> Upstream:
    """``pools`` (additive, ADR-0111): the pools this caller may serve. The worker proxy uses the default
    ``SERVED_POOLS`` (WORKER); only the owner-triggered feasibility probe passes ``PROBE_POOLS``."""
    from aeos.broker.facade import SERVED_POOLS
    from aeos.secrets import facade as secrets

    allowed_pools = pools or SERVED_POOLS
    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    if route is None:
        raise ProxyError(403, "ROUTE_UNKNOWN", "route in grant scope no longer exists")
    if route.provider == "claude-native" or route.pool == "SUPERVISOR":
        raise ProxyError(403, "ROUTE_NOT_WORKER", "route is not a worker route")
    if route.pool not in allowed_pools:
        # ARENA/COMPARATOR/LOCAL/WATCHLIST never serve worker tasks (ADR-0106); routing refuses such tasks at
        # admission, this is the boundary's second line. Their feasibility runs only through the probe (ADR-0111).
        raise ProxyError(403, "ROUTE_NOT_WORKER",
                         f"route pool {route.pool} has no broker serving path (only {', '.join(allowed_pools)})")
    if route.status not in {"APPROVED", "TRIAL"}:
        raise ProxyError(403, "ROUTE_NOT_APPROVED", f"route status {route.status}")
    if domain not in (route.eligible_domains or []):
        raise ProxyError(403, "ROUTE_DOMAIN", "route not eligible for this domain")
    restriction = client_restriction_problem(session, route)
    if restriction:
        raise ProxyError(403, "CLIENT_TERMS_RESTRICTED", restriction)
    problem = route_authorisation_problem(session, route)
    if problem:
        raise ProxyError(403, "ROUTE_APPROVAL_INVALID", problem)
    shape = request_shape(route)
    if shape["code"]:
        raise ProxyError(403, shape["code"], shape["problem"])
    effort = shape["reasoning_effort"] if shape["reasoning_effort"] != REASONING_NOT_SENT else None
    catalog_id = route.model_id if route.model_id != shape["model"] else None
    cred_ref = (route.mode_params or {}).get("credential_ref")
    purpose = f"model proxy route {route_key}"
    if route.provider == "openrouter":
        base = route.endpoint or OPENROUTER_BASE
        refs = [cred_ref] if cred_ref else (["workers/openrouter", "personal/openrouter"]
                                            if domain == "PERSONAL" else [])
        for ref in refs:
            _check_credential_domain(domain, ref, project_id)
        key = None
        for ref in refs:
            try:
                key = _fetch_key(secrets, ref, purpose)
                break
            except Blocked:
                continue
        if not key:
            raise ProxyError(503, "UPSTREAM_CREDENTIAL_MISSING",
                             "upstream credential not configured (aeos secrets set workers/openrouter)")
        return Upstream(base.rstrip("/"), key, "openrouter", route_key, shape["model"], True, effort, catalog_id)
    base = route.endpoint or ""
    key = None
    if cred_ref:
        _check_credential_domain(domain, cred_ref, project_id)
        try:
            key = _fetch_key(secrets, cred_ref, purpose)
        except Blocked as exc:
            raise ProxyError(503, "UPSTREAM_CREDENTIAL_MISSING", exc.message) from exc
    elif not _is_loopback_http(base):
        raise ProxyError(503, "UPSTREAM_CREDENTIAL_MISSING", "route has no credential_ref")
    return Upstream(base.rstrip("/"), key, route.provider, route_key, shape["model"], False, effort, catalog_id)


def _check_auth_headers(headers: dict[str, str], cookies: dict[str, str]) -> str:
    if SESSION_COOKIE in cookies:
        raise ProxyError(401, "COOKIE_AUTH_REJECTED", "the model proxy accepts capability bearer tokens only")
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ProxyError(401, "CAPABILITY_TOKEN_MISSING", "Authorization: Bearer <capability token> required")
    return auth[7:].strip()


def _authenticate(session: Any, token: str) -> CapabilityGrant:
    try:
        return grants.authenticate(session, token, kind="MODEL_PROXY")
    except AeosError as exc:
        session.commit()  # persist expiry deactivation
        raise ProxyError(exc.status_code, exc.code, exc.message) from exc


def _prepare(headers: dict[str, str], cookies: dict[str, str], body: dict[str, Any]) -> Prepared:
    token = _check_auth_headers(headers, cookies)
    with session_factory()() as s:
        grant = _authenticate(s, token)
        models: dict[str, str] = dict((grant.scope or {}).get("models") or {})
        exposed = str(body.get("model") or "")
        if exposed not in models:
            _deny(s, grant, "UNKNOWN_MODEL", f"model {exposed!r} is not in this capability's scope")
            raise ProxyError(404, "MODEL_NOT_ALLOWED", f"model {exposed!r} is not available for this run")
        rem = grants.remaining(grant)
        if rem["tokens"] is not None and rem["tokens"] <= 0:
            _deny(s, grant, "CAP_EXCEEDED", "token cap exhausted")
            raise ProxyError(403, "CAP_EXCEEDED", "token cap for this run is exhausted")
        cap = float(grant.max_cost_usd) if grant.max_cost_usd is not None else None
        used = float(grant.used_cost_usd or 0)
        if cap is not None and (used > cap or (cap > 0 and used >= cap)):
            _deny(s, grant, "CAP_EXCEEDED", "cost cap exhausted")
            raise ProxyError(403, "CAP_EXCEEDED", "cost cap for this run is exhausted")
        if not grants.consume_request(s, grant.id):
            s.rollback()
            grant = s.get(CapabilityGrant, grant.id)
            _deny(s, grant, "CAP_EXCEEDED", "request cap exhausted")
            raise ProxyError(403, "CAP_EXCEEDED", "request cap for this run is exhausted")
        try:
            upstream = resolve_upstream(s, models[exposed], domain=grant.domain, project_id=grant.project_id)
        except ProxyError as exc:
            _deny(s, grant, exc.code, exc.message, commit=False)
            s.commit()
            raise
        out = {k: v for k, v in body.items() if k in ALLOWED_FIELDS}
        out["model"] = upstream.model_id
        out["n"] = 1
        # §23 "mode/thinking params": the reasoning level is the route's configuration. A level the worker sent is
        # dropped (never forwarded silently) and recorded; the route's own level, if it states one, is sent.
        requested_label = requested_reasoning(body)
        if upstream.reasoning_effort:
            out["reasoning_effort"] = upstream.reasoning_effort
        per_req = (grant.scope or {}).get("per_request_output_cap")
        caps = [int(c) for c in (per_req, rem["tokens"]) if c is not None]
        requested = out.get("max_tokens") or out.get("max_completion_tokens")
        if caps:
            limit = min(caps)
            out["max_tokens"] = min(int(requested), limit) if requested else limit
            out.pop("max_completion_tokens", None)
        stream = bool(out.get("stream"))
        if stream:
            opts = dict(out.get("stream_options") or {})
            opts["include_usage"] = True
            out["stream_options"] = opts
        if upstream.is_openrouter:
            out["usage"] = {"include": True}
        req = BrokerRequest(grant_id=grant.id, run_id=grant.run_id, project_id=grant.project_id, domain=grant.domain,
                            route_key=upstream.route_key, provider=upstream.provider, exposed_model=exposed,
                            upstream_model=upstream.model_id, stream=stream, outcome="PENDING",
                            reasoning_effort=upstream.reasoning_effort or REASONING_NOT_SENT,
                            reasoning_requested=requested_label)
        s.add(req)
        s.commit()
        return Prepared(grant.id, grant.run_id, grant.project_id, grant.domain, exposed, upstream, out, req.id, stream)


def _deny(s: Any, grant: CapabilityGrant, code: str, message: str, *, commit: bool = True) -> None:
    s.add(BrokerRequest(grant_id=grant.id, run_id=grant.run_id, project_id=grant.project_id, domain=grant.domain,
                        outcome="DENIED" if code != "CAP_EXCEEDED" else "CAP_EXCEEDED", error_summary=message[:500]))
    audit.record(s, actor_type="worker", actor_id=f"run:{grant.run_id}", action="broker.model_proxy.deny",
                 result="DENIED", target_type="capability_grant", target_id=grant.id, project_id=grant.project_id,
                 domain=grant.domain, reason=f"{code}: {message}")
    if commit:
        s.commit()


def _finish(p: Prepared, *, status: int, outcome: str, prompt: int | None = None, completion: int | None = None,
            cost: float | None = None, model_resolved: str | None = None, latency_ms: int | None = None,
            retry_after: int | None = None, error: str = "") -> None:
    with session_factory()() as s:
        req = s.get(BrokerRequest, p.request_id)
        if req is not None:
            req.status_code = status
            req.outcome = outcome
            req.prompt_tokens = prompt
            req.completion_tokens = completion
            req.cost_usd = cost
            req.model_resolved = model_resolved
            req.latency_ms = latency_ms
            req.retry_after_seconds = retry_after
            req.error_summary = redact_text(error)[:500]
        tokens = (prompt or 0) + (completion or 0)
        # Usage the upstream reported is consumed even when the stream broke afterwards: it counts
        # against the caps and the ledger (never silently dropped).
        counted = outcome == "OK" or prompt is not None or completion is not None
        if counted:
            grants.add_usage(s, p.grant_id, tokens=tokens, cost_usd=cost)
        run = s.get(WorkerRun, p.run_id)
        ledger = "NOT_RECORDED"
        if counted:
            ledger = _record_ledger(s, p, run, prompt=prompt, completion=completion, cost=cost,
                                    model=model_resolved or p.upstream.model_id)
            if ledger != "RECORDED" and req is not None:
                req.error_summary = f"usage ledger: {ledger}; counted on grant only"[:500]
        if run is not None:
            us = dict(run.usage_summary or {})
            us["requests"] = int(us.get("requests", 0)) + 1
            if counted:
                us["prompt_tokens"] = int(us.get("prompt_tokens", 0)) + (prompt or 0)
                us["completion_tokens"] = int(us.get("completion_tokens", 0)) + (completion or 0)
                if cost is None:
                    us["cost_unknown_requests"] = int(us.get("cost_unknown_requests", 0)) + 1
                else:
                    us["cost_usd"] = round(float(us.get("cost_usd", 0.0)) + cost, 6)
                us["ledger"] = ledger
                if model_resolved:
                    run.model_resolved = model_resolved[:128]
            if outcome == "RATE_LIMITED":
                us["rate_limited"] = int(us.get("rate_limited", 0)) + 1
                run.quota_status = "RATE_LIMITED"
                run.current_action = f"WAITING_QUOTA: upstream 429 (retry-after {retry_after}s)"
            run.usage_summary = us
        if outcome == "RATE_LIMITED":
            _record_quota(s, p, retry_after or DEFAULT_RETRY_AFTER)
        events.emit(s, source="broker", kind="run.model_call", entity_type="run", entity_id=p.run_id,
                    run_id=p.run_id, project_id=p.project_id, domain=p.domain, mode=run.mode if run else "LIVE",
                    payload={"route_key": p.upstream.route_key, "outcome": outcome, "status": status,
                             "prompt_tokens": prompt, "completion_tokens": completion,
                             "cost_usd": cost if cost is not None else "UNKNOWN", "latency_ms": latency_ms,
                             "reasoning_effort": req.reasoning_effort if req is not None else None,
                             "reasoning_requested_dropped": req.reasoning_requested if req is not None else None})
        audit.record(s, actor_type="worker", actor_id=f"run:{p.run_id}", action="broker.model_proxy.call",
                     result="SUCCESS" if outcome == "OK" else "FAILED", target_type="model_route",
                     target_id=p.upstream.route_key, project_id=p.project_id, domain=p.domain,
                     reason=outcome, details={"status": status, "stream": p.stream, "ledger": ledger})
        s.commit()


def _record_ledger(s: Any, p: Prepared, run: WorkerRun | None, *, prompt: int | None, completion: int | None,
                   cost: float | None, model: str) -> str:
    """One broker call -> one ledger entry (§42), committed into the run's budget hold.

    LIVE: ``record_usage(partial=True)`` with the run's reservation (the hold stays until the run ends
    and the scheduler settles it). MOCK/DEMO: recorded with its mode and never against the hold — the
    hold is released instead (ADR-0080 ``NON_LIVE_RESERVATION_COMMIT``). A hold that is already
    settled (late stream end after the run finished) still gets its usage recorded, never dropped."""
    from aeos.cost import facade as cost_facade

    mode = run.mode if run is not None and run.mode else "LIVE"
    reservation_id = run.budget_reservation_id if run is not None else None
    base = dict(provider=p.upstream.provider, category="WORKER_API", source="broker", project_id=p.project_id,
                domain=p.domain, run_id=p.run_id, route_key=p.upstream.route_key, model=model, input_tokens=prompt,
                output_tokens=completion, requests=1, cost_usd=cost, is_estimate=False, mode=mode)
    try:
        if mode != "LIVE":
            cost_facade.record_usage(s, **base)
            if reservation_id:
                cost_facade.release(s, reservation_id)
            return "RECORDED"
        try:
            # record_usage raises Conflict before it writes anything when the hold is not RESERVED
            cost_facade.record_usage(s, **base, reservation_id=reservation_id, partial=True)
        except Conflict:
            cost_facade.record_usage(s, **base)  # hold already settled/released: plain LIVE usage
        return "RECORDED"
    except ImportError:
        return "LEDGER_UNAVAILABLE"
    except AeosError as exc:
        log.warning("usage ledger refused broker usage for run %s: %s", p.run_id, exc.code)
        return f"LEDGER_REJECTED:{exc.code}"


def _record_quota(s: Any, p: Prepared, retry_after: int) -> None:
    """Observed 429 -> QuotaState EXHAUSTED for this provider/route through the cost module (the single
    owner of quota state). No other route is tried (§42, §50 "Free quota habis / 429")."""
    from aeos.cost import facade as cost_facade

    now = utcnow()
    try:
        cost_facade.set_quota_state(
            s, provider=p.upstream.provider, state="EXHAUSTED", source="observed-429", scope=p.upstream.route_key,
            details={"status": 429, "run_id": p.run_id, "retry_after_seconds": retry_after,
                     "note": "observed by broker; no fallback route used"},
            observed_at=now, resets_at=now + timedelta(seconds=retry_after))
    except ImportError:
        log.warning("cost module unavailable: 429 for %s not recorded as quota state", p.upstream.route_key)


def _retry_after(resp: httpx.Response) -> int:
    raw = resp.headers.get("retry-after", "")
    try:
        return max(1, min(int(float(raw)), 3600))
    except ValueError:
        return DEFAULT_RETRY_AFTER


def _usage_of(data: dict[str, Any]) -> tuple[int | None, int | None, float | None, str | None]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    cost = usage.get("cost")
    try:
        cost_f = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_f = None
    model = data.get("model") if isinstance(data.get("model"), str) else None
    return (int(prompt) if isinstance(prompt, int | float) else None,
            int(completion) if isinstance(completion, int | float) else None, cost_f, model)


def _upstream_headers(up: Upstream) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if up.api_key:
        h["Authorization"] = f"Bearer {up.api_key}"
    if up.is_openrouter:
        h["X-Title"] = "DLY AEOS worker broker"
    return h


async def _upstream_failure(p: Prepared, resp: httpx.Response, started: float) -> JSONResponse:
    latency = int((time.monotonic() - started) * 1000)
    text = redact_text((await resp.aread()).decode("utf-8", "replace"))[:400]
    if resp.status_code == 429:
        ra = _retry_after(resp)
        await run_in_threadpool(_finish, p, status=429, outcome="RATE_LIMITED", latency_ms=latency, retry_after=ra,
                                error=text)
        return _error(429, "UPSTREAM_RATE_LIMITED",
                      "upstream quota/rate limit reached; no fallback model is used", retry_after=ra)
    await run_in_threadpool(_finish, p, status=resp.status_code, outcome="UPSTREAM_ERROR", latency_ms=latency, error=text)
    if resp.status_code in (401, 403):
        return _error(502, "UPSTREAM_AUTH_FAILED", "upstream rejected the broker credential")
    if 400 <= resp.status_code < 500:
        return _error(resp.status_code, "UPSTREAM_REJECTED", f"upstream rejected the request: {text[:200]}")
    return _error(502, "UPSTREAM_ERROR", f"upstream error {resp.status_code}")


@router.get("/models")
async def list_models(request: Request) -> Response:
    def _do() -> dict[str, Any]:
        token = _check_auth_headers({k.lower(): v for k, v in request.headers.items()}, dict(request.cookies))
        with session_factory()() as s:
            grant = _authenticate(s, token)
            models = sorted(((grant.scope or {}).get("models") or {}).keys())
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "aeos-broker"} for m in models]}

    try:
        return JSONResponse(await run_in_threadpool(_do), headers={"Cache-Control": "no-store"})
    except ProxyError as exc:
        return _error(exc.status, exc.code, exc.message)


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY_BYTES:
        return _error(413, "BODY_TOO_LARGE", "request body too large")
    # Authentication shape first (no DB): cookie sessions and missing bearer tokens are rejected
    # before any body is interpreted.
    try:
        _check_auth_headers({k.lower(): v for k, v in request.headers.items()}, dict(request.cookies))
    except ProxyError as exc:
        return _error(exc.status, exc.code, exc.message)
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(413, "BODY_TOO_LARGE", "request body too large")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _error(400, "INVALID_JSON", "body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return _error(400, "INVALID_REQUEST", "messages[] required")
    try:
        p = await run_in_threadpool(_prepare, {k.lower(): v for k, v in request.headers.items()},
                                    dict(request.cookies), body)
    except ProxyError as exc:
        return _error(exc.status, exc.code, exc.message, retry_after=exc.retry_after)
    url = p.upstream.url
    started = time.monotonic()
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, follow_redirects=False, trust_env=False)
    try:
        resp = await client.send(client.build_request("POST", url, json=p.body, headers=_upstream_headers(p.upstream)),
                                 stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        await run_in_threadpool(_finish, p, status=504, outcome="TIMEOUT", error="upstream timeout")
        return _error(504, "UPSTREAM_TIMEOUT", "upstream timed out")
    except httpx.HTTPError as exc:
        await client.aclose()
        await run_in_threadpool(_finish, p, status=502, outcome="UPSTREAM_ERROR", error=type(exc).__name__)
        return _error(502, "UPSTREAM_UNREACHABLE", "upstream unreachable")
    if resp.status_code != 200:
        try:
            return await _upstream_failure(p, resp, started)
        finally:
            await resp.aclose()
            await client.aclose()
    if not p.stream:
        try:
            data = json.loads(await resp.aread())
        except (json.JSONDecodeError, httpx.HTTPError):
            await resp.aclose()
            await client.aclose()
            await run_in_threadpool(_finish, p, status=502, outcome="UPSTREAM_ERROR", error="invalid upstream JSON")
            return _error(502, "UPSTREAM_INVALID", "upstream returned invalid JSON")
        await resp.aclose()
        await client.aclose()
        prompt, completion, cost, model = _usage_of(data if isinstance(data, dict) else {})
        await run_in_threadpool(_finish, p, status=200, outcome="OK", prompt=prompt, completion=completion, cost=cost,
                                model_resolved=model, latency_ms=int((time.monotonic() - started) * 1000))
        if isinstance(data, dict):
            data["model"] = p.exposed
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    async def relay():  # type: ignore[no-untyped-def]
        buf = b""
        usage: tuple[int | None, int | None, float | None, str | None] = (None, None, None, None)
        failed = ""
        try:
            async for chunk in resp.aiter_raw():
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line.startswith(b"data:") and line[5:].strip() not in (b"", b"[DONE]"):
                        try:
                            obj = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict):
                            u = _usage_of(obj)
                            if u[0] is not None or u[1] is not None:
                                usage = (u[0], u[1], u[2], u[3] or usage[3])
                            elif u[3]:
                                usage = (usage[0], usage[1], usage[2], u[3])
                yield chunk
        except httpx.HTTPError as exc:
            failed = type(exc).__name__
        finally:
            # Shielded: a worker killed mid-stream (cancel) must still have its usage recorded.
            with anyio.CancelScope(shield=True):
                await resp.aclose()
                await client.aclose()
                await run_in_threadpool(
                    _finish, p, status=200, outcome="OK" if not failed else "UPSTREAM_ERROR", prompt=usage[0],
                    completion=usage[1], cost=usage[2], model_resolved=usage[3],
                    latency_ms=int((time.monotonic() - started) * 1000), error=failed)

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
