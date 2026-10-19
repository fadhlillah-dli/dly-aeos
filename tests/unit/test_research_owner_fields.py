"""Research & Technology Registry gaps (§47, §52): owner-recorded fields with a write path, the DISCOVERED ->
SOURCE_VERIFIED transition, dated manual review and STALE freshness, advisories and dependency diff by code,
maintainer from the live check, and a concise UPGRADE draft only for relevant (pinned + linked) updates."""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from aeos.core.errors import Blocked, NotFound, PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import Event
from aeos.models.research import ChangeProposal, ResearchEntry
from aeos.research import service as research
from aeos.research.dependencies import dependency_diff, parse_file

API = "https://api.github.com"


def _seeded(db, key="tool:opencode") -> ResearchEntry:
    research.seed_registry(db)
    db.commit()
    return db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == key))


def _manual(db, url="https://github.com/acme/widget", domain="PERSONAL") -> ResearchEntry:
    e, _dup = research.add_by_url(db, url=url, name=None, category=None, notes=None, actor_user_id="owner", domain=domain)
    db.commit()
    return e


def test_owner_records_maintainer_pin_terms_compatibility_distribution_and_supersession(db):
    e = _manual(db)
    old = _manual(db, url="https://github.com/acme/widget-legacy")
    research.update_entry(db, e, fields={"maintainer": "acme-org", "source_version": "v1.2.0", "data_terms": "tanpa telemetri",
                                         "terms_checked": True, "compatibility": "API v2", "distribution": "npm: @acme/widget",
                                         "limitations": "Belum mendukung Windows ARM"}, actor_user_id="owner",
                         domain="PERSONAL")
    research.update_entry(db, old, fields={"superseded_by": e.id}, actor_user_id="owner", domain="PERSONAL")
    db.commit()
    v = research.entry_view(e, detail=True)
    assert (v["maintainer"], v["source_version"], v["distribution"]) == ("acme-org", "v1.2.0", "npm: @acme/widget")
    assert v["data_terms"] == "tanpa telemetri" and v["terms_checked_at"] and v["compatibility"] == "API v2"
    assert v["limitations"] == "Belum mendukung Windows ARM" and v["history"][-1]["event"] == "OWNER_EDIT"
    assert research.entry_view(old, detail=True)["superseded_by"] == e.id
    with pytest.raises(ValidationFailed):  # the terms date needs the terms text first
        research.update_entry(db, old, fields={"terms_checked": True}, actor_user_id="owner", domain="PERSONAL")


def test_seeded_rows_keep_master_text_and_office_never_edits_shared_rows(db):
    oc = _seeded(db)
    with pytest.raises(ValidationFailed):
        research.update_entry(db, oc, fields={"limitations": "x"}, actor_user_id="owner", domain="PERSONAL")
    research.update_entry(db, oc, fields={"source_version": "1.3.0"}, actor_user_id="owner", domain="PERSONAL")
    with pytest.raises(PolicyDenied):
        research.update_entry(db, oc, fields={"maintainer": "corp"}, actor_user_id="owner", domain="OFFICE")
    ref = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "ref:S01"))
    with pytest.raises(PolicyDenied):
        research.update_entry(db, ref, fields={"maintainer": "x"}, actor_user_id="owner", domain="PERSONAL")
    office = _manual(db, url="https://github.com/corp/tool", domain="OFFICE")
    with pytest.raises(NotFound):
        research.update_entry(db, office, fields={"maintainer": "x"}, actor_user_id="owner", domain="PERSONAL")


