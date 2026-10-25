"""Observe-only deployment targets beyond one health GET (master §31 G6/G8, §33, §40, §50 1.35; §08 gates).

* W6-036/W6-091: the platform's own /api/health shape proves a commit only while the controller runs the ACTIVE
  clean release; a health_path that is not an absolute path is refused by the Integration Registry and by the
  one checked target the observer and the screenshot collector share.
* W6-193/W6-194: owner smoke / critical-flow GET checks (each a SMOKE_TEST evidence) and product SLI checks
  (owner thresholds; none = NO_TARGET) decide the observation; G6 NOT_APPLICABLE only with an owner "no staging"
  decision; a local-target is reported as not observable.
* W6-173: a staging observation runs only while CAN_VALIDATE_STAGING is READY; a refusal is audited.
* W6-202/W6-213/W6-136: a FAIL production observation raises the post_release_failure alert, opens/links an
  incident with a HUMAN rollback recommendation and files one decision request. AEOS never deploys or rolls back.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.errors import Blocked, ValidationFailed
from aeos.delivery import deployment, facade
from aeos.models.audit import AuditEvent
from aeos.models.evidence import EvidenceArtifact
from aeos.models.operations_ext import OpsAlert, OpsIncident
from aeos.models.policy_ext import DecisionRequest
from aeos.models.portfolio import Project
from tests.unit.test_deployment_support import (
    HEALTHY_RELEASE,
    authorise_staging,
    deploy_target,
    environment_decision,
    make_staging_ready,
)

SHA = "c0ffee" + "1" * 34
STAGING = "https://staging.example.org"
PROD = "https://app.example.org"


def _project(db, key: str = "shop") -> Project:
    p = Project(project_key=key, name=key.title(), domain="PERSONAL")
    db.add(p)
    db.commit()
    return p


def _ok(commit: str = SHA, **extra):
    return httpx.Response(200, json={"status": "ok", "commit": commit, **extra})


# ----------------------------------------------------------------------------------------- platform health shape


@pytest.mark.parametrize(("controller", "expected"), [
    ({**HEALTHY_RELEASE, "commit": SHA}, "PASS"),
    ({**HEALTHY_RELEASE, "commit": SHA, "dirty": True}, "NOT_PROVEN"),
    ({**HEALTHY_RELEASE, "commit": SHA, "matches_active": False, "status": "NOT_ACTIVE_RELEASE"}, "NOT_PROVEN"),
    ({"mode": "checkout", "commit": SHA, "dirty": False, "matches_active": None, "status": "NO_ACTIVE_RELEASE"},
     "NOT_PROVEN"),
    ({**HEALTHY_RELEASE, "commit": None}, "NOT_PROVEN"),
    ({**HEALTHY_RELEASE, "commit": "f" * 40}, "NOT_PROVEN"),
])
def test_the_platform_health_shape_is_attributable_only_for_the_active_clean_release(controller, expected):
    body = {"status": "ok", "version": "0.1.0", "schema_version": "0005", "environment": "local",
            "controller": controller}
    status, reason, _reported = deployment.classify(SHA, 200, body)
    assert status == expected, reason
    unhealthy = deployment.classify(SHA, 503, {**body, "status": "down"})[0]
    assert unhealthy == ("FAIL" if expected == "PASS" else "NOT_PROVEN")


def test_the_real_api_health_endpoint_is_classified_by_the_same_rule(client):
    """The shape the observer reads is the one /api/health serves (a checkout here: never a deployment identity)."""
    body = client.get("/api/health").json()
    assert "controller" in body and "commit" not in body
    commit = body["controller"].get("commit") or SHA
    status, _reason, _r = deployment.classify(commit, 200, body)
    assert status == ("PASS" if body["controller"].get("status") == "ACTIVE_RELEASE" else "NOT_PROVEN")


# ----------------------------------------------------------------------------------------- one checked target


@pytest.mark.parametrize("bad", ["@evil.example/health", "//evil.example/health", "health", "/h?x=1", "/h#f"])
def test_the_integration_registry_refuses_an_off_host_health_path(personal_client, bad):
    from tests.unit.test_portfolio_support import create_project

    p = create_project(personal_client, name="Health Path")
    r = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "staging", "root_id": STAGING, "config": {"health_path": bad}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "HEALTH_PATH_INVALID", r.text
    ok = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "staging", "root_id": STAGING,
        "config": {"health_path": "/api/health"}})
    assert ok.status_code == 200, ok.text


def test_a_stored_bad_health_path_is_never_used_by_the_observer_or_the_collector(db):
    p = _project(db)
    deploy_target(db, p, "production", PROD, {"health_path": "@evil.example/health"})
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        with pytest.raises(ValidationFailed) as exc:
            deployment.observe(db, p.id, "production", SHA, actor="owner")
        assert not mock.calls
    assert exc.value.code == "HEALTH_PATH_INVALID"
    with pytest.raises(ValidationFailed):
        facade.deployment_target(db, p, "production")
    from aeos.docs_engine import service

    assert service.deployment_environments(db, p) == ["production"], "registered, but never observed/captured"


def test_facade_and_module_name_the_same_config_keys():
    assert facade.DEPLOYMENT_TARGET_CONFIG_KEYS == deployment.CONFIG_KEYS
    assert facade.DEPLOYMENT_TARGET_CHECK_KEYS == deployment.CHECK_CONFIG_KEYS


# ----------------------------------------------------------------------------------------- smoke / critical flows


CHECKS = {"smoke_checks": [
    {"name": "home", "path": "/", "expect_status": 200},
    {"name": "items-api", "path": "/api/items", "json_keys": ["items", "meta.total"]},
    {"name": "checkout", "path": "/api/checkout/health", "kind": "critical_flow"},
]}


def _observe(db, p, env, routes: dict[str, httpx.Response], sha: str = SHA, base: str = STAGING):
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        for path, resp in routes.items():
            mock.get(base + path).mock(return_value=resp)
        out = deployment.observe(db, p.id, env, sha, actor="owner")
        assert all(c.request.method == "GET" for c in mock.calls)
        called = [str(c.request.url) for c in mock.calls]
    db.commit()
    return out, called


def test_every_configured_check_is_required_and_recorded_as_smoke_test_evidence(db):
    p = _project(db)
    deploy_target(db, p, "staging", STAGING, CHECKS)
    make_staging_ready(db, p)
    authorise_staging(db, p, STAGING)
    routes = {"/health": _ok(), "/": httpx.Response(200, text="<html>ok</html>"),
              "/api/items": httpx.Response(200, json={"items": [], "meta": {"total": 0}}),
              "/api/checkout/health": httpx.Response(200, json={})}
    out, _called = _observe(db, p, "staging", routes)
    assert out["status"] == "PASS", out["reason"]
    smoke = list(db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "SMOKE_TEST")))
    assert len(smoke) == 3 and all(e.trusted and e.evidence_level == "E3" for e in smoke)
    assert {e.environment["check_name"] for e in smoke} == {"home", "items-api", "checkout"}
    assert all(e.environment["deployment_environment"] == "staging" and e.subject_id == SHA for e in smoke)
    assert {c["evidence_id"] for c in out["checks"]} == {e.id for e in smoke}
    from aeos.delivery import gates

    ev, status = gates.runtime_observation(db, p, [SHA], "staging")
    assert status == "PASS" and ev.id == out["evidence_id"]
    # a missing JSON key fails the observation (FAIL: the target names the commit)
    routes["/api/items"] = httpx.Response(200, json={"items": []})
    out, _ = _observe(db, p, "staging", routes)
    assert out["status"] == "FAIL" and out["failure_conditions"] == ["smoke_fail"]
    assert "meta.total" in next(c for c in out["checks"] if c["name"] == "items-api")["reason"]
    # a failing critical flow is its own failure condition
    routes["/api/items"] = httpx.Response(200, json={"items": [], "meta": {"total": 1}})
    routes["/api/checkout/health"] = httpx.Response(500, json={})
    out, _ = _observe(db, p, "staging", routes)
    assert out["status"] == "FAIL" and out["failure_conditions"] == ["critical_flow_fail"]
    # an unreachable check is not a pass and not a failure of the app: NOT_PROVEN
    routes["/api/checkout/health"] = httpx.ConnectError("refused")  # type: ignore[assignment]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        for path, resp in routes.items():
            if isinstance(resp, Exception):
                mock.get(STAGING + path).mock(side_effect=resp)
            else:
                mock.get(STAGING + path).mock(return_value=resp)
        out = deployment.observe(db, p.id, "staging", SHA, actor="owner")
    db.commit()
    assert out["status"] == "NOT_PROVEN"


def test_checks_are_skipped_when_the_commit_is_not_proven(db):
    p = _project(db)
    deploy_target(db, p, "staging", STAGING, CHECKS)
    make_staging_ready(db, p)
    out, called = _observe(db, p, "staging", {"/health": _ok(commit="f" * 40)})
    assert out["status"] == "NOT_PROVEN" and "skipped" in out["reason"]
    assert called == [f"{STAGING}/health"], "no check runs against another build"
    assert db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "SMOKE_TEST")).first() is None


def test_changing_the_checks_makes_an_earlier_observation_stale(db):
    from aeos.delivery import gates

    p = _project(db)
    target = deploy_target(db, p, "staging", STAGING, {})
    make_staging_ready(db, p)
    out, _ = _observe(db, p, "staging", {"/health": _ok()})
    assert out["status"] == "PASS"
    assert gates.runtime_observation(db, p, [SHA], "staging")[1] == "PASS"
    deployment.set_target_checks(db, target, {"smoke_checks": [{"path": "/login"}]}, actor="owner")
    db.commit()
    assert gates.runtime_observation(db, p, [SHA], "staging")[1] == "STALE"


@pytest.mark.parametrize(("config", "code"), [
    ({"smoke_checks": [{"path": "@evil/x"}]}, "SMOKE_CHECKS_INVALID"),
    ({"smoke_checks": [{"path": "/x", "expect_status": 99}]}, "SMOKE_CHECKS_INVALID"),
    ({"smoke_checks": [{"path": "/x", "kind": "deploy"}]}, "SMOKE_CHECKS_INVALID"),
    ({"smoke_checks": [{"path": "/x", "url": "https://other"}]}, "SMOKE_CHECKS_INVALID"),
    ({"smoke_checks": [{"path": "/x"}, {"path": "/x"}]}, "SMOKE_CHECKS_INVALID"),
    ({"sli_checks": [{"path": "/m", "json_key": "rate", "max": "high"}]}, "SLI_CHECKS_INVALID"),
    ({"sli_checks": [{"path": "/m", "json_key": "rate", "min": 2, "max": 1}]}, "SLI_CHECKS_INVALID"),
    ({"rollback_on": ["always"]}, "ROLLBACK_CRITERIA_INVALID"),
    ({"require_artifact_digest": "yes"}, "INVALID_CONFIG"),
])
def test_invalid_check_configuration_is_refused(config, code):
    with pytest.raises(ValidationFailed) as exc:
        deployment.validate_target_config(config)
    assert exc.value.code == code


def test_checks_of_a_read_only_project_cannot_change(db):
    from aeos.core.errors import Conflict

    p = _project(db, "archived")
    target = deploy_target(db, p, "staging", STAGING)
    p.lifecycle = "ARCHIVED"
    db.commit()
    with pytest.raises(Conflict) as exc:
        deployment.set_target_checks(db, target, {"rollback_on": ["health_fail"]}, actor="owner")
    assert exc.value.code == "PROJECT_READ_ONLY"


def test_the_generic_integration_form_keeps_the_owner_checks(personal_client, db):
    from tests.unit.test_portfolio_support import create_project

    p = create_project(personal_client, name="Keep Checks")
    r = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "production", "root_id": PROD})
    assert r.status_code == 200, r.text
    integ_id = r.json()["integration"]["id"]
    body = {"smoke_checks": [{"name": "home", "path": "/"}], "rollback_on": ["health_fail", "critical_flow_fail"],
            "sli_checks": [{"name": "errors", "path": "/metrics", "json_key": "error_rate", "max": 0.05}]}
    put = personal_client.put(f"/api/delivery/targets/{integ_id}/checks", json=body)
    assert put.status_code == 200, put.text
    assert put.json()["settings"]["rollback_on"] == ["health_fail", "critical_flow_fail"]
    # a later generic save (root_id / health_path only, as the Integrasi form sends) keeps them
    again = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "production", "root_id": PROD,
        "config": {"health_path": "/api/health"}})
    assert again.status_code == 200, again.text
    listed = personal_client.get(f"/api/delivery/targets?project_id={p['id']}").json()
    item = listed["items"][0]
    assert item["settings"]["health_path"] == "/api/health" and item["settings"]["smoke_checks"][0]["name"] == "home"
    assert item["settings"]["sli_checks"][0]["max"] == 0.05 and listed["source"] and listed["generated_at"]
    assert db.scalars(select(AuditEvent).where(AuditEvent.action == "delivery.target.checks")).first() is not None


def test_the_health_path_can_be_set_without_the_integration_form(personal_client, db, settings):
    """W6-036/W6-091 (c): the owner can point the observer at e.g. DLY AEOS' own /api/health through the checks API
    or the CLI (both OSes) - validated by the same absolute-path rule; a generic save that does not name it keeps
    it, an explicit empty value goes back to /health."""
    from typer.testing import CliRunner

    from aeos.cli import app
    from aeos.models.portfolio import ProjectIntegration
    from tests.unit.test_portfolio_support import create_project

    p = create_project(personal_client, name="Health Api")
    r = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "production", "root_id": "http://127.0.0.1:8787"})
    integ_id = r.json()["integration"]["id"]
    bad = personal_client.put(f"/api/delivery/targets/{integ_id}/checks", json={"health_path": "@evil.example/h"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "HEALTH_PATH_INVALID", bad.text
    typo = personal_client.put(f"/api/delivery/targets/{integ_id}/checks", json={"smoke_check": [{"path": "/"}]})
    assert typo.status_code == 422, "an unknown key is refused, never silently ignored"
    ok = personal_client.put(f"/api/delivery/targets/{integ_id}/checks", json={"health_path": "/api/health"})
    assert ok.status_code == 200 and ok.json()["settings"]["health_path"] == "/api/health", ok.text
    # the Integrasi form re-saves without config: the health path stays
    assert personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "production", "root_id": "http://127.0.0.1:8787"}).status_code == 200
    db.expire_all()
    assert db.get(ProjectIntegration, integ_id).config["health_path"] == "/api/health"
    # an explicit empty value in the form clears it (default /health)
    assert personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "production", "root_id": "http://127.0.0.1:8787",
        "config": {"health_path": ""}}).status_code == 200
    db.expire_all()
    assert "health_path" not in db.get(ProjectIntegration, integ_id).config
    runner = CliRunner()
    out = runner.invoke(app, ["delivery", "target-checks", p["id"], "--environment", "production",
                              "--health-path", "/api/health"])
    assert out.exit_code == 0 and "http://127.0.0.1:8787/api/health" in out.output and out.output.isascii(), out.output
    refused = runner.invoke(app, ["delivery", "target-checks", p["id"], "--environment", "production",
                                  "--health-path", "//evil.example/h"])
    assert refused.exit_code == 2 and "HEALTH_PATH_INVALID" in refused.output
    db.expire_all()
    assert db.get(ProjectIntegration, integ_id).config["health_path"] == "/api/health"
    cleared = personal_client.put(f"/api/delivery/targets/{integ_id}/checks", json={"health_path": ""})
    assert cleared.status_code == 200 and cleared.json()["settings"]["health_path"] == "/health", cleared.text


def test_viewers_cannot_change_target_checks(personal_client, db):
    from aeos.core.enums import Domain
    from tests.conftest import VIEWER_PW, create_viewer
    from tests.unit.test_portfolio_support import create_project

    p = create_project(personal_client, name="Viewer Checks")
    integ = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "staging", "root_id": STAGING}).json()["integration"]
    create_viewer(db)
    personal_client.login("viewer", VIEWER_PW)
    personal_client.select_domain(Domain.PERSONAL)
    r = personal_client.put(f"/api/delivery/targets/{integ['id']}/checks", json={"rollback_on": ["health_fail"]})
    assert r.status_code == 403


# ----------------------------------------------------------------------------------------- product SLIs


def test_product_slis_use_owner_thresholds_only(db):
    p = _project(db)
    deploy_target(db, p, "production", PROD, {"sli_checks": [
        {"name": "error-rate", "path": "/metrics", "json_key": "errors.rate"},
        {"name": "p95", "path": "/metrics/latency", "json_key": "p95_ms", "max": 800}]})
    routes = {"/health": _ok(), "/metrics": httpx.Response(200, json={"errors": {"rate": 0.9}}),
              "/metrics/latency": httpx.Response(200, json={"p95_ms": 420})}
    out, _ = _observe(db, p, "production", routes, base=PROD)
    by = {s["name"]: s for s in out["slis"]}
    # no owner threshold: reported, never "within target", and it does not decide the observation
    assert by["error-rate"]["status"] == "NO_TARGET" and by["error-rate"]["value"] == 0.9
    assert by["p95"]["status"] == "WITHIN_TARGET" and out["status"] == "PASS"
    routes["/metrics/latency"] = httpx.Response(200, json={"p95_ms": 1200})
    out, _ = _observe(db, p, "production", routes, base=PROD)
    assert out["status"] == "FAIL" and out["failure_conditions"] == ["sli_breach"]
    routes["/metrics/latency"] = httpx.Response(200, json={"p95_ms": "fast"})
    out, _ = _observe(db, p, "production", routes, base=PROD)
    assert out["status"] == "NOT_PROVEN" and {s["name"]: s for s in out["slis"]}["p95"]["status"] == "UNKNOWN"
    probes = list(db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "PROBE")))
    assert probes and all(e.environment["check_kind"] == "sli" for e in probes)


# ----------------------------------------------------------------------------------------- G6 rules


def test_g6_not_applicable_needs_the_owner_no_staging_decision(db, monkeypatch):
    from aeos.delivery import gates
    from aeos.models.portfolio import ProjectIntegration

    p = _project(db)
    assert gates._g6(db, p, [SHA]).status == "NOT_PROVEN"
    environment_decision(db, p, "staging", staging="NONE")
    g6 = gates._g6(db, p, [SHA])
    assert g6.status == "NOT_APPLICABLE" and g6.evidence_refs
    # a local-target is recorded but not observable: never NOT_APPLICABLE, never PASS
    q = _project(db, "local-only")
    db.add(ProjectIntegration(project_id=q.id, domain=q.domain, provider="local-target", purpose="staging",
                              root_id="container:shop-staging", config={}))
    db.commit()
    g6 = gates._g6(db, q, [SHA])
    assert g6.status == "NOT_PROVEN" and "container:shop-staging" in g6.summary and g6.source == "local-target"
    with pytest.raises(Blocked) as exc:
        deployment.observe(db, q.id, "staging", SHA, actor="owner")
    assert exc.value.code == "NO_DEPLOYMENT_TARGET" and "local-target" in exc.value.message


def test_a_target_registered_without_a_base_url_is_not_observable_and_never_crashes_the_gates(db):
    from aeos.delivery import gates

    p = _project(db, "no-url")
    deploy_target(db, p, "staging", None)  # type: ignore[arg-type]  # the registry allows a row without root_id
    deploy_target(db, p, "production", None)  # type: ignore[arg-type]
    authorise_staging(db, p, STAGING)
    g6 = gates._g6(db, p, [SHA])
    assert g6.status == "NOT_PROVEN" and "Belum ada target staging" in g6.summary
    assert gates._g8(db, p, None).status == "PENDING"
    with pytest.raises(Blocked) as exc:
        deployment.observe(db, p.id, "staging", SHA, actor="owner")
    assert exc.value.code == "NO_DEPLOYMENT_TARGET"


def test_a_staging_authorisation_for_another_target_does_not_pass_g6(db):
    from aeos.delivery import gates

    p = _project(db)
    deploy_target(db, p, "staging", STAGING)
    make_staging_ready(db, p)
    _observe(db, p, "staging", {"/health": _ok()})
    authorise_staging(db, p, "https://old-staging.example.org")
    assert gates._g6(db, p, [SHA]).status == "NOT_PROVEN"


# ----------------------------------------------------------------------------------------- staging gate


def test_observe_staging_is_refused_and_audited_while_the_gate_is_not_ready(personal_client, db):
    from tests.unit.test_portfolio_support import create_project

    p = create_project(personal_client, name="Gate App")
    assert personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "staging", "root_id": STAGING}).status_code == 200
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        r = personal_client.post("/api/delivery/targets/observe",
                                 json={"project_id": p["id"], "environment": "staging", "sha": SHA})
        assert not mock.calls, "nothing is requested from the target"
    assert r.status_code == 409 and r.json()["error"]["code"] == "CAPABILITY_NOT_READY", r.text
    assert "CAN_VALIDATE_STAGING" in r.json()["error"]["message"]
    row = db.scalars(select(AuditEvent).where(AuditEvent.action == "delivery.target.observe")
                     .order_by(AuditEvent.seq.desc())).first()
    assert row is not None and row.result == "DENIED" and "CAPABILITY_NOT_READY" in row.reason
    # production observation is not a staging validation and is not gated by it
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PROD}/health").mock(return_value=_ok())
        assert personal_client.put(f"/api/projects/{p['id']}/integrations", json={
            "provider": "deployment-target", "purpose": "production", "root_id": PROD}).status_code == 200
        ok = personal_client.post("/api/delivery/targets/observe",
                                  json={"project_id": p["id"], "environment": "production", "sha": SHA})
    assert ok.status_code == 200 and ok.json()["status"] == "PASS", ok.text


def test_cli_observe_target_refusal_is_audited_and_ascii(settings, db):
    from typer.testing import CliRunner

    from aeos.cli import app

    p = _project(db, "cli-gate")
    deploy_target(db, p, "staging", STAGING)
    runner = CliRunner()
    r = runner.invoke(app, ["delivery", "observe-target", p.id, "--environment", "staging", "--sha", SHA])
    assert r.exit_code == 2 and "CAPABILITY_NOT_READY" in r.output and r.output.isascii(), r.output
    db.expire_all()
    row = db.scalars(select(AuditEvent).where(AuditEvent.action == "delivery.target.observe")).first()
    assert row is not None and row.result == "DENIED" and row.project_id == p.id
    show = runner.invoke(app, ["delivery", "target-checks", p.id, "--environment", "staging"])
    assert show.exit_code == 0 and "UNDECIDED" in show.output and show.output.isascii(), show.output


# ----------------------------------------------------------------------------------------- post-release response


def test_a_failed_production_observation_raises_alert_incident_and_decision_request(db, personal_client):
    from aeos.policy import queue

    p = _project(db, "prod-fail")
    deploy_target(db, p, "production", PROD, {"smoke_checks": [
        {"name": "checkout", "path": "/api/checkout", "kind": "critical_flow"}]})
    down = {"/health": httpx.Response(503, json={"status": "down", "commit": SHA}),
            "/api/checkout": httpx.Response(500, json={})}
    out, _ = _observe(db, p, "production", down, base=PROD)
    assert out["status"] == "FAIL" and sorted(out["failure_conditions"]) == ["critical_flow_fail", "health_fail"]
    post = out["post_release"]
    alert = db.get(OpsAlert, post["alert_id"])
    assert alert.kind == "post_release_failure" and alert.severity == "HIGH" and alert.status == "OPEN"
    assert alert.domain == "PERSONAL" and alert.project_id == p.id and alert.auto_resolve is False
    inc = db.get(OpsIncident, post["incident_id"])
    assert inc.source == "alert" and inc.source_ref == alert.id and inc.owner == "owner"
    notes = " ".join(e["note"] for e in inc.timeline)
    assert "UNDECIDED" in notes and "AEOS tidak pernah melakukan deploy atau rollback" in notes
    assert out["evidence_id"] in inc.evidence_ids
    req = db.get(DecisionRequest, post["decision_request_id"])
    assert req.status == "OPEN" and req.kind == "RISK_ACCEPTANCE" and req.requested_by_kind == "SYSTEM"
    assert [o["key"] for o in req.details["options"]] == ["ROLLBACK", "FORWARD_FIX", "ACCEPT_RISK"]
    assert req.details["recommendation"]["status"] == "UNDECIDED"
    items = [i for i in queue.queue_items(db, "PERSONAL") if i["kind"] == "DECISION_REQUEST"]
    assert any(i["target"]["id"] == req.id for i in items), "the owner sees it in the one decision queue"
    # a repeated failure is deduplicated: same alert (2 occurrences), same incident, same request
    again, _ = _observe(db, p, "production", down, base=PROD)
    assert again["post_release"]["alert_id"] == alert.id and again["post_release"]["alert_new"] is False
    assert again["post_release"]["incident_id"] == inc.id and again["post_release"]["decision_request_id"] == req.id
    db.refresh(alert)
    assert alert.occurrences == 2
    # the alert -> incident API returns the linked incident (no second incident)
    r = personal_client.post(f"/api/ops/alerts/{alert.id}/incident", json={})
    assert r.status_code == 200 and r.json()["id"] == inc.id and r.json()["created"] is False, r.text
    # a later PASS of the same commit resolves the alert; incident and request stay with the human
    ok, _ = _observe(db, p, "production", {"/health": _ok(), "/api/checkout": httpx.Response(200, json={})},
                     base=PROD)
    assert ok["status"] == "PASS" and ok["post_release"]["alert_status"] == "RESOLVED"
    db.refresh(alert)
    db.refresh(inc)
    db.refresh(req)
    assert alert.status == "RESOLVED" and inc.status == "OPEN" and req.status == "OPEN"


def test_a_failure_after_a_resolved_incident_opens_one_new_incident_not_one_per_observation(db):
    """Dedup survives a resolved incident: the next failure of the same commit opens ONE new incident, and every
    later failure is linked to that open incident (never a new incident per observation)."""
    p = _project(db, "prod-again")
    deploy_target(db, p, "production", PROD)
    down = {"/health": httpx.Response(503, json={"status": "down", "commit": SHA})}
    first, _ = _observe(db, p, "production", down, base=PROD)
    old = db.get(OpsIncident, first["post_release"]["incident_id"])
    old.status = "RESOLVED"  # controlled precondition: the human closed the first incident
    db.commit()
    second, _ = _observe(db, p, "production", down, base=PROD)
    third, _ = _observe(db, p, "production", down, base=PROD)
    assert second["post_release"]["incident_id"] != old.id and second["post_release"]["incident_created"] is True
    assert third["post_release"]["incident_id"] == second["post_release"]["incident_id"]
    assert third["post_release"]["incident_created"] is False
    alert_id = first["post_release"]["alert_id"]
    rows = list(db.scalars(select(OpsIncident).where(OpsIncident.source == "alert", OpsIncident.source_ref == alert_id)))
    assert len(rows) == 2, [r.status for r in rows]
    from aeos.operations import facade as operations

    assert operations.post_release_status(db, p, SHA)["incident_id"] == second["post_release"]["incident_id"]


@pytest.mark.parametrize(("rollback_on", "expected"), [
    ([], "UNDECIDED"), (["health_fail"], "ROLLBACK_RECOMMENDED"), (["sli_breach"], "CRITERIA_NOT_MET")])
def test_the_rollback_recommendation_follows_the_owner_criteria(db, rollback_on, expected):
    p = _project(db, f"crit-{expected.lower()}")
    deploy_target(db, p, "production", PROD, {"rollback_on": rollback_on})
    out, _ = _observe(db, p, "production", {"/health": httpx.Response(200, json={"status": "down", "commit": SHA})},
                      base=PROD)
    rec = out["post_release"]["recommendation"]
    assert rec["status"] == expected and rec["executed_by"] == "HUMAN" and rec["aeos_action"] == "none"


def test_staging_failures_and_unattributable_production_observations_raise_nothing(db):
    p = _project(db, "quiet")
    deploy_target(db, p, "production", PROD)
    out, _ = _observe(db, p, "production", {"/health": _ok(commit="f" * 40)}, base=PROD)
    assert out["status"] == "NOT_PROVEN" and out["post_release"] is None
    assert db.scalars(select(OpsAlert)).first() is None and db.scalars(select(OpsIncident)).first() is None


def test_the_post_release_rule_is_in_the_alert_catalog_and_the_platform_documents():
    from aeos.operations import alerts
    from aeos.operations import facade as operations

    assert alerts.ALERT_RULES["post_release_failure"]["severity"] == "HIGH"
    assert "post_release_failure" in operations.platform_operations_facts("PERSONAL")["alerting.severity"]
    from aeos.operations import services

    assert "post-release-failure" in {r["id"] for r in services.runbooks()}
