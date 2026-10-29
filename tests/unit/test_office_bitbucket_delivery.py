"""OFFICE delivery on Bitbucket Cloud (master §01 U-03, §04, §31, §45, §48; ADR-0111).

* read side (``aeos.delivery.bitbucket``): commit statuses + Pipelines steps of one exact commit become
  ``provider-ci`` evidence for the gates; merge checks without ``enforce merge checks`` (Premium) are
  NOT_ENFORCEABLE, never "required checks block merge"; abbreviated PR hashes are resolved, never compared;
* readiness: disabled by default (company authorisation POLICY decision + enabled integration + Cloud +
  verified repository), Data Center NOT_SUPPORTED;
* broker (``aeos.broker.bitbucket_broker``): sha-pinned ``refs/heads/aeos/*`` push and a draft PR, only behind
  the run-bound grant and the real external-write interlock with the company authorisation; refused before any
  credential read otherwise; no integration endpoint anywhere.
All HTTP is mocked (respx); git pushes go to local bare repositories. Live tenant behaviour is NOT-PROVEN (M5).
"""

from __future__ import annotations

import ast
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import delete, select

from aeos.broker import bitbucket_broker, git_broker
from aeos.broker import facade as broker
from aeos.core.attribution import find_ai_attribution
from aeos.core.errors import Blocked, PolicyDenied
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.delivery import bitbucket, gates, provider
from aeos.models.audit import AuditEvent
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.evidence import EvidenceArtifact, ReviewRecord
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project, ProjectIntegration, RepositoryReference
from aeos.models.workforce import WorkerRun
from tests.conftest import create_admin
from tests.unit.review_gate_support import record_claude_review, record_push
from tests.unit.test_workforce_support import init_git_repo

REPO = "company_ws/svc"  # the underscore is valid for a Bitbucket workspace, invalid as a GitHub owner
API = "https://api.bitbucket.org/2.0/repositories/company_ws/svc"
SHA = "c0ffee" + "1" * 34
OTHER = "d" * 40
BRANCH = "aeos/t1-attempt-1"
ACCESS = "bbat-" + "Q7" * 20
AUTH = "Bearer " + ACCESS


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


# ------------------------------------------------------------------------------------------- fixtures


def _company_authorisation(db, owner, providers=("bitbucket",), deployment="CLOUD", approved_targets=None):
    """The company authorisation, naming the approved Bitbucket workspace (approved_targets, W6-157)."""
    from aeos.policy import facade as policy

    if approved_targets is None:
        approved_targets = [{"provider": "bitbucket", "workspace": "company_ws"}] if "bitbucket" in providers else []
    policy.record_decision(db, kind="POLICY", actor_user_id=owner.id, target_type="office_policy", target_id="OFFICE",
                           domain="OFFICE", rationale="company authorisation (test)",
                           details={"office_authorization": True, "company_reference": "CHG-42 signed",
                                    "allowed_providers": list(providers), "approved_targets": approved_targets,
                                    "office_policy": {"atlassian_deployment": deployment}},
                           auth_scope={"providers": list(providers)})
    db.commit()


def _office_project(db, *, enabled=True, deployment="CLOUD", verified=True, root=REPO) -> Project:
    p = Project(project_key="office-svc", name="Office service", domain="OFFICE", is_platform=False)
    db.add(p)
    db.flush()
    db.add(RepositoryReference(project_id=p.id, provider="BITBUCKET", full_name=REPO, default_branch="main",
                               verification_status="VERIFIED" if verified else "UNVERIFIED", provider_repo_id="{u1}"))
    if enabled is not None:
        db.add(ProjectIntegration(project_id=p.id, domain="OFFICE", provider="bitbucket", purpose="code",
                                  root_id=root, credential_ref="office/bitbucket",
                                  config={"enabled": enabled, "deployment": deployment}))
    db.commit()
    return p


@pytest.fixture
def owner(db):
    return create_admin(db)


@pytest.fixture
def ready_office(db, owner, secrets_memory):
    """Everything the OFFICE adapter needs: company authorisation, enabled Cloud integration, verified repo,
    credential (a repository access token)."""
    _company_authorisation(db, owner)
    p = _office_project(db)
    secrets_memory.put("office/bitbucket", json.dumps({"access_token": ACCESS}), actor="test")
    return p


# ------------------------------------------------------------------------------------------- readiness


def test_office_delivery_is_disabled_by_default_and_never_guessed(db, owner, secrets_memory):
    p = _office_project(db, enabled=None)
    r = bitbucket.office_delivery_readiness(db, p)
    assert r.status == "DISABLED"
    assert any("otorisasi perusahaan" in x for x in r.reasons) and any("nonaktif" in x for x in r.reasons)
    _company_authorisation(db, owner, providers=("jira",))  # authorised, but not for Bitbucket
    assert bitbucket.office_delivery_readiness(db, p).status == "DISABLED"


def test_data_center_is_not_supported_and_unknown_deployment_blocked(db, owner, secrets_memory):
    _company_authorisation(db, owner, deployment="UNKNOWN")
    secrets_memory.put("office/bitbucket", json.dumps({"access_token": ACCESS}), actor="test")
    p = _office_project(db, deployment="DATA_CENTER")
    r = bitbucket.office_delivery_readiness(db, p)
    assert r.status == "NOT_SUPPORTED" and any("Data Center NOT SUPPORTED" in x for x in r.reasons)
    db.get(ProjectIntegration, r.integration_id).config = {"enabled": True}
    db.commit()
    r = bitbucket.office_delivery_readiness(db, p)
    assert r.status == "BLOCKED" and any("belum diverifikasi" in x for x in r.reasons)


def test_unverified_repository_or_missing_credential_blocks(db, owner, secrets_memory):
    _company_authorisation(db, owner)
    p = _office_project(db, verified=False)
    r = bitbucket.office_delivery_readiness(db, p)
    assert r.status == "BLOCKED"
    assert any("belum VERIFIED" in x for x in r.reasons) and any("belum disimpan" in x for x in r.reasons)


def test_ready_office_is_available_and_names_the_exact_repository(db, ready_office):
    r = bitbucket.office_delivery_readiness(db, ready_office)
    assert r.status == "AVAILABLE" and r.reasons == [] and r.repo == REPO and r.deployment == "CLOUD"
    assert "NOT-PROVEN" in r.live_proof
    dp = gates.delivery_provider_status(ready_office, db)
    assert dp["status"] == "AVAILABLE" and dp["provider"] == "bitbucket" and "NOT-PROVEN" in dp["reason"]


