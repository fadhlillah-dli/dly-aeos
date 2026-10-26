"""Provider contract for publication adapters (master §14, §29, §41).

Adapters perform exactly one outbox operation per ``execute`` call and must be able to
``reconcile`` it — find an earlier side effect by its marker (Drive appProperties, issue body
marker, Jira label, Confluence content property) — so that a timeout or unknown result never leads
to a duplicate (§29 "rekonsiliasi dahulu agar retry tidak membuat duplikat").

Error taxonomy (the executor maps these to outbox states):

* ``ProviderTimeout`` — the request may or may not have taken effect → ``UNKNOWN`` → reconcile
  before any retry;
* ``ProviderConflict`` — the target changed outside AEOS (revision/version mismatch) → ``CONFLICT``;
  nothing is overwritten and the human edit becomes revision input;
* ``ProviderBlocked`` — capability/credential/enablement missing (fail closed, needs action);
* ``ProviderError(retryable=True|False)`` — definitive failure without side effect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from aeos.core.redaction import redact_text

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HTTP_ERROR_BODY_LIMIT = 400

# Response observers (§08 "a change of endpoint/permission makes the affected checks STALE"): another module (the
# Readiness Doctor) registers a callback that sees the method, URL, status and RESPONSE headers of every provider
# call - never a body or the request headers (credentials). An observer can never fail or change a call.
ResponseObserver = Callable[[str, str, int, Mapping[str, str]], None]
_response_observers: list[ResponseObserver] = []


def add_response_observer(fn: ResponseObserver) -> None:
    if fn not in _response_observers:
        _response_observers.append(fn)


def remove_response_observer(fn: ResponseObserver) -> None:
    if fn in _response_observers:
        _response_observers.remove(fn)


def notify_response(method: str, url: str, status: int, headers: Mapping[str, str]) -> None:
    """Hand one provider response (method, URL, status, response headers - never a body or a request header) to the
    registered observers. Delivery and broker calls that do not go through :func:`send` (GitHub REST reads, PR
    creation, ``git push`` over HTTPS) report through here too, so a permission change they see marks the dependent
    readiness checks STALE (§08). Never raises."""
    for fn in list(_response_observers):
        try:
            fn(method, url, status, headers)
        except Exception:  # an observer never breaks a provider call
            logging.getLogger("aeos.publisher").debug("response observer failed", exc_info=True)


def _notify_observers(method: str, url: str, resp: httpx.Response) -> None:
    notify_response(method, url, resp.status_code, resp.headers)


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(redact_text(message))
        self.message = redact_text(message)
        self.retryable = retryable
        self.status_code = status_code
        self.details = details or {}


class ProviderTimeout(ProviderError):
    """Outcome unknown (timeout / transport error after the request may have been sent)."""

    def __init__(self, message: str, **kw: Any):
        super().__init__(message, retryable=True, **kw)


class ProviderConflict(ProviderError):
    def __init__(self, message: str, *, expected_revision: str | None, observed_revision: str | None,
                 current_text: str = "", **kw: Any):
        super().__init__(message, retryable=False, **kw)
        self.expected_revision = expected_revision
        self.observed_revision = observed_revision
        self.current_text = redact_text(current_text or "")


class ProviderBlocked(ProviderError):
    def __init__(self, message: str, *, remedy: str = "", code: str = "BLOCKED", **kw: Any):
        super().__init__(message, retryable=False, **kw)
        self.remedy = remedy
        self.code = code


@dataclass
class OpContext:
    """Everything an adapter needs for one outbox operation. ``payload`` comes from the approved
    snapshot captured at preview time — adapters never query the latest plan version (§50)."""

    operation_id: str
    publication_id: str
    project_id: str
    project_key: str
    project_name: str
    domain: str
    provider: str
    operation: str
    logical_id: str
    idempotency_key: str
    title: str
    target: dict[str, Any]
    payload: dict[str, Any]
    parent_external_id: str | None = None
    existing_external_id: str | None = None
    existing_revision: str | None = None
    attachments: dict[str, bytes] = field(default_factory=dict)


@dataclass
class OpResult:
    external_id: str
    external_url: str | None = None
    external_revision: str | None = None
    kind: str = ""
    response: dict[str, Any] = field(default_factory=dict)


@dataclass
class Inspection:
    """Current state of a published resource (read-only)."""

    exists: bool
    revision: str | None = None
    text: str = ""
    url: str | None = None


class Provider(Protocol):
    name: str

    def execute(self, ctx: OpContext) -> OpResult: ...

    def reconcile(self, ctx: OpContext) -> OpResult | None:
        """Return the existing resource for ``ctx`` or ``None`` when it definitively does not exist.
        Raise ``ProviderError`` when absence cannot be proven (the op then stays UNKNOWN)."""
        ...

    def close(self) -> None: ...


def classify_http_error(resp: httpx.Response, *, write: bool, what: str) -> ProviderError:
    """Map an HTTP error response. A 5xx on a write means the outcome is unknown."""
    body = redact_text(resp.text[:HTTP_ERROR_BODY_LIMIT]) if resp.text else ""
    msg = f"{what}: HTTP {resp.status_code} {body}".strip()
    if resp.status_code == 429:
        return ProviderError(msg, retryable=True, status_code=429)
    if resp.status_code >= 500:
        if write:
            return ProviderTimeout(msg, status_code=resp.status_code)
        return ProviderError(msg, retryable=True, status_code=resp.status_code)
    if resp.status_code in (401, 403):
        return ProviderBlocked(msg, remedy="periksa credential/izin akun provider", code="PROVIDER_AUTH",
                               status_code=resp.status_code)
    return ProviderError(msg, retryable=False, status_code=resp.status_code)


ATTRIBUTION_REMEDY = ("hapus atribusi AI dari teks (aturan owner, AGENTS.md invariant 11: tidak ada AI di GitHub/"
                      "Bitbucket/Atlassian perusahaan) lalu terbitkan ulang; provenance tetap di catatan DLY AEOS")


def refuse_ai_attribution(where: str, texts: Mapping[str, str | None]) -> None:
    """Owner rule (AGENTS.md invariant 11): text bound for GitHub Issues or the company's Jira/Confluence that carries
    AI attribution is refused BEFORE anything is sent (``ProviderBlocked`` AI_ATTRIBUTION_IN_TEXT -> the operation is
    FAILED with the reason; nothing is stripped silently)."""
    from aeos.core.attribution import find_ai_attribution, refusal_message

    for field_name, text in texts.items():
        findings = find_ai_attribution(text or "")
        if findings:
            raise ProviderBlocked(refusal_message(f"{where} {field_name}", findings), remedy=ATTRIBUTION_REMEDY,
                                  code="AI_ATTRIBUTION_IN_TEXT",
                                  details={"where": where, "field": field_name,
                                           "findings": [f.as_dict() for f in findings[:10]]})


def send(client: httpx.Client, method: str, url: str, *, write: bool, what: str, observe: bool = True,
         **kw: Any) -> httpx.Response:
    """Send one request with the timeout/unknown-outcome rules applied. ``observe=False`` only for capability reads
    whose 403 is an expected answer about a permission the least-privilege credential lacks (e.g. Bitbucket branch
    restrictions need repository admin), never a sign that the credential changed."""
    try:
        resp = client.request(method, url, **kw)
    except httpx.ConnectError as exc:  # nothing was sent
        raise ProviderError(f"{what}: connection failed ({exc.__class__.__name__})", retryable=True) from exc
    except httpx.TimeoutException as exc:
        if write:
            raise ProviderTimeout(f"{what}: timeout — outcome unknown, reconcile before retry") from exc
        raise ProviderError(f"{what}: timeout", retryable=True) from exc
    except httpx.TransportError as exc:
        if write:
            raise ProviderTimeout(f"{what}: transport error — outcome unknown ({exc.__class__.__name__})") from exc
        raise ProviderError(f"{what}: transport error ({exc.__class__.__name__})", retryable=True) from exc
    if observe:
        _notify_observers(method, url, resp)
    return resp
