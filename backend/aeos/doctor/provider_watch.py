"""Per-call permission observation (master §08: "Perubahan ... endpoint, permission ... membuat pemeriksaan
terdampak STALE dan memicu recheck sebelum capability tersebut digunakan kembali").

The Doctor's functional probes compare token scopes/permissions when someone runs ``aeos doctor --functional``.
Between those runs the provider calls AEOS already makes (publisher: Google Drive/Docs, GitHub Issues, Jira,
Confluence, Bitbucket; delivery: GitHub/Bitbucket PR, CI and branch-protection reads; broker: PR calls and
``git push`` over HTTPS) carry the same signal: a ``401`` (credential rejected), a ``403`` (permission denied, not
rate limiting), a rejected token refresh, or GitHub's ``X-OAuth-Scopes`` header changing. This module receives
those observations through the publisher's response-observer hook (``publisher.facade.observe_provider_response``
for calls outside the publisher) and marks the checks that depend on the
credential STALE (``secret:<ref>``), which queues the targeted recheck (``capabilities``) and keeps the gates
UNKNOWN until it lands (fail closed).

* Only the method, URL, status and response headers are seen; never a body or a credential.
* Observations are queued in memory and applied by a background thread with its own session (a provider call
  may run inside an open write transaction; the invalidation must never block or fail it).
* One invalidation per (credential, kind) per ``DEBOUNCE_SECONDS``: a burst of 403s is one STALE event.
* Active only while the server runs (doctor lifecycle hook), like the auto-recheck.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from aeos.doctor.checks.adapters import ATLASSIAN_REF, BITBUCKET_REF, GITHUB_REF, GOOGLE_TOKEN_REF

log = logging.getLogger("aeos.doctor")

DEBOUNCE_SECONDS = 60.0
APPLY_DELAY_SECONDS = 0.5


def credential_ref(url: str) -> str | None:
    """The stored credential a provider URL is called with (None = not a watched provider). ``github.com`` and
    ``bitbucket.org`` are the broker's ``git push`` / ``ls-remote`` remotes (same credentials as the APIs)."""
    host = (urlsplit(url).hostname or "").lower()
    if host in ("api.github.com", "uploads.github.com", "github.com"):
        return GITHUB_REF
    if host == "googleapis.com" or host.endswith(".googleapis.com"):
        return GOOGLE_TOKEN_REF
    if host.endswith(".atlassian.net"):
        return ATLASSIAN_REF
    if host in ("api.bitbucket.org", "bitbucket.org"):
        return BITBUCKET_REF
    return None


def _rate_limited(headers: Mapping[str, str]) -> bool:
    return str(headers.get("x-ratelimit-remaining", "")) == "0" or "retry-after" in {k.lower() for k in headers}


def classify(method: str, url: str, status: int, headers: Mapping[str, str],
             last_scopes: dict[str, tuple[str, ...]] | None = None) -> tuple[str, str, str] | None:
    """(invalidator, kind, reason) when the response shows the credential's permissions changed, else None.

    ``last_scopes`` (credential -> scopes last seen) is updated in place with GitHub's ``X-OAuth-Scopes``."""
    ref = credential_ref(url)
    if ref is None:
        return None
    host = (urlsplit(url).hostname or "").lower()
    inv = f"secret:{ref}"
    if status == 401:
        return inv, "rejected", f"{host}: HTTP 401 pada {method} - credential tersimpan ditolak provider"
    if status == 403 and not _rate_limited(headers):
        return inv, "denied", f"{host}: HTTP 403 pada {method} - izin ditolak provider (permission berubah?)"
    if status == 400 and host == "oauth2.googleapis.com":
        return inv, "rejected", f"{host}: refresh token ditolak (HTTP 400) - token dicabut atau kedaluwarsa"
    raw = headers.get("x-oauth-scopes")
    if raw is not None and last_scopes is not None:
        scopes = tuple(sorted({s.strip() for s in str(raw).split(",") if s.strip()}))
        before = last_scopes.get(ref)
        last_scopes[ref] = scopes
        if before is not None and before != scopes:
            return inv, "scopes", (f"{host}: scope token berubah ({', '.join(before) or '-'} -> "
                                   f"{', '.join(scopes) or '-'})")
    return None


class _WatchState:
    """Process-wide state; every field is read/written under ``_lock``."""

    def __init__(self) -> None:
        self.enabled = False
        self.queue: list[tuple[str, str]] = []  # (invalidator, reason)
        self.last: dict[tuple[str, str], float] = {}
        self.scopes: dict[str, tuple[str, ...]] = {}
        self.thread: threading.Thread | None = None


