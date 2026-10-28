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
* OpenCode Go (ADR-0041 amendment 2026-10-26): the broker identifies itself with its own User-Agent and sends a
  stable, opaque ``x-opencode-session`` per worker run (a hash of the run id, never a secret), as the Go docs ask
  of clients other than OpenCode;
* request/token/cost caps and expiry are enforced before forwarding; ``max_tokens`` is clamped;
* nothing sensitive leaves the host (ADR-0127, owner request 2026-10-28): before any request is forwarded - every route
  and pool, worker and probe traffic alike - every string of the forwarded payload is scanned with the one secret
  detector (``aeos.core.redaction``: token shapes and the secret values registered at runtime). A match is never sent:
  403 ``SENSITIVE_CONTENT_BLOCKED``, a ``SENSITIVE_BLOCKED`` broker request, an audit record and a run event naming the
  detector kinds with the offset and length of each match (never the text and nothing derived from it: an unkeyed
  hash of a short registered secret could be brute-forced), and the executor stops the run. No setting disables or
  narrows it;
* upstream 429 -> 429 + ``Retry-After`` to the worker, QuotaState recorded, and NO fallback to any
  other (paid or unapproved) model (§42, §50 "Free quota habis / 429");
* the real provider key is fetched in-process from the secrets facade, registered for redaction,
  and never appears in any response, log, event or audit record;
* usage (prompt/completion tokens, cost when the upstream reports it, else UNKNOWN) goes to the
  cost ledger via ``aeos.cost.facade.record_usage`` and to the grant counters;
* a SUBSCRIPTION route (ADR-0080 amendment 2026-10-26, OpenCode Go): ``usage.cost`` is the marginal charge (0 within
  the plan) and ``usage.opencode.allowance`` / ``billing_source`` go to the ledger as the allowance the call consumed
  (the cost module estimates it from the tokens, or from the request's upper bound - clamped ``max_tokens`` plus the
  prompt size - when the provider reports no usage) - never the reservation as dollars. A provider dollar figure above
  ``MAX_PROVIDER_USD`` is unreported. A call billed beyond the plan (a charge, a non-plan ``billing_source``) refuses
  the route on every later request until the owner re-activates it (``SUBSCRIPTION_BEYOND_PLAN``, fail closed);
* an OpenRouter ``:free`` WORKER route (owner decision C, ADR-0041 amendment 2026-10-26) serves only a run whose
  TaskSpec is PUBLIC and only while it meets decision C (``FREE_WORKER_ROUTE_INELIGIBLE``, 403); a call of any ``:free``
  model over the account's free-model limit (requests/minute, requests/UTC day, a recorded 429) is refused with 429 +
  Retry-After (``FREE_TIER_LIMIT_REACHED``) before it counts against the grant - never sent upstream, never another
  route. An upstream 429 of a ``:free`` model is also recorded account-wide (every free route waits).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from aeos import __version__
from aeos.audit import service as audit
from aeos.broker import grants
from aeos.broker.facade import SENSITIVE_BLOCKED_OUTCOME, SENSITIVE_CONTENT_BLOCKED
from aeos.core.db import session_factory
from aeos.core.errors import AeosError, Blocked, Conflict, PolicyDenied
from aeos.core.redaction import find_high_confidence_secrets, redact_text
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
# OpenCode Go (ADR-0041 amendment 2026-10-26, E1 Go docs read 2026-10-26): ``opencode-go/<id>`` is the OpenCode config
# format, the API takes the bare id; clients other than OpenCode identify themselves with their own User-Agent ("rather
# than a generic SDK or HTTP-library name") and send a stable ``x-opencode-session`` per conversation ("Go requires it").
GO_PROVIDER = "opencode-go"
GO_CONFIG_PREFIX = "opencode-go/"
BROKER_USER_AGENT = f"dly-aeos-broker/{__version__}"
OPENCODE_SESSION_HEADER = "x-opencode-session"
CONFIG_PREFIXES = {"opencode-zen": ZEN_CONFIG_PREFIX, GO_PROVIDER: GO_CONFIG_PREFIX}
# A provider-stated dollar figure above this is not a real figure (a broken upstream or proxy): treated as unreported
# (ADR-0080 amendment 2026-10-26). On a SUBSCRIPTION route an unreadable stated cost is paid usage beyond the plan.
MAX_PROVIDER_USD = 1e9
# Chat-template tokens the prompt-size bound adds on top of the request body's UTF-8 bytes (a byte-level tokenizer never
# makes more tokens than bytes): part of the upper bound recorded when the upstream reports no usage.
TEMPLATE_TOKEN_MARGIN = 64
# ADR-0127 (owner request 2026-10-28): the outgoing-content guard. No setting turns it off or narrows it.
SENSITIVE_FINDINGS_RECORDED = 20  # findings kept on one audit record (the total is recorded beside them)
# Joins the payload's strings for one scan: no token pattern crosses it (none of their character classes holds a NUL).
_SCAN_SEPARATOR = "\n\x00\n"


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
    subscription: bool = False  # flat-fee SUBSCRIPTION route (``pricing.billing``, ADR-0080 amendment 2026-10-26)

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
    token_cap: int | None = None  # the grant's token cap (an upper bound of any one call of the run)


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