# ------------------------------------------------------------------------------------------- CI reader


def _statuses(*rows, next_url=None):
    body = {"values": [{"type": "build", "key": k, "name": n, "state": s, "url": f"https://ci.example/{k}",
                        "commit": {"hash": h}, "created_on": "2026-10-19T10:00:00Z",
                        "updated_on": "2026-10-19T10:05:00Z"} for k, n, s, h in rows]}
    if next_url:
        body["next"] = next_url
    return body


def _mock_ci(mock, statuses, *, steps_state="COMPLETED", step_results=(("lint", "SUCCESSFUL"),
                                                                       ("unit tests", "SUCCESSFUL"))):
    mock.get(f"{API}/commit/{SHA}/statuses").mock(return_value=httpx.Response(200, json=statuses))
    mock.get(f"{API}/pipelines").mock(return_value=httpx.Response(200, json={"values": [
        {"uuid": "{p1}", "build_number": 7, "state": {"name": "COMPLETED"},
         "target": {"type": "pipeline_ref_target", "ref_name": BRANCH, "commit": {"hash": SHA}}},
        # another commit's pipeline is never attributed to this one
        {"uuid": "{p2}", "build_number": 6, "target": {"commit": {"hash": OTHER}}}]}))
    mock.get(f"{API}/pipelines/%7Bp1%7D/steps").mock(return_value=httpx.Response(200, json={"values": [
        {"uuid": f"{{s{i}}}", "name": n, "state": {"name": steps_state, "result": {"name": r}},
         "started_on": "2026-10-19T10:00:00Z", "completed_on": "2026-10-19T10:04:00Z"}
        for i, (n, r) in enumerate(step_results)]}))


def test_commit_statuses_and_pipeline_steps_become_gate_evidence(db, ready_office):
    gate_map = {"G1": ["lint"], "G2": ["unit tests"]}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_ci(mock, _statuses(("{p1}", "Pipeline #7 for aeos/t1", "SUCCESSFUL", SHA[:12])),
                 step_results=(("lint", "SUCCESSFUL"), ("unit tests", "FAILED")))
        ci = bitbucket.fetch_ci_status(REPO, SHA, auth_header=AUTH, gate_map=gate_map)
        assert all(c.request.method == "GET" for c in mock.calls)
        assert all(c.request.headers["authorization"] == AUTH for c in mock.calls)
    assert ci.provider == "bitbucket" and ci.gate_mapping == "PIPELINE_REFERENCE" and ci.error is None
    assert ci.overall == "FAIL" and ci.total == 3
    assert ci.gate_status("G1") == ("PASS", ["lint"]) and ci.gate_status("G2")[0] == "FAIL"
    ev = provider.record_ci_evidence(db, ready_office, ci)
    db.commit()
    assert ev.origin == "provider-ci" and ev.trusted and ev.evidence_level == "E3" and ev.subject_id == SHA
    assert ev.environment["provider"] == "bitbucket" and ev.collector_identity.startswith("bitbucket-commit-statuses")
    stored = gates.latest_ci_evidence(db, ready_office.id, SHA)
    assert stored.id == ev.id and gates.ci_gate_status(stored, "G1")[0] == "PASS"


def test_running_or_stopped_pipelines_are_never_a_pass():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_ci(mock, _statuses(), steps_state="IN_PROGRESS")
        assert bitbucket.fetch_ci_status(REPO, SHA, auth_header=AUTH).overall == "PENDING"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_ci(mock, _statuses(("k", "office-ci", "STOPPED", SHA)), step_results=(("lint", "SUCCESSFUL"),))
        assert bitbucket.fetch_ci_status(REPO, SHA, auth_header=AUTH).overall == "NOT_PROVEN"


def test_partial_read_and_foreign_commit_are_not_proven():
    nxt = f"{API}/commit/{SHA}/statuses?page=2"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_ci(mock, _statuses(("k", "office-ci", "SUCCESSFUL", SHA), next_url=nxt))
        ci = bitbucket.fetch_ci_status(REPO, SHA, auth_header=AUTH, max_pages=1)
    assert ci.truncated and ci.overall == "NOT_PROVEN"  # unseen statuses could be failing
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_ci(mock, _statuses(("k", "office-ci", "SUCCESSFUL", OTHER)), step_results=())
        ci = bitbucket.fetch_ci_status(REPO, SHA, auth_header=AUTH)
    assert ci.overall == "NO_CHECKS" and ci.runs == []


def test_pagination_never_leaves_the_bitbucket_api_host(db, ready_office):
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_ci(mock, _statuses(("k", "office-ci", "SUCCESSFUL", SHA), next_url="https://evil.example/steal"))
        ci = bitbucket.fetch_ci_status(REPO, SHA, auth_header=AUTH)
        assert all(c.request.url.host == "api.bitbucket.org" for c in mock.calls)
    assert ci.overall == "UNKNOWN" and "leaves the Bitbucket API host" in ci.error
    assert provider.record_ci_evidence(db, ready_office, ci) is None  # an error result is never E3 evidence
    assert db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "CI_STATUS")) is None


def test_ci_without_credential_makes_no_call():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        ci = bitbucket.fetch_ci_status(REPO, SHA, auth_header=None)
        assert not mock.calls
    assert ci.overall == "UNKNOWN" and "credential" in ci.error


# ------------------------------------------------------------------------------------------- capabilities


def _mock_repo(mock, restrictions, *, status=200):
    mock.get(API).mock(return_value=httpx.Response(200, json={
        "full_name": REPO, "uuid": "{u1}", "is_private": True, "mainbranch": {"name": "main"}}))
    mock.get(f"{API}/branch-restrictions").mock(return_value=httpx.Response(
        status, json={"values": restrictions} if status == 200 else {"error": {"message": "admin required"}}))
    mock.get(f"{API}/branching-model").mock(return_value=httpx.Response(200, json={
        "development": {"name": "main", "use_mainbranch": True}, "production": None,
        "branch_types": [{"kind": "release", "prefix": "release/"}]}))
    mock.get(f"{API}/environments").mock(return_value=httpx.Response(200, json={"values": [
        {"name": "Production", "environment_type": {"name": "Production"}, "restrictions": {"admin_only": True}}]}))