@respx.mock(assert_all_called=False)
def test_check_upstream_records_maintainer_and_advisories_then_owner_confirms_source(respx_mock, db):
    e = _manual(db)
    with pytest.raises(Blocked) as blocked:  # no live check yet -> no SOURCE_VERIFIED
        research.confirm_source(db, e, actor_user_id="owner", domain="PERSONAL")
    assert blocked.value.code == "SOURCE_NOT_CHECKED"
    respx_mock.get(f"{API}/repos/acme/widget").mock(return_value=httpx.Response(200, json={
        "full_name": "acme/widget", "owner": {"login": "acme"}, "license": {"spdx_id": "MIT"}, "fork": False}))
    respx_mock.get(f"{API}/repos/acme/widget/releases/latest").mock(return_value=httpx.Response(200, json={"tag_name": "v1.4.0"}))
    respx_mock.get(f"{API}/repos/acme/widget/security-advisories").mock(return_value=httpx.Response(200, json=[
        {"ghsa_id": "GHSA-xxxx", "severity": "high", "summary": "RCE", "published_at": "2026-10-01T00:00:00Z",
         "vulnerabilities": [{"package": {"name": "widget", "ecosystem": "npm"}, "vulnerable_version_range": "< 1.4.0",
                              "patched_versions": "1.4.0"}]}]))
    view = research.check_upstream(db, e, actor_type="user", actor_id="owner", domain="PERSONAL")
    db.commit()
    assert view["maintainer"] == "acme" and view["license"] == "MIT"
    assert view["upstream"]["advisories"]["status"] == "OK" and view["upstream"]["advisories"]["items"][0]["ghsa_id"] == "GHSA-xxxx"
    research.update_entry(db, e, fields={"source_version": "v1.3.0"}, actor_user_id="owner", domain="PERSONAL")
    assert research.entry_view(e, detail=True)["advisories"]["status"] == "AFFECTED"
    assert view["can_confirm_source"] is True
    research.confirm_source(db, e, actor_user_id="owner", domain="PERSONAL")
    db.commit()
    assert e.lifecycle == "SOURCE_VERIFIED" and e.history[-1]["event"] == "SOURCE_CONFIRMED"
    with pytest.raises(ValidationFailed):
        research.confirm_source(db, e, actor_user_id="owner", domain="PERSONAL")


@respx.mock(assert_all_called=False)
def test_advisory_read_failure_is_unknown_not_clean(respx_mock, db):
    e = _seeded(db)
    respx_mock.get(f"{API}/repos/anomalyco/opencode").mock(return_value=httpx.Response(200, json={
        "full_name": "anomalyco/opencode", "owner": {"login": "anomalyco"}}))
    respx_mock.get(f"{API}/repos/anomalyco/opencode/releases/latest").mock(return_value=httpx.Response(404, json={}))
    respx_mock.get(f"{API}/repos/anomalyco/opencode/tags?per_page=1").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{API}/repos/anomalyco/opencode/security-advisories").mock(return_value=httpx.Response(500))
    view = research.check_upstream(db, e, actor_type="user", actor_id="owner", domain="PERSONAL")
    assert view["upstream"]["advisories"]["status"] == "ERROR" and view["advisories"]["status"] == "ERROR"


def test_manual_review_makes_freshness_dated_and_old_checks_turn_stale(db):
    e = _seeded(db, "tool:litellm")  # docs page: Check Upstream cannot read it
    assert research.freshness(e) == "NOT_CHECKED"
    with pytest.raises(ValidationFailed):
        research.mark_reviewed(db, e, note="ok", actor_user_id="owner", domain="PERSONAL")
    research.mark_reviewed(db, e, note="Membaca ulang docs virtual keys + budget 19 Okt", actor_user_id="owner",
                           domain="PERSONAL")
    db.commit()
    assert research.freshness(e) == "FRESH" and e.history[-1]["event"] == "MANUAL_REVIEW"
    assert e.identity_status == "STATED_BY_MASTER", "a manual review is not a live identity check"
    e.reviewed_at = utcnow() - timedelta(days=31)
    db.commit()
    assert research.freshness(e) == "STALE"
    assert research.entry_view(e)["freshness"] == "STALE"
    oc = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:opencode"))
    oc.checked_at = utcnow() - timedelta(days=31)
    db.commit()
    assert [i["freshness"] for i in research.list_entries(db, domain="PERSONAL", q="anomalyco")
            if i["entry_key"] == "tool:opencode"] == ["STALE"]


def test_update_notice_only_for_pinned_linked_entries_and_never_a_proposal(db):
    oc = _seeded(db)
    oc.upstream = {"update_available": True, "latest_tag": "1.4.0",
                   "latest_release": {"url": "https://github.com/anomalyco/opencode/releases/tag/1.4.0"}}
    oc.source_version = "1.3.0"
    db.commit()
    assert research.update_notice(oc) is None, "not linked to an inventory component -> not relevant"
    oc.component_id = "opencode"
    db.commit()
    notice = research.update_notice(oc)
    assert notice["from"] == "1.3.0" and notice["to"] == "1.4.0"
    assert notice["draft_link"].startswith("/proposals?kind=UPGRADE&component=opencode") and "pin=1.4.0" in notice["draft_link"]
    assert "tidak berarti kompatibel" in notice["summary"]
    assert db.scalar(select(func.count()).select_from(ChangeProposal)) == 0