def is_subscription_route(route: ModelRoute) -> bool:
    return isinstance(route.pricing, dict) and route.pricing.get("billing") == "SUBSCRIPTION"


def subscription_beyond_plan_problem(session: Any, route: ModelRoute) -> str | None:
    """A SUBSCRIPTION route with a call billed beyond its plan (provider-side paid fallback, e.g. Extra Usage ON) that
    the owner has not re-activated the route after: refused per request, so the run that hit it stops too, not only
    new admissions (ADR-0080 amendment 2026-10-26; invariant 8, fail closed)."""
    if not is_subscription_route(route):
        return None
    try:
        from aeos.cost import facade as cost

        bp = cost.subscription_beyond_plan(session, route)
    except (ImportError, AttributeError):  # cost module unavailable: the billing state cannot be checked
        return "cost module unavailable: the subscription route's billing state cannot be checked (fail closed)"
    if bp is None:
        return None
    return (f"a call at {bp.get('at')} on subscription route {route.route_key} was billed beyond the plan (billing "
            f"source {bp.get('billing_source') or 'not reported'}); refused until the owner checks that Extra Usage "
            "is OFF and re-activates the route")


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
    id - for OpenCode Zen/Go without the ``opencode/`` / ``opencode-go/`` OpenCode-config prefix (S07 and the Go
    docs: API requests use the bare id)."""
    explicit = (route.mode_params or {}).get("upstream_model")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    model = route.model_id or ""
    prefix = CONFIG_PREFIXES.get(route.provider or "")
    if prefix and model.startswith(prefix):
        return model[len(prefix):]
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
    beyond = subscription_beyond_plan_problem(session, route)
    if beyond:
        raise ProxyError(403, "SUBSCRIPTION_BEYOND_PLAN", beyond)
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
    return Upstream(base.rstrip("/"), key, route.provider, route_key, shape["model"], False, effort, catalog_id,
                    is_subscription_route(route))


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
        # ADR-0127: the outgoing-content guard, before anything counts against the grant or a free-model limit
        findings = sensitive_content_findings({k: v for k, v in body.items() if k in ALLOWED_FIELDS})
        if findings:
            _sensitive_blocked(s, grant, models[exposed], exposed, findings)
            raise ProxyError(403, SENSITIVE_CONTENT_BLOCKED, sensitive_content_message(findings))
        rem = grants.remaining(grant)
        if rem["tokens"] is not None and rem["tokens"] <= 0:
            _deny(s, grant, "CAP_EXCEEDED", "token cap exhausted")
            raise ProxyError(403, "CAP_EXCEEDED", "token cap for this run is exhausted")
        cap = float(grant.max_cost_usd) if grant.max_cost_usd is not None else None
        used = float(grant.used_cost_usd or 0)
        if cap is not None and (used > cap or (cap > 0 and used >= cap)):
            _deny(s, grant, "CAP_EXCEEDED", "cost cap exhausted")
            raise ProxyError(403, "CAP_EXCEEDED", "cost cap for this run is exhausted")
        # decision C (ADR-0041 amendment 2026-10-26), before the request counts against the grant: a free OpenRouter
        # WORKER route serves PUBLIC tasks only, and every OpenRouter :free call respects the account's free-model limits.
        # The count and this request's PENDING row are one atomic step: the SQLite write lock is taken first and held
        # until the commit below, so concurrent requests cannot all pass the same count.
        if _is_free_model(s, models[exposed]):
            # end this transaction's reads first: under WAL a transaction that read before another writer committed
            # cannot take the write lock (SQLITE_BUSY_SNAPSHOT); the lock is the first statement of the next one
            s.commit()
            if not _free_count_lock(s):  # nothing is written: the database is busy beyond the timeout
                s.rollback()
                raise ProxyError(503, "FREE_TIER_LOCK_UNAVAILABLE", "the free-model request count is busy; retry",
                                 retry_after=1)
        refused = free_tier_refusal(s, grant, models[exposed])
        if refused is not None:
            status, code, message, retry = refused
            if status == 429:
                _rate_limited_locally(s, grant, models[exposed], code, message, retry)
            else:
                _deny(s, grant, code, message)
            raise ProxyError(status, code, message, retry_after=retry)
        if not grants.consume_request(s, grant.id):
            s.rollback()
            fresh = s.get(CapabilityGrant, grant.id)
            assert fresh is not None  # authenticated in this session a moment ago
            _deny(s, fresh, "CAP_EXCEEDED", "request cap exhausted")
            raise ProxyError(403, "CAP_EXCEEDED", "request cap for this run is exhausted")
        try:
            upstream = resolve_upstream(s, models[exposed], domain=grant.domain, project_id=grant.project_id,
                                        pools=_eval_pools(s, grant, models[exposed]))
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
        return Prepared(grant.id, grant.run_id, grant.project_id, grant.domain, exposed, upstream, out, req.id, stream,
                        grant.max_tokens)


FREE_WORKER_INELIGIBLE = "FREE_WORKER_ROUTE_INELIGIBLE"


def _is_free_model(s: Any, route_key: str) -> bool:
    """Whether *route_key* is an OpenRouter ``:free`` model (its calls count against the account's free limits)."""
    route = s.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    return route is not None and route.provider == "openrouter" and str(route.model_id or "").endswith(":free")


def _free_count_lock(s: Any) -> bool:
    try:
        from aeos.routing import facade as routing

        return bool(routing.free_tier_count_lock(s))
    except (ImportError, AttributeError):  # routing unavailable: the count cannot be serialised (fail closed)
        return False


def free_tier_refusal(s: Any, grant: CapabilityGrant, route_key: str) -> tuple[int, str, str, int | None] | None:
    """(status, code, message, retry-after) when decision C refuses this request; None otherwise (ADR-0041 amendment
    2026-10-26). A free OpenRouter WORKER route must still meet its eligibility rules and the run's TaskSpec must be
    PUBLIC (403 ``FREE_WORKER_ROUTE_INELIGIBLE``); a call of an OpenRouter ``:free`` model over the account's
    free-model limit is refused with 429 + Retry-After (``FREE_TIER_LIMIT_REACHED``) - never sent upstream, never
    another route. Routing unavailable -> an OpenRouter ``:free`` route is refused (fail closed)."""
    route = s.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    if route is None:
        return None  # resolve_upstream refuses it (ROUTE_UNKNOWN)
    try:
        from aeos.routing import facade as routing

        problem = routing.free_worker_problem(s, route, run_id=grant.run_id)
        limit = None if problem else routing.free_tier_limit_problem(s, route)
    except (ImportError, AttributeError):
        if route.provider == "openrouter" and str(route.model_id or "").endswith(":free"):
            return 403, FREE_WORKER_INELIGIBLE, "routing module unavailable: decision C cannot be checked", None
        return None  # any other route: resolve_upstream's per-request check fails closed on its own
    if problem:
        return 403, FREE_WORKER_INELIGIBLE, f"free OpenRouter WORKER route refused: {problem}", None
    if limit:
        retry = limit.get("retry_after_seconds")
        return (429, str(limit.get("code") or "FREE_TIER_LIMIT_REACHED"), str(limit.get("reason") or ""),
                int(retry) if isinstance(retry, int) else DEFAULT_RETRY_AFTER)
    return None


def _rate_limited_locally(s: Any, grant: CapabilityGrant, route_key: str, code: str, message: str,
                          retry_after: int | None) -> None:
    """A request the broker refused at the free-model limit: recorded RATE_LIMITED (the executor stops a run after
    repeated rate limits and the task waits) but never forwarded, so it carries no upstream model and does not count
    as a request OpenRouter saw."""
    s.add(BrokerRequest(grant_id=grant.id, run_id=grant.run_id, project_id=grant.project_id, domain=grant.domain,
                        route_key=route_key, outcome="RATE_LIMITED", status_code=429, retry_after_seconds=retry_after,
                        error_summary=redact_text(f"{code}: {message}")[:500]))
    run = s.get(WorkerRun, grant.run_id)
    if run is not None:
        run.quota_status = "RATE_LIMITED"
        run.current_action = f"WAITING_QUOTA: {code} (retry-after {retry_after}s)"
    audit.record(s, actor_type="worker", actor_id=f"run:{grant.run_id}", action="broker.model_proxy.deny",
                 result="DENIED", target_type="capability_grant", target_id=grant.id, project_id=grant.project_id,
                 domain=grant.domain, reason=f"{code}: {message}"[:500])
    s.commit()


def _eval_pools(s: Any, grant: CapabilityGrant, route_key: str) -> tuple[str, ...] | None:
    """The pools this request may be served from. Ordinary grants: ``SERVED_POOLS`` (None = default). A grant issued
    with an evaluation scope (``grants.EVAL_SCOPE_KEY``) for *route_key* additionally serves that route's eval-only pool
    - only while its TaskSpec is still bound to the same RUNNING, servable evaluation (re-checked per request; the
    run stopping, the binding becoming invalid, the route's decision/price/terms changing refuse the next request)."""
    from aeos.broker.facade import EVAL_POOLS, SERVED_POOLS

    ev = (grant.scope or {}).get(grants.EVAL_SCOPE_KEY)
    if not isinstance(ev, dict) or ev.get("route_key") != route_key:
        return None
    current, problem = grants.eval_scope(s, run_id=grant.run_id, route_key=route_key)
    if current is None or current.get("eval_run_id") != ev.get("eval_run_id"):
        raise ProxyError(403, "EVAL_SCOPE_INVALID", problem or "the task is bound to another evaluation now")
    return tuple(SERVED_POOLS) + tuple(EVAL_POOLS)


def outgoing_text(payload: Any) -> str:
    """Every string of *payload* - mapping keys and values, list items, at any depth - joined for one scan: message
    contents of every role, tool calls and their arguments, tool definitions, stop sequences, response schemas. A tool
    call's ``arguments`` is a JSON-encoded string: its decoded strings are scanned as well, so a JSON escape
    (``\\u0067``) does not hide a token."""
    parts: list[str] = []
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key, value in item.items():
                if isinstance(key, str):
                    parts.append(key)
                if key == "arguments" and isinstance(value, str):
                    try:
                        stack.append(json.loads(value))
                    except (ValueError, RecursionError):
                        pass  # not JSON: the string itself is scanned
                stack.append(value)
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return _SCAN_SEPARATOR.join(parts)


def sensitive_content_findings(payload: Any) -> list[dict[str, Any]]:
    """The secrets *payload* would carry upstream (ADR-0127): one ``{"kind", "offset", "length"}`` per distinct
    detector kind and match (offset into the scanned text, ``outgoing_text``), sorted - never the matched text and
    nothing derived from it (an unkeyed hash of a short registered secret, such as a password, could be brute-forced;
    CodeQL py/weak-sensitive-data-hashing, review of PR #51). Empty when the payload may be sent. Detection is the one
    owner of secret detection, ``aeos.core.redaction.find_high_confidence_secrets`` (token shapes and the registered
    runtime secret values); personal data such as e-mail addresses is not blocked (ADR-0127: code is full of them)."""
    text = outgoing_text(payload)
    found = {(f.kind, f.start, f.end - f.start) for f in find_high_confidence_secrets(text)}
    return [{"kind": kind, "offset": offset, "length": length} for kind, offset, length in sorted(found)]


def sensitive_content_message(findings: list[dict[str, Any]]) -> str:
    kinds = ", ".join(sorted({f["kind"] for f in findings}))
    return (f"the request was not sent: it carries a credential ({kinds}). Remove the secret from the conversation "
            "(do not read key, environment or token files into the prompt) and start again; nothing sensitive leaves "
            "this host in a model request")


def _sensitive_blocked(s: Any, grant: CapabilityGrant, route_key: str, exposed: str,
                       findings: list[dict[str, Any]]) -> None:
    """A request refused by the outgoing-content guard (ADR-0127): never forwarded and never counted against a
    free-model limit (no upstream model), recorded for the run (the executor stops it), the audit trail and the run
    timeline."""
    kinds = sorted({f["kind"] for f in findings})
    summary = f"{SENSITIVE_CONTENT_BLOCKED}: {', '.join(kinds)}"
    s.add(BrokerRequest(grant_id=grant.id, run_id=grant.run_id, project_id=grant.project_id, domain=grant.domain,
                        route_key=route_key, exposed_model=exposed, outcome=SENSITIVE_BLOCKED_OUTCOME,
                        status_code=403, error_summary=summary[:500]))
    run = s.get(WorkerRun, grant.run_id)
    if run is not None:
        us = dict(run.usage_summary or {})
        us["sensitive_blocked"] = int(us.get("sensitive_blocked", 0)) + 1
        run.usage_summary = us
        run.current_action = f"{SENSITIVE_CONTENT_BLOCKED}: request not sent ({', '.join(kinds)})"
    details = {"route_key": route_key, "kinds": kinds, "findings": findings[:SENSITIVE_FINDINGS_RECORDED],
               "findings_total": len(findings), "sent": False}
    audit.record(s, actor_type="worker", actor_id=f"run:{grant.run_id}", action="broker.model_proxy.sensitive_blocked",
                 result="DENIED", target_type="model_route", target_id=route_key, project_id=grant.project_id,
                 domain=grant.domain, reason=summary[:500], details=details)
    events.emit(s, source="broker", kind="run.model_call_blocked", entity_type="run", entity_id=grant.run_id,
                run_id=grant.run_id, project_id=grant.project_id, domain=grant.domain,
                mode=run.mode if run is not None and run.mode else "LIVE",
                payload={**details, "code": SENSITIVE_CONTENT_BLOCKED,
                         "label": f"Permintaan model ditahan, tidak dikirim: berisi kredensial ({', '.join(kinds)})",
                         "detail": SENSITIVE_CONTENT_BLOCKED})
    s.commit()


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
            retry_after: int | None = None, error: str = "", billing: dict[str, Any] | None = None,
            rate_limit: dict[str, Any] | None = None) -> None:
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
        recorded: dict[str, Any] = {}
        if counted and prompt is None and completion is None and p.upstream.subscription:
            # no usage reported: the call's upper bound, so the allowance is ESTIMATED, never UNKNOWN
            bound = request_token_bound(p.body, p.token_cap)
            if bound is not None:
                billing = {**(billing or {}), "charged_tokens": bound}
        if counted:
            ledger = _record_ledger(s, p, run, prompt=prompt, completion=completion, cost=cost,
                                    model=model_resolved or p.upstream.model_id, billing=billing, facts=recorded)
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
                if recorded.get("billing") == "SUBSCRIPTION":
                    # the plan allowance this run consumed (REPORTED by the provider or ESTIMATED), apart from money
                    us["billing"] = "SUBSCRIPTION"
                    if recorded.get("allowance_usd") is not None:
                        us["allowance_usd"] = round(float(us.get("allowance_usd", 0.0))
                                                    + float(recorded["allowance_usd"]), 6)
                    if recorded.get("allowance_basis") != "REPORTED":
                        us["allowance_not_reported_requests"] = int(us.get("allowance_not_reported_requests", 0)) + 1
                if model_resolved:
                    run.model_resolved = model_resolved[:128]
            if outcome == "RATE_LIMITED":
                us["rate_limited"] = int(us.get("rate_limited", 0)) + 1
                run.quota_status = "RATE_LIMITED"
                run.current_action = f"WAITING_QUOTA: upstream 429 (retry-after {retry_after}s)"
            run.usage_summary = us
        if outcome == "RATE_LIMITED":
            _record_quota(s, p, retry_after or DEFAULT_RETRY_AFTER, rate_limit)
        events.emit(s, source="broker", kind="run.model_call", entity_type="run", entity_id=p.run_id,
                    run_id=p.run_id, project_id=p.project_id, domain=p.domain, mode=run.mode if run else "LIVE",
                    payload={"route_key": p.upstream.route_key, "outcome": outcome, "status": status,
                             "prompt_tokens": prompt, "completion_tokens": completion,
                             "cost_usd": cost if cost is not None else "UNKNOWN", "latency_ms": latency_ms,
                             **({"allowance_usd": recorded.get("allowance_usd"),
                                 "allowance_basis": recorded.get("allowance_basis")}
                                if recorded.get("billing") == "SUBSCRIPTION" else {}),
                             "reasoning_effort": req.reasoning_effort if req is not None else None,
                             "reasoning_requested_dropped": req.reasoning_requested if req is not None else None})
        audit.record(s, actor_type="worker", actor_id=f"run:{p.run_id}", action="broker.model_proxy.call",
                     result="SUCCESS" if outcome == "OK" else "FAILED", target_type="model_route",
                     target_id=p.upstream.route_key, project_id=p.project_id, domain=p.domain,
                     reason=outcome, details={"status": status, "stream": p.stream, "ledger": ledger})
        s.commit()