def _rule(kind, pattern="main", value=None, match="glob"):
    return {"kind": kind, "branch_match_kind": match, "pattern": pattern, "value": value,
            "branch_type": "development" if match == "branching_model" else None}


def test_merge_checks_without_premium_enforcement_are_not_enforceable():
    rules = [_rule("require_passing_builds_to_merge", value=1), _rule("require_approvals_to_merge", value=1),
             _rule("push", match="branching_model"), _rule("force", pattern="*"), _rule("delete", pattern="feat/*")]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_repo(mock, rules)
        caps = bitbucket.check_capabilities(REPO, None, auth_header=AUTH)
        assert all(c.request.method == "GET" for c in mock.calls)
    checks = {c.key: c for c in caps.checks}
    assert caps.provider == "BITBUCKET" and caps.branch == "main"
    assert caps.merge_gate == "NOT_ENFORCEABLE" and caps.entitlement == "NOT_OBSERVED"
    assert checks["merge_check_enforcement"].status == "NOT_ENFORCEABLE"
    assert checks["required_status_checks"].status == "NOT_ENFORCEABLE"  # configured, but only "recommended"
    assert checks["plan"].status == "UNKNOWN" and caps.enforcement_tested == "NOT-PROVEN"
    assert any("Premium" in r for r in caps.residual_risks)
    summary = checks["branch_restrictions"].detail
    assert summary["direct_push_restricted"] and summary["history_rewrite_prevented"]
    assert not summary["deletion_prevented"]  # the delete rule targets feat/* only
    assert any("Penghapusan branch" in r for r in caps.residual_risks)


def test_enforced_merge_checks_are_configured_but_never_proven():
    rules = [_rule("require_passing_builds_to_merge", value=2), _rule("enforce_merge_checks"),
             _rule("restrict_merges"), _rule("push"), _rule("force"), _rule("delete")]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_repo(mock, rules)
        caps = bitbucket.check_capabilities(REPO, "main", auth_header=AUTH)
    checks = {c.key: c for c in caps.checks}
    assert caps.merge_gate == "CONFIGURED" and caps.entitlement == "OBSERVED"
    assert caps.enforcement_tested == "NOT-PROVEN" and caps.required_status_checks == ["min_successful_builds=2"]
    # Bitbucket counts build statuses without binding who reported them
    assert caps.status_check_identity == "NOT_CONFIGURED" and checks["deploy_environments"].status == "CONFIGURED"


def test_unreadable_restrictions_are_unknown_not_assumed():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_repo(mock, [], status=403)
        caps = bitbucket.check_capabilities(REPO, "main", auth_header=AUTH)
    assert caps.merge_gate == "UNKNOWN" and caps.entitlement == "UNKNOWN"
    assert {c.key: c.status for c in caps.checks}["branch_restrictions"] == "UNKNOWN"


def test_capabilities_are_not_probed_while_office_delivery_is_disabled(db, owner, secrets_memory):
    p = _office_project(db, enabled=False)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        caps = bitbucket.capabilities_for(db, p, REPO, "main")
        assert not mock.calls
    assert caps.merge_gate == "UNKNOWN" and caps.checks[0].status == "DISABLED"


# ------------------------------------------------------------------------------------------- PR observation


def _mock_pull(mock, *, state="MERGED", closer_type="user"):
    mock.get(f"{API}/pullrequests/5").mock(return_value=httpx.Response(200, json={
        "id": 5, "state": state, "source": {"branch": {"name": BRANCH}, "commit": {"hash": SHA[:12]}},
        "destination": {"branch": {"name": "main"}, "commit": {"hash": "b" * 12}},
        "merge_commit": {"hash": "e" * 12} if state == "MERGED" else None,
        "closed_by": {"type": closer_type, "account_id": "557058:owner-1", "nickname": "owner"},
        "links": {"html": {"href": "https://bitbucket.org/company_ws/svc/pull-requests/5"}}}))
    for full in (SHA, "b" * 40, "e" * 40):
        mock.get(f"{API}/commit/{full[:12]}").mock(return_value=httpx.Response(200, json={
            "hash": full, "parents": [{"hash": "b" * 40}, {"hash": SHA}] if full.startswith("e") else []}))


def test_pull_request_hashes_are_resolved_to_full_commits():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_pull(mock)
        pr = bitbucket.fetch_pull(REPO, 5, auth_header=AUTH)
    assert pr["head_sha"] == SHA and pr["base_sha"] == "b" * 40 and pr["merge_commit_sha"] == "e" * 40
    assert pr["merged"] and pr["merged_by_type"] == "User" and pr["merged_by_login"] == "557058:owner-1"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_pull(mock, closer_type="app_user")
        assert bitbucket.fetch_pull(REPO, 5, auth_header=AUTH)["merged_by_type"] == "App"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:  # an unresolvable abbreviated hash is a failed read
        mock.get(f"{API}/pullrequests/5").mock(return_value=httpx.Response(200, json={
            "id": 5, "state": "OPEN", "source": {"commit": {"hash": SHA[:12]}}}))
        mock.get(f"{API}/commit/{SHA[:12]}").mock(return_value=httpx.Response(404))
        assert "error" in bitbucket.fetch_pull(REPO, 5, auth_header=AUTH)


# ------------------------------------------------------------------------------------------- broker


