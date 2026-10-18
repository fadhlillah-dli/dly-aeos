"""GitHub Issues adapter + OperationalWorkProjection (§04, §13, §14) with respx.

§50 row: "Jira/GitHub issue Done tanpa evidence runtime → Tidak otomatis membuka dependency eksekusi".
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.timeutil import utcnow
from aeos.models.evidence import AcceptanceRecord
from aeos.models.planning import TaskSpec
from aeos.models.publishing import OperationalWorkProjection
from aeos.models.workforce import TaskExecution
from aeos.publisher import jobs, work_items
from aeos.publisher.providers import github_issues as gh
from aeos.publisher.providers.base import OpContext, ProviderError
from tests.unit.test_publisher_support import (
    confirm_of,
    install_fakes,
    make_plan,
    make_project,
    seed_personal_secrets,
)

API = f"{gh.GITHUB_API}/repos/owner/contoh"


def _ctx(**kw) -> OpContext:
    base = dict(operation_id="op", publication_id="pub", project_id="proj", project_key="k", project_name="Contoh",
                domain="PERSONAL", provider="github-issues", operation="create_issue", logical_id="issue:task:T1",
                idempotency_key="sha256:" + "2" * 64, title="T1", target={},
                payload={"title": "[k] T1", "body": "isi", "labels": ["aeos"]})
    base.update(kw)
    return OpContext(**base)


def _p() -> gh.GitHubIssuesProvider:
    return gh.GitHubIssuesProvider(repo="owner/contoh", client=httpx.Client(), token="ghp_unit0123456789abcdefghijklmnopq")


def test_missing_repo_is_blocked():
    from aeos.publisher.providers.base import ProviderBlocked

    with pytest.raises(ProviderBlocked):
        gh.GitHubIssuesProvider(repo="", token="x" * 20)


@respx.mock
def test_create_issue_embeds_hidden_markers():
    route = respx.post(f"{API}/issues").mock(return_value=httpx.Response(201, json={
        "number": 7, "html_url": "https://github.com/owner/contoh/issues/7", "state": "open"}))
    res = _p().execute(_ctx())
    body = json.loads(route.calls[0].request.content)["body"]
    assert gh.marker("issue:task:T1") in body and gh.project_marker("proj") in body
    assert res.external_id == "7" and res.kind == "issue"
    assert route.calls[0].request.headers["Authorization"].startswith("Bearer ")


@respx.mock
def test_ensure_label_is_idempotent():
    respx.get(f"{API}/labels/feature%3Atasks").mock(return_value=httpx.Response(404))
    create = respx.post(f"{API}/labels").mock(return_value=httpx.Response(422, json={"message": "already_exists"}))
    res = _p().execute(_ctx(operation="ensure_label", logical_id="label:feature:tasks",
                            payload={"name": "feature:tasks"}))
    assert create.called and res.external_id == "feature:tasks"


@respx.mock
def test_reconcile_by_marker_uses_consistent_list_and_skips_prs():
    respx.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[
        {"number": 1, "body": gh.marker("issue:task:T1") + gh.project_marker("proj"), "pull_request": {}},
        {"number": 2, "body": gh.marker("issue:task:T1") + gh.project_marker("OTHER")},
        {"number": 3, "body": "x\n" + gh.marker("issue:task:T1") + "\n" + gh.project_marker("proj"),
         "html_url": "https://github.com/owner/contoh/issues/3", "state": "open"},
    ]))
    found = _p().reconcile(_ctx())
    assert found is not None and found.external_id == "3"
    req = respx.calls[0].request
    assert req.url.params["labels"] == "aeos" and req.url.params["state"] == "all"
    assert "search" not in str(req.url)


@respx.mock
def test_reconcile_page_budget_exhaustion_keeps_unknown():
    respx.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[{"number": i, "body": ""}
                                                                           for i in range(100)]))
    with pytest.raises(ProviderError) as exc:
        _p().reconcile(_ctx())
    assert "absence not proven" in exc.value.message


@pytest.fixture
def gh_env(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    seed_personal_secrets(secrets_memory)
    from aeos.publisher import providers

    monkeypatch.setitem(providers._overrides, "github-issues", lambda cfg: _p())
    project = make_project(db)
    plan, version = make_plan(db, project)
    return {"db": db, "settings": settings, "plan": plan, "project": project, "version": version}


def _publish_issues(env):
    db = env["db"]
    pub = jobs.create_preview(db, env["settings"], plan_id=env["plan"].id, actor_user_id="owner",
                              options={"targets": ["issues"]}, render_visuals=False)
    db.commit()
    assert jobs.execute_publication(db, pub.id, actor_user_id="owner", confirm=confirm_of(pub)).allowed
    db.commit()
    return pub


def test_issue_timeout_reconciled_by_marker_no_duplicate(gh_env):
    db = gh_env["db"]
    created: list[dict] = []

    def create(request):
        body = json.loads(request.content)
        created.append(body)
        num = len(created)
        if "issue:task:T1" in body["body"] and num == 1:
            raise httpx.ReadTimeout("lost")
        return httpx.Response(201, json={"number": num, "html_url": f"https://github.com/owner/contoh/issues/{num}",
                                         "state": "open"})

    def list_issues(request):
        return httpx.Response(200, json=[{"number": i + 1, "body": b["body"], "state": "open",
                                          "html_url": f"https://github.com/owner/contoh/issues/{i + 1}"}
                                         for i, b in enumerate(created)])

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{API}/labels/.*").mock(return_value=httpx.Response(200, json={"url": "u"}))
        mock.post(f"{API}/issues").mock(side_effect=create)
        mock.get(f"{API}/issues").mock(side_effect=list_issues)
        pub = _publish_issues(gh_env)
        jobs.process_publication(db, pub.id)
        t1 = next(o for o in jobs._ops_for(db, pub.id) if o.logical_id == "issue:task:T1")
        assert t1.status == "UNKNOWN"
        jobs.process_publication(db, pub.id, now=utcnow() + timedelta(hours=1))
    db.refresh(t1)
    assert t1.status == "SUCCEEDED" and t1.external_id == "1"
    assert sum(1 for b in created if gh.marker("issue:task:T1") in b["body"]) == 1
    db.refresh(pub)
    assert pub.status == "PUBLISHED"
    proj = db.scalar(select(OperationalWorkProjection).where(OperationalWorkProjection.issue_id == "1"))
    assert proj is not None and proj.task_id is not None and proj.sync_status == "SYNCED"


def test_closed_issue_projection_never_unlocks_dependencies(gh_env):
    db = gh_env["db"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{API}/labels/.*").mock(return_value=httpx.Response(200, json={}))
        mock.post(f"{API}/issues").mock(side_effect=lambda req: httpx.Response(201, json={
            "number": 10 + len(mock.calls), "html_url": "https://github.com/owner/contoh/issues/x", "state": "open"}))
        pub = _publish_issues(gh_env)
        jobs.process_publication(db, pub.id)
    tasks = list(db.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == gh_env["version"].id)))
    for t in tasks:
        db.add(TaskExecution(task_id=t.id, project_id=t.project_id, domain=t.domain, state="BLOCKED",
                             reason="waiting for dependency"))
    db.commit()
    rows = list(db.scalars(select(OperationalWorkProjection)))
    assert rows
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{API}/issues/\d+").mock(return_value=httpx.Response(200, json={
            "state": "closed", "state_reason": "completed", "html_url": "https://github.com/owner/contoh/issues/1",
            "title": "Done di GitHub"}))
        result = work_items.sync_work_items(db, gh_env["project"], actor_user_id="owner")
    db.commit()
    assert result["synced"] == len(rows) and result["authority"] == "PROJECTION_ONLY"
    for row in db.scalars(select(OperationalWorkProjection)):
        assert row.external_status == "closed:completed" and row.last_synced_at is not None
    # projection only: no acceptance, runtime states untouched
    assert db.scalar(select(AcceptanceRecord)) is None
    assert {te.state for te in db.scalars(select(TaskExecution))} == {"BLOCKED"}
    view = work_items.list_work_items(db, gh_env["project"])
    assert all(i["unlocks_runtime_dependencies"] is False for i in view["items"])
    assert view["capabilities"]["github_projects"] == "NOT_IMPLEMENTED"


def test_sync_failure_keeps_last_known_status(gh_env):
    db = gh_env["db"]
    db.add(OperationalWorkProjection(project_id=gh_env["project"].id, provider="github-issues", issue_id="5",
                                     external_status="open", last_synced_at=utcnow() - timedelta(days=1),
                                     sync_status="SYNCED"))
    db.commit()
    with respx.mock:
        respx.get(f"{API}/issues/5").mock(return_value=httpx.Response(503))
        result = work_items.sync_work_items(db, gh_env["project"], actor_user_id="owner")
    row = db.scalar(select(OperationalWorkProjection).where(OperationalWorkProjection.issue_id == "5"))
    assert result["failed"] == 1 and row.sync_status == "FAILED" and row.external_status == "open"


def test_sync_without_token_is_blocked(settings, db, monkeypatch, secrets_memory):
    install_fakes(monkeypatch)
    from aeos.core.errors import Blocked

    project = make_project(db)
    with pytest.raises(Blocked) as exc:
        work_items.sync_work_items(db, project, actor_user_id="owner")
    assert gh.CONNECT_REMEDY in exc.value.details["remedies"]