def _record_ledger(s: Any, p: Prepared, run: WorkerRun | None, *, prompt: int | None, completion: int | None,
                   cost: float | None, model: str, billing: dict[str, Any] | None = None,
                   facts: dict[str, Any] | None = None) -> str:
    """One broker call -> one ledger entry (§42), committed into the run's budget hold. ``billing``: the provider's
    billing facts of the call; ``facts`` receives the entry's subscription allowance (ADR-0080 amendment 2026-10-26).

    LIVE: ``record_usage(partial=True)`` with the run's reservation (the hold stays until the run ends
    and the scheduler settles it). MOCK/DEMO: recorded with its mode and never against the hold — the
    hold is released instead (ADR-0080 ``NON_LIVE_RESERVATION_COMMIT``). A hold that is already
    settled (late stream end after the run finished) still gets its usage recorded, never dropped."""
    from aeos.cost import facade as cost_facade

    mode = run.mode if run is not None and run.mode else "LIVE"
    reservation_id = run.budget_reservation_id if run is not None else None
    base: dict[str, Any] = dict(provider=p.upstream.provider, category="WORKER_API", source="broker", project_id=p.project_id,
                domain=p.domain, run_id=p.run_id, route_key=p.upstream.route_key, model=model, input_tokens=prompt,
                output_tokens=completion, requests=1, cost_usd=cost, is_estimate=False, mode=mode)
    if billing:
        base["details"] = dict(billing)
    try:
        if mode != "LIVE":
            entry = cost_facade.record_usage(s, **base)
            if reservation_id:
                cost_facade.release(s, reservation_id)
        else:
            try:
                # record_usage raises Conflict before it writes anything when the hold is not RESERVED
                entry = cost_facade.record_usage(s, **base, reservation_id=reservation_id, partial=True)
            except Conflict:
                entry = cost_facade.record_usage(s, **base)  # hold already settled/released: plain LIVE usage
    except ImportError:
        return "LEDGER_UNAVAILABLE"
    except AeosError as exc:
        log.warning("usage ledger refused broker usage for run %s: %s", p.run_id, exc.code)
        return f"LEDGER_REJECTED:{exc.code}"
    if facts is not None:
        info = getattr(entry, "details", None) or {}
        facts.update({k: info[k] for k in ("billing", "allowance_usd", "allowance_basis") if k in info})
    return "RECORDED"


