"""Provider-specific Integration Registry config, validated with the rule of the code that reads it (§04, §31):

* the observe-only deployment target (W6-099): ``root_id`` = https base URL, optional ``config.health_path``
  checked with the deployment observer's own path rule before it is stored;
* the per-project pipeline reference (W6-103/W6-011): ``config.gate_G1/G2/G3/G5`` on the CI integration
  (``github-actions`` PERSONAL, ``bitbucket-pipelines`` OFFICE), exact job/step names;
* the OFFICE Bitbucket code adapter switch (W6-003/W6-012): ``config.enabled`` without a deployment field.
"""

from __future__ import annotations

import pytest

from aeos.core.enums import Domain
from aeos.delivery import deployment
from aeos.delivery import facade as delivery
from aeos.delivery.provider import PIPELINE_GATES, pipeline_reference
from aeos.models.portfolio import Project, ProjectIntegration
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project


def _put(c: AuthedClient, pid: str, body: dict, code: int = 200) -> dict:
    r = c.put(f"/api/projects/{pid}/integrations", json=body)
    assert r.status_code == code, r.text
    return r.json()


@pytest.fixture
def personal(personal_client: AuthedClient) -> tuple[AuthedClient, str]:
    return personal_client, create_project(personal_client, name="Web App")["id"]


@pytest.fixture
def office_proj(admin_client: AuthedClient) -> tuple[AuthedClient, str]:
    admin_client.select_domain(Domain.OFFICE)
    return admin_client, create_project(admin_client, domain=Domain.OFFICE, name="Office Svc")["id"]


def test_deployment_target_with_health_path_is_registered_and_observed_at_that_path(personal, db):
    c, pid = personal
    _put(c, pid, {"provider": "deployment-target", "purpose": "staging", "root_id": "https://staging.example.org",
                  "config": {"health_path": "/api/health"}})
    target = delivery.deployment_target(db, db.get(Project, pid), "staging")
    assert target == {"integration_id": target["integration_id"], "base_url": "https://staging.example.org",
                      "health_path": "/api/health"}
    _put(c, pid, {"provider": "deployment-target", "purpose": "production", "root_id": "https://app.example.org",
                  "config": {"health_path": ""}})  # empty = the default /health
    assert delivery.deployment_target(db, db.get(Project, pid), "production")["health_path"] == "/health"


