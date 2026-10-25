"""§08 group Delivery: "actual provider entitlements, branch/merge/deploy restrictions, status-check identity,
trusted CI" are exposed **and evaluated** by the Doctor (not only as provider-layer residual-risk text)."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.runner import make_context, run_check
from tests.unit.test_inventory_fakes import base_runner


def check(cid, ctx):
    load_all()
    return run_check(REGISTRY[cid], ctx)


def _mock_github(gh, slug, *, app_id=None, reviewers=False):
    gh.get(f"https://api.github.com/repos/{slug}").mock(return_value=Response(200, json={
        "default_branch": "main", "private": True, "visibility": "private",
        "owner": {"type": "User", "login": slug.split("/")[0]}}))
    gh.get("https://api.github.com/user").mock(return_value=Response(200, json={
        "login": slug.split("/")[0], "plan": {"name": "pro"}}))
    gh.get(f"https://api.github.com/repos/{slug}/branches/main/protection").mock(return_value=Response(200, json={
        "required_status_checks": {"strict": True, "checks": [{"context": "backend", "app_id": app_id}]},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "enforce_admins": {"enabled": True}}))
    gh.get(f"https://api.github.com/repos/{slug}/rulesets").mock(return_value=Response(200, json=[]))
    gh.get(f"https://api.github.com/repos/{slug}/rules/branches/main").mock(return_value=Response(200, json=[]))
    envs = [{"name": "production", "protection_rules": [{"type": "required_reviewers"}] if reviewers else []}]
    gh.get(f"https://api.github.com/repos/{slug}/environments").mock(
        return_value=Response(200, json={"total_count": 1, "environments": envs}))


@pytest.mark.parametrize("app_id,reviewers,identity,deploy", [
    (None, False, "NOT_CONFIGURED", "NOT_CONFIGURED"),
    (15368, True, "CONFIGURED", "CONFIGURED"),
])
def test_status_check_identity_and_deploy_restrictions_reach_the_doctor(settings, db, secrets_memory, app_id,
                                                                        reviewers, identity, deploy):
    secrets_memory.put("personal/github", "t" * 40, actor="test")
    slug = settings.platform_repo_slug
    with respx.mock(assert_all_called=False) as gh:
        _mock_github(gh, slug, app_id=app_id, reviewers=reviewers)
        res = check("delivery.branch_protection", make_context(settings, db, runner=base_runner(), functional=True))
    ev = res.observed_evidence
    assert res.status == "CONFIGURED"  # configured, still NOT-PROVEN enforcement
    assert ev["status_check_identity"] == identity and ev["deploy_restrictions"] == deploy
    # an enforcing rule is in place on GitHub: the entitlement is observed (still NOT-PROVEN enforcement)
    assert ev["restrictions"] == {"status_check_identity": identity, "deploy_restrictions": deploy,
                                  "entitlement": "OBSERVED"}
    assert f"identitas pelapor: {identity}" in res.summary and f"pembatasan deploy: {deploy}" in res.summary
    assert "entitlement: OBSERVED" in res.summary and "NOT-PROVEN" in res.summary
    unbound = any("tidak terikat" in r for r in ev["residual_risks"])
    assert unbound is (identity == "NOT_CONFIGURED")
    assert any("deploy" in r.lower() for r in ev["residual_risks"]) is (deploy != "CONFIGURED")
    assert "t" * 40 not in json.dumps(res.as_dict())


def test_free_plan_entitlement_is_reported_as_not_entitled_never_a_pass(settings, db, secrets_memory):
    """§08 "actual provider entitlements": GitHub's own "upgrade required" answer reaches the Doctor as
    NOT_ENTITLED next to the NOT_ENFORCEABLE merge gate (a residual risk, never CONFIGURED)."""
    secrets_memory.put("personal/github", "t" * 40, actor="test")
    slug = settings.platform_repo_slug
    upgrade = {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature."}
    with respx.mock(assert_all_called=False) as gh:
        _mock_github(gh, slug)
        gh.get("https://api.github.com/user").mock(return_value=Response(200, json={
            "login": slug.split("/")[0], "plan": {"name": "free"}}))
        gh.get(f"https://api.github.com/repos/{slug}/branches/main/protection").mock(
            return_value=Response(403, json=upgrade))
        gh.get(f"https://api.github.com/repos/{slug}/rulesets").mock(return_value=Response(403, json=upgrade))
        res = check("delivery.branch_protection", make_context(settings, db, runner=base_runner(), functional=True))
    ev = res.observed_evidence
    assert res.status == "MISSING" and res.error_code == "BRANCH_PROTECTION_NOT_ENFORCEABLE"
    assert ev["entitlement"] == "NOT_ENTITLED" and ev["restrictions"]["entitlement"] == "NOT_ENTITLED"
    assert "entitlement: NOT_ENTITLED" in res.summary and ev["enforceable"] is False


def test_stored_capability_evidence_keeps_the_restrictions(settings, db):
    """Without --functional the Doctor reads the recorded PROVIDER_CAPABILITY evidence: the identity / deploy
    fields and the entitlement are evaluated from it, not dropped."""
    from aeos.delivery import provider
    from aeos.models.portfolio import Project

    p = Project(project_key="dly-aeos", name="platform", domain="PERSONAL", is_platform=True)
    db.add(p)
    db.flush()
    caps = provider.ProviderCapabilities(provider="GITHUB", repo=settings.platform_repo_slug, branch="main",
                                         checked_at="2026-10-19T00:00:00+00:00", credential="AVAILABLE",
                                         merge_gate="CONFIGURED", required_status_checks=["backend"],
                                         status_check_identity="NOT_CONFIGURED", deploy_restrictions="UNKNOWN")
    provider.record_capability_evidence(db, p, caps)
    db.commit()
    res = check("delivery.branch_protection", make_context(settings, db, runner=base_runner()))
    ev = res.observed_evidence
    assert res.status == "CONFIGURED" and ev["restrictions"]["status_check_identity"] == "NOT_CONFIGURED"
    assert ev["restrictions"]["deploy_restrictions"] == "UNKNOWN"
    assert any("tidak terikat" in r for r in ev["residual_risks"])


def test_trusted_ci_reports_the_provider_observation(settings, db):
    """The pin check is static (E1); the Doctor also shows whether provider CI was actually observed (E3)."""
    from aeos.delivery import provider
    from aeos.models.portfolio import Project

    res = check("delivery.ci_workflow", make_context(settings, db, runner=base_runner()))
    assert res.status == "VERIFIED" and res.observed_evidence["provider_ci"]["status"] == "NOT_OBSERVED"
    p = Project(project_key="dly-aeos", name="platform", domain="PERSONAL", is_platform=True)
    db.add(p)
    db.flush()
    ci = provider.parse_check_runs("o/r", "a" * 40, [{"total_count": 1, "check_runs": [
        {"name": "backend", "status": "completed", "conclusion": "success", "head_sha": "a" * 40}]}])
    provider.record_ci_evidence(db, p, ci)
    db.commit()
    res = check("delivery.ci_workflow", make_context(settings, db, runner=base_runner()))
    obs = res.observed_evidence["provider_ci"]
    assert obs["status"] == "OBSERVED" and obs["overall"] == "PASS" and obs["commit"] == "a" * 40
    assert "CI provider teramati" in res.summary