def _record_quota(s: Any, p: Prepared, retry_after: int, rate_limit: dict[str, Any] | None = None) -> None:
    """Observed 429 -> QuotaState EXHAUSTED for this provider/route through the cost module (the single
    owner of quota state). No other route is tried (§42, §50 "Free quota habis / 429"). An OpenRouter ``:free`` model
    is limited per account (shared by every ``:free`` model, decision C): the 429 is also recorded account-wide, so
    every free OpenRouter route waits."""
    from aeos.cost import facade as cost_facade

    now = utcnow()
    resets_at = now + timedelta(seconds=retry_after)
    cooldown: dict[str, Any] | None = None
    try:
        # ADR-0124 cool-down: 60 s doubling per repeat up to 30 minutes, or the provider's longer Retry-After; new
        # tasks skip the route until it ends, a running attempt stays on it (no other route is tried here)
        from aeos.routing import facade as routing

        cooldown = routing.route_cooldown(s, provider=p.upstream.provider, route_key=p.upstream.route_key,
                                          reported_seconds=(rate_limit or {}).get("retry_after"))
        resets_at = max(resets_at, datetime.fromisoformat(str(cooldown["resets_at"])))
    except (ImportError, AttributeError, KeyError, ValueError):
        log.warning("routing unavailable: 429 for %s recorded without a cool-down", p.upstream.route_key)
    try:
        cost_facade.set_quota_state(
            s, provider=p.upstream.provider, state="EXHAUSTED", source="observed-429", scope=p.upstream.route_key,
            details={"status": 429, "run_id": p.run_id, "retry_after_seconds": retry_after,
                     "note": "observed by broker; no fallback route used",
                     **({"cooldown": cooldown} if cooldown else {})},
            observed_at=now, resets_at=resets_at)
    except ImportError:
        log.warning("cost module unavailable: 429 for %s not recorded as quota state", p.upstream.route_key)
    if p.upstream.is_openrouter and p.upstream.model_id.endswith(":free"):
        info = rate_limit or {}
        try:
            from aeos.routing import facade as routing

            routing.note_free_tier_rate_limited(s, route_key=p.upstream.route_key,
                                                retry_after_seconds=info.get("retry_after"),
                                                headers=info.get("headers"), run_id=p.run_id)
        except (ImportError, AttributeError):
            log.warning("routing unavailable: free-model 429 for %s recorded on the route only", p.upstream.route_key)