@pytest.mark.parametrize("path", ["health", "/a/../b", "/ok?x=1", "/" + "a" * 300, "/h ealth"])
def test_an_invalid_health_path_is_refused_with_the_observer_rule(personal, db, path):
    c, pid = personal
    r = c.put(f"/api/projects/{pid}/integrations", json={
        "provider": "deployment-target", "purpose": "staging", "root_id": "https://staging.example.org",
        "config": {"health_path": path}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "HEALTH_PATH_INVALID", r.text
    assert not deployment.health_path_ok(path)
    assert db.query(ProjectIntegration).filter_by(project_id=pid, provider="deployment-target").count() == 0


def test_the_checks_api_and_the_observer_apply_the_registry_health_path_rule(personal, db):
    """Wave-7 integration (A with B): the checks API/CLI, the observer and the screenshot collector use the same
    path rule as the Integration Registry - a '..' path refused at registration is never set or used another way."""
    from aeos.core.errors import ValidationFailed

    c, pid = personal
    integ_id = _put(c, pid, {"provider": "deployment-target", "purpose": "staging",
                             "root_id": "https://staging.example.org"})["integration"]["id"]
    bad = c.put(f"/api/delivery/targets/{integ_id}/checks", json={"health_path": "/a/../b"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "HEALTH_PATH_INVALID", bad.text
    smoke = c.put(f"/api/delivery/targets/{integ_id}/checks",
                  json={"smoke_checks": [{"name": "home", "path": "/app/../admin"}]})
    assert smoke.status_code == 422, smoke.text
    # a stored row that names such a path (e.g. written by an older build) is refused before any request is made
    row = db.get(ProjectIntegration, integ_id)
    row.config = {**(row.config or {}), "health_path": "/a/../b"}
    db.commit()
    with pytest.raises(ValidationFailed) as exc:
        delivery.deployment_target(db, db.get(Project, pid), "staging")
    assert exc.value.code == "HEALTH_PATH_INVALID"


def test_health_path_belongs_to_deployment_targets_only(personal):
    c, pid = personal
    r = c.put(f"/api/projects/{pid}/integrations", json={
        "provider": "github-issues", "purpose": "issues", "root_id": "owner/app", "config": {"health_path": "/h"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_CONFIG"


def test_deployment_target_root_must_be_an_https_base_url(personal):
    c, pid = personal
    for bad in ("http://staging.example.org", "https://user:pw@host.example", "staging.example.org"):
        r = c.put(f"/api/projects/{pid}/integrations", json={"provider": "deployment-target", "purpose": "staging",
                                                            "root_id": bad})
        assert r.status_code in (422, 403), r.text


def test_personal_pipeline_reference_is_stored_and_read_by_delivery(personal, db):
    c, pid = personal
    _put(c, pid, {"provider": "github-actions", "purpose": "ci",
                  "config": {"gate_G1": " lint ,  typecheck", "gate_G2": "backend (windows-latest)", "gate_G5": ""}})
    ref = pipeline_reference(db, db.get(Project, pid))
    assert ref == {"G1": ["lint", "typecheck"], "G2": ["backend (windows-latest)"]}
    assert delivery.pipeline_gates() == PIPELINE_GATES


@pytest.mark.parametrize(("body", "code"), [
    ({"provider": "github-actions", "purpose": "ci", "config": {"gate_G4": "x"}}, "INVALID_CONFIG"),
    ({"provider": "github-actions", "purpose": "ci", "config": {"gate_G1": 3}}, "INVALID_CONFIG"),
    ({"provider": "github-actions", "purpose": "ci", "config": {"gate_G1": " , "}}, "INVALID_CONFIG"),
    ({"provider": "github-issues", "purpose": "issues", "root_id": "owner/app", "config": {"gate_G1": "lint"}},
     "INVALID_CONFIG"),
])
def test_pipeline_reference_maps_only_known_gates_on_ci_integrations(personal, body, code):
    c, pid = personal
    r = c.put(f"/api/projects/{pid}/integrations", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text


def test_office_bitbucket_code_adapter_can_be_enabled_without_a_deployment_field(office_proj, db):
    c, pid = office_proj
    body = {"provider": "bitbucket", "purpose": "code", "root_id": "acme/app", "credential_ref": "office/bitbucket",
            "config": {"enabled": True}}
    view = _put(c, pid, body)["integration"]
    assert view["config"] == {"enabled": True} and view["root_id"] == "acme/app"
    r = c.put(f"/api/projects/{pid}/integrations", json={**body, "config": {"enabled": True, "deployment": "CLOUD"}})
    assert r.status_code == 422, "the Bitbucket deployment comes from office_policy.atlassian_deployment"
    pipes = _put(c, pid, {"provider": "bitbucket-pipelines", "purpose": "ci",
                          "config": {"gate_G1": "Lint", "gate_G3": "Integration tests"}})["integration"]
    assert pipes["config"] == {"gate_G1": "Lint", "gate_G3": "Integration tests"}
    assert pipeline_reference(db, db.get(Project, pid)) == {"G1": ["Lint"], "G3": ["Integration tests"]}


def test_switching_the_office_bitbucket_adapter_on_and_off_is_audited(office_proj, db):
    """Enabling OFFICE delivery is now a Control Center action: the audit trail names who switched the adapter on or
    off (before/after) and which config keys were stored - never their values beyond the registry row."""
    from sqlalchemy import select

    from aeos.models.audit import AuditEvent

    c, pid = office_proj
    body = {"provider": "bitbucket", "purpose": "code", "root_id": "acme/app", "credential_ref": "office/bitbucket"}
    _put(c, pid, {**body, "config": {"enabled": False}})
    _put(c, pid, {**body, "config": {"enabled": True}})
    _put(c, pid, {**body, "config": {"enabled": False}})
    rows = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "portfolio.integration.update",
                                                    AuditEvent.project_id == pid).order_by(AuditEvent.seq)))
    assert [r.details["adapter_enabled"] for r in rows] == [{"before": False, "after": False},
                                                            {"before": False, "after": True},
                                                            {"before": True, "after": False}]
    assert all(r.details["config_keys"] == ["enabled"] and r.actor_type == "user" for r in rows)
