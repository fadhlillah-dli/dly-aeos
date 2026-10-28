"""OFFICE PR delivery end to end (master §04 "OFFICE PR: Bitbucket pull request", §31; ADR-0111).

prepare-PR for an accepted OFFICE task goes through the real Bitbucket broker (grants, outbox, sha-pinned push
to a local bare remote, draft PR over mocked HTTP) behind the interlock, needs the OFFICE integration gate, and
is refused honestly while disabled or on Data Center. PR sync reads Bitbucket (PR, full hashes, diffstat, commit
statuses) and feeds G5/G7 with the owner's recorded Bitbucket release approvers."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.broker import bitbucket_broker
from aeos.core.errors import Blocked
from aeos.delivery import gates, pr_bundle, sync
from aeos.models.decisions import Decision
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.portfolio import ProjectIntegration, RepositoryReference
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import TaskExecution
from aeos.policy.facade import InterlockResult
from aeos.verification import acceptance
from tests.conftest import create_admin
from tests.unit.review_gate_support import record_claude_review
from tests.unit.test_verification_support import (
    NR_COVERED,
    FakeWorkforce,
    git,
    install_module,
    install_planning,
    make_candidate,
    make_world,
    record_verification,
    requires_git,
)

REPO = "company_ws/svc"
API = "https://api.bitbucket.org/2.0/repositories/company_ws/svc"
BRANCH = "aeos/t1-attempt-1"
ACCESS = "bbat-" + "Z9" * 20
pytestmark = requires_git


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def _office_setup(db, project, *, deployment="CLOUD", authorise=True):
    owner = create_admin(db)
    if authorise:
        from aeos.policy import facade as policy

        policy.record_decision(db, kind="POLICY", actor_user_id=owner.id, target_type="office_policy",
                               target_id="OFFICE", domain="OFFICE", rationale="company authorisation (test)",
                               details={"office_authorization": True, "company_reference": "CHG-42",
                                        "allowed_providers": ["bitbucket"],
                                        "approved_targets": [{"provider": "bitbucket", "workspace": "company_ws"}],
                                        "office_policy": {"atlassian_deployment": "CLOUD"}})
    db.add(RepositoryReference(project_id=project.id, provider="BITBUCKET", full_name=REPO, default_branch="main",
                               verification_status="VERIFIED", provider_repo_id="{u1}"))
    db.add(ProjectIntegration(project_id=project.id, domain="OFFICE", provider="bitbucket", purpose="code",
                              root_id=REPO, config={"enabled": True, "deployment": deployment}))
    db.commit()
    return owner


@pytest.fixture
def office_accepted(db, wf, monkeypatch, settings, secrets_memory, tmp_path):
    w = make_world(db, domain="OFFICE", is_platform=False, project_key="office-app")
    owner = _office_setup(db, w.project)
    secrets_memory.put("office/bitbucket", json.dumps({"access_token": ACCESS}), actor="test")
    cand, base, head = make_candidate(settings, w, head_files={"backend/extra.py": "X = 1\n"})
    git(cand, "branch", BRANCH, head)
    w.run.workspace_ref = str(cand)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, head=head, base=base)
    # the Claude review PASS bound to this head, as review_run records it (owner rule R44, ADR-0132)
    record_claude_review(db, w.run, head=head, artifact_hash=report.environment["artifact_hash"],
                         non_regression=NR_COVERED)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    db.add(TaskExecution(task_id=w.task.id, project_id=w.project.id, domain="OFFICE", state="DONE",
                         accepted_run_id=w.run.id, plan_id=w.plan.id, plan_version_id=w.version.id,
                         task_key=w.task.task_key))
    db.commit()
    assert acc.acceptance_status == "ACCEPTED"
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    return w, acc, owner, bare


def _doctor(monkeypatch, status="READY"):
    asked: list[str] = []
    from aeos.doctor.facade import CapabilityState

    def capability_state(session, capability, *, domain=None, project_id=None, **_kw):
        asked.append(str(getattr(capability, "value", capability)))
        return CapabilityState(capability=capability, status=status, reasons=[] if status == "READY" else ["no"])

    install_module(monkeypatch, "aeos.doctor.capabilities", capability_state=capability_state,
                   invalidate=lambda session, *, invalidator, reason="": 0)
    return asked


def _interlock(monkeypatch, allowed=True):
    seen = []

    def check_external_write(session, intent):
        seen.append(intent)
        return InterlockResult(allowed=allowed, reasons=[] if allowed else ["OFFICE_NOT_AUTHORISED: test"])

    install_module(monkeypatch, "aeos.policy.interlock", check_external_write=check_external_write)
    return seen


def test_prepare_pr_for_office_goes_through_the_bitbucket_broker(db, office_accepted, monkeypatch):
    w, acc, owner, bare = office_accepted
    asked = _doctor(monkeypatch)
    intents = _interlock(monkeypatch)
    real = bitbucket_broker.push_from_mirror
    monkeypatch.setattr(bitbucket_broker, "push_from_mirror", lambda **kw: real(**{**kw, "remote_url": str(bare)}))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.post(f"{API}/pullrequests").mock(return_value=httpx.Response(201, json={
            "id": 5, "links": {"html": {"href": "https://bitbucket.org/company_ws/svc/pull-requests/5"}},
            "source": {"commit": {"hash": acc.head_commit[:12]}}}))
        out = pr_bundle.prepare_pr(db, w.task.id, actor_user_id=owner.id)
        db.commit()
        body = json.loads(route.calls[0].request.content)
    assert asked == ["CAN_USE_OFFICE_INTEGRATIONS"]  # the OFFICE gate, not the PERSONAL CAN_PREPARE_PR
    # interlock: prepare-PR intent + the broker's own push and PR intents, all on the bitbucket provider
    assert [i.provider for i in intents] == ["bitbucket"] * 3
    assert [i.operation for i in intents] == ["create_pr", "push_branch", "create_pr"]
    assert all(i.target["repo"] == REPO for i in intents)
    assert git(bare, "rev-parse", f"refs/heads/{BRANCH}") == acc.head_commit  # exactly the accepted head
    assert body["draft"] is True and "Merge authority: human only" in body["description"]
    pr = db.get(PullRequestRecord, out["pull_request"]["id"])
    assert pr.provider == "BITBUCKET" and pr.number == 5 and pr.repo_full_name == REPO
    assert pr.observed_head_sha == acc.head_commit and pr.url.endswith("/pull-requests/5")
    assert out["merge_authority"] == "HUMAN_ONLY"
    grants = db.scalars(select(CapabilityGrant).where(CapabilityGrant.run_id == w.run.id)).all()
    assert grants and all(not g.active for g in grants)  # the delivery capabilities died with the step


def test_prepare_pr_for_office_is_refused_on_data_center_or_while_disabled(db, wf, monkeypatch, secrets_memory):
    w = make_world(db, domain="OFFICE", is_platform=False, project_key="office-dc")
    _office_setup(db, w.project, deployment="DATA_CENTER")
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "BITBUCKET_DC_NOT_SUPPORTED" and e.value.details["status"] == "NOT_SUPPORTED"
    integ = db.scalar(select(ProjectIntegration).where(ProjectIntegration.project_id == w.project.id))
    integ.config = {"enabled": False, "deployment": "CLOUD"}
    db.commit()
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "OFFICE_DELIVERY_NOT_AVAILABLE" and e.value.details["status"] == "DISABLED"


def test_prepare_pr_for_office_needs_the_office_integration_gate(db, office_accepted, monkeypatch):
    w, acc, owner, _bare = office_accepted
    _doctor(monkeypatch, status="BLOCKED")
    _interlock(monkeypatch)
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id=owner.id)
    assert e.value.code == "CAPABILITY_NOT_READY" and "CAN_USE_OFFICE_INTEGRATIONS" in e.value.message


def _pr(db, w, acc, **kw):
    pr = PullRequestRecord(project_id=w.project.id, domain="OFFICE", task_id=w.task.id, run_id=w.run.id,
                           acceptance_id=acc.id, provider="BITBUCKET", repo_full_name=REPO, number=5, head_ref=BRANCH,
                           base_ref="main", accepted_head_sha=acc.head_commit, initial_base_sha="b" * 40,
                           observed_head_sha=acc.head_commit, observed_base_sha="b" * 40, bundle_hash="sha256:x",
                           state="OPEN", **kw)
    db.add(pr)
    db.commit()
    return pr


def _approvers(db, project, accounts):
    db.add(Decision(kind="POLICY", actor_user_id="owner", actor_kind="HUMAN", target_type="release_approvers",
                    target_id=project.id, project_id=project.id, domain=project.domain,
                    auth_scope={"bitbucket_accounts": accounts}, rationale="owner names the release approvers"))
    db.commit()


def _mock_merged(mock, head, *, closer="557058:owner-1", closer_type="user"):
    merge = "e" * 40
    mock.get(f"{API}/pullrequests/5").mock(return_value=httpx.Response(200, json={
        "id": 5, "state": "MERGED", "source": {"branch": {"name": BRANCH}, "commit": {"hash": head[:12]}},
        "destination": {"branch": {"name": "main"}, "commit": {"hash": "b" * 12}},
        "merge_commit": {"hash": merge[:12]}, "closed_by": {"type": closer_type, "account_id": closer},
        "links": {"html": {"href": "https://bitbucket.org/company_ws/svc/pull-requests/5"}}}))
    for full in (head, "b" * 40, merge):
        mock.get(f"{API}/commit/{full[:12]}").mock(return_value=httpx.Response(200, json={"hash": full}))
    mock.get(f"{API}/commit/{merge}").mock(return_value=httpx.Response(200, json={
        "hash": merge, "parents": [{"hash": "b" * 40}, {"hash": head}]}))
    mock.get(f"{API}/diffstat/{merge}").mock(return_value=httpx.Response(200, json={"values": [
        {"status": "added", "new": {"path": "backend/extra.py"}, "old": None}]}))
    for sha in (head, merge):
        mock.get(f"{API}/commit/{sha}/statuses").mock(return_value=httpx.Response(200, json={"values": [
            {"key": "k1", "name": "backend tests", "state": "SUCCESSFUL", "commit": {"hash": sha}}]}))
    mock.get(f"{API}/pipelines").mock(return_value=httpx.Response(200, json={"values": []}))
    return merge


def test_sync_reads_bitbucket_and_feeds_g5_g7(db, office_accepted, monkeypatch):
    w, acc, _owner, _bare = office_accepted
    pr = _pr(db, w, acc)
    _approvers(db, w.project, ["557058:owner-1"])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        merge = _mock_merged(mock, acc.head_commit)
        out = sync.sync_pull_request(db, pr.id, actor="owner")
        db.commit()
        assert all(c.request.method == "GET" for c in mock.calls)
    assert pr.state == "MERGED" and pr.merged_by_type == "User" and pr.merge_commit_sha == merge
    assert pr.observed_head_sha == acc.head_commit and acc.acceptance_status == "ACCEPTED"
    assert len(out["ci_evidence_ids"]) == 2 and pr.details["merge_delta"]["files"] == ["backend/extra.py"]
    g = {x["gate"]: x for x in gates.compute_gates(db, w.task.id)["gates"]}
    assert g["G5"]["status"] == "PASS" and g["G7"]["status"] == "PASS"
    assert g["G7"]["source"] == "Bitbucket closed_by"


def test_sync_non_human_or_unlisted_merge_is_never_a_release(db, office_accepted, monkeypatch):
    w, acc, _owner, _bare = office_accepted
    pr = _pr(db, w, acc)
    _approvers(db, w.project, ["557058:owner-1"])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_merged(mock, acc.head_commit, closer="557058:someone-else")
        sync.sync_pull_request(db, pr.id, actor="owner")
    assert {x["gate"]: x for x in gates.compute_gates(db, w.task.id)["gates"]}["G7"]["status"] == "FAIL"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_merged(mock, acc.head_commit, closer_type="app_user")
        sync.sync_pull_request(db, pr.id, actor="owner")
    assert pr.merged_by_type == "App"
    assert {x["gate"]: x for x in gates.compute_gates(db, w.task.id)["gates"]}["G7"]["status"] == "FAIL"


def test_sync_new_commits_on_bitbucket_make_acceptance_stale(db, office_accepted):
    w, acc, _owner, _bare = office_accepted
    pr = _pr(db, w, acc)
    new = "9" * 40
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(f"{API}/pullrequests/5").mock(return_value=httpx.Response(200, json={
            "id": 5, "state": "OPEN", "source": {"branch": {"name": BRANCH}, "commit": {"hash": new[:12]}},
            "destination": {"branch": {"name": "main"}, "commit": {"hash": "b" * 12}}}))
        mock.get(f"{API}/commit/{new[:12]}").mock(return_value=httpx.Response(200, json={"hash": new}))
        mock.get(f"{API}/commit/{'b' * 12}").mock(return_value=httpx.Response(200, json={"hash": "b" * 40}))
        mock.get(f"{API}/commit/{new}/statuses").mock(return_value=httpx.Response(200, json={"values": []}))
        mock.get(f"{API}/pipelines").mock(return_value=httpx.Response(200, json={"values": []}))
        out = sync.sync_pull_request(db, pr.id, actor="owner")
    assert acc.acceptance_status == "STALE" and any("acceptance STALE" in n for n in out["notes"])


def test_office_pipeline_reference_maps_bitbucket_pipelines_steps_to_gates(db, office_accepted):
    """§31 "pipeline reference per project, with the domain-appropriate CI": the bitbucket-pipelines/ci
    integration names the exact Pipelines steps per gate; sync records them as provider-ci evidence."""
    w, acc, _owner, _bare = office_accepted
    db.add(ProjectIntegration(project_id=w.project.id, domain="OFFICE", provider="bitbucket-pipelines", purpose="ci",
                              config={"gate_G1": "lint", "gate_G3": "e2e"}))
    db.commit()
    pr = _pr(db, w, acc)
    head = acc.head_commit
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(f"{API}/pullrequests/5").mock(return_value=httpx.Response(200, json={
            "id": 5, "state": "OPEN", "source": {"branch": {"name": BRANCH}, "commit": {"hash": head[:12]}},
            "destination": {"branch": {"name": "main"}, "commit": {"hash": "b" * 12}}}))
        mock.get(f"{API}/commit/{head[:12]}").mock(return_value=httpx.Response(200, json={"hash": head}))
        mock.get(f"{API}/commit/{'b' * 12}").mock(return_value=httpx.Response(200, json={"hash": "b" * 40}))
        mock.get(f"{API}/commit/{head}/statuses").mock(return_value=httpx.Response(200, json={"values": []}))
        mock.get(f"{API}/pipelines").mock(return_value=httpx.Response(200, json={"values": [
            {"uuid": "{p1}", "build_number": 3, "target": {"commit": {"hash": head}}}]}))
        mock.get(f"{API}/pipelines/%7Bp1%7D/steps").mock(return_value=httpx.Response(200, json={"values": [
            {"uuid": "{s1}", "name": "lint", "state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}}},
            {"uuid": "{s2}", "name": "e2e", "state": {"name": "COMPLETED", "result": {"name": "FAILED"}}}]}))
        out = sync.sync_pull_request(db, pr.id, actor="owner")
        db.commit()
    from aeos.models.evidence import EvidenceArtifact
    from aeos.verification import collector

    ev = db.get(EvidenceArtifact, out["ci_evidence_ids"][0])
    data = collector.read_json(ev)
    assert data["gate_mapping"] == "PIPELINE_REFERENCE" and data["provider"] == "bitbucket"
    g = {x["gate"]: x for x in gates.compute_gates(db, w.task.id)["gates"]}
    assert g["G1"]["status"] == "PASS" and g["G3"]["status"] == "FAIL"


def test_sync_is_refused_without_the_company_authorisation(db, office_accepted):
    w, acc, _owner, _bare = office_accepted
    from aeos.policy import facade as policy

    pr = _pr(db, w, acc)
    policy.invalidate_decisions(db, target_type="office_policy", target_id="OFFICE", reason="revoked")
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        out = sync.sync_pull_request(db, pr.id, actor="owner")
        assert not mock.calls
    assert pr.sync_status == "FAILED" and "DISABLED" in out["notes"][0] and pr.state == "OPEN"