def _note_cooldown(p: Prepared, status: int, reported: int | None) -> None:
    """A provider 5xx starts a cool-down of the route (ADR-0124): new tasks skip it until it ends; the running attempt
    keeps its route and its requests are not refused for it. Never a fallback route here."""
    try:
        from aeos.routing import facade as routing

        with session_factory()() as s:
            routing.note_route_cooldown(s, provider=p.upstream.provider, route_key=p.upstream.route_key, status=status,
                                        reported_seconds=reported, run_id=p.run_id)
            s.commit()
    except (ImportError, AttributeError):
        log.warning("routing unavailable: provider %s for %s recorded without a cool-down", status,
                    p.upstream.route_key)


def reported_retry_after(resp: httpx.Response) -> int | None:
    """The Retry-After the upstream sent (seconds or an HTTP date, 1..86400), or None when it sent none readable."""
    return parse_retry_after(resp.headers.get("retry-after"))


def rate_limit_info(resp: httpx.Response) -> dict[str, Any]:
    """What a 429 says about the limit it hit: the reported Retry-After and the ``X-RateLimit-*`` headers."""
    headers: dict[str, str] = {}
    for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
        value = resp.headers.get(name)
        if value is not None:
            headers[name] = redact_text(str(value))[:32]
    return {"retry_after": reported_retry_after(resp), "headers": headers}


