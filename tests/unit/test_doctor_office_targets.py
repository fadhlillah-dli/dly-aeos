"""§08 project adapters, OFFICE side: once the company authorisation gate is open, the Doctor proves the exact
Jira project key, Confluence parent (and its space) and Bitbucket repository with ONE read-only GET each and the
stored OFFICE credential. Behind the closed gate nothing is probed (OFFICE default deny, §23). No network: every
request goes through the fake runner."""

from __future__ import annotations

import json

import pytest

from aeos.doctor import office
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.checks import adapters as adapters_mod
from aeos.doctor.runner import make_context, run_check
from aeos.inventory.probes import HttpResult
from tests.unit.test_inventory_fakes import base_runner

TENANT = "corp.atlassian.net"
JIRA_URL = f"https://{TENANT}/rest/api/3/project/CORP"
FOLDER_URL = f"https://{TENANT}/wiki/api/v2/folders/12345"
PAGE_URL = f"https://{TENANT}/wiki/api/v2/pages/12345"
REPO_URL = "https://api.bitbucket.org/2.0/repositories/corp/app"
TOKEN = "ATATT3x" + "k" * 30


@pytest.fixture
def office_project(db):
    from aeos.models.portfolio import Project, ProjectIntegration

    p = Project(project_key="corp", name="Corp", domain="OFFICE")
    db.add(p)
    db.flush()
    db.add_all([
        ProjectIntegration(project_id=p.id, domain="OFFICE", provider="jira", purpose="issues", root_id="CORP",
                           tenant_id=TENANT, config={"deployment": "CLOUD"}),
        ProjectIntegration(project_id=p.id, domain="OFFICE", provider="confluence", purpose="docs", root_id="12345",
                           tenant_id=TENANT, config={"deployment": "CLOUD", "space_id": "777"}),
        ProjectIntegration(project_id=p.id, domain="OFFICE", provider="bitbucket", purpose="code",
                           root_id="corp/app", config={}),
    ])
    db.commit()
    return p


def _run(settings, db, runner, *, functional=True):
    load_all()
    ctx = make_context(settings, db, runner=runner, functional=functional)
    (target,) = adapters_mod.project_office_targets(ctx)
    ctx.project = target
    return run_check(REGISTRY["adapter.project_office_targets"], ctx)


def _open_gate(monkeypatch):
    monkeypatch.setattr(office, "office_gate", lambda capability, session, root=None: (True, "test: recorded"))


def _credentials(secrets_memory):
    secrets_memory.put("office/atlassian", json.dumps({"email": "me@corp.example", "api_token": TOKEN}), actor="t")
    secrets_memory.put("office/bitbucket", json.dumps({"username": "me", "app_password": "bb-" + "p" * 20}),
                       actor="t")


def _healthy(runner):
    runner.http[JIRA_URL] = HttpResult(200, {"id": "10001", "key": "CORP", "name": "Corp"})
    runner.http[FOLDER_URL] = HttpResult(200, {"id": "12345", "spaceId": "777"})
    runner.http[REPO_URL] = HttpResult(200, {"full_name": "corp/app", "uuid": "{u}", "is_private": True})


def test_closed_gate_probes_nothing(settings, db, office_project, secrets_memory):
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r)
    assert res.status == "UNKNOWN" and res.error_code == "OFFICE_DEFAULT_DENY"
    assert not r.http_calls, "behind the closed OFFICE gate no request is sent"


def test_open_gate_proves_each_exact_target_read_only(settings, db, office_project, secrets_memory, monkeypatch):
    _open_gate(monkeypatch)
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    assert _run(settings, db, r, functional=False).status == "CONFIGURED" and not r.http_calls
    res = _run(settings, db, r)
    assert res.status == "CONNECTED", res.observed_evidence
    by = {x["provider"]: x for x in res.observed_evidence["results"]}
    assert by["jira"]["project_id"] == "10001" and by["confluence"]["parent_kind"] == "folder"
    assert by["bitbucket"]["status"] == "CONNECTED"
    assert {c["url"] for c in r.http_calls} == {JIRA_URL, FOLDER_URL, REPO_URL}
    assert all(c["allow_remote"] and c["headers"]["Authorization"].startswith("Basic ") for c in r.http_calls)
    assert TOKEN not in json.dumps(res.as_dict()), "the credential never reaches the result"
    assert res.scope["domain"] == "OFFICE" and res.scope["project_id"] == office_project.id


@pytest.mark.parametrize(("url", "result", "status", "error"), [
    (JIRA_URL, HttpResult(200, {"key": "OTHER"}), "FAILED", "JIRA_PROJECT_MISMATCH"),
    (JIRA_URL, HttpResult(401, {}), "EXPIRED", "OFFICE_CREDENTIAL_REJECTED"),
    (FOLDER_URL, HttpResult(200, {"id": "12345", "spaceId": "999"}), "FAILED", "CONFLUENCE_PARENT_OTHER_SPACE"),
    (REPO_URL, HttpResult(404, {}), "FAILED", "BITBUCKET_NOT_VISIBLE"),
    (REPO_URL, HttpResult(None, error="TIMEOUT"), "UNKNOWN", "TIMEOUT"),
])
def test_a_wrong_or_unreachable_target_is_never_green(settings, db, office_project, secrets_memory, monkeypatch,
                                                       url, result, status, error):
    _open_gate(monkeypatch)
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    r.http[url] = result
    res = _run(settings, db, r)
    assert res.status == status
    assert error in {x.get("error") for x in res.observed_evidence["results"]}


def test_confluence_parent_page_is_accepted_and_missing_credentials_are_missing(settings, db, office_project,
                                                                                 secrets_memory, monkeypatch):
    _open_gate(monkeypatch)
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r)
    assert res.status == "MISSING" and res.error_code == "OFFICE_CREDENTIAL_MISSING" and not r.http_calls
    _credentials(secrets_memory)
    r.http[FOLDER_URL] = HttpResult(404, {})
    r.http[PAGE_URL] = HttpResult(200, {"id": "12345", "spaceId": "777"})
    res = _run(settings, db, r)
    assert res.status == "CONNECTED"
    conf = next(x for x in res.observed_evidence["results"] if x["provider"] == "confluence")
    assert conf["parent_kind"] == "page"


def test_data_center_tenant_is_not_supported(settings, db, office_project, secrets_memory, monkeypatch):
    from aeos.models.portfolio import ProjectIntegration

    _open_gate(monkeypatch)
    _credentials(secrets_memory)
    jira = db.query(ProjectIntegration).filter_by(provider="jira").one()
    jira.config = {"deployment": "DATA_CENTER"}
    db.commit()
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r)
    assert res.status == "FAILED"
    assert "ATLASSIAN_DC_NOT_SUPPORTED" in {x.get("error") for x in res.observed_evidence["results"]}
    assert JIRA_URL not in {c["url"] for c in r.http_calls}
