"""Integration Registry (§04, §13 ProjectIntegration, §23 OFFICE default deny, §26 credential refs only)."""

from __future__ import annotations

from aeos.core.enums import Domain
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project

DRIVE_FOLDER = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


def _put(client: AuthedClient, pid: str, **body):
    return client.put(f"/api/projects/{pid}/integrations", json=body)


def test_personal_drive_docs_with_remedy(personal_client: AuthedClient, secrets_memory):
    p = create_project(personal_client)
    r = _put(personal_client, p["id"], provider="GOOGLE_DRIVE", purpose="docs", credential_ref="personal/google-oauth")
    assert r.status_code == 200, r.text
    integ = r.json()["integration"]
    cap = integ["capability_status"]
    assert cap["configuration"] == "MISSING" and cap["root"] == "MISSING"
    assert cap["connectivity"] == "NOT_CHECKED" and cap["functional"] == "NOT_TESTED"  # never claims VERIFIED
    assert cap["write_enabled"] is False
    assert any(x["command"] == "aeos publish google-connect" for x in cap["remedies"])
    assert integ["root_id_name"] == "GOOGLE_DRIVE_FOLDER_ID"
    r = _put(personal_client, p["id"], provider="GOOGLE_DRIVE", purpose="docs", root_id=DRIVE_FOLDER,
             credential_ref="personal/google-oauth")
    assert r.json()["integration"]["capability_status"]["root"] == "CONFIGURED"
    secrets_memory.put("personal/google-oauth", "refresh-value-not-shown-123456", actor="test")
    listed = personal_client.get(f"/api/projects/{p['id']}/integrations").json()
    assert listed["items"][0]["capability_status"]["configuration"] == "CONFIGURED"
    assert "refresh-value-not-shown" not in str(listed)
    assert listed["source"] and listed["checked_at"]
    assert "GOOGLE_DRIVE" in listed["allowed_providers"] and "CONFLUENCE" not in listed["allowed_providers"]


def test_office_provider_rejected_in_personal(personal_client: AuthedClient):
    p = create_project(personal_client)
    for provider, purpose in (("CONFLUENCE", "docs"), ("JIRA", "issues"), ("BITBUCKET", "code")):
        r = _put(personal_client, p["id"], provider=provider, purpose=purpose)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "OFFICE_PROVIDER_IN_PERSONAL"
    r = _put(personal_client, p["id"], provider="NOTION", purpose="docs")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PROVIDER_DOMAIN_MISMATCH"


def test_office_confluence_parent_and_default_deny(admin_client: AuthedClient, secrets_memory):
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Office Docs")
    r = _put(admin_client, p["id"], provider="CONFLUENCE", purpose="docs", tenant_id="company.atlassian.net",
             root_id="123456789", credential_ref="office/atlassian")
    assert r.status_code == 200, r.text
    integ = r.json()["integration"]
    assert integ["root_id_name"] == "CONFLUENCE_PROJECTS_PARENT_ID"
    assert integ["capability_status"]["office_default_deny"] is True
    assert any("OFFICE default deny" in x["issue"] for x in integ["capability_status"]["remedies"])
    assert _put(admin_client, p["id"], provider="CONFLUENCE", purpose="docs", root_id="not-a-number").status_code == 422
    assert _put(admin_client, p["id"], provider="GOOGLE_DRIVE", purpose="docs").status_code == 403
    r = _put(admin_client, p["id"], provider="JIRA", purpose="issues", root_id="AEOS", credential_ref="personal/x")
    assert r.status_code == 422 and r.json()["error"]["code"] == "CREDENTIAL_NAMESPACE_MISMATCH"


def test_credential_values_are_refused(personal_client: AuthedClient):
    p = create_project(personal_client)
    token = "ghp_" + "Q" * 36
    for body in (
        {"credential_ref": token},
        {"credential_ref": f"personal/{token}"},
        {"root_id": "octo/demo", "config": {"api_token": "abc"}},
        {"root_id": "octo/demo", "config": {"note": f"use {token}"}},
        {"tenant_id": token},
    ):
        r = _put(personal_client, p["id"], provider="GITHUB_ISSUES", purpose="issues", **body)
        assert r.status_code == 422, body
        assert token not in r.text
    r = _put(personal_client, p["id"], provider="GITHUB_ISSUES", purpose="issues", credential_ref="../etc/passwd")
    assert r.status_code == 422


def test_purpose_must_match_provider(personal_client: AuthedClient):
    p = create_project(personal_client)
    r = _put(personal_client, p["id"], provider="GOOGLE_DRIVE", purpose="issues")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_PURPOSE"


def test_domain_field_and_unknown_fields_rejected(personal_client: AuthedClient):
    p = create_project(personal_client)
    r = _put(personal_client, p["id"], provider="GITHUB_ISSUES", purpose="issues", domain="OFFICE")
    assert r.status_code == 403 and r.json()["error"]["code"] == "DOMAIN_IMMUTABLE"
    r = _put(personal_client, p["id"], provider="GITHUB_ISSUES", purpose="issues", last_verified_at="2026-01-01")
    assert r.status_code == 422


def test_target_change_resets_verification(personal_client: AuthedClient, db, secrets_memory):
    from aeos.core.timeutil import utcnow
    from aeos.models.portfolio import ProjectIntegration

    p = create_project(personal_client)
    iid = _put(personal_client, p["id"], provider="GITHUB_ISSUES", purpose="issues", root_id="octo/demo",
               credential_ref="personal/github").json()["integration"]["id"]
    row = db.get(ProjectIntegration, iid)
    row.last_verified_at = utcnow()  # e.g. set by the doctor after a functional probe
    db.commit()
    assert personal_client.get(f"/api/projects/{p['id']}").json()["project"]["integration_state"]["issues"] == "VERIFIED"
    r = _put(personal_client, p["id"], provider="GITHUB_ISSUES", purpose="issues", root_id="octo/other",
             credential_ref="personal/github")
    assert r.json()["integration"]["last_verified_at"] is None
    assert personal_client.get(f"/api/projects/{p['id']}").json()["project"]["integration_state"]["issues"] != "VERIFIED"


def test_viewer_cannot_change_integrations(client: AuthedClient, db):
    from tests.conftest import VIEWER_PW, create_admin, create_viewer

    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.put("/api/projects/x/integrations", json={"provider": "GITHUB_ISSUES", "purpose": "issues"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"