@pytest.fixture
def office_run(db, owner, ready_office, tmp_path):
    """An OFFICE run whose plan version carries the owner's APPROVAL for push_branch/create_pr on the repository,
    with write enablement for bitbucket; the candidate is a real git repo."""
    from aeos.planning import service
    from aeos.policy import decisions as registry
    from aeos.policy import enablement
    from tests.unit.test_planning_support import sample_content

    content = sample_content()
    plan = Plan(project_id=ready_office.id, domain="OFFICE", title="p", state="READY_FOR_HUMAN_REVIEW")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=ready_office.id, domain="OFFICE", version=1, content=content,
                    manifest_hash=content_hash(content))
    db.add(v)
    db.flush()
    plan.current_version_id = v.id
    service.decide(db, plan.id, action="APPROVE", version_id=v.id, version_hash=v.manifest_hash,
                   targets=[{"provider": "bitbucket", "repo": REPO}], rationale="ok", actor_user_id=owner.id)
    registry.record_decision(db, kind="WRITE_ENABLEMENT", actor_user_id=owner.id, target_type="provider_write",
                             target_id="bitbucket", domain="OFFICE", allowed_operations=["push_branch", "create_pr"],
                             auth_scope={"provider": "bitbucket"}, rationale="enable", details={"enabled": True})
    enablement.set_flag(db, "bitbucket", True)
    src = tmp_path / "candidate"
    base = init_git_repo(src)
    _git(src, "checkout", "-q", "-b", BRANCH)
    (src / "feature.txt").write_text("work\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "candidate")
    head = _git(src, "rev-parse", "HEAD")
    task = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id).order_by(TaskSpec.task_key))
    if task is None:  # the approval materialises the version's TaskSpecs; create one only if it did not
        task = TaskSpec(plan_version_id=v.id, plan_version_hash=v.manifest_hash, project_id=ready_office.id,
                        domain="OFFICE", task_key="T1", title="t", goal="g", allowed_paths=["src/**"], spec_hash="s")
        db.add(task)
        db.flush()
    run = WorkerRun(task_id=task.id, project_id=ready_office.id, domain="OFFICE", execution_status="REVIEW",
                    workspace_ref=str(src), branch=BRANCH, head_commit=head, base_commit=base,
                    requested_by=owner.id)
    db.add(run)
    db.flush()
    record_claude_review(db, run)  # owner rule R44: the Claude review PASS of this head (ADR-0132)
    db.commit()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    return {"run": run, "src": src, "head": head, "bare": bare, "project": ready_office}


def _grants(db, run, *, branch=BRANCH, base="main"):
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain="OFFICE", kind="GIT_BRANCH",
                       scope={"repo": REPO, "branch": branch}, ttl_seconds=300, max_requests=3)
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain="OFFICE", kind="PR_CREATE",
                       scope={"repo": REPO, "head": branch, "base": base}, ttl_seconds=300, max_requests=3)
    db.commit()


def _count_secret_reads(monkeypatch, secrets_memory) -> list[str]:
    reads: list[str] = []
    real = secrets_memory.get

    def get(ref, *, purpose, actor):
        reads.append(f"{ref}|{purpose}")
        return real(ref, purpose=purpose, actor=actor)

    monkeypatch.setattr(secrets_memory, "get", get)
    return reads


def test_push_is_sha_pinned_behind_the_real_interlock(db, office_run, secrets_memory, monkeypatch):
    run = office_run["run"]
    _grants(db, run)
    seen: dict = {}
    real = bitbucket_broker.push_from_mirror

    def to_local_bare(**kw):  # the network remote is replaced by a local bare repo; everything else is real
        seen.update(kw)
        return real(**{**kw, "remote_url": str(office_run["bare"])})

    monkeypatch.setattr(bitbucket_broker, "push_from_mirror", to_local_bare)
    res = broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                  repo_full_name=REPO, expected_head_sha=office_run["head"])
    db.commit()
    assert res["ok"] and res["status"] == "PUSHED" and res["head_sha"] == office_run["head"], res
    assert res["provider"] == "bitbucket"
    assert seen["remote_url"] == "https://bitbucket.org/company_ws/svc.git"
    assert seen["expected_head"] == office_run["head"] and seen["basic_user"] == "x-token-auth"
    assert seen["token"] == ACCESS  # handed to the one git child only
    assert _git(office_run["bare"], "for-each-ref", "--format=%(refname)").split() == [f"refs/heads/{BRANCH}"]
    assert _git(office_run["bare"], "rev-parse", f"refs/heads/{BRANCH}") == office_run["head"]
    se = db.scalar(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run.id))
    assert se.status == "SUCCEEDED" and se.target["provider"] == "bitbucket" and ACCESS not in json.dumps(se.target)
    interlock = db.scalars(select(AuditEvent).where(AuditEvent.action == "policy.interlock.check")).all()
    assert interlock and interlock[-1].result == "SUCCESS" and interlock[-1].target_type == "bitbucket"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.bitbucket.push_branch"))


def test_push_without_company_authorisation_is_denied_before_the_credential(db, office_run, secrets_memory,
                                                                            monkeypatch):
    from aeos.models.decisions import Decision
    from aeos.policy import facade as policy

    run = office_run["run"]
    _grants(db, run)
    auth = db.scalar(select(Decision).where(Decision.target_type == "office_policy"))
    policy.invalidate_decisions(db, target_type="office_policy", target_id="OFFICE", reason="authorisation revoked")
    db.commit()
    assert db.get(Decision, auth.id).status != "ACTIVE"
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    with pytest.raises((PolicyDenied, Blocked)) as e:
        broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                repo_full_name=REPO, expected_head_sha=office_run["head"])
    assert e.value.code in ("INTERLOCK_DENIED", "BITBUCKET_DEPLOYMENT_UNKNOWN")
    assert reads == []  # never read the OFFICE credential
    assert _git(office_run["bare"], "for-each-ref", "--format=%(refname)") == ""


def _reauthorise_for(db, owner_id, approved_targets):
    """Replace the company authorisation by one that approves other targets (W6-157)."""
    from aeos.models.identity import User
    from aeos.policy import facade as policy

    policy.invalidate_decisions(db, target_type="office_policy", target_id="OFFICE", reason="re-scoped")
    db.commit()
    _company_authorisation(db, db.get(User, owner_id), approved_targets=approved_targets)


@pytest.mark.parametrize("approved", [
    [],  # UNDECIDED: the company named no Bitbucket target
    [{"provider": "bitbucket", "workspace": "other_ws"}],
    [{"provider": "bitbucket", "workspace": "company_ws", "root_id": "company_ws/other"}],
])
def test_push_outside_the_company_approved_targets_is_denied_before_the_credential(db, office_run, secrets_memory,
                                                                                   monkeypatch, approved):
    run = office_run["run"]
    _grants(db, run)
    _reauthorise_for(db, run.requested_by, approved)
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    with pytest.raises(PolicyDenied) as e:
        broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                repo_full_name=REPO, expected_head_sha=office_run["head"])
    assert e.value.code == "INTERLOCK_DENIED"
    assert any(r.startswith("OFFICE_TARGET_NOT_AUTHORISED") for r in e.value.details["reasons"])
    assert reads == [] and _git(office_run["bare"], "for-each-ref", "--format=%(refname)") == ""


