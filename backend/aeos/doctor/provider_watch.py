"""Per-call permission observation (master §08: "Perubahan ... endpoint, permission ... membuat pemeriksaan
terdampak STALE dan memicu recheck sebelum capability tersebut digunakan kembali").

The Doctor's functional probes compare token scopes/permissions when someone runs ``aeos doctor --functional``.
Between those runs the provider calls AEOS already makes (publisher: Google Drive/Docs, GitHub Issues, Jira,
Confluence, Bitbucket) carry the same signal: a ``401`` (credential rejected), a ``403`` (permission denied, not
rate limiting), a rejected token refresh, or GitHub's ``X-OAuth-Scopes`` header changing. This module receives
those observations through the publisher's response-observer hook and marks the checks that depend on the
credential STALE (``secret:<ref>``), which queues the targeted recheck (``capabilities``) and keeps the gates
UNKNOWN until it lands (fail closed).

* Only the method, URL, status and response headers are seen; never a body or a credential.
* Observations are queued in memory and applied by a background thread with its own session (a provider call
  may run inside an open write transaction; the invalidation must never block or fail it).
* One invalidation per (credential, kind) per ``DEBOUNCE_SECONDS``: a burst of 403s is one STALE event.
* Active only while the server runs (doctor lifecycle hook), like the auto-recheck.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from aeos.doctor.checks.adapters import ATLASSIAN_REF, BITBUCKET_REF, GITHUB_REF, GOOGLE_TOKEN_REF

log = logging.getLogger("aeos.doctor")

DEBOUNCE_SECONDS = 60.0
APPLY_DELAY_SECONDS = 0.5


def credential_ref(url: str) -> str | None:
    """The stored credential a provider URL is called with (None = not a watched provider)."""
    host = (urlsplit(url).hostname or "").lower()
    if host in ("api.github.com", "uploads.github.com"):
        return GITHUB_REF
    if host == "googleapis.com" or host.endswith(".googleapis.com"):
        return GOOGLE_TOKEN_REF
    if host.endswith(".atlassian.net"):
        return ATLASSIAN_REF
    if host == "api.bitbucket.org":
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


def observe(method: str, url: str, status: int, headers: Mapping[str, str]) -> None:
    """Publisher response observer: queue an invalidation when the response shows a permission change."""
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


def status() -> dict[str, Any]:
    with _lock:
        return {"enabled": _state.enabled, "queued": len(_state.queue),
                "watched": sorted({GITHUB_REF, GOOGLE_TOKEN_REF, ATLASSIAN_REF, BITBUCKET_REF})}
