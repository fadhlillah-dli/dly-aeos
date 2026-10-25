"""§08 "Perubahan ... endpoint, permission ... membuat pemeriksaan terdampak STALE dan memicu recheck sebelum
capability tersebut digunakan kembali":

* a provider call AEOS already makes (publisher ``send``) that shows a permission change - 401, 403 (not rate
  limiting), a rejected token refresh, GitHub's X-OAuth-Scopes changing - marks the checks of that credential
  STALE without waiting for a manual ``aeos doctor --functional`` run;
* the invalidation queues a targeted recheck that re-runs the read-only remote identity probes FUNCTIONALLY
  (quota-bearing checks never), so the capability is rechecked before it is used again.
No network: provider responses come from an httpx MockTransport, Doctor runs are captured.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from aeos.core.timeutil import utcnow
from aeos.doctor import capabilities as caps
from aeos.doctor import provider_watch as watch
from aeos.doctor.checks import REGISTRY, load_all
from aeos.models.inventory import CheckResult
from aeos.publisher.providers import base as provider_base


def _row(db, cid, status="VERIFIED"):
    load_all()
    spec = REGISTRY[cid]
    now = utcnow()
    r = CheckResult(check_id=cid, title=spec.title, group=spec.group, component_id=spec.component_id,
                    scope=dict(spec.scope), status=status, last_checked_at=now, expires_at=now + timedelta(hours=1),
                    invalidators=list(spec.invalidators))
    db.add(r)
    db.flush()
    return r


@pytest.fixture
def watching():
    watch.enable()
    try:
        yield
    finally:
        watch.disable()


def _client(status: int, headers: dict[str, str] | None = None) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, headers=headers or {},
                                                                                json={})))


@pytest.mark.parametrize(("url", "status", "headers", "kind"), [
    ("https://api.github.com/repos/o/r/issues", 401, {}, "rejected"),
    ("https://api.github.com/repos/o/r/issues", 403, {}, "denied"),
    ("https://www.googleapis.com/drive/v3/files/x", 403, {}, "denied"),
    ("https://oauth2.googleapis.com/token", 400, {}, "rejected"),
    ("https://corp.atlassian.net/wiki/api/v2/pages/1", 401, {}, "rejected"),
    ("https://api.bitbucket.org/2.0/repositories/c/a", 403, {}, "denied"),
])
def test_permission_signals_are_classified_per_credential(url, status, headers, kind):
    hit = watch.classify("GET", url, status, headers, {})
    assert hit is not None and hit[1] == kind and hit[0] == f"secret:{watch.credential_ref(url)}"


def test_rate_limits_successes_and_foreign_hosts_are_not_permission_changes():
    gh = "https://api.github.com/repos/o/r"
    assert watch.classify("GET", gh, 403, {"x-ratelimit-remaining": "0"}, {}) is None
    assert watch.classify("GET", gh, 403, {"Retry-After": "30"}, {}) is None
    assert watch.classify("GET", gh, 200, {}, {}) is None
    assert watch.classify("GET", "https://example.org/x", 401, {}, {}) is None
    seen: dict[str, tuple[str, ...]] = {}
    assert watch.classify("GET", gh, 200, {"x-oauth-scopes": "repo"}, seen) is None  # first sighting seeds
    assert watch.classify("GET", gh, 200, {"x-oauth-scopes": "repo"}, seen) is None
    hit = watch.classify("GET", gh, 200, {"x-oauth-scopes": "repo, workflow"}, seen)
    assert hit is not None and hit[1] == "scopes" and "workflow" in hit[2]


def test_a_403_during_a_publisher_call_marks_the_credentials_checks_stale(settings, db, watching, monkeypatch):
    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)  # applied synchronously below, not by the thread
    token_row = _row(db, "adapter.github_token", status="CONNECTED")
    other = _row(db, "adapter.google_oauth", status="CONFIGURED")
    db.commit()
    resp = provider_base.send(_client(403), "POST", "https://api.github.com/repos/o/r/issues", write=True,
                              what="github create issue")
    assert resp.status_code == 403, "the observer never changes the provider call"
    provider_base.send(_client(403), "POST", "https://api.github.com/repos/o/r/issues", write=True, what="again")
    assert len(watch.pending()) == 1, "a burst of 403s is one invalidation"
    assert watch.apply_pending(db) >= 1
    db.commit()
    db.refresh(token_row)
    db.refresh(other)
    assert token_row.status == "STALE" and "HTTP 403" in token_row.observed_evidence["stale_reason"]
    assert other.status == "CONFIGURED", "checks of other credentials are untouched"


def test_scope_change_seen_on_a_normal_call_invalidates(settings, db, watching, monkeypatch):
    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)
    row = _row(db, "adapter.project_repo", status="CONNECTED")
    db.commit()
    url = "https://api.github.com/repos/o/r/labels/aeos"
    provider_base.send(_client(200, {"X-OAuth-Scopes": "public_repo"}), "GET", url, write=False, what="label")
    assert watch.pending() == []
    provider_base.send(_client(200, {"X-OAuth-Scopes": "public_repo, repo"}), "GET", url, write=False, what="label")
    watch.apply_pending(db)
    db.commit()
    db.refresh(row)
    assert row.status == "STALE" and "scope token berubah" in row.observed_evidence["stale_reason"]


def test_nothing_is_observed_without_the_server_hook(settings, db):
    assert watch.observe not in provider_base._response_observers
    provider_base.send(_client(401), "GET", "https://api.github.com/user", write=False, what="user")
    assert watch.pending() == []


def test_a_failing_observer_never_breaks_a_provider_call():
    def boom(*_a):
        raise RuntimeError("observer bug")

    provider_base.add_response_observer(boom)
    try:
        assert provider_base.send(_client(200), "GET", "https://api.github.com/user", write=False,
                                  what="user").status_code == 200
    finally:
        provider_base.remove_response_observer(boom)


def test_invalidation_rechecks_remote_identity_probes_functionally_and_never_quota_checks(settings, db,
                                                                                        monkeypatch):
    load_all()
    assert caps.remote_recheckable(["adapter.github_token", "adapter.project_repo", "delivery.branch_protection",
                                    "claude.quota", "host.disk"]) == [
        "adapter.github_token", "adapter.project_repo", "delivery.branch_protection"]
    assert "adapter.github_token" not in caps.recheckable(["adapter.github_token"])
    assert all(not REGISTRY[c].functional for c in caps.REMOTE_RECHECK), "no functional-only (heavy) check"
    calls: list[tuple[list[str], bool]] = []
    monkeypatch.setattr("aeos.doctor.runner.run_doctor",
                        lambda settings, s, *, checks, functional, actor, trigger: calls.append((checks, functional)))
    monkeypatch.setattr(caps, "RECHECK_DELAY_SECONDS", 0.01)
    with caps._recheck_lock:
        caps._recheck.enabled, caps._recheck.settings = True, settings
        caps._recheck.pending = {"adapter.github_token", "adapter.google_oauth"}
    try:
        caps._recheck_worker()
    finally:
        caps.disable_auto_recheck()
    assert (["adapter.google_oauth"], False) in calls
    assert (["adapter.github_token"], True) in calls, "the remote probe is re-run functionally (read-only GET)"


def test_stale_github_rows_queue_a_functional_recheck(settings, db, monkeypatch):
    queued: list[list[str]] = []
    monkeypatch.setattr(caps, "request_recheck", lambda ids: queued.append(list(ids)))
    _row(db, "adapter.github_token", status="CONNECTED")
    _row(db, "adapter.project_repo", status="CONNECTED")
    db.commit()
    caps.enable_auto_recheck(settings)
    try:
        caps.invalidate(db, invalidator="secret:personal/github", reason="HTTP 403 observed")
        db.commit()
    finally:
        caps.disable_auto_recheck()
    flat = {c for ids in queued for c in ids}
    assert {"adapter.github_token", "adapter.project_repo"} <= flat


def test_the_capability_view_states_which_provider_calls_are_watched_and_which_are_not(personal_client):
    """Evidence honesty: per-call observation covers the publisher only; delivery/broker GitHub calls are named as
    not observed (a permission change they see waits for the next functional run or TTL expiry)."""
    body = personal_client.get("/api/doctor/capabilities").json()
    pw = body["permission_watch"]
    assert any("publisher" in c for c in pw["observed_calls"])
    assert any("delivery" in c for c in pw["not_observed_calls"]) and any("broker" in c for c in
                                                                           pw["not_observed_calls"])
    assert "personal/github" in pw["watched"]
