"""Check Upstream (§38, §47; §50 rows "Upstream release baru terdeteksi -> Notification/proposal;
tidak auto-install", "Source repo rename/redirect/fork/publisher berubah -> Canonical identity
diperiksa; tidak mewarisi trust otomatis", "Nama repo ambigu ... -> UNVERIFIED dan blocked")."""

from __future__ import annotations

import os

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.models.audit import AuditEvent, Event
from aeos.models.research import ResearchEntry
from aeos.research import service as research

API = "https://api.github.com"


def _entry(db, key="tool:opencode") -> ResearchEntry:
    research.seed_registry(db)
    db.commit()
    return db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == key))


def _repo(full_name, **kw):
    return {"full_name": full_name, "html_url": f"https://github.com/{full_name}", "archived": False, "fork": False,
            "default_branch": "main", "pushed_at": "2026-10-17T10:00:00Z", "license": {"spdx_id": "MIT"},
            "owner": {"login": full_name.split("/")[0]}, **kw}


def _check(db, e, domain="PERSONAL"):
    out = research.check_upstream(db, e, actor_type="user", actor_id="owner", domain=domain)
    db.commit()
    return out


@respx.mock(assert_all_called=False)
def test_check_ok_records_release_metadata_without_installing(respx_mock, db):
    e = _entry(db)
    respx_mock.get(f"{API}/repos/anomalyco/opencode").mock(return_value=httpx.Response(200, json=_repo("anomalyco/opencode")))
    respx_mock.get(f"{API}/repos/anomalyco/opencode/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "v1.4.0", "published_at": "2026-10-10T00:00:00Z",
                                               "html_url": "https://github.com/anomalyco/opencode/releases/v1.4.0"}))
    out = _check(db, e)
    assert out["check_status"] == "OK" and out["identity_status"] == "CHECKED"
    assert out["lifecycle"] == "SOURCE_VERIFIED" and out["latest_tag"] == "v1.4.0"
    assert out["checked_at"] and out["freshness"] == "FRESH"
    assert out["upstream"]["update_available"] is None  # nothing pinned -> no "update" claim
    assert "UNKNOWN" in out["upstream"]["compatibility"]
    assert out["license"] == "MIT"
    a = db.scalar(select(AuditEvent).where(AuditEvent.action == "research.upstream.check"))
    assert a.details["installed"] is False and a.details["activated"] is False
    # only api.github.com was contacted, with no credentials
    for call in respx_mock.calls:
        assert call.request.url.host == "api.github.com"
        assert "authorization" not in {k.lower() for k in call.request.headers}


@respx.mock(assert_all_called=False)
def test_redirect_flags_identity_and_trust_is_not_inherited(respx_mock, db):
    e = _entry(db, "tool:gastown")
    respx_mock.get(f"{API}/repos/gastownhall/gastown").mock(
        return_value=httpx.Response(301, headers={"location": f"{API}/repositories/4242"}))
    respx_mock.get(f"{API}/repositories/4242").mock(return_value=httpx.Response(200, json=_repo("newhall/gastown2")))
    respx_mock.get(f"{API}/repos/newhall/gastown2/releases/latest").mock(return_value=httpx.Response(404, json={}))
    respx_mock.get(f"{API}/repos/newhall/gastown2/tags?per_page=1").mock(
        return_value=httpx.Response(200, json=[{"name": "v0.9"}]))
    out = _check(db, e)
    assert out["check_status"] == "IDENTITY_CHANGED" and out["identity_status"] == "CHANGED"
    assert out["lifecycle"] == "DISCOVERED"  # SOURCE_VERIFIED trust dropped
    assert out["identity"] == "github:gastownhall/gastown"  # recorded identity not silently replaced
    assert out["upstream"]["observed_identity"] == "github:newhall/gastown2" and out["upstream"]["redirected"]
    assert out["latest_tag"] == "v0.9"
    events = [h["event"] for h in out["history"]]
    assert "IDENTITY_CHANGED" in events and "LIFECYCLE_RESET" in events
    assert research.resolve_alias(db, "github:gastownhall/gastown") == "github:newhall/gastown2"


@respx.mock(assert_all_called=False)
def test_redirect_off_github_api_is_refused(respx_mock, db):
    e = _entry(db)
    respx_mock.get(f"{API}/repos/anomalyco/opencode").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/repos/x"}))
    with pytest.raises(Blocked) as exc:
        research.check_upstream(db, e, actor_type="user", actor_id="o", domain="PERSONAL")
    assert exc.value.code == "UPSTREAM_REDIRECT_REFUSED"
    db.rollback()


@respx.mock(assert_all_called=False)
def test_fork_and_archived_and_not_found(respx_mock, db):
    e = _entry(db, "tool:toolhive")
    respx_mock.get(f"{API}/repos/stacklok/toolhive").mock(
        return_value=httpx.Response(200, json=_repo("stacklok/toolhive", fork=True, parent={"full_name": "orig/toolhive"})))
    respx_mock.get(f"{API}/repos/stacklok/toolhive/releases/latest").mock(return_value=httpx.Response(404, json={}))
    respx_mock.get(f"{API}/repos/stacklok/toolhive/tags?per_page=1").mock(return_value=httpx.Response(200, json=[]))
    out = _check(db, e)
    assert out["check_status"] == "FORK" and out["lifecycle"] == "DISCOVERED"

    e2 = _entry(db, "tool:serena")
    respx_mock.get(f"{API}/repos/oraios/serena").mock(
        return_value=httpx.Response(200, json=_repo("oraios/serena", archived=True)))
    respx_mock.get(f"{API}/repos/oraios/serena/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "v2"}))
    out2 = _check(db, e2)
    assert out2["check_status"] == "ARCHIVED" and out2["upstream"]["archived"] is True

    e3 = _entry(db, "tool:rtk")
    respx_mock.get(f"{API}/repos/rtk-ai/rtk").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    out3 = _check(db, e3)
    assert out3["check_status"] == "NOT_FOUND" and out3["identity_status"] == "NOT_FOUND"
    assert out3["lifecycle"] == "DISCOVERED"


@respx.mock(assert_all_called=False)
def test_rate_limited_keeps_previous_check_time(respx_mock, db):
    e = _entry(db)
    respx_mock.get(f"{API}/repos/anomalyco/opencode").mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={}))
    out = _check(db, e)
    assert out["check_status"] == "RATE_LIMITED" and out["checked_at"] is None and out["touched"] is False