def test_reconcile_and_readiness_refuse_a_target_the_company_did_not_approve(db, office_run, secrets_memory,
                                                                            monkeypatch):
    run = office_run["run"]
    _grants(db, run)
    record_push(db, run, repo=REPO, provider="bitbucket")  # the broker pushed this head first
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(f"{API}/pullrequests").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get(f"{API}/pullrequests").mock(return_value=httpx.Response(200, json={"values": []}))
        res = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                         title="t", body="b")
    assert res["status"] == "UNKNOWN"
    _reauthorise_for(db, run.requested_by, [{"provider": "bitbucket", "workspace": "other_ws"}])
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as e:
            broker.reconcile_side_effect(db, side_effect_id=res["side_effect_id"], actor="delivery")
        assert not mock.calls
    assert e.value.code == "OFFICE_TARGET_NOT_AUTHORISED" and reads == []
    r = bitbucket.office_delivery_readiness(db, office_run["project"])
    assert r.status == "BLOCKED" and any("bukan target yang disetujui perusahaan" in x for x in r.reasons)


def test_push_refused_for_disabled_data_center_unverified_or_personal(db, office_run, secrets_memory, monkeypatch):
    run = office_run["run"]
    _grants(db, run)
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    integ = db.scalar(select(ProjectIntegration).where(ProjectIntegration.provider == "bitbucket"))
    ref = db.scalar(select(RepositoryReference).where(RepositoryReference.full_name == REPO))

    def attempt() -> str:
        with pytest.raises((PolicyDenied, Blocked)) as e:
            broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                    repo_full_name=REPO)
        return e.value.code

    integ.config = {"enabled": False, "deployment": "CLOUD"}
    db.commit()
    assert attempt() == "OFFICE_ADAPTER_DISABLED"
    integ.config = {"enabled": True, "deployment": "DATA_CENTER"}
    db.commit()
    assert attempt() == "BITBUCKET_DC_NOT_SUPPORTED"
    integ.config = {"enabled": True, "deployment": "CLOUD"}
    ref.verification_status = "UNVERIFIED"
    db.commit()
    assert attempt() == "REPO_NOT_VERIFIED"
    ref.verification_status = "VERIFIED"
    integ.root_id = "company_ws/other"
    db.commit()
    assert attempt() == "TARGET_MISMATCH"
    assert reads == []
    with pytest.raises(PolicyDenied) as e:  # a PERSONAL run never reaches the OFFICE credential
        bitbucket_broker.office_target(db, WorkerRun(project_id=run.project_id, domain="PERSONAL"), REPO)
    assert e.value.code == "DOMAIN_CREDENTIAL"


# ------------------------------------------------------------------- owner rule: company identity, no AI (inv. 11)
def _identities(monkeypatch, **providers) -> None:
    from aeos.core import config as config_mod

    monkeypatch.setenv("AEOS_COMMIT_IDENTITIES", json.dumps(providers))
    config_mod.reset_settings_cache()


def test_push_without_the_company_identity_is_refused_before_the_credential(db, office_run, secrets_memory,
                                                                           monkeypatch):
    from tests.conftest import TEST_GIT_EMAIL

    _identities(monkeypatch, github={"name": "Test", "email": TEST_GIT_EMAIL})  # the GitHub identity never covers it
    run = office_run["run"]
    _grants(db, run)
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    with pytest.raises(PolicyDenied) as e:
        broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                repo_full_name=REPO, expected_head_sha=office_run["head"])
    assert e.value.code == "COMMIT_IDENTITY_MISSING" and "commit_identities.bitbucket" in e.value.message
    assert reads == [] and _git(office_run["bare"], "for-each-ref", "--format=%(refname)") == ""
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.bitbucket.push_branch",
                                              AuditEvent.result == "DENIED"))
    readiness = bitbucket.office_delivery_readiness(db, office_run["project"])
    assert readiness.status != "AVAILABLE" and any("commit_identities.bitbucket" in r for r in readiness.reasons)


def test_push_whose_commits_carry_the_github_identity_is_refused_on_bitbucket(db, office_run, monkeypatch):
    """The two identities are different addresses: a candidate committed with the GitHub identity is not the company
    identity and never reaches Bitbucket (COMMIT_IDENTITY_MISMATCH, audited, nothing pushed)."""
    from tests.conftest import TEST_GIT_EMAIL

    _identities(monkeypatch, github={"name": "Test", "email": TEST_GIT_EMAIL},
                bitbucket={"name": "Test", "email": "owner@company.example.com"})
    run = office_run["run"]
    _grants(db, run)
    real = bitbucket_broker.push_from_mirror
    monkeypatch.setattr(bitbucket_broker, "push_from_mirror",
                        lambda **kw: real(**{**kw, "remote_url": str(office_run["bare"])}))
    with pytest.raises(PolicyDenied) as e:
        broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                repo_full_name=REPO, expected_head_sha=office_run["head"])
    assert e.value.code == "COMMIT_IDENTITY_MISMATCH" and "commit_identities.bitbucket" in e.value.message
    assert _git(office_run["bare"], "for-each-ref", "--format=%(refname)") == ""
    se = db.scalar(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run.id))
    assert se.status == "FAILED"
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.bitbucket.push_branch",
                                             AuditEvent.result == "DENIED"))
    assert row.details["code"] == "COMMIT_IDENTITY_MISMATCH" and row.details["provider"] == "bitbucket"


ATTRIBUTION_TRAILERS = [
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)",
    "AEOS-Route: worker.test-free",
]


@pytest.mark.parametrize("trailer", ATTRIBUTION_TRAILERS)
def test_push_whose_commit_carries_ai_attribution_is_refused_on_bitbucket(db, office_run, monkeypatch, trailer):
    """The Bitbucket path runs the same commit gate as GitHub: a commit authored and committed by the configured
    company identity but whose message carries AI attribution never reaches Bitbucket (AI_ATTRIBUTION_IN_COMMIT,
    audited, outbox FAILED, nothing pushed)."""
    from tests.conftest import TEST_GIT_EMAIL, TEST_GIT_NAME

    _identities(monkeypatch, github={"name": TEST_GIT_NAME, "email": TEST_GIT_EMAIL},
                bitbucket={"name": TEST_GIT_NAME, "email": TEST_GIT_EMAIL})
    src, run = office_run["src"], office_run["run"]
    (src / "feature.txt").write_text("more work\n", encoding="utf-8")
    _git(src, "add", "-A")
    subprocess.run(["git", "-c", f"user.name={TEST_GIT_NAME}", "-c", f"user.email={TEST_GIT_EMAIL}", "-c",
                    "commit.gpgsign=false", "commit", "-q", "-m", f"feat: more work\n\n{trailer}"], cwd=src,
                   check=True, capture_output=True)
    head = _git(src, "rev-parse", "HEAD")
    run.head_commit = head
    record_claude_review(db, run)  # reviewed by Claude: the commit gate still refuses the attribution
    db.commit()
    _grants(db, run)
    real = bitbucket_broker.push_from_mirror
    monkeypatch.setattr(bitbucket_broker, "push_from_mirror",
                        lambda **kw: real(**{**kw, "remote_url": str(office_run["bare"])}))
    with pytest.raises(PolicyDenied) as e:
        broker.push_task_branch(db, run_id=run.id, worktree=str(src), branch=BRANCH, repo_full_name=REPO,
                                expected_head_sha=head)
    assert e.value.code == "AI_ATTRIBUTION_IN_COMMIT"
    assert _git(office_run["bare"], "for-each-ref", "--format=%(refname)") == ""
    se = db.scalar(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run.id))
    assert se.status == "FAILED" and se.commit_sha == head
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.bitbucket.push_branch",
                                             AuditEvent.result == "DENIED"))
    assert row.details["code"] == "AI_ATTRIBUTION_IN_COMMIT" and row.details["provider"] == "bitbucket"
    assert all(p["code"] != "COMMIT_IDENTITY_MISMATCH" for p in row.details["problems"])  # the identity matched


