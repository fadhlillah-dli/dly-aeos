"""OFFICE repository identity (master §04 "OFFICE source: the selected Bitbucket repository"): verified
only behind a recorded company authorisation for Bitbucket and an enabled adapter; exact identity, never
guessed (rename / 404 / uuid change stay UNVERIFIED)."""

from __future__ import annotations

import httpx
import pytest
import respx

from aeos.core.enums import Domain
from aeos.portfolio import repositories
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project

API = "https://api.bitbucket.org/2.0/repositories/company/svc"


@pytest.fixture
def office(admin_client: AuthedClient, db, secrets_memory, monkeypatch):
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Office Repo")
    r = admin_client.post(f"/api/projects/{p['id']}/repositories",
                          json={"provider": "BITBUCKET", "owner": "company", "name": "svc"})
    assert r.status_code == 200, r.text
    secrets_memory.put("office/bitbucket", "dev" + "@" + "example.com:app-password-value-123", actor="test")
    monkeypatch.setattr(repositories, "BITBUCKET_CLIENT_FACTORY", lambda: httpx.Client(timeout=5))
    return {"client": admin_client, "project_id": p["id"], "repo_id": r.json()["repository"]["id"]}


def _authorise(db, providers=("bitbucket",)):
    from aeos.models.identity import User
    from aeos.policy import facade as policy

    admin = db.query(User).first()
    policy.record_decision(db, kind="POLICY", actor_user_id=admin.id, target_type="office_policy", target_id="OFFICE",
                           domain="OFFICE", rationale="company authorisation (test)",
                           details={"office_authorization": True, "company_reference": "CHG-7 signed",
                                    "allowed_providers": list(providers)}, auth_scope={"providers": list(providers)})
    db.commit()


def _enable(o, enabled=True):
    r = o["client"].put(f"/api/projects/{o['project_id']}/integrations",
                        json={"provider": "bitbucket", "purpose": "code", "root_id": "company/svc",
                              "credential_ref": "office/bitbucket", "config": {"enabled": enabled}})
    assert r.status_code == 200, r.text


def _verify(o):
    r = o["client"].post(f"/api/repositories/{o['repo_id']}/verify")
    assert r.status_code == 200, r.text
    return r.json()["verification"]


def test_no_provider_call_without_authorisation_or_enabled_adapter(office, db):
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        v = _verify(office)
        assert v["verification_status"] == "UNVERIFIED" and "company authorisation" in v["reason"]
        _authorise(db, providers=("jira",))  # authorised, but not for Bitbucket
        assert "company authorisation" in _verify(office)["reason"]
        _authorise(db)
        _enable(office, enabled=False)
        assert "disabled" in _verify(office)["reason"]
        assert not mock.calls


def test_verified_with_exact_identity(office, db):
    _authorise(db)
    _enable(office)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(API).mock(return_value=httpx.Response(200, json={
            "full_name": "company/svc", "uuid": "{1111-2222}", "is_private": True, "mainbranch": {"name": "develop"},
            "links": {"html": {"href": "https://bitbucket.org/company/svc"}}}))
        v = _verify(office)
    assert v["verification_status"] == "VERIFIED", v
    repo = office["client"].get(f"/api/repositories/{office['repo_id']}").json()["repository"]
    assert repo["provider_repo_id"] == "{1111-2222}" and repo["default_branch"] == "develop"
    assert "app-password-value" not in str(repo)
    # a later uuid change is never accepted silently
    with respx.mock() as mock:
        mock.get(API).mock(return_value=httpx.Response(200, json={"full_name": "company/svc", "uuid": "{9999}"}))
        v = _verify(office)
    assert v["verification_status"] == "UNVERIFIED" and "uuid changed" in v["reason"]


@pytest.mark.parametrize("response,needle", [
    (httpx.Response(404, json={"error": {"message": "not found"}}), "never guessed"),
    (httpx.Response(200, json={"full_name": "company/svc-renamed", "uuid": "{1}"}), "renamed"),
])
def test_missing_or_renamed_repository_stays_unverified(office, db, response, needle):
    _authorise(db)
    _enable(office)
    with respx.mock() as mock:
        mock.get(API).mock(return_value=response)
        v = _verify(office)
    assert v["verification_status"] == "UNVERIFIED" and needle in v["reason"]