@respx.mock(assert_all_called=False)
def test_network_error_is_reported_not_success(respx_mock, db):
    e = _entry(db)
    respx_mock.get(f"{API}/repos/anomalyco/opencode").mock(side_effect=httpx.ConnectError("offline"))
    out = _check(db, e)
    assert out["check_status"] == "ERROR" and out["checked_at"] is None and out["touched"] is False
    assert out["upstream"]["last_error"] and db.get(ResearchEntry, e.id).upstream["component_hints"] == ["opencode"]


@respx.mock(assert_all_called=False)
def test_update_available_is_notification_not_compatibility(respx_mock, db):
    e = _entry(db, "tool:hermes")
    e.source_version = "v1.0.0"
    db.commit()
    respx_mock.get(f"{API}/repos/nousresearch/hermes-agent").mock(
        return_value=httpx.Response(200, json=_repo("NousResearch/hermes-agent")))
    respx_mock.get(f"{API}/repos/NousResearch/hermes-agent/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "v1.1.0"}))
    out = _check(db, e)
    assert out["update_available"] is True and "UNKNOWN" in out["upstream"]["compatibility"]
    assert out["source_version"] == "v1.0.0"  # the pin is never bumped automatically
    ev = db.scalar(select(Event).where(Event.kind == "research.upstream.update_detected"))
    assert ev is not None and "notification only" in ev.payload["action"]


def test_unverified_and_non_github_entries_are_not_checked(db):
    dd = _entry(db, "tool:ddorchestrator")
    with pytest.raises(Blocked) as e1:
        research.check_upstream(db, dd, actor_type="user", actor_id="o", domain="PERSONAL")
    assert e1.value.code == "IDENTITY_UNVERIFIED"
    temporal = _entry(db, "tool:temporal")
    with pytest.raises(Blocked) as e2:
        research.check_upstream(db, temporal, actor_type="user", actor_id="o", domain="PERSONAL")
    assert e2.value.code == "UPSTREAM_CHECK_NOT_SUPPORTED"


def test_office_context_never_reaches_public_research(db):
    e = _entry(db)
    with pytest.raises(PolicyDenied):
        research.check_upstream(db, e, actor_type="user", actor_id="o", domain="OFFICE")
    added, _ = research.add_by_url(db, url="https://github.com/acme/private-office-tool", name=None, category=None,
                                   notes=None, actor_user_id="o", domain="OFFICE")
    db.commit()
    with pytest.raises(PolicyDenied):
        research.check_upstream(db, added, actor_type="user", actor_id="o", domain="PERSONAL")
    # and Office-submitted entries are invisible in Personal views
    assert all(r["id"] != added.id for r in research.list_entries(db, domain="PERSONAL"))


def test_add_by_url_creates_discovered_entry_without_network(db):
    with respx.mock(assert_all_called=False) as m:  # any HTTP call would be unmocked -> error
        e, dup = research.add_by_url(db, url="https://github.com/Example/NewTool.git", name="NewTool", category=None,
                                     notes="token=abcdef123456 looks useful", actor_user_id="o", domain="PERSONAL")
        db.commit()
        assert not m.calls
    assert not dup and e.lifecycle == "DISCOVERED" and e.identity == "github:example/newtool"
    assert e.identity_status == "UNVERIFIED" and "abcdef123456" not in e.relevance
    again, dup2 = research.add_by_url(db, url="git@github.com:example/newtool", name=None, category=None, notes=None,
                                      actor_user_id="o", domain="PERSONAL")
    assert dup2 and again.id == e.id
    assert research.entry_view(e)["checkable"] is True  # user-added GitHub URL can be verified live
    dd = _entry(db, "tool:ddorchestrator")
    assert research.entry_view(dd)["checkable"] is False
    for bad in ("https://u:p@github.com/x/y", "ftp://x.example/y"):
        with pytest.raises(ValidationFailed):
            research.add_by_url(db, url=bad, name=None, category=None, notes=None, actor_user_id="o", domain="PERSONAL")


@pytest.mark.network
def test_real_github_check_when_network_allowed(db):
    if os.environ.get("RUN_NETWORK_TESTS") != "1":  # AEOS_* vars are cleared by the settings fixture
        pytest.skip("set RUN_NETWORK_TESTS=1 to call the public GitHub API")
    e = _entry(db)
    out = _check(db, e)
    if out["check_status"] in ("RATE_LIMITED", "ERROR"):
        pytest.skip(f"GitHub unavailable: {out['check_status']}")
    assert out["check_status"] in ("OK", "IDENTITY_CHANGED", "ARCHIVED", "FORK", "NOT_FOUND")
    if out["check_status"] == "OK":  # published advisories are read live (or recorded as not readable)
        assert out["upstream"]["advisories"]["status"] in ("OK", "ERROR", "RATE_LIMITED")