def test_bitbucket_pr_with_ai_attribution_is_refused_before_the_credential(db, office_run, secrets_memory,
                                                                          monkeypatch):
    run = office_run["run"]
    _grants(db, run)
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as e:
            broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                       title="[AEOS] t", body="evidence\n\n\U0001F916 Generated with Claude Code")
        assert not mock.calls
    assert e.value.code == "AI_ATTRIBUTION_IN_TEXT" and e.value.details["field"] == "description"
    assert reads == []


@pytest.mark.parametrize("branch", ["main", "aeos/main", "feature/x", f"{BRANCH}:refs/heads/main", "+aeos/x"])
def test_push_never_targets_main_or_a_non_task_branch(db, office_run, branch):
    _grants(db, office_run["run"], branch=BRANCH)
    with pytest.raises(PolicyDenied):
        broker.push_task_branch(db, run_id=office_run["run"].id, worktree=str(office_run["src"]), branch=branch,
                                repo_full_name=REPO)


@pytest.mark.parametrize("remote", ["https://evil.example/company_ws/svc.git", "https://bitbucket.org/company_ws/other.git",
                                    "https://bitbucket.org.evil.example/company_ws/svc.git"])
def test_push_credential_never_goes_to_another_host_or_repository(db, office_run, secrets_memory, monkeypatch,
                                                                  remote):
    """The office/bitbucket credential is only handed to the repository's own Bitbucket Cloud remote: a
    caller-chosen remote_url on another host/repository is refused before the credential is read."""
    run = office_run["run"]
    _grants(db, run)
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    pushed: list = []
    monkeypatch.setattr(bitbucket_broker, "push_from_mirror", lambda **kw: pushed.append(kw) or {"ok": True})
    with pytest.raises(PolicyDenied) as e:  # git_broker is the entry point that takes a remote_url
        git_broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                    repo_full_name=REPO, remote_url=remote)
    assert e.value.code == "REMOTE_REJECTED"
    assert reads == [] and pushed == []
    assert bitbucket_broker.pinned_remote(REPO, None) == "https://bitbucket.org/company_ws/svc.git"
    assert bitbucket_broker.pinned_remote(REPO, "https://bitbucket.org/Company_WS/svc") == \
        "https://bitbucket.org/Company_WS/svc"
    assert bitbucket_broker.pinned_remote(REPO, str(office_run["bare"])) == str(office_run["bare"])


def test_draft_pr_is_created_once_behind_the_interlock(db, office_run):
    run = office_run["run"]
    _grants(db, run)
    record_push(db, run, repo=REPO, provider="bitbucket")  # the broker pushed this head first
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.post(f"{API}/pullrequests").mock(return_value=httpx.Response(201, json={
            "id": 5, "links": {"html": {"href": "https://bitbucket.org/company_ws/svc/pull-requests/5"}},
            "source": {"commit": {"hash": office_run["head"][:12]}}}))
        res = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                         title="[AEOS] t", body="evidence: NOT-PROVEN")
        sent = json.loads(route.calls[0].request.content)
        assert route.calls[0].request.headers["authorization"] == AUTH
        assert len(mock.calls) == 1
    assert sent["draft"] is True and sent["close_source_branch"] is False
    assert sent["source"] == {"branch": {"name": BRANCH}} and sent["destination"] == {"branch": {"name": "main"}}
    assert res["ok"] and res["number"] == "5" and res["head_sha"] == office_run["head"] and ACCESS not in str(res)
    again = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                       title="t", body="b")
    assert again["status"] == "EXISTS"  # idempotent outbox: no second POST


def test_pr_timeout_is_reconciled_or_unknown_and_reconcile_is_read_only(db, office_run):
    run = office_run["run"]
    _grants(db, run)
    record_push(db, run, repo=REPO, provider="bitbucket")  # the broker pushed this head first
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(f"{API}/pullrequests").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get(f"{API}/pullrequests").mock(return_value=httpx.Response(200, json={"values": []}))
        res = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                         title="t", body="b")
    assert not res["ok"] and res["status"] == "UNKNOWN"
    db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        listed = mock.get(f"{API}/pullrequests").mock(return_value=httpx.Response(200, json={"values": [
            {"id": 9, "links": {"html": {"href": "u"}}}]}))
        out = broker.reconcile_side_effect(db, side_effect_id=res["side_effect_id"], actor="delivery")
        assert all(c.request.method == "GET" for c in mock.calls)
        q = listed.calls[0].request.url.params
        assert q["q"] == f'source.branch.name="{BRANCH}" AND destination.branch.name="main"'
    assert out["status"] == "SUCCEEDED" and db.get(BrokerSideEffect, res["side_effect_id"]).external_id == "9"