@respx.mock(assert_all_called=False)
def test_update_detected_event_carries_relevance_and_draft(respx_mock, db):
    oc = _seeded(db)
    oc.source_version, oc.component_id = "1.3.0", "opencode"
    db.commit()
    respx_mock.get(f"{API}/repos/anomalyco/opencode").mock(return_value=httpx.Response(200, json={"full_name": "anomalyco/opencode"}))
    respx_mock.get(f"{API}/repos/anomalyco/opencode/releases/latest").mock(return_value=httpx.Response(200, json={"tag_name": "1.4.0"}))
    research.check_upstream(db, oc, actor_type="user", actor_id="owner", domain="PERSONAL")
    db.commit()
    ev = db.scalar(select(Event).where(Event.kind == "research.upstream.update_detected"))
    assert ev.payload["relevant"] is True and ev.payload["draft_link"] and ev.payload["action"].startswith("notification only")


def _content(obj: str) -> dict:
    return {"type": "file", "encoding": "base64", "size": len(obj), "content": base64.b64encode(obj.encode()).decode()}


def test_parse_manifests_and_lockfiles():
    assert parse_file("package.json", json.dumps({"dependencies": {"a": "^1"}, "devDependencies": {"b": "2"}})) == \
        {"a": "^1", "b": "devDependencies:2"}
    assert parse_file("requirements.txt", "httpx>=0.27 # http\n-r other.txt\nPyYAML==6.0\n") == \
        {"httpx": ">=0.27", "pyyaml": "==6.0"}
    assert parse_file("pyproject.toml", '[project]\ndependencies = ["fastapi>=0.110", "pydantic[email]>=2"]\n') == \
        {"fastapi": ">=0.110", "pydantic": "[email]>=2"}
    assert parse_file("uv.lock", '[[package]]\nname = "anyio"\nversion = "4.4.0"\n') == {"anyio": "4.4.0"}
    assert parse_file("go.mod", "module x\nrequire (\n  golang.org/x/net v0.20.0\n)\n") == {"golang.org/x/net": "v0.20.0"}
    with pytest.raises(ValueError):
        parse_file("package.json", "{not json")


@respx.mock(assert_all_called=False)
def test_dependency_diff_by_code_between_two_refs(respx_mock, db):
    oc = _seeded(db)
    old = json.dumps({"dependencies": {"left-pad": "1.0.0", "chalk": "4.0.0"}})
    new = json.dumps({"dependencies": {"chalk": "5.0.0", "zod": "3.0.0"}})
    lock_new = json.dumps({"packages": {"": {}, "node_modules/zod": {"version": "3.0.0"}}})

    def route(request):
        path = request.url.path.split("/contents/", 1)[1]
        ref = request.url.params.get("ref")
        files = {("package.json", "1.3.0"): old, ("package.json", "1.4.0"): new, ("package-lock.json", "1.4.0"): lock_new}
        body = files.get((path, ref))
        return httpx.Response(200, json=_content(body)) if body is not None else httpx.Response(404, json={})

    respx_mock.get(url__regex=r"https://api\.github\.com/repos/anomalyco/opencode/contents/.*").mock(side_effect=route)
    result = dependency_diff(db, oc, base_ref="1.3.0", head_ref="1.4.0", actor_type="user", actor_id="owner",
                             domain="PERSONAL")
    db.commit()
    pkg = next(m for m in result["manifests"] if m["path"] == "package.json")
    assert pkg["status"] == "CHANGED" and [a["name"] for a in pkg["added"]] == ["zod"]
    assert [r["name"] for r in pkg["removed"]] == ["left-pad"] and pkg["changed"] == [{"name": "chalk", "from": "4.0.0", "to": "5.0.0"}]
    lock = next(m for m in result["manifests"] if m["path"] == "package-lock.json")
    assert lock["status"] == "ADDED" and result["transitive"] == "dari lockfile"
    assert "UNKNOWN" in result["licenses"] and oc.upstream["dependency_diff"]["head"] == "1.4.0"
    with pytest.raises(ValidationFailed):
        dependency_diff(db, oc, base_ref="../x", head_ref="1.4.0", actor_type="user", actor_id="owner", domain="PERSONAL")
    with pytest.raises(PolicyDenied):
        dependency_diff(db, oc, base_ref="1.3.0", head_ref="1.4.0", actor_type="user", actor_id="owner", domain="OFFICE")
