"""OFFICE adapters (Confluence/Jira/Bitbucket) — disabled by default, Cloud-only, respx-mocked (§04, §23, §29).

§50 rows: "Tenant tidak mendukung folder Confluence yang wajib → Block/needs action; tidak diam-diam mengganti
struktur"; OFFICE default deny (§23).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.models.portfolio import ProjectIntegration
from aeos.publisher import jobs
from aeos.publisher.providers import atlassian as atl
from aeos.publisher.providers.base import OpContext, ProviderBlocked, ProviderConflict
from aeos.publisher.targets import resolve_target
from tests.unit.test_publisher_support import confirm_of, install_fakes, make_plan, make_project

BASE = "https://contoh.atlassian.net"
CFG = atl.AtlassianConfig(base_url=BASE, enabled=True, deployment="CLOUD", space_id="SP1", parent_id="PARENT",
                          project_key="ABC")
AUTH = "Basic dGVzdDp0ZXN0"


def _ctx(**kw) -> OpContext:
    base = dict(operation_id="op", publication_id="pub-1", project_id="proj", project_key="k", project_name="Kantor",
                domain="OFFICE", provider="confluence", operation="create_folder", logical_id="root",
                idempotency_key="sha256:" + "3" * 64, title="Kantor", target={}, payload={})
    base.update(kw)
    return OpContext(**base)


def _conf(cfg: atl.AtlassianConfig = CFG) -> atl.ConfluenceProvider:
    return atl.ConfluenceProvider(cfg, client=httpx.Client(), auth_header=AUTH)


def test_adapters_are_disabled_by_default():
    off = atl.AtlassianConfig(base_url=BASE, deployment="CLOUD", space_id="S", parent_id="P", project_key="A")
    for cls in (atl.ConfluenceProvider, atl.JiraProvider):
        with pytest.raises(ProviderBlocked) as exc:
            cls(off, client=httpx.Client(), auth_header=AUTH)
        assert exc.value.code == "OFFICE_ADAPTER_DISABLED"
    with pytest.raises(ProviderBlocked):
        atl.BitbucketProvider(full_name="ws/repo", enabled=False, auth_header=AUTH)


def test_data_center_is_not_supported_and_unknown_deployment_blocked():
    dc = atl.AtlassianConfig(base_url=BASE, enabled=True, deployment="DATA_CENTER", space_id="S", parent_id="P")
    status, reasons, _ = atl.readiness(dc, product="confluence")
    assert status == "NOT_SUPPORTED" and "Data Center" in reasons[0]
    with pytest.raises(ProviderBlocked) as exc:
        _conf(dc)
    assert exc.value.code == "ATLASSIAN_DC_NOT_SUPPORTED"
    unk = atl.AtlassianConfig(base_url=BASE, enabled=True, space_id="S", parent_id="P")
    assert atl.readiness(unk, product="confluence")[0] == "BLOCKED"


def test_config_uses_canonical_parent_key_only():
    cfg = atl.AtlassianConfig.from_integration(BASE, None, {"CONFLUENCE_PROJECTS_PARENT_ID": "123", "enabled": True})
    assert cfg.parent_id == "123" and atl.CONFLUENCE_PARENT_KEY == "CONFLUENCE_PROJECTS_PARENT_ID"
    legacy = atl.AtlassianConfig.from_integration(BASE, None, {"confluence_parent": "999"})
    assert legacy.parent_id == ""  # no second legacy target
    assert atl.AtlassianConfig.from_integration(BASE, None, {"enabled": "true"}).enabled is False  # explicit bool only


@respx.mock
def test_tenant_without_folder_support_blocks_without_restructuring():
    respx.get(f"{BASE}/wiki/api/v2/spaces/SP1").mock(return_value=httpx.Response(200, json={"id": "SP1"}))
    respx.get(f"{BASE}/wiki/api/v2/folders/PARENT").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/wiki/api/v2/pages/PARENT").mock(return_value=httpx.Response(200, json={"id": "PARENT"}))
    folders = respx.post(f"{BASE}/wiki/api/v2/folders").mock(return_value=httpx.Response(200, json={"id": "X"}))
    pages = respx.post(f"{BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json={"id": "Y"}))
    p = _conf()
    with pytest.raises(ProviderBlocked) as exc:
        p.execute(_ctx())
    assert exc.value.code == "CONFLUENCE_FOLDERS_UNSUPPORTED" and "needs action" in exc.value.message
    with pytest.raises(ProviderBlocked):
        p.execute(_ctx(operation="create_doc", logical_id="00-product-home", payload={"markdown": "# x"}))
    assert not folders.called and not pages.called  # never silently switched to pages


@respx.mock
def test_api_v2_missing_means_not_cloud():
    respx.get(f"{BASE}/wiki/api/v2/spaces/SP1").mock(return_value=httpx.Response(404))
    caps = _conf().check_capabilities()
    assert caps["status"] == "BLOCKED" and caps["api_v2"] == "NOT_AVAILABLE"


@pytest.fixture
def cloud_ok():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/wiki/api/v2/spaces/SP1").mock(return_value=httpx.Response(200, json={"id": "SP1"}))
        mock.get(f"{BASE}/wiki/api/v2/folders/PARENT").mock(return_value=httpx.Response(200, json={"id": "PARENT"}))
        yield mock


def test_create_folder_and_page_in_real_project_folder(cloud_ok):
    folder = cloud_ok.post(f"{BASE}/wiki/api/v2/folders").mock(return_value=httpx.Response(200, json={
        "id": "F1", "_links": {"webui": "/spaces/SP1/folder/F1"}}))
    props = cloud_ok.post(url__regex=r".*/properties$").mock(return_value=httpx.Response(200, json={}))
    page = cloud_ok.post(f"{BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 1}, "_links": {"webui": "/spaces/SP1/pages/P1"}}))
    p = _conf()
    f = p.execute(_ctx())
    sent = json.loads(folder.calls[0].request.content)
    assert sent == {"spaceId": "SP1", "title": "Kantor", "parentId": "PARENT"} and f.external_id == "F1"
    md = "# Judul\n\n```mermaid\nflowchart TD\n A-->B\n```\n\n![Ctx](visual:ctx)\n\n![Other](visual:other)\n"
    res = p.execute(_ctx(operation="create_doc", logical_id="00-product-home", title="00. Product Home",
                         parent_external_id="F1", payload={"markdown": md, "rendered_visuals": ["ctx"]}))
    body = json.loads(page.calls[0].request.content)
    storage = body["body"]["value"]
    assert body["parentId"] == "F1" and body["body"]["representation"] == "storage"
    assert '<ac:structured-macro ac:name="code">' in storage and "tidak diasumsikan dirender" in storage
    assert '<ri:attachment ri:filename="aeos-ctx.png"/>' in storage and "[visual pending: Other]" in storage
    assert "```" not in storage
    assert res.external_revision == "1" and props.call_count == 2  # marker property on folder + page


def test_page_version_mismatch_is_conflict_without_put(cloud_ok):
    cloud_ok.get(f"{BASE}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 5}, "body": {"storage": {"value": "<p>diedit manusia</p>"}}}))
    put = cloud_ok.put(f"{BASE}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(ProviderConflict) as exc:
        _conf().execute(_ctx(operation="update_doc", logical_id="x", existing_external_id="P1", existing_revision="3",
                             payload={"markdown": "# baru"}))
    assert not put.called and exc.value.observed_revision == "5" and "diedit manusia" in exc.value.current_text


def test_page_update_increments_version(cloud_ok):
    cloud_ok.get(f"{BASE}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 3}}))
    put = cloud_ok.put(f"{BASE}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 4}}))
    res = _conf().execute(_ctx(operation="update_doc", logical_id="x", title="X", existing_external_id="P1",
                               existing_revision="3", payload={"markdown": "# baru"}))
    sent = json.loads(put.calls[0].request.content)
    assert sent["version"]["number"] == 4 and "pub-1" in sent["version"]["message"]
    assert res.external_revision == "4"


def test_confluence_reconcile_requires_marker_not_just_title(cloud_ok):
    cloud_ok.get(f"{BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json={"results": [
        {"id": "P9", "version": {"number": 2}}]}))
    cloud_ok.get(f"{BASE}/wiki/api/v2/pages/P9/properties").mock(return_value=httpx.Response(200, json={
        "results": [{"key": "aeos", "value": {"logical_id": "00-product-home", "project_id": "proj"}}]}))
    found = _conf().reconcile(_ctx(operation="create_doc", logical_id="00-product-home", title="00. Product Home"))
    assert found is not None and found.external_id == "P9" and found.external_revision == "2"
    cloud_ok.get(f"{BASE}/wiki/api/v2/pages/P9/properties").mock(return_value=httpx.Response(200, json={"results": []}))
    from aeos.publisher.providers.base import ProviderError

    with pytest.raises(ProviderError):  # same title without marker → human review, never adopted by title
        _conf().reconcile(_ctx(operation="create_doc", logical_id="00-product-home", title="00. Product Home"))


@respx.mock
def test_jira_issue_with_marker_label_and_reconcile():
    create = respx.post(f"{BASE}/rest/api/3/issue").mock(return_value=httpx.Response(201, json={
        "id": "100", "key": "ABC-1"}))
    search = respx.post(f"{BASE}/rest/api/3/search/jql").mock(return_value=httpx.Response(200, json={
        "issues": [{"key": "ABC-1"}]}))
    j = atl.JiraProvider(CFG, client=httpx.Client(), auth_header=AUTH)
    ctx = _ctx(provider="jira", operation="create_issue", logical_id="issue:task:T1",
               payload={"title": "T1", "body": "Tujuan\n<!-- aeos:logical_id=issue:task:T1 -->"})
    res = j.execute(ctx)
    fields = json.loads(create.calls[0].request.content)["fields"]
    label = atl.jira_marker_label("proj", "issue:task:T1")
    assert label in fields["labels"] and "aeos" in fields["labels"] and fields["description"]["type"] == "doc"
    assert res.external_id == "ABC-1" and res.external_url == f"{BASE}/browse/ABC-1"
    found = j.reconcile(ctx)
    assert found.external_id == "ABC-1" and label in json.loads(search.calls[0].request.content)["jql"]


@respx.mock
def test_bitbucket_is_read_only_references():
    respx.get("https://api.bitbucket.org/2.0/repositories/ws/repo").mock(return_value=httpx.Response(200, json={
        "full_name": "ws/repo", "uuid": "{u}", "links": {"html": {"href": "https://bitbucket.org/ws/repo"}},
        "mainbranch": {"name": "main"}, "is_private": True}))
    respx.get("https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests").mock(return_value=httpx.Response(
        200, json={"values": [{"id": 3, "title": "PR", "state": "OPEN", "source": {"branch": {"name": "aeos/t1"}},
                               "destination": {"branch": {"name": "main"}}}]}))
    b = atl.BitbucketProvider(full_name="ws/repo", enabled=True, client=httpx.Client(), auth_header=AUTH)
    assert b.repository_reference()["main_branch"] == "main"
    assert b.pull_request_references()[0]["source"] == "aeos/t1"
    assert not any(hasattr(b, m) for m in ("merge", "merge_pull_request", "execute"))  # human-only merge (U-09)


def test_office_end_to_end_blocks_when_folders_unsupported(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    from aeos.publisher import targets as targets_mod

    # CONTROLLED PRECONDITION: the company authorisation / CAN_USE_OFFICE_INTEGRATIONS gate is READY here; this
    # test is about the tenant's missing folder support (the gate itself: test_publisher_gaps).
    monkeypatch.setattr(targets_mod, "office_gate", lambda session, project: (True, []))
    secrets_memory.put("office/atlassian", json.dumps({"email": "a@b.invalid", "api_token": "atl-token-1234567890"}),
                       actor="test")
    project = make_project(db, domain="OFFICE")
    for provider, purpose in (("confluence", "docs"), ("jira", "issues")):
        db.add(ProjectIntegration(project_id=project.id, domain="OFFICE", provider=provider, purpose=purpose,
                                  tenant_id=BASE, config={"enabled": True, "deployment": "CLOUD", "space_id": "SP1",
                                                          "CONFLUENCE_PROJECTS_PARENT_ID": "PARENT",
                                                          "project_key": "ABC"}))
    db.commit()
    assert resolve_target(db, project, "docs").status == "READY"
    plan, _v = make_plan(db, project)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/wiki/api/v2/spaces/SP1").mock(return_value=httpx.Response(200, json={"id": "SP1"}))
        mock.get(f"{BASE}/wiki/api/v2/folders/PARENT").mock(return_value=httpx.Response(404))
        mock.get(f"{BASE}/wiki/api/v2/pages/PARENT").mock(return_value=httpx.Response(200, json={"id": "PARENT"}))
        posts = mock.post(url__regex=rf"{BASE}/wiki/.*").mock(return_value=httpx.Response(200, json={"id": "X"}))
        jira = mock.post(f"{BASE}/rest/api/3/issue").mock(return_value=httpx.Response(201, json={"key": "ABC-9"}))
        pub = jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="owner", options={"targets": ["docs"]},
                                  render_visuals=False)
        db.commit()
        assert jobs.execute_publication(db, pub.id, actor_user_id="owner", confirm=confirm_of(pub)).allowed
        db.commit()
        jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert not posts.called and not jira.called
    assert pub.status == "FAILED" and "BLOCKED: needs action" in pub.status_reason
    root = next(o for o in jobs._ops_for(db, pub.id) if o.logical_id == "root")
    assert root.status == "FAILED" and root.response["blocked"]["code"] == "CONFLUENCE_FOLDERS_UNSUPPORTED"
    children = [o for o in jobs._ops_for(db, pub.id) if o.logical_id != "root"]
    assert all(o.status == "PENDING" and o.attempts == 0 for o in children)  # nothing else attempted
    # OFFICE always went through the interlock for the attempted op
    from aeos.models.decisions import Decision

    assert db.scalar(select(Decision).where(Decision.kind == "PUBLICATION", Decision.domain == "OFFICE"))


@respx.mock
def test_parent_folder_in_another_space_is_not_a_valid_target():
    """§04: CONFLUENCE_PROJECTS_PARENT_ID is re-validated against the configured space (never a second target)."""
    respx.get(f"{BASE}/wiki/api/v2/spaces/SP1").mock(return_value=httpx.Response(200, json={"id": "SP1"}))
    respx.get(f"{BASE}/wiki/api/v2/folders/PARENT").mock(return_value=httpx.Response(200, json={
        "id": "PARENT", "spaceId": "OTHER"}))
    folders = respx.post(f"{BASE}/wiki/api/v2/folders").mock(return_value=httpx.Response(200, json={"id": "X"}))
    p = _conf()
    caps = p.check_capabilities()
    assert caps["status"] == "BLOCKED" and caps["folders"] == "SUPPORTED"
    assert any("space OTHER" in r and "needs action" in r for r in caps["reasons"])
    with pytest.raises(ProviderBlocked):
        p.execute(_ctx())
    assert not folders.called


def test_page_titles_carry_the_project_key_so_projects_can_share_a_space(cloud_ok):
    """Confluence titles are unique per space: create, update and reconcile use the same keyed title."""
    page = cloud_ok.post(f"{BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 1}}))
    cloud_ok.post(url__regex=r".*/properties$").mock(return_value=httpx.Response(200, json={}))
    sub = cloud_ok.post(f"{BASE}/wiki/api/v2/folders").mock(return_value=httpx.Response(200, json={"id": "F2"}))
    p = _conf()
    p.execute(_ctx(operation="create_doc", logical_id="00-product-home", title="00. Product Home",
                   parent_external_id="F1", payload={"markdown": "# x"}))
    assert json.loads(page.calls[0].request.content)["title"] == "00. Product Home [k]"
    p.execute(_ctx(logical_id="02-requirements", title="02. Requirements", parent_external_id="F1"))
    assert json.loads(sub.calls[0].request.content)["title"] == "02. Requirements [k]"
    p.execute(_ctx())  # the project folder itself keeps the project name
    assert json.loads(sub.calls[1].request.content)["title"] == "Kantor"
    search = cloud_ok.get(f"{BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json={"results": []}))
    assert p.reconcile(_ctx(operation="create_doc", logical_id="00-product-home", title="00. Product Home")) is None
    assert search.calls[0].request.url.params["title"] == "00. Product Home [k]"
    cloud_ok.get(f"{BASE}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 1}}))
    put = cloud_ok.put(f"{BASE}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 2}}))
    p.execute(_ctx(operation="update_doc", logical_id="00-product-home", title="00. Product Home",
                   existing_external_id="P1", existing_revision="1", payload={"markdown": "# y"}))
    assert json.loads(put.calls[0].request.content)["title"] == "00. Product Home [k]"
    assert atl.ConfluenceProvider.title_for(_ctx(title="X [k]", logical_id="a")) == "X [k]"  # idempotent
    assert len(atl.ConfluenceProvider.title_for(_ctx(title="T" * 400, logical_id="a"))) == 255