def test_pr_retry_after_unknown_reads_first_and_never_posts_a_second_pr(db, office_run):
    """§50 "reconcile before any retry": after a timed-out (UNKNOWN) creation the next attempt reads Bitbucket;
    an unreadable provider keeps it UNKNOWN without a POST, an existing PR is adopted without a POST."""
    run = office_run["run"]
    _grants(db, run)
    record_push(db, run, repo=REPO, provider="bitbucket")  # the broker pushed this head first
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(f"{API}/pullrequests").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get(f"{API}/pullrequests").mock(return_value=httpx.Response(200, json={"values": []}))
        first = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                           title="t", body="b")
    assert first["status"] == "UNKNOWN"
    db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        post = mock.post(f"{API}/pullrequests").mock(return_value=httpx.Response(201, json={"id": 77}))
        mock.get(f"{API}/pullrequests").mock(return_value=httpx.Response(503, json={}))
        again = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                           title="t", body="b")
        assert not post.called  # the earlier outcome is still unknown: never a blind second POST
    assert again["status"] == "UNKNOWN" and not again["ok"]
    db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        post = mock.post(f"{API}/pullrequests").mock(return_value=httpx.Response(201, json={"id": 77}))
        listed = mock.get(f"{API}/pullrequests").mock(return_value=httpx.Response(200, json={"values": [
            {"id": 9, "links": {"html": {"href": "https://bitbucket.org/company_ws/svc/pull-requests/9"}},
             "source": {"commit": {"hash": run.head_commit[:12]}}}]}))
        done = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                          title="t", body="b")
        assert not post.called and set(listed.calls[0].request.url.params.get_list("state")) == {
            "OPEN", "MERGED", "DECLINED", "SUPERSEDED"}
    assert done["ok"] and done["number"] == "9" and done["side_effect_id"] == first["side_effect_id"]


def test_reconcile_without_company_authorisation_never_reads_the_office_credential(db, office_run, secrets_memory,
                                                                                    monkeypatch):
    """§23 OFFICE default deny covers reads: a reconciliation after the company authorisation was withdrawn reads
    neither the office/bitbucket credential nor Bitbucket, and the recorded outcome stays UNKNOWN."""
    from aeos.policy import facade as policy

    run = office_run["run"]
    _grants(db, run)
    record_push(db, run, repo=REPO, provider="bitbucket")  # the broker pushed this head first
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(f"{API}/pullrequests").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get(f"{API}/pullrequests").mock(return_value=httpx.Response(200, json={"values": []}))
        res = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                         title="t", body="b")
    assert res["status"] == "UNKNOWN"
    policy.invalidate_decisions(db, target_type="office_policy", target_id="OFFICE", reason="authorisation withdrawn")
    db.commit()
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as e:
            broker.reconcile_side_effect(db, side_effect_id=res["side_effect_id"], actor="delivery")
        assert not mock.calls
    assert e.value.code == "OFFICE_NOT_AUTHORISED" and reads == []
    assert db.get(BrokerSideEffect, res["side_effect_id"]).status == "UNKNOWN"


def test_pr_base_and_head_guards(db, office_run):
    run = office_run["run"]
    _grants(db, run)
    for head, base in ((BRANCH, "aeos/other"), ("main", "main"), (BRANCH, 'main" OR "1')):
        with pytest.raises(PolicyDenied):
            broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=head, base=base, title="t",
                                       body="b")


def test_credential_shapes_and_refusals():
    c = bitbucket_broker.parse_credential(json.dumps({"access_token": ACCESS}))
    assert c.api_header == AUTH and c.git_user == "x-token-auth" and ACCESS not in repr(c)
    c = bitbucket_broker.parse_credential(json.dumps({"email": "dev@example.invalid", "api_token": "tok-123456"}))
    assert c.api_header.startswith("Basic ") and c.git_user == "x-bitbucket-api-token-auth"
    with pytest.raises(Blocked):
        bitbucket_broker.parse_credential(json.dumps({"email": "dev@example.invalid"}))


# ------------------------------------------------------------------------------------------- static guards


def _http_calls(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "post", "put", "patch", "delete", "request", "get", "send"):
            out.append((node.func.attr, node.lineno))
    return out


def test_bitbucket_broker_has_one_write_and_no_integration_endpoint():
    """U-09: the only Bitbucket REST write is the PR creation POST; there is no merge/decline/approve path."""
    path = Path(bitbucket_broker.__file__)
    text = path.read_text(encoding="utf-8")
    writes = [c for c in _http_calls(path) if c[0] in ("post", "put", "patch", "delete", "request")]
    assert len(writes) == 1 and writes[0][0] == "post", writes
    for needle in ("/merge", "/decline", "/approve", "/request-changes", "/branches", "--force", '"+"'):
        assert needle not in text, needle
    assert "force" not in {n.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Name)}


def test_bitbucket_read_side_is_get_only():
    import aeos.publisher.providers.atlassian as atl

    reader = Path(bitbucket.__file__)
    assert not [c for c in _http_calls(reader) if c[0] in ("post", "put", "patch", "delete", "request")]
    src = Path(atl.__file__).read_text(encoding="utf-8")
    cls = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef) and n.name == "BitbucketProvider")
    methods = [m for m in ast.walk(cls) if isinstance(m, ast.Call) and getattr(m.func, "id", "") == "send"]
    assert methods and all(isinstance(m.args[1], ast.Constant) and m.args[1].value == "GET" for m in methods)
    assert not any(hasattr(atl.BitbucketProvider, m) for m in ("merge", "decline", "approve", "execute", "push"))


def test_git_broker_hands_office_runs_to_bitbucket_before_github_validation(db):
    run = WorkerRun(project_id="p", domain="OFFICE", branch=BRANCH)
    assert git_broker.is_bitbucket_run(db, run, REPO)
    assert not git_broker.is_bitbucket_run(db, WorkerRun(project_id="p", domain="PERSONAL"), "o/r")


def test_capability_grant_expiry_and_one_shot_still_apply(db, office_run, monkeypatch):
    run = office_run["run"]
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain="OFFICE", kind="GIT_BRANCH",
                       scope={"repo": REPO, "branch": BRANCH}, ttl_seconds=300, max_requests=1)
    db.commit()
    monkeypatch.setattr(bitbucket_broker, "push_from_mirror", lambda **kw: {"ok": True, "stage": "push",
                                                                            "commit": run.head_commit})
    broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH, repo_full_name=REPO)
    with pytest.raises(PolicyDenied) as e:
        broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                repo_full_name=REPO)
    assert e.value.code == "CAPABILITY_EXHAUSTED"
    from aeos.models.routing import CapabilityGrant

    g = db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id))
    g.expires_at = utcnow() - timedelta(seconds=1)
    g.used_requests = 0
    db.commit()
    with pytest.raises(PolicyDenied) as e:  # an expired grant never authorises a push
        broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                repo_full_name=REPO)
    assert e.value.code == "CAPABILITY_MISSING"