def parse_retry_after(raw: str | None, *, cap: int = 86400) -> int | None:
    """A ``Retry-After`` value as seconds (1..*cap*): delta-seconds or an HTTP date (RFC 9110). None when it is absent
    or unreadable: text, ``inf``/``nan``, a negative delta, or a date no datetime can hold (a huge year, hour or offset
    - never an exception: a 429 is always recorded as one). A date in the past is 1 s."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - utcnow()).total_seconds()
        except (TypeError, ValueError, IndexError, ArithmeticError):  # ArithmeticError includes OverflowError
            return None
    else:
        if seconds < 0:  # delta-seconds is a non-negative number: a negative one is not a readable hint
            return None
    if not math.isfinite(seconds):
        return None
    return max(1, min(int(seconds), cap))


def _retry_after(resp: httpx.Response) -> int:
    """The Retry-After to hand back to the worker (capped at an hour), else the default."""
    parsed = parse_retry_after(resp.headers.get("retry-after"), cap=3600)
    return parsed if parsed is not None else DEFAULT_RETRY_AFTER


def request_token_bound(body: dict[str, Any], token_cap: int | None = None) -> int | None:
    """Upper bound of one forwarded request's tokens when the upstream reports no usage (ADR-0080 amendment
    2026-10-26): the clamped ``max_tokens`` the broker sent plus the prompt size (the UTF-8 bytes of the whole forwarded
    body - a byte-level tokenizer never makes more tokens than bytes - plus a chat-template margin); else the grant's
    token cap. None when nothing bounds the call."""
    out = body.get("max_tokens")
    if isinstance(out, int) and not isinstance(out, bool) and out >= 0:
        try:
            prompt = len(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")) + TEMPLATE_TOKEN_MARGIN
        except (TypeError, ValueError):
            prompt = None
        if prompt is not None:
            return out + prompt
    if isinstance(token_cap, int) and not isinstance(token_cap, bool) and token_cap >= 0:
        return token_cap
    return None


def _provider_usd(value: Any) -> float | None:
    """A provider-stated dollar figure: finite, >= 0 and <= ``MAX_PROVIDER_USD``; None otherwise (unreported)."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and 0 <= f <= MAX_PROVIDER_USD else None


