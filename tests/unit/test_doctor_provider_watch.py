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
    """Evidence honesty: per-call observation covers the publisher, the delivery reads and the broker's PR calls and
    git pushes (W6-027); what is still not observed (a git failure that names no HTTP status) is stated."""
    body = personal_client.get("/api/doctor/capabilities").json()
    pw = body["permission_watch"]
    assert any("publisher" in c for c in pw["observed_calls"])
    assert any(c.startswith("delivery GitHub") for c in pw["observed_calls"])
    assert any(c.startswith("delivery Bitbucket") for c in pw["observed_calls"])
    assert any("broker git push" in c for c in pw["observed_calls"])
    assert pw["not_observed_calls"] and not any(c.startswith("delivery") for c in pw["not_observed_calls"])
    assert "personal/github" in pw["watched"] and "office/bitbucket" in pw["watched"]


# ---------------------------------------------------------------- delivery and broker calls (W6-027)


def _stale_rows(db, *ids):
    rows = [_row(db, cid, status="CONNECTED") for cid in ids]
    db.commit()
    return rows


def test_a_403_on_a_delivery_pr_sync_marks_the_github_checks_stale(settings, db, watching, monkeypatch):
    from aeos.delivery import provider as delivery_provider

    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)
    token_row, protection_row = _stale_rows(db, "adapter.github_token", "delivery.branch_protection")
    other = _row(db, "adapter.google_oauth", status="CONFIGURED")
    db.commit()
    pr = delivery_provider.fetch_pull("o/r", 7, token="ghp_" + "x" * 36, client=_client(403))
    assert pr["error"].startswith("HTTP 403"), "the observer never changes the delivery read"
    assert watch.pending() and watch.pending()[0][0] == "secret:personal/github"
    watch.apply_pending(db)
    db.commit()
    for row in (token_row, protection_row):
        db.refresh(row)
        assert row.status == "STALE" and "HTTP 403" in row.observed_evidence["stale_reason"]
    db.refresh(other)
    assert other.status == "CONFIGURED"


def test_a_scope_change_seen_by_a_delivery_check_run_read_invalidates(settings, db, watching, monkeypatch):
    from aeos.delivery import provider as delivery_provider

    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)
    sha = "a" * 40

    def client(scopes: str) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, headers={"X-OAuth-Scopes": scopes}, json={"total_count": 0, "check_runs": []})))

    delivery_provider.fetch_check_runs("o/r", sha, token="ghp_" + "x" * 36, client=client("repo"))
    assert watch.pending() == []
    delivery_provider.fetch_check_runs("o/r", sha, token="ghp_" + "x" * 36, client=client("repo, workflow"))
    assert watch.pending() and "scope token berubah" in watch.pending()[0][1]


def test_a_403_on_a_delivery_bitbucket_ci_read_marks_the_office_credential_stale(settings, db, watching,
                                                                                  monkeypatch):
    from aeos.delivery import bitbucket as delivery_bitbucket

    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)
    ci = delivery_bitbucket.fetch_ci_status("company_ws/svc", "c" * 40, auth_header="Bearer t", client=_client(403))
    assert ci.overall == "UNKNOWN"
    assert [inv for inv, _ in watch.pending()] == ["secret:office/bitbucket"]


def test_capability_probe_answers_are_not_permission_changes(settings, db, watching, monkeypatch):
    """A least-privilege token (no Administration, FINE_GRAINED_REMEDY) or GitHub Free answers 403 on branch
    protection/rulesets by design: reporting it would keep the token's checks STALE on every capability read. A 401
    on the repository itself still is a signal."""
    from aeos.delivery import provider as delivery_provider

    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/o/r":
            return httpx.Response(200, json={"private": True, "default_branch": "main", "owner": {"type": "User",
                                                                                                  "login": "o"}})
        if path.endswith("/protection") or path.endswith("/rulesets"):
            return httpx.Response(403, json={"message": "Upgrade to GitHub Pro or make this repository public"})
        return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    delivery_provider.check_capabilities("o/r", "main", token="ghp_" + "x" * 36, client=client)
    assert watch.pending() == []
    rejected = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={})))
    delivery_provider.check_capabilities("o/r", "main", token="ghp_" + "x" * 36, client=rejected)
    assert [inv for inv, _ in watch.pending()] == ["secret:personal/github"]


