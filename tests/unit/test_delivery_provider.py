"""GitHub provider capability + CI checks (§31). §31: "Jika fitur enforcement tidak tersedia, jangan
menyatakan required checks telah memblokir merge" → NOT_ENFORCEABLE residual risk. CI result source
of truth is the provider (§30), recorded as provider-ci E3 evidence for the exact commit."""

from __future__ import annotations

import httpx
import pytest
import respx

from aeos.core.errors import ValidationFailed
from aeos.delivery import provider
from aeos.models.portfolio import Project

API = provider.GITHUB_API
REPO = "fadhlillah-dli/dly-aeos"
TOKEN = "test-token-not-real"
UPGRADE = {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature."}


def _repo(private=True, owner_type="User"):
    return {"private": private, "visibility": "private" if private else "public", "default_branch": "main",
            "owner": {"login": "fadhlillah-dli", "type": owner_type}, "permissions": {"admin": True, "push": True}}


def _base_routes(mock, *, private=True, plan="free", environments=None):
    mock.get(f"{API}/repos/{REPO}").respond(200, json=_repo(private))
    mock.get(f"{API}/user").respond(200, json={"login": "fadhlillah-dli", "plan": {"name": plan}})
    mock.get(f"{API}/repos/{REPO}/environments").respond(
        200, json={"total_count": len(environments or []), "environments": environments or []})


@respx.mock(assert_all_called=False)
def test_free_private_403_upgrade_is_not_enforceable(respx_mock):
    _base_routes(respx_mock)
    respx_mock.get(f"{API}/repos/{REPO}/branches/main/protection").respond(403, json=UPGRADE)
    respx_mock.get(f"{API}/repos/{REPO}/rulesets").respond(403, json=UPGRADE)
    respx_mock.get(f"{API}/repos/{REPO}/rules/branches/main").respond(200, json=[])
    caps = provider.check_capabilities(REPO, token=TOKEN)
    checks = {c.key: c for c in caps.checks}
    assert checks["branch_protection"].status == "NOT_ENFORCEABLE"
    assert checks["rulesets"].status == "NOT_ENFORCEABLE"
    assert checks["required_status_checks"].status == "NOT_ENFORCEABLE"
    assert caps.merge_gate == "NOT_ENFORCEABLE"
    assert provider.NOT_ENFORCEABLE_MESSAGE in caps.residual_risks
    assert caps.enforcement_tested == "NOT-PROVEN"
    assert caps.plan == "free" and caps.visibility == "private"
    assert all(call.request.method == "GET" for call in respx_mock.calls)
    assert TOKEN not in str(caps.to_dict())


@respx.mock(assert_all_called=False)
def test_free_private_404_is_not_enforceable(respx_mock):
    _base_routes(respx_mock)
    respx_mock.get(f"{API}/repos/{REPO}/branches/main/protection").respond(404, json={"message": "Not Found"})
    respx_mock.get(f"{API}/repos/{REPO}/rulesets").respond(404, json={"message": "Not Found"})
    respx_mock.get(f"{API}/repos/{REPO}/rules/branches/main").respond(404, json={"message": "Not Found"})
    caps = provider.check_capabilities(REPO, token=TOKEN)
    assert {c.key: c.status for c in caps.checks}["branch_protection"] == "NOT_ENFORCEABLE"
    assert caps.merge_gate == "NOT_ENFORCEABLE"


@respx.mock(assert_all_called=False)
def test_public_repo_not_protected_is_not_configured(respx_mock):
    _base_routes(respx_mock, private=False)
    respx_mock.get(f"{API}/repos/{REPO}/branches/main/protection").respond(404, json={"message": "Branch not protected"})
    respx_mock.get(f"{API}/repos/{REPO}/rulesets").respond(200, json=[])
    respx_mock.get(f"{API}/repos/{REPO}/rules/branches/main").respond(200, json=[])
    caps = provider.check_capabilities(REPO, token=TOKEN)
    assert caps.merge_gate == "NOT_CONFIGURED"
    assert provider.NOT_CONFIGURED_MESSAGE in caps.residual_risks


@respx.mock(assert_all_called=False)
def test_configured_protection_reports_required_checks_but_not_proven(respx_mock):
    _base_routes(respx_mock, plan="pro")
    respx_mock.get(f"{API}/repos/{REPO}/branches/main/protection").respond(200, json={
        "required_status_checks": {"strict": True, "contexts": ["backend (ubuntu-latest)"],
                                   "checks": [{"context": "frontend", "app_id": 1}]},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "enforce_admins": {"enabled": False}, "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False}})
    respx_mock.get(f"{API}/repos/{REPO}/rulesets").respond(200, json=[{"name": "main", "enforcement": "active"}])
    respx_mock.get(f"{API}/repos/{REPO}/rules/branches/main").respond(200, json=[
        {"type": "pull_request"}, {"type": "required_status_checks",
                                   "parameters": {"required_status_checks": [{"context": "secret-scan"}]}}])
    caps = provider.check_capabilities(REPO, token=TOKEN)
    assert caps.merge_gate == "CONFIGURED"
    assert caps.required_status_checks == ["backend (ubuntu-latest)", "frontend", "secret-scan"]
    assert caps.enforcement_tested == "NOT-PROVEN"
    assert any("enforce_admins=false" in r for r in caps.residual_risks)
    assert provider.ENFORCEMENT_UNTESTED in caps.residual_risks
    # §08 status-check identity: "backend" (legacy context) and "secret-scan" may be reported by any app
    checks = {c.key: c for c in caps.checks}
    assert caps.status_check_identity == "NOT_CONFIGURED"
    assert checks["status_check_identity"].detail["unbound"] == ["backend (ubuntu-latest)", "secret-scan"]
    assert checks["status_check_identity"].detail["apps"]["frontend"] == 1
    assert any("tidak terikat ke GitHub App" in r for r in caps.residual_risks)
    # §08 deploy restrictions: no deployment environment exists (and AEOS has no deploy operation)
    assert caps.deploy_restrictions == "NOT_CONFIGURED"
    assert any("tidak memiliki operasi deploy" in r for r in caps.residual_risks)


def test_pipeline_reference_maps_jobs_to_gates_explicitly(db):
    """§31 pipeline reference per project: with the project's reference, gates come from exact job names
    (no name heuristics); the CI evidence records which mapping it used."""
    from aeos.models.portfolio import ProjectIntegration

    payload = [{"total_count": 3, "check_runs": [
        {"name": "lint", "status": "completed", "conclusion": "success", "head_sha": "a" * 40},
        {"name": "frontend", "status": "completed", "conclusion": "success", "head_sha": "a" * 40},
        {"name": "backend (ubuntu-latest)", "status": "completed", "conclusion": "success", "head_sha": "a" * 40}]}]
    heuristic = provider.parse_check_runs(REPO, "a" * 40, payload)
    assert heuristic.gate_mapping == "NAME_HEURISTIC"
    assert "G1" in {g for r in heuristic.runs if r.name == "frontend" for g in r.gates}  # heuristic: frontend ~ G1
    p = Project(project_key="pipe", name="pipe", domain="PERSONAL")
    db.add(p)
    db.flush()
    db.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="github-actions", purpose="ci",
                              config={"gate_G1": "lint", "gate_G2": "backend (ubuntu-latest)", "gate_G3": "frontend"}))
    db.commit()
    gate_map = provider.pipeline_reference(db, p)
    assert gate_map == {"G1": ["lint"], "G2": ["backend (ubuntu-latest)"], "G3": ["frontend"]}
    explicit = provider.parse_check_runs(REPO, "a" * 40, payload, gate_map)
    by_name = {r.name: r.gates for r in explicit.runs}
    assert explicit.gate_mapping == "PIPELINE_REFERENCE"
    assert by_name == {"lint": ["G1"], "frontend": ["G3"], "backend (ubuntu-latest)": ["G2"]}
    assert provider.pipeline_reference(db, Project(project_key="none", name="n", domain="PERSONAL", id="x")) is None


@respx.mock(assert_all_called=False)
def test_status_check_identity_and_protected_environments_are_read(respx_mock):
    envs = [{"name": "production", "protection_rules": [{"type": "required_reviewers", "reviewers": []},
                                                        {"type": "wait_timer"}],
             "deployment_branch_policy": {"protected_branches": True}}]
    _base_routes(respx_mock, plan="pro", environments=envs)
    respx_mock.get(f"{API}/repos/{REPO}/branches/main/protection").respond(200, json={
        "required_status_checks": {"strict": True, "checks": [{"context": "backend", "app_id": 15368}]},
        "required_pull_request_reviews": {"required_approving_review_count": 1}, "enforce_admins": {"enabled": True}})
    respx_mock.get(f"{API}/repos/{REPO}/rulesets").respond(200, json=[])
    respx_mock.get(f"{API}/repos/{REPO}/rules/branches/main").respond(200, json=[
        {"type": "required_status_checks",
         "parameters": {"required_status_checks": [{"context": "frontend", "integration_id": 15368}]}}])
    caps = provider.check_capabilities(REPO, token=TOKEN)
    checks = {c.key: c for c in caps.checks}
    assert caps.status_check_identity == "CONFIGURED", checks["status_check_identity"]
    assert checks["status_check_identity"].detail["apps"] == {"backend": 15368, "frontend": 15368}
    assert caps.deploy_restrictions == "CONFIGURED"
    assert checks["deploy_environments"].detail["environments"][0]["protection_rules"] == [
        "required_reviewers", "wait_timer"]
    assert all(call.request.method == "GET" for call in respx_mock.calls)  # read-only


def test_no_token_means_no_network_and_unknown():
    with respx.mock(assert_all_called=False) as mock:
        caps = provider.check_capabilities(REPO, token=None, credential="MISSING: SECRET_MISSING")
        assert not mock.calls
    assert caps.merge_gate == "UNKNOWN"
    assert caps.checks[0].status == "UNKNOWN"


@respx.mock(assert_all_called=False)
def test_network_error_is_unknown(respx_mock):
    respx_mock.get(f"{API}/repos/{REPO}").mock(side_effect=httpx.ConnectError("down"))
    caps = provider.check_capabilities(REPO, token=TOKEN)
    assert caps.merge_gate == "UNKNOWN"


def test_repo_validation():
    for bad in ("../etc", "owner", "owner/../x", "a b/c"):
        with pytest.raises(ValidationFailed):
            provider.validate_repo(bad)


SHA = "1234567890abcdef1234567890abcdef12345678"


def _run(name, status="completed", conclusion="success", sha=SHA):
    return {"name": name, "status": status, "conclusion": conclusion, "head_sha": sha, "app": {"slug": "github-actions"},
            "html_url": f"https://github.com/x/{name}"}


def test_check_runs_parsing_and_gate_classification():
    payload = {"total_count": 4, "check_runs": [
        _run("backend (ubuntu-latest)"), _run("backend (windows-latest)"), _run("frontend"),
        _run("other", sha="f" * 40)]}
    ci = provider.parse_check_runs("o/r", SHA, [payload])
    assert ci.overall == "PASS" and ci.total == 3, "check runs of another commit are ignored"
    assert ci.gate_status("G1")[0] == "PASS"
    assert ci.gate_status("G3") == ("PASS", ["frontend"])
    fail = provider.parse_check_runs("o/r", SHA, [{"check_runs": [_run("backend (x)"),
                                                                  _run("frontend", conclusion="failure")]}])
    assert fail.overall == "FAIL" and fail.gate_status("G3")[0] == "FAIL"
    pending = provider.parse_check_runs("o/r", SHA, [{"check_runs": [_run("frontend", status="in_progress",
                                                                          conclusion=None)]}])
    assert pending.overall == "PENDING"
    cancelled = provider.parse_check_runs("o/r", SHA, [{"check_runs": [_run("frontend", conclusion="cancelled")]}])
    assert cancelled.overall == "NOT_PROVEN"
    skipped = provider.parse_check_runs("o/r", SHA, [{"check_runs": [_run("frontend", conclusion="skipped")]}])
    assert skipped.overall == "NOT_PROVEN"
    none = provider.parse_check_runs("o/r", SHA, [{"total_count": 0, "check_runs": []}])
    assert none.overall == "NO_CHECKS"


@respx.mock(assert_all_called=False)
def test_fetch_check_runs_and_record_e3_evidence(respx_mock, db):
    respx_mock.get(f"{API}/repos/{REPO}/commits/{SHA}/check-runs").respond(200, json={
        "total_count": 1, "check_runs": [_run("backend (ubuntu-latest)")]})
    ci = provider.fetch_check_runs(REPO, SHA, token=TOKEN)
    assert ci.overall == "PASS"
    p = Project(project_key="dly-aeos", name="x", domain="PERSONAL")
    db.add(p)
    db.flush()
    ev = provider.record_ci_evidence(db, p, ci)
    assert ev.origin == "provider-ci" and ev.trusted and ev.evidence_level == "E3"
    assert ev.subject_type == "commit" and ev.subject_id == SHA and ev.environment["commit"] == SHA


@respx.mock(assert_all_called=False)
def test_fetch_check_runs_error_is_unknown_and_not_recorded(respx_mock, db):
    respx_mock.get(f"{API}/repos/{REPO}/commits/{SHA}/check-runs").respond(401, json={"message": "Bad credentials"})
    ci = provider.fetch_check_runs(REPO, SHA, token=TOKEN)
    assert ci.overall == "UNKNOWN" and "401" in ci.error
    p = Project(project_key="dly-aeos", name="x", domain="PERSONAL")
    db.add(p)
    db.flush()
    assert provider.record_ci_evidence(db, p, ci) is None


@respx.mock(assert_all_called=False)
def test_fetch_pull_reads_merged_by(respx_mock):
    respx_mock.get(f"{API}/repos/{REPO}/pulls/7").respond(200, json={
        "number": 7, "state": "closed", "merged": True, "merged_by": {"login": "fadhlillah-dli", "type": "User"},
        "merged_at": "2026-10-18T10:00:00Z", "merge_commit_sha": "m" * 40, "head": {"sha": SHA, "ref": "aeos/t1"},
        "base": {"sha": "b" * 40, "ref": "main"}, "html_url": "https://github.com/x/pull/7"})
    d = provider.fetch_pull(REPO, 7, token=TOKEN)
    assert d["merged"] and d["merged_by_type"] == "User" and d["head_sha"] == SHA


def test_github_token_office_not_applicable_and_missing(secrets_memory):
    tok, status = provider.github_token("OFFICE")
    assert tok is None and status.startswith("NOT_APPLICABLE")
    tok, status = provider.github_token("PERSONAL")
    assert tok is None and status.startswith("MISSING")
    secrets_memory.put("personal/github", "a-test-token-value", actor="t")
    tok, status = provider.github_token("PERSONAL")
    assert tok == "a-test-token-value" and status == "AVAILABLE"