def _usage_of(data: dict[str, Any]) -> tuple[int | None, int | None, float | None, str | None]:
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    cost = usage.get("cost")
    cost_f = _provider_usd(cost) if cost is not None else None  # absurd or unreadable: unreported
    model = data.get("model") if isinstance(data.get("model"), str) else None
    return (int(prompt) if isinstance(prompt, int | float) else None,
            int(completion) if isinstance(completion, int | float) else None, cost_f, model)


def _billing_of(data: dict[str, Any]) -> dict[str, Any]:
    """Provider billing facts beyond the parsed ``usage.cost`` (OpenCode Go API docs, E1 2026-10-26):
    ``usage.opencode.allowance`` (dollars of the plan allowance the request counted) and ``usage.opencode.billing_source``
    (e.g. go / go-plus / balance). Only finite numbers in [0, ``MAX_PROVIDER_USD``] and short source names are kept;
    an absurd figure (e.g. 1e25) is unreported. A ``usage.cost`` the provider stated but that is unreadable or absurd is
    flagged ``cost_reported_unusable``, and a stated ``usage.opencode`` / ``billing_source`` that is not readable is
    flagged ``billing_source_unreadable`` (both count as paid usage beyond the plan on a SUBSCRIPTION route). {} when
    nothing applies."""
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    out: dict[str, Any] = {}
    if usage.get("cost") is not None and _provider_usd(usage.get("cost")) is None:
        out["cost_reported_unusable"] = True
    oc = usage.get("opencode")
    if oc is None:
        return out
    if not isinstance(oc, dict):
        out["billing_source_unreadable"] = True  # stated but unreadable: never read as "within the plan"
        return out
    value = _provider_usd(oc.get("allowance"))
    if value is not None:
        out["reported_allowance_usd"] = value
    source = oc.get("billing_source")
    if isinstance(source, str) and source.strip():
        out["billing_source"] = redact_text(source.strip())[:32]
    elif "billing_source" in oc:
        out["billing_source_unreadable"] = True
    return out