def test_the_attribution_samples_of_this_file_are_attribution_for_the_canonical_guard():
    """The samples of this file only prove the Bitbucket refusal while the canonical guard (aeos.core.attribution)
    still calls them attribution; an ordinary trailer stays clean."""
    for trailer in ATTRIBUTION_TRAILERS + ["[AEOS] t\n\nevidence\n\n\U0001F916 Generated with Claude Code"]:
        assert find_ai_attribution(f"aeos: candidate\n\n{trailer}"), trailer
    assert not find_ai_attribution("aeos: candidate\n\nRefs: nothing")


# ------------------------------------------------------------------------------------------- owner rule R44
# (2026-10-28, ADR-0132; paraphrased: all OpenCode and OpenRouter work must pass a Claude review): the Bitbucket
# push and PR of a worker run need the run's latest Claude checkpoint review to be a PASS of exactly the head being
# published, before any grant, credential or request (the GitHub path: tests/unit/test_broker_publication_review.py).
def _new_head(office_run) -> str:
    from tests.conftest import TEST_GIT_EMAIL, TEST_GIT_NAME

    src = office_run["src"]
    (src / "later.txt").write_text("a later candidate of the same run\n", encoding="utf-8")
    _git(src, "add", "-A")
    # amended: the new head is again the single candidate commit on the base (only the reviewed head differs)
    subprocess.run(["git", "-c", f"user.name={TEST_GIT_NAME}", "-c", f"user.email={TEST_GIT_EMAIL}", "-c",
                    "commit.gpgsign=false", "commit", "-q", "--amend", "-m", "aeos: candidate"], cwd=src, check=True,
                   capture_output=True)
    return _git(src, "rev-parse", "HEAD")


@pytest.mark.parametrize("case,message,reason", [
    ("none", "no Claude checkpoint review exists for this run", "NO_REVIEW"),
    ("older", "the latest Claude checkpoint review covers head {old}, not this head", "OTHER_HEAD"),
    ("fail", "the latest Claude checkpoint review verdict is FAIL", "NOT_PASS"),
])
def test_bitbucket_push_and_pr_need_a_claude_pass_of_the_head(db, office_run, secrets_memory, monkeypatch, case,
                                                              message, reason):
    run = office_run["run"]
    old = run.head_commit
    if case == "none":
        db.execute(delete(ReviewRecord).where(ReviewRecord.run_id == run.id))
    elif case == "older":
        run.head_commit = _new_head(office_run)  # the fixture's PASS now covers the head before this commit
    else:
        record_claude_review(db, run, verdict="FAIL")
    db.commit()
    _grants(db, run)
    message = message.format(old=old[:12])
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    real = bitbucket_broker.push_from_mirror
    monkeypatch.setattr(bitbucket_broker, "push_from_mirror",
                        lambda **kw: real(**{**kw, "remote_url": str(office_run["bare"])}))
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as pushed:
            broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                    repo_full_name=REPO, expected_head_sha=run.head_commit)
        with pytest.raises(PolicyDenied) as opened:
            broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main", title="t",
                                       body="b")
        assert not mock.calls, "no request reached Bitbucket"
    for e in (pushed, opened):
        assert e.value.code == "REVIEW_REQUIRED" and message in e.value.message, e.value.message
        assert e.value.details["reason"] == reason and e.value.details["head_commit"] == run.head_commit
    assert reads == [], "the OFFICE credential is never read for a refused write"
    assert _git(office_run["bare"], "for-each-ref", "--format=%(refname)") == ""
    assert db.scalar(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run.id)) is None
    for action in ("broker.bitbucket.push_branch", "broker.bitbucket.create_pr"):
        row = db.scalar(select(AuditEvent).where(AuditEvent.action == action, AuditEvent.result == "DENIED"))
        assert row is not None and row.details["code"] == "REVIEW_REQUIRED" and row.details["reason"] == reason
    # near miss: Claude passes exactly this head -> the same push and PR go through
    record_claude_review(db, run)
    db.commit()
    res = broker.push_task_branch(db, run_id=run.id, worktree=str(office_run["src"]), branch=BRANCH,
                                  repo_full_name=REPO, expected_head_sha=run.head_commit)
    assert res["ok"] and res["head_sha"] == run.head_commit, res
    assert _git(office_run["bare"], "rev-parse", f"refs/heads/{BRANCH}") == run.head_commit
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(f"{API}/pullrequests").mock(return_value=httpx.Response(201, json={
            "id": 6, "links": {"html": {"href": "https://bitbucket.org/company_ws/svc/pull-requests/6"}},
            "source": {"commit": {"hash": run.head_commit[:12]}}}))
        pr = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                        title="t", body="b")
        assert route.called
    assert pr["ok"] and pr["head_sha"] == run.head_commit


def test_bitbucket_pr_needs_a_recorded_push_of_exactly_this_head(db, office_run, secrets_memory, monkeypatch):
    """ADR-0132 decision 3: a PR is opened only for a branch the broker pushed with this head (the push is where the
    reviewed history is checked) - and a PR whose reported head is another commit is not counted."""
    run = office_run["run"]
    _grants(db, run)
    reads = _count_secret_reads(monkeypatch, secrets_memory)
    record_push(db, run, repo=REPO, provider="bitbucket", head="0" * 40)  # another head
    record_push(db, run, repo=REPO, head=run.head_commit)  # this head, but a GitHub push record
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as e:
            broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main", title="t",
                                       body="b")
        assert not mock.calls
    assert e.value.code == "REVIEW_REQUIRED" and e.value.details["reason"] == "NOT_PUSHED"
    assert f"no reviewed push of head {run.head_commit[:12]} to {REPO}:{BRANCH}: NOT_PUSHED" in e.value.message
    assert reads == []
    record_push(db, run, repo=REPO, provider="bitbucket")  # near miss: the broker's push of exactly this head
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(f"{API}/pullrequests").mock(return_value=httpx.Response(201, json={
            "id": 8, "links": {"html": {"href": "https://bitbucket.org/company_ws/svc/pull-requests/8"}},
            "source": {"commit": {"hash": "abcdef123456"}}}))
        moved = broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                           title="t", body="b")
    assert not moved["ok"] and moved["status"] == "FAILED"
    assert moved["error"].startswith("HEAD_MISMATCH: the pull request's head abcdef123456 is not the reviewed head")