_state = _WatchState()
_lock = threading.Lock()


_doctor_probe: contextvars.ContextVar[bool] = contextvars.ContextVar("aeos_doctor_probe", default=False)


@contextmanager
def doctor_probe() -> Iterator[None]:
    """While the Doctor runs a check, the provider calls it makes ARE the (re)check: their outcome is recorded as
    the check result and must not invalidate it again (a probe that keeps answering 403 would otherwise loop:
    STALE -> recheck -> 403 -> STALE ...). Per thread/task (context variable)."""
    token = _doctor_probe.set(True)
    try:
        yield
    finally:
        _doctor_probe.reset(token)


def observe(method: str, url: str, status: int, headers: Mapping[str, str]) -> None:
    """Publisher response observer: queue an invalidation when the response shows a permission change."""
    if _doctor_probe.get():
        return
    with _lock:
        if not _state.enabled:
            return
        hit = classify(method, url, status, headers, _state.scopes)
        if hit is None:
            return
        inv, kind, reason = hit
        now = time.monotonic()
        if now - _state.last.get((inv, kind), -DEBOUNCE_SECONDS) < DEBOUNCE_SECONDS:
            return
        _state.last[(inv, kind)] = now
        _state.queue.append((inv, reason))
        t = _state.thread
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_worker, name="aeos-doctor-provider-watch", daemon=True)
        _state.thread = t
        t.start()


def pending() -> list[tuple[str, str]]:
    with _lock:
        return list(_state.queue)


def apply_pending(session: Session) -> int:
    """Apply the queued observations: mark the dependent checks STALE (caller commits). Returns rows marked."""
    from aeos.doctor.capabilities import invalidate

    with _lock:
        queued, _state.queue = _state.queue, []
    n = 0
    for inv, reason in dict(queued).items():  # one invalidation per credential per batch (last reason wins)
        n += invalidate(session, invalidator=inv, reason=reason[:200])
    return n


def _worker() -> None:
    from aeos.core.db import session_scope

    while True:
        time.sleep(APPLY_DELAY_SECONDS)
        with _lock:
            if not _state.queue or not _state.enabled:
                _state.thread = None
                return
        try:
            with session_scope() as s:
                apply_pending(s)
        except Exception:  # visible as the not-yet-STALE result; never fatal
            log.exception("provider permission observation could not be applied")


def enable() -> None:
    from aeos.publisher.providers import base as provider_base

    with _lock:
        _state.enabled = True
    provider_base.add_response_observer(observe)


def disable() -> None:
    from aeos.publisher.providers import base as provider_base

    provider_base.remove_response_observer(observe)
    with _lock:
        _state.enabled = False
        _state.queue = []
        _state.last = {}
        _state.scopes = {}


# Which provider calls feed this observer (and which do not yet): stated, never implied (evidence honesty).
OBSERVED_CALLS = (
    "publisher providers via publisher.providers.base.send (Google Drive/Docs, GitHub Issues, Jira, Confluence, "
    "Bitbucket)",
    "delivery GitHub API reads (pull request / check-run / merge-state sync, branch protection) via "
    "delivery.provider.GitHubReader",
    "delivery Bitbucket reads (commit statuses, Pipelines, pull request, branch restrictions) via the publisher's "
    "BitbucketProvider (send)",
    "broker GitHub and Bitbucket pull-request calls (create / find / reconcile) via broker.git_broker.http_client",
    "broker git push and ls-remote over HTTPS (status reported in git's output: 'returned error: 403', "
    "'Authentication failed')",
)
NOT_OBSERVED_CALLS: tuple[str, ...] = (
    "a git push failure whose output names no HTTP status (e.g. a network error) - noticed at the next "
    "aeos doctor --functional run or TTL expiry",
)


def status() -> dict[str, Any]:
    """Coverage of per-call permission observation (§08). A credential change seen only by an unobserved call
    is noticed at the next ``aeos doctor --functional`` run or TTL expiry, not per call."""
    with _lock:
        return {"enabled": _state.enabled, "queued": len(_state.queue),
                "watched": sorted({GITHUB_REF, GOOGLE_TOKEN_REF, ATLASSIAN_REF, BITBUCKET_REF}),
                "observed_calls": list(OBSERVED_CALLS), "not_observed_calls": list(NOT_OBSERVED_CALLS),
                "note": "Panggilan yang tidak diamati baru terdeteksi pada `aeos doctor --functional` berikutnya "
                        "atau saat TTL habis (§08)."}
