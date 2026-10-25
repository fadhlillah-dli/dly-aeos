"""§08 project adapters, OFFICE side: once the company authorisation gate is open, the Doctor proves the exact
Jira project key, Confluence space + parent folder (the publisher's own capability rule) and Bitbucket repository
with read-only GETs and the stored OFFICE credential. Behind the closed gate nothing is probed (OFFICE default
deny, §23); a target outside the company's approved targets FAILS without a probe (§01 U-03, §07 step 10). The
OFFICE credential identity is compared with the company-approved identity (secret.office_identity). No network:
every request goes through the fake runner."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.checks import adapters as adapters_mod
from aeos.doctor.runner import make_context, run_check
from aeos.inventory.probes import HttpResult
from aeos.models.identity import User
from tests.conftest import create_admin
from tests.unit.test_inventory_fakes import base_runner

TENANT = "corp.atlassian.net"
JIRA_URL = f"https://{TENANT}/rest/api/3/project/CORP"
SPACE_URL = f"https://{TENANT}/wiki/api/v2/spaces/777"
FOLDER_URL = f"https://{TENANT}/wiki/api/v2/folders/12345"
PAGE_URL = f"https://{TENANT}/wiki/api/v2/pages/12345"
REPO_URL = "https://api.bitbucket.org/2.0/repositories/corp/app"
MYSELF_URL = f"https://{TENANT}/rest/api/3/myself"
BB_USER_URL = "https://api.bitbucket.org/2.0/user"
TOKEN = "ATATT3x" + "k" * 30
BB_ACCESS = "bbat-" + "q" * 40
APPROVED = [f"jira:tenant={TENANT},root_id=CORP", f"confluence:tenant={TENANT},space_id=777",
            "bitbucket:workspace=corp"]


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


def _run(settings, db, runner, *, functional=True, check="adapter.project_office_targets"):
    load_all()
    ctx = make_context(settings, db, runner=runner, functional=functional)
    if check == "adapter.project_office_targets":
        (target,) = adapters_mod.project_office_targets(ctx)
        ctx.project = target
    return run_check(REGISTRY[check], ctx)


def _authorise(db, *, targets=tuple(APPROVED), identities=()):
    """A real, human company-authorisation decision (opens CAN_USE_OFFICE_INTEGRATIONS through the policy module)."""
    from aeos.policy import recording

    owner = db.scalar(select(User).where(User.username == "owner")) or create_admin(db)
    d = recording.record_generic_decision(
        db, actor_user_id=owner.id, domain=Domain.OFFICE, kind="POLICY", rationale="surat otorisasi perusahaan",
        auth_scope={"providers": "jira,confluence,bitbucket"}, office_authorization=True,
        company_reference="CHG-2026-77 signed", allowed_providers=["jira", "confluence", "bitbucket"],
        approved_targets=list(targets), approved_identities=list(identities))
    db.commit()
    return d


def _credentials(secrets_memory, *, bitbucket=None):
    secrets_memory.put("office/atlassian", json.dumps({"email": "me@corp.example", "api_token": TOKEN}), actor="t")
    secrets_memory.put("office/bitbucket", json.dumps(bitbucket or {"username": "me", "app_password": "bb-" + "p" * 20}),
                       actor="t")


def _healthy(runner):
    runner.http[JIRA_URL] = HttpResult(200, {"id": "10001", "key": "CORP", "name": "Corp"})
    runner.http[SPACE_URL] = HttpResult(200, {"id": "777", "key": "ENG"})
    runner.http[FOLDER_URL] = HttpResult(200, {"id": "12345", "spaceId": "777"})
    runner.http[REPO_URL] = HttpResult(200, {"full_name": "corp/app", "uuid": "{u}", "is_private": True})


def test_closed_gate_probes_nothing(settings, db, office_project, secrets_memory):
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r)
    assert res.status == "UNKNOWN" and res.error_code == "OFFICE_DEFAULT_DENY"
    assert not r.http_calls, "behind the closed OFFICE gate no request is sent"


def test_open_gate_proves_each_exact_target_read_only(settings, db, office_project, secrets_memory):
    _authorise(db)
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    assert _run(settings, db, r, functional=False).status == "CONFIGURED" and not r.http_calls
    res = _run(settings, db, r)
    assert res.status == "CONNECTED", res.observed_evidence
    by = {x["provider"]: x for x in res.observed_evidence["results"]}
    assert by["jira"]["project_id"] == "10001"
    assert by["confluence"]["parent_kind"] == "folder" and by["confluence"]["folders"] == "SUPPORTED"
    assert by["bitbucket"]["status"] == "CONNECTED"
    assert {c["url"] for c in r.http_calls} == {JIRA_URL, SPACE_URL, FOLDER_URL, REPO_URL}
    assert all(c["allow_remote"] and c["headers"]["Authorization"].startswith("Basic ") for c in r.http_calls)
    assert TOKEN not in json.dumps(res.as_dict()), "the credential never reaches the result"
    assert res.scope["domain"] == "OFFICE" and res.scope["project_id"] == office_project.id


def test_bitbucket_access_token_is_sent_as_bearer_like_the_broker_and_publisher(settings, db, office_project,
                                                                                 secrets_memory):
    """W6-023: the first credential shape the docs recommend ({"access_token"}) is accepted by the broker and the
    publisher; the Doctor builds the same header (Bearer) instead of reporting OFFICE_CREDENTIAL_MISSING."""
    _authorise(db)
    _credentials(secrets_memory, bitbucket={"access_token": BB_ACCESS})
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r)
    assert res.status == "CONNECTED", res.observed_evidence
    bb = next(c for c in r.http_calls if c["url"] == REPO_URL)
    assert bb["headers"]["Authorization"] == f"Bearer {BB_ACCESS}"
    assert BB_ACCESS not in json.dumps(res.as_dict())


@pytest.mark.parametrize(("url", "result", "status", "error"), [
    (JIRA_URL, HttpResult(200, {"key": "OTHER"}), "FAILED", "JIRA_PROJECT_MISMATCH"),
    (JIRA_URL, HttpResult(401, {}), "EXPIRED", "OFFICE_CREDENTIAL_REJECTED"),
    (FOLDER_URL, HttpResult(200, {"id": "12345", "spaceId": "999"}), "FAILED", "CONFLUENCE_PARENT_OTHER_SPACE"),
    (SPACE_URL, HttpResult(404, {}), "FAILED", "CONFLUENCE_SPACE_NOT_VISIBLE"),
    (SPACE_URL, HttpResult(401, {}), "EXPIRED", "OFFICE_CREDENTIAL_REJECTED"),
    (REPO_URL, HttpResult(404, {}), "FAILED", "BITBUCKET_NOT_VISIBLE"),
    (REPO_URL, HttpResult(None, error="TIMEOUT"), "UNKNOWN", "TIMEOUT"),
])
def test_a_wrong_or_unreachable_target_is_never_green(settings, db, office_project, secrets_memory,
                                                       url, result, status, error):
    _authorise(db)
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    r.http[url] = result
    res = _run(settings, db, r)
    assert res.status == status
    assert error in {x.get("error") for x in res.observed_evidence["results"]}


@pytest.mark.parametrize(("folder", "page", "folders", "kind"), [
    (HttpResult(404, {}), HttpResult(200, {"id": "12345", "spaceId": "777"}), "UNKNOWN", "page"),  # a page parent
    (HttpResult(400, {}), HttpResult(200, {"id": "12345", "spaceId": "777"}), "NOT_SUPPORTED", "page"),
    (HttpResult(501, {}), HttpResult(404, {}), "NOT_SUPPORTED", None),  # tenant without folder support
])
def test_confluence_follows_the_publisher_capability_rule_needs_action_not_connected(
        settings, db, office_project, secrets_memory, folder, page, folders, kind):
    """W6-210 / §50 T1 #1.19: a page parent or a tenant without folder support is 'needs action' (the publisher
    refuses it with CONFLUENCE_FOLDERS_UNSUPPORTED), never CONNECTED; folder support is stated in the evidence."""
    _authorise(db)
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    r.http[FOLDER_URL] = folder
    r.http[PAGE_URL] = page
    res = _run(settings, db, r)
    assert res.status == "FAILED" and res.error_code == "OFFICE_TARGET_NOT_VERIFIED"
    conf = next(x for x in res.observed_evidence["results"] if x["provider"] == "confluence")
    assert conf["folders"] == folders and conf["parent_kind"] == kind
    assert conf["error"] == "CONFLUENCE_FOLDERS_UNSUPPORTED" and conf["needs_action"] is True
    if kind == "page":
        assert any("needs action" in reason for reason in conf["capability_reasons"])


def test_doctor_and_publisher_agree_on_the_same_confluence_answers(settings, db, office_project, secrets_memory):
    """One rule (publisher.providers.atlassian.confluence_capabilities) for both: READY <-> CONNECTED."""
    from aeos.publisher.providers.atlassian import confluence_capabilities

    answers = {"/spaces/777": (200, {"id": "777"}), "/folders/12345": (400, {}), "/pages/12345": (200, {"id": "1"})}
    caps = confluence_capabilities(lambda path, _w: answers[path], space_id="777", parent_id="12345",
                                   deployment="CLOUD")
    assert caps["status"] != "READY" and caps["folders"] == "NOT_SUPPORTED" and caps["parent_kind"] == "page"
    _authorise(db)
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    r.http[FOLDER_URL] = HttpResult(400, {})
    r.http[PAGE_URL] = HttpResult(200, {"id": "1"})
    assert _run(settings, db, r).status != "CONNECTED"


def test_missing_credentials_are_missing_and_nothing_is_sent(settings, db, office_project, secrets_memory):
    _authorise(db)
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r)
    assert res.status == "MISSING" and res.error_code == "OFFICE_CREDENTIAL_MISSING" and not r.http_calls


def test_a_target_outside_the_company_approved_targets_fails_without_a_probe(settings, db, office_project,
                                                                            secrets_memory):
    """W6-165: the configured tenant/target must be one the company approved; otherwise FAILED, nothing probed."""
    _authorise(db, targets=[f"jira:tenant={TENANT},root_id=OTHER", f"confluence:tenant={TENANT},space_id=777",
                            "bitbucket:workspace=corp"])
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    for functional in (False, True):
        res = _run(settings, db, r, functional=functional)
        assert res.status == "FAILED" and res.error_code == "OFFICE_TARGET_NOT_AUTHORISED"
        outside = res.observed_evidence["company_approved_targets"]["outside"]
        assert [o["provider"] for o in outside] == ["jira"]
    assert not r.http_calls, "an unapproved target is never probed with the OFFICE credential"


def test_no_approved_targets_is_undecided_and_fails_closed(settings, db, office_project, secrets_memory):
    _authorise(db, targets=())
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r, functional=False)
    assert res.status == "FAILED" and res.error_code == "OFFICE_TARGET_NOT_AUTHORISED"
    assert "UNDECIDED" in json.dumps(res.observed_evidence) and not r.http_calls


def test_unreadable_company_scope_is_unknown_not_outside_and_probes_nothing(settings, db, office_project,
                                                                           secrets_memory, monkeypatch):
    """If the approved targets cannot be read, the check is UNKNOWN (never green, never a claimed mismatch)."""
    from aeos.doctor import office as doctor_office
    from aeos.policy import facade as policy

    def unavailable(_session):
        raise ImportError("policy module unavailable")

    monkeypatch.setattr(doctor_office, "office_gate", lambda capability, session, root=None: (True, "test: open"))
    monkeypatch.setattr(policy, "office_policy", unavailable)
    _credentials(secrets_memory)
    r = base_runner()
    _healthy(r)
    res = _run(settings, db, r)
    assert res.status == "UNKNOWN" and res.error_code == "POLICY_UNAVAILABLE"
    assert not r.http_calls


def test_data_center_tenant_is_not_supported(settings, db, office_project, secrets_memory):
    from aeos.models.portfolio import ProjectIntegration

    _authorise(db)
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


# ---------------------------------------------------------------- secret.office_identity (W6-151)


def test_identity_check_is_not_applicable_without_a_company_authorisation(settings, db, secrets_memory):
    _credentials(secrets_memory)
    r = base_runner()
    res = _run(settings, db, r, check="secret.office_identity")
    assert res.status == "NOT_APPLICABLE" and not r.http_calls


def test_identity_check_is_unknown_while_no_identity_was_approved(settings, db, secrets_memory):
    _authorise(db)
    _credentials(secrets_memory)
    r = base_runner()
    res = _run(settings, db, r, check="secret.office_identity")
    assert res.status == "UNKNOWN" and res.error_code == "OFFICE_IDENTITY_UNDECIDED" and not r.http_calls


def test_identity_matches_the_company_approved_account(settings, db, secrets_memory):
    _authorise(db, identities=("office/atlassian=me@corp.example", "office/bitbucket={bb-uuid-1}"))
    _credentials(secrets_memory, bitbucket={"access_token": BB_ACCESS})
    r = base_runner()
    r.http[MYSELF_URL] = HttpResult(200, {"accountId": "557058:abc", "emailAddress": "Me@Corp.example"})
    r.http[BB_USER_URL] = HttpResult(200, {"uuid": "{bb-uuid-1}", "nickname": "me"})
    assert _run(settings, db, r, functional=False, check="secret.office_identity").status == "CONFIGURED"
    assert not r.http_calls
    res = _run(settings, db, r, check="secret.office_identity")
    assert res.status == "VERIFIED", res.observed_evidence
    assert {c["url"] for c in r.http_calls} == {MYSELF_URL, BB_USER_URL}
    blob = json.dumps(res.as_dict())
    assert TOKEN not in blob and BB_ACCESS not in blob and "me@corp.example" not in blob.lower()


def test_a_credential_acting_as_another_account_fails(settings, db, secrets_memory):
    _authorise(db, identities=("office/atlassian=557058:approved",))
    _credentials(secrets_memory)
    r = base_runner()
    r.http[MYSELF_URL] = HttpResult(200, {"accountId": "557058:someone-else", "emailAddress": "x@corp.example"})
    res = _run(settings, db, r, check="secret.office_identity")
    assert res.status == "FAILED" and res.error_code == "OFFICE_IDENTITY_MISMATCH"
    r.http[MYSELF_URL] = HttpResult(401, {})
    assert _run(settings, db, r, check="secret.office_identity").status == "EXPIRED"
    r.http[MYSELF_URL] = HttpResult(None, error="TIMEOUT")
    assert _run(settings, db, r, check="secret.office_identity").status == "UNKNOWN"


def test_the_identity_check_guards_office_integrations():
    """A FAILED identity (another account) blocks CAN_USE_OFFICE_INTEGRATIONS; undecided/unprobed is a note."""
    from aeos.core.enums import Capability
    from aeos.doctor import capabilities

    assert "secret.office_identity" in capabilities.RULES[Capability.CAN_USE_OFFICE_INTEGRATIONS].guard