def test_bitbucket_branch_restrictions_403_is_an_answer_not_a_change(watching):
    from aeos.publisher.providers.atlassian import BitbucketProvider
    from aeos.publisher.providers.base import ProviderError

    bb = BitbucketProvider(full_name="ws/r", enabled=True, client=_client(403), auth_header="Bearer t")
    with pytest.raises(ProviderError):
        bb.branch_restrictions()
    assert watch.pending() == []
    with pytest.raises(ProviderError):
        bb.repository()
    assert [inv for inv, _ in watch.pending()] == ["secret:office/bitbucket"]


def test_the_doctors_own_probes_never_invalidate_their_own_result(settings, db, watching, monkeypatch):
    """A Doctor check's provider calls ARE the recheck: a probe that keeps answering 403 must not loop
    (STALE -> recheck -> 403 -> STALE ...)."""
    import dataclasses

    from aeos.doctor.model import Outcome
    from aeos.doctor.runner import make_context, run_check

    load_all()

    def probe(_ctx):
        provider_base.send(_client(403), "GET", "https://api.github.com/repos/o/r/branches/main/protection",
                           write=False, what="probe")
        return Outcome("FAILED", "probe saw 403")

    spec = dataclasses.replace(REGISTRY["host.disk"], fn=probe)
    res = run_check(spec, make_context(settings, db, functional=True))
    assert res.status == "FAILED" and watch.pending() == []
    with watch.doctor_probe():
        provider_base.send(_client(401), "GET", "https://api.github.com/user", write=False, what="x")
    assert watch.pending() == []
    provider_base.send(_client(401), "GET", "https://api.github.com/user", write=False, what="x")
    assert watch.pending(), "outside a Doctor probe the same signal is observed"


@pytest.mark.parametrize(("output", "status"), [
    ("remote: Permission to o/r.git denied to bot.\nfatal: unable to access 'https://github.com/o/r.git/': "
     "The requested URL returned error: 403", 403),
    ("fatal: Authentication failed for 'https://github.com/o/r.git/'", 401),
    ("fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com", None),
])
def test_git_output_names_the_http_status_of_a_failed_push(output, status):
    from aeos.broker import git_broker

    assert git_broker.git_http_status(output) == status


def test_a_403_on_a_broker_push_marks_the_github_checks_stale(settings, db, watching, monkeypatch, tmp_path):
    """The broker's sha-pinned push runs git; its HTTP status (git's 'returned error: 403') is observed."""
    import subprocess

    from aeos.broker import git_broker

    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)
    (token_row,) = _stale_rows(db, "adapter.github_token")
    head = "b" * 40

    def fake_git(args, *, cwd, env, hooks):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout=head + "\n", stderr="")
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 128, stdout="", stderr=(
                "remote: Permission to o/r.git denied.\nfatal: unable to access 'https://github.com/o/r.git/': "
                "The requested URL returned error: 403"))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_broker, "_git", fake_git)
    res = git_broker.push_from_mirror(source_repo=tmp_path, branch="aeos/t1-attempt-1",
                                      remote_url="https://github.com/o/r.git", token="ghp_" + "y" * 36,
                                      work_root=tmp_path,
                                      commit_policy=git_broker.CommitPolicy(provider="github", email="t@example.invalid"))
    assert res["ok"] is False and res["stage"] == "push"
    assert [inv for inv, _ in watch.pending()] == ["secret:personal/github"]
    assert "GIT PUSH" in watch.pending()[0][1] and "ghp_" not in watch.pending()[0][1]
    watch.apply_pending(db)
    db.commit()
    db.refresh(token_row)
    assert token_row.status == "STALE"


def test_broker_api_clients_are_observed(settings, db, watching, monkeypatch):
    """The PR create/find/reconcile calls of both brokers use broker.git_broker.http_client (one observed client)."""
    import ast
    import inspect

    import respx

    from aeos.broker import bitbucket_broker, git_broker

    monkeypatch.setattr(watch, "APPLY_DELAY_SECONDS", 3600.0)
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.bitbucket.org/2.0/repositories/ws/r/pullrequests").respond(401, json={})
        with git_broker.http_client() as client:
            assert client.post("https://api.bitbucket.org/2.0/repositories/ws/r/pullrequests",
                               json={}).status_code == 401
    assert [inv for inv, _ in watch.pending()] == ["secret:office/bitbucket"]
    for mod in (git_broker, bitbucket_broker):
        tree = ast.parse(inspect.getsource(mod))
        raw = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "Client" and getattr(n.func.value, "id", None) == "httpx"]
        assert len(raw) == (1 if mod is git_broker else 0), f"{mod.__name__}: every API client is the observed one"
