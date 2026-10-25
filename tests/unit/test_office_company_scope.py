"""The structured scope of the OFFICE company authorisation (§01 U-03 "OFFICE use authorised by the company: routes,
data classes, targets, Claude Max", §07 step 10 "tenant/scope/model/data/authority/isolasi terverifikasi", §04
"Credential OFFICE: identity yang disetujui kantor", §51 P1).

The owner records what the company approved - targets, data classes, optionally the identity of each office/
credential - on the company-authorisation decision (API, Control Center, CLI). Nothing is inferred: absent means
UNDECIDED and every consumer fails closed. The password-confirmed ``aeos policy office-route`` CLI is the controlled
path that makes a company-approved worker route OFFICE-eligible (§23)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.policy import office, office_scope
from aeos.routing import service as routing
from tests.conftest import ADMIN_PW, AuthedClient, create_admin
from tests.unit.test_portfolio_support import create_project

BASE = {"auth_scope": {"scope": "company authorisation CHG-9"}, "rationale": "otorisasi perusahaan tertulis"}
AUTH = {"kind": "POLICY", "office_authorization": True, "company_reference": "CHG-9 signed 2026-10",
        "allowed_providers": ["confluence", "jira", "bitbucket"]}


@pytest.fixture
def office_client(admin_client: AuthedClient) -> AuthedClient:
    admin_client.select_domain(Domain.OFFICE)
    return admin_client


def _decide(c: AuthedClient, code: int = 201, **body):
    r = c.post("/api/decisions", json={**BASE, **AUTH, **body})
    assert r.status_code == code, r.text
    return r.json()


# ---------------------------------------------------------------- recording + reading


def test_company_authorisation_records_targets_data_classes_and_identities(office_client, db):
    d = _decide(office_client,
                approved_targets=["confluence:tenant=https://ACME.atlassian.net/,space_id=98765",
                                  {"provider": "jira", "tenant": "acme.atlassian.net", "root_id": "eng"},
                                  "bitbucket:workspace=Acme,root_id=acme/app"],
                approved_data_classes=["internal", "PUBLIC"],
                approved_identities={"office/atlassian": "557058:abc", "bitbucket": "{bb-uuid}"})
    det = db.get(Decision, d["id"]).details
    assert det["approved_targets"] == [
        {"provider": "confluence", "tenant": "acme.atlassian.net", "space_id": "98765"},
        {"provider": "jira", "tenant": "acme.atlassian.net", "root_id": "ENG"},
        {"provider": "bitbucket", "workspace": "acme", "root_id": "acme/app"}]
    assert det["approved_data_classes"] == ["PUBLIC", "INTERNAL"]
    assert det["approved_identities"] == {"office/atlassian": "557058:abc", "office/bitbucket": "{bb-uuid}"}
    pol = office.office_policy(db)
    assert pol["approved_targets"] == det["approved_targets"] and pol["approved_data_classes"] == ["PUBLIC", "INTERNAL"]
    view = office_client.get("/api/policy/office").json()
    assert view["approved_identities"]["office/bitbucket"] == "{bb-uuid}"


def test_the_approved_targets_and_identities_stay_in_office_sessions(office_client, db):
    _decide(office_client, approved_targets=["jira:tenant=acme.atlassian.net"], approved_data_classes=["INTERNAL"],
            approved_identities={"office/atlassian": "owner@acme.example"})
    office_client.select_domain(Domain.PERSONAL)
    view = office_client.get("/api/policy/office").json()
    assert view["approved_targets"] == [] and view["approved_identities"] == {}
    assert view["approved_target_count"] == 1 and view["approved_identity_count"] == 1
    assert "acme.atlassian.net" not in str(view) and "owner@acme.example" not in str(view)


def test_without_an_authorisation_the_scope_is_empty_and_nothing_is_authorised(db):
    pol = office.office_policy(db)
    assert pol["approved_targets"] == [] and pol["approved_data_classes"] == [] and pol["approved_identities"] == {}
    assert office_scope.target_check(pol, "jira", root_id="ENG", tenant="acme.atlassian.net")[0] is False
    assert office_scope.data_class_check(pol, "PUBLIC")[0] is False


@pytest.mark.parametrize(("body", "code"), [
    ({"approved_targets": ["jira"]}, "APPROVED_TARGET_INVALID"),  # no provider:key=value
    ({"approved_targets": [{"provider": "jira"}]}, "APPROVED_TARGET_INVALID"),  # provider-only scope too broad
    ({"approved_targets": ["confluence:space_id=1"]}, "APPROVED_TARGET_INVALID"),  # no tenant
    ({"approved_targets": ["bitbucket:workspace=acme,root_id=other/app"]}, "APPROVED_TARGET_INVALID"),
    ({"approved_targets": ["jira:tenant=acme.atlassian.net,parent=1"]}, "APPROVED_TARGET_INVALID"),  # unknown key
    ({"approved_targets": ["google-drive:root_id=F1"]}, "APPROVED_TARGET_INVALID"),
    ({"allowed_providers": ["jira"], "approved_targets": ["bitbucket:workspace=acme"]},
     "APPROVED_TARGET_PROVIDER_NOT_ALLOWED"),
    ({"approved_data_classes": ["SECRET"]}, "APPROVED_DATA_CLASS_INVALID"),
    ({"approved_identities": {"personal/github": "me"}}, "APPROVED_IDENTITY_INVALID"),
    ({"approved_identities": {"office/bitbucket": "ghp_" + "a" * 36}}, "SECRET_IN_FIELD"),
])
def test_malformed_scope_is_refused(office_client, db, body, code):
    r = office_client.post("/api/decisions", json={**BASE, **AUTH, **body})
    assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    assert office.company_authorisation(db) is None


def test_scope_fields_belong_to_the_company_authorisation_only(office_client):
    r = office_client.post("/api/decisions", json={**BASE, "kind": "POLICY", "target_type": "repository",
                                                   "target_id": "o/r", "approved_data_classes": ["PUBLIC"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "OFFICE_SCOPE_WITHOUT_AUTHORISATION"
    smuggled = office_client.post("/api/decisions", json={
        **BASE, **AUTH, "details": {"approved_targets": [{"provider": "jira", "tenant": "x.atlassian.net"}]}})
    assert smuggled.status_code == 422 and smuggled.json()["error"]["code"] == "RESERVED_DETAIL_KEYS"


def test_decision_shapes_name_the_scope_vocabulary(office_client):
    shapes = office_client.get("/api/decisions/shapes").json()["office_authorization"]
    assert shapes["approved_target_keys"]["bitbucket"] == {"required": ["workspace"], "optional": ["root_id"]}
    assert shapes["data_classes"] == ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
    assert shapes["identity_refs"] == ["office/atlassian", "office/bitbucket"]


# ---------------------------------------------------------------- consumers


def test_office_routes_reject_data_classes_the_company_did_not_approve(office_client, db):
    """OFFICE route eligibility (and so scheduler admission) refuses a workload whose classification is not
    company-approved, even on a company-approved route."""
    route = ModelRoute(route_key="worker.office", pool="WORKER", provider="openrouter", model_id="v/m",
                       status="CANDIDATE", eligible_domains=["OFFICE"], allowed_data_classes=["INTERNAL",
                                                                                              "CONFIDENTIAL"])
    db.add(route)
    db.commit()
    _decide(office_client, approved_data_classes=["INTERNAL"])
    pol = office.office_policy(db)

    def class_reasons(cls: str) -> list[str]:
        return [r for r in routing.route_rejections(db, route, domain="OFFICE", data_classification=cls,
                                                    needs_tools=False, min_context=None, office=pol)
                if "data classification" in r and "company-approved" in r]

    assert class_reasons("INTERNAL") == []
    assert class_reasons("CONFIDENTIAL"), "CONFIDENTIAL was not approved by the company"
    personal = routing.route_rejections(db, route, domain="PERSONAL", data_classification="CONFIDENTIAL",
                                        needs_tools=False, min_context=None)
    assert not any("company-approved" in r for r in personal), "PERSONAL is never subject to the OFFICE scope"


def test_registering_an_office_target_outside_the_approved_list_is_refused(office_client, db):
    p = create_project(office_client, domain=Domain.OFFICE, name="Office Docs")
    conf = {"provider": "confluence", "purpose": "docs", "tenant_id": "acme.atlassian.net", "root_id": "123456",
            "credential_ref": "office/atlassian", "config": {"space_id": "98765", "deployment": "CLOUD"}}
    # before any authorisation: configuration only (nothing can use it - default deny)
    assert office_client.put(f"/api/projects/{p['id']}/integrations", json=conf).status_code == 200
    _decide(office_client, approved_targets=["confluence:tenant=acme.atlassian.net,space_id=11111"])
    r = office_client.put(f"/api/projects/{p['id']}/integrations", json=conf)
    assert r.status_code == 403 and r.json()["error"]["code"] == "OFFICE_TARGET_NOT_AUTHORISED"
    ok = office_client.put(f"/api/projects/{p['id']}/integrations",
                           json={**conf, "config": {**conf["config"], "space_id": "11111"}})
    assert ok.status_code == 200, ok.text


def test_confluence_and_jira_targets_outside_the_approved_list_are_blocked_before_any_office_call(office_client, db,
                                                                                              monkeypatch,
                                                                                              secrets_memory):
    """The Atlassian adapters (publication, capability probe, issue sync all resolve their target here) read or
    write only a company-approved tenant/space/target, even with CAN_USE_OFFICE_INTEGRATIONS READY."""
    import json

    from aeos.models.portfolio import Project, ProjectIntegration
    from aeos.publisher import targets as targets_mod

    monkeypatch.setattr(targets_mod, "office_gate", lambda session, project: (True, []))
    secrets_memory.put("office/atlassian", json.dumps({"email": "a@acme.example", "api_token": "atl-" + "t" * 24}),
                       actor="test")
    p = Project(project_key="office-docs", name="Office Docs", domain="OFFICE")
    db.add(p)
    db.flush()
    for provider, purpose, root in (("confluence", "docs", "123456"), ("jira", "issues", "ENG")):
        db.add(ProjectIntegration(project_id=p.id, domain="OFFICE", provider=provider, purpose=purpose, root_id=root,
                                  tenant_id="acme.atlassian.net",
                                  config={"enabled": True, "deployment": "CLOUD", "space_id": "98765"}))
    db.commit()
    _decide(office_client, approved_targets=["confluence:tenant=acme.atlassian.net,space_id=11111",
                                             "jira:tenant=other.atlassian.net"])
    for purpose in ("docs", "issues"):
        t = targets_mod.resolve_target(db, p, purpose)
        assert t.status == "BLOCKED" and any("disetujui perusahaan" in r for r in t.reasons), (purpose, t.reasons)
    _decide(office_client, approved_targets=["confluence:tenant=acme.atlassian.net,space_id=98765",
                                             "jira:tenant=acme.atlassian.net,root_id=ENG"])
    for purpose in ("docs", "issues"):
        assert targets_mod.resolve_target(db, p, purpose).status == "READY", purpose


# ---------------------------------------------------------------- CLI


def test_cli_decide_records_the_scope_and_office_policy_overrides(settings, db):
    from aeos.cli import app

    create_admin(db)
    res = CliRunner().invoke(app, [
        "policy", "decide", "--kind", "POLICY", "--as", "owner", "--domain", "OFFICE", "--office-authorization",
        "--company-reference", "CHG-9 signed", "--allowed-provider", "jira", "--allowed-provider", "bitbucket",
        "--approved-target", "jira:tenant=acme.atlassian.net,root_id=ENG",
        "--approved-target", "bitbucket:workspace=acme", "--approved-data-class", "INTERNAL",
        "--approved-identity", "office/bitbucket={bb-uuid}",
        "--office-policy", "atlassian_deployment=CLOUD", "--office-policy", "approved_worker_routes=worker.a,worker.b",
        "--office-policy", "claude_max_allowed_for_office_data=true",
        "--scope", "providers=jira,bitbucket", "--rationale", "otorisasi perusahaan tertulis"], input=ADMIN_PW + "\n")
    assert res.exit_code == 0, res.output
    assert res.output.isascii()
    db.expire_all()
    pol = office.office_policy(db)
    assert pol["approved_targets"] == [{"provider": "jira", "tenant": "acme.atlassian.net", "root_id": "ENG"},
                                       {"provider": "bitbucket", "workspace": "acme"}]
    assert pol["approved_data_classes"] == ["INTERNAL"]
    assert pol["approved_identities"] == {"office/bitbucket": "{bb-uuid}"}
    assert pol["atlassian_deployment"] == "CLOUD" and pol["approved_worker_routes"] == ["worker.a", "worker.b"]
    assert pol["claude_max_allowed_for_office_data"] is True


def test_cli_decide_refuses_an_unknown_office_policy_key(settings, db):
    from aeos.cli import app

    create_admin(db)
    res = CliRunner().invoke(app, [
        "policy", "decide", "--kind", "POLICY", "--as", "owner", "--domain", "OFFICE", "--office-authorization",
        "--company-reference", "CHG-9 signed", "--allowed-provider", "jira", "--office-policy", "allow_all=true",
        "--rationale", "otorisasi perusahaan tertulis"], input=ADMIN_PW + "\n")
    assert res.exit_code != 0 and office.company_authorisation(db) is None


def _route(db) -> ModelRoute:
    r = ModelRoute(route_key="worker.office-free", pool="WORKER", provider="openrouter", model_id="vendor/model:free",
                   status="CANDIDATE", eligible_domains=["PERSONAL"], is_free="YES", mode_params={})
    db.add(r)
    db.flush()
    r.config_hash = routing.compute_config_hash(r)
    db.commit()
    return r


def _office_route_cli(ref: str = "office/openrouter", password: str = ADMIN_PW):
    from aeos.cli import app

    return CliRunner().invoke(app, ["policy", "office-route", "worker.office-free", "--as", "owner",
                                    "--credential-ref", ref, "--rationale", "perusahaan menyetujui rute ini"],
                              input=password + "\n")


def test_cli_office_route_is_password_confirmed_and_needs_the_company_authorisation(settings, db):
    """W6-185: the controlled §23 path from the CLI - refused without a company authorisation, for a route that is
    not company-approved, for a personal/ credential and without the right password; success makes it OFFICE-only."""
    from aeos.policy import office_routes, recording

    owner = create_admin(db)
    _route(db)
    res = _office_route_cli()
    assert res.exit_code != 0 and "OFFICE_AUTHORISATION_REQUIRED" in res.output
    recording.record_generic_decision(db, actor_user_id=owner.id, domain=Domain.OFFICE, kind="POLICY",
                                      rationale="otorisasi perusahaan tertulis", auth_scope={"office": "auth"},
                                      office_authorization=True, company_reference="LETTER-2026-01",
                                      allowed_providers=["jira"],
                                      office_policy={"approved_worker_routes": ["worker.other"],
                                                     "external_worker_allowed": True})
    db.commit()
    res = _office_route_cli()
    assert res.exit_code != 0 and "ROUTE_NOT_COMPANY_APPROVED" in res.output
    recording.record_generic_decision(db, actor_user_id=owner.id, domain=Domain.OFFICE, kind="POLICY",
                                      rationale="rute disetujui perusahaan", auth_scope={"office": "routes"},
                                      office_policy={"approved_worker_routes": ["worker.office-free"]})
    db.commit()
    assert _office_route_cli(password="wrong").exit_code != 0
    res = _office_route_cli(ref="personal/openrouter")
    assert res.exit_code != 0 and "OFFICE_CREDENTIAL_REF_REQUIRED" in res.output
    assert db.scalar(select(Decision).where(Decision.target_type == "office_route")) is None
    res = _office_route_cli()
    assert res.exit_code == 0, res.output
    assert res.output.isascii() and "office_route:worker.office-free" in res.output
    db.expire_all()
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "worker.office-free"))
    assert route.eligible_domains == ["OFFICE"] and route.mode_params["credential_ref"] == "office/openrouter"
    assert office_routes.office_route_authorised(db, route) is not None
    d = db.scalar(select(Decision).where(Decision.target_type == "office_route"))
    assert d.actor_kind == "HUMAN" and d.actor_user_id == owner.id