def opencode_session_id(run_id: str) -> str:
    """The stable ``x-opencode-session`` of one worker run (the Go docs: one stable id per conversation, used for
    routing and prompt caching). Opaque: a hash prefix of the run id, never the capability token or any secret."""
    return "ses_" + hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:24]


def _upstream_headers(up: Upstream, *, run_id: str | None = None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if up.api_key:
        h["Authorization"] = f"Bearer {up.api_key}"
    if up.is_openrouter:
        h["X-Title"] = "DLY AEOS worker broker"
    if up.provider == GO_PROVIDER:
        # the client identification the Go docs ask of other clients (not httpx's generic default User-Agent)
        h["User-Agent"] = BROKER_USER_AGENT
        if run_id:
            h[OPENCODE_SESSION_HEADER] = opencode_session_id(run_id)
    return h


async def _upstream_failure(p: Prepared, resp: httpx.Response, started: float) -> JSONResponse:
    latency = int((time.monotonic() - started) * 1000)
    text = redact_text((await resp.aread()).decode("utf-8", "replace"))[:400]
    if resp.status_code == 429:
        ra = _retry_after(resp)
        await run_in_threadpool(_finish, p, status=429, outcome="RATE_LIMITED", latency_ms=latency, retry_after=ra,
                                error=text, rate_limit=rate_limit_info(resp))
        return _error(429, "UPSTREAM_RATE_LIMITED",
                      "upstream quota/rate limit reached; no fallback model is used", retry_after=ra)
    await run_in_threadpool(_finish, p, status=resp.status_code, outcome="UPSTREAM_ERROR", latency_ms=latency, error=text)
    if 500 <= resp.status_code < 600:
        await run_in_threadpool(_note_cooldown, p, resp.status_code, reported_retry_after(resp))
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
        resp = await client.send(client.build_request("POST", url, json=p.body,
                                                      headers=_upstream_headers(p.upstream, run_id=p.run_id)),
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
                                model_resolved=model, latency_ms=int((time.monotonic() - started) * 1000),
                                billing=_billing_of(data) if isinstance(data, dict) else None)
        if isinstance(data, dict):
            data["model"] = p.exposed
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    async def relay():  # type: ignore[no-untyped-def]
        buf = b""
        usage: tuple[int | None, int | None, float | None, str | None] = (None, None, None, None)
        billing: dict[str, Any] = {}
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
                                billing = _billing_of(obj) or billing
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
                    latency_ms=int((time.monotonic() - started) * 1000), error=failed, billing=billing)

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
