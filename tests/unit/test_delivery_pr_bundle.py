"""PR evidence bundle + PR preparation via the trusted broker (§31, §34, §35), and PR observation
(§50 "PR baru mengubah head atau base terkait" → acceptance STALE). Human-only merge: there is no
merge call anywhere (see test_delivery_no_merge)."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from aeos.core.errors import Blocked, Conflict, PolicyDenied
from aeos.delivery import gates, pr_bundle, provider, sync
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import EvidenceArtifact
from aeos.models.workforce import TaskExecution
from aeos.policy.facade import InterlockResult
from aeos.verification import acceptance
from tests.unit.test_verification_support import (
    FakeWorkforce,
    git,
    install_doctor,
    install_module,
    install_planning,
    make_candidate,
    make_review,
    make_world,
    record_verification,
    remove_module,
    requires_git,
)

REPO = "fadhlillah-dli/dly-aeos"
BRANCH = "aeos/t1-attempt-1"
pytestmark = requires_git


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


@pytest.fixture
def accepted(db, wf, monkeypatch, settings):
    """An accepted task whose candidate is a real git repo: the task branch points at the accepted head."""
    w = make_world(db, repo=REPO)
    cand, base, head = make_candidate(settings, w, head_files={"backend/extra.py": "X = 1\n"})
    git(cand, "branch", BRANCH, head)
    w.run.workspace_ref = str(cand)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, head=head, base=base)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    # what the real scheduler's mark_accepted records (FakeWorkforce only logs the call)
    db.add(TaskExecution(task_id=w.task.id, project_id=w.project.id, domain=w.project.domain, state="DONE",
                         accepted_run_id=w.run.id, plan_id=w.plan.id, plan_version_id=w.version.id,
                         task_key=w.task.task_key))
    db.commit()
    assert acc.acceptance_status == "ACCEPTED"
    return w, acc


class FakeBroker:
    def __init__(self, monkeypatch, *, head_sha: str | None):
        self.calls: list[tuple[str, dict]] = []

        # Integration contract (ADR-0053/0063 amendments 2026-10-18): sha-pinned push with
        # expected_head_sha, results always carry status + head_sha.
        def push_task_branch(session, *, run_id, worktree, branch, repo_full_name, expected_head_sha=None):
            self.calls.append(("push", {"run_id": run_id, "branch": branch, "repo": repo_full_name,
                                        "expected_head_sha": expected_head_sha}))
            return {"status": "PUSHED", "ok": True, "head_sha": head_sha}

        def create_pull_request(session, *, run_id, repo_full_name, head, base, title, body):
            self.calls.append(("pr", {"head": head, "base": base, "title": title, "body": body}))
            return {"status": "CREATED", "ok": True, "number": 12,
                    "html_url": f"https://github.com/{repo_full_name}/pull/12", "head_sha": head_sha,
                    "base_sha": "b" * 40}

        install_module(monkeypatch, "aeos.broker.git_broker", push_task_branch=push_task_branch,
                       create_pull_request=create_pull_request)


def _allow(monkeypatch, allowed=True, reasons=None):
    seen = []

    def check_external_write(session, intent):
        seen.append(intent)
        return InterlockResult(allowed=allowed, reasons=reasons or [])

    install_module(monkeypatch, "aeos.policy.interlock", check_external_write=check_external_write)
    return seen


def test_bundle_contains_hashes_unknowns_rollback_and_human_only_statement(db, accepted):
    w, acc = accepted
    b = pr_bundle.build_pr_bundle(db, w.task.id)
    md = b.markdown
    assert b.bundle_hash.startswith("sha256:")
    for needle in (w.task.id, w.task.plan_version_id, w.task.plan_version_hash, w.task.spec_hash, acc.artifact_hash,
                   acc.head_commit, acc.id, "PASS-CONTROLLED", "### Unknowns / NOT-PROVEN", "### Rollback",
                   "Merge authority: human only", "G5 Integration", "G7 Human release"):
        assert needle in md, needle
    assert "NOT_PROVEN" in md or "NOT-PROVEN" in md
    assert "Provider merge-gate capability not checked yet" in md
    assert b.data["acceptance"]["is_human_approval"] is False
    # deterministic: same records -> same hash
    assert pr_bundle.build_pr_bundle(db, w.task.id).bundle_hash == b.bundle_hash


def test_bundle_refused_without_valid_acceptance(db, wf, monkeypatch, accepted):
    w, acc = accepted
    acceptance.invalidate(db, task_id=w.task.id, reason="PR head changed", actor="t", head_commit="9" * 40)
    with pytest.raises(Blocked) as e:
        pr_bundle.build_pr_bundle(db, w.task.id)
    assert e.value.code == "ACCEPTANCE_REQUIRED"
    w2 = make_world(db, project_key="p2")
    with pytest.raises(Blocked):
        pr_bundle.build_pr_bundle(db, w2.task.id)


def test_prepare_pr_happy_path_uses_broker_only(db, accepted, monkeypatch):
    w, acc = accepted
    install_doctor(monkeypatch, "READY")
    intents = _allow(monkeypatch)
    broker = FakeBroker(monkeypatch, head_sha=acc.head_commit)
    out = pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    db.commit()
    assert [c[0] for c in broker.calls] == ["push", "pr"]
    assert broker.calls[0][1]["branch"].startswith("aeos/")
    assert broker.calls[1][1]["base"] == "main" and "Merge authority: human only" in broker.calls[1][1]["body"]
    assert intents[0].operation == "create_pr" and intents[0].approved_hash == w.task.plan_version_hash
    pr = db.get(PullRequestRecord, out["pull_request"]["id"])
    assert pr.state == "OPEN" and pr.number == 12 and pr.accepted_head_sha == acc.head_commit
    assert pr.initial_base_sha == "b" * 40 and out["merge_authority"] == "HUMAN_ONLY"
    bundle_ev = db.get(EvidenceArtifact, pr.bundle_evidence_id)
    assert bundle_ev.kind == "PR_BUNDLE"
    with pytest.raises(Conflict):
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")


@pytest.mark.parametrize("state,accepted_run", [("BLOCKED", "same"), ("READY", "same"), ("DONE", "other")])
def test_prepare_pr_requires_the_scheduler_acceptance_of_this_run(db, accepted, monkeypatch, state, accepted_run):
    """An ACCEPTED record whose mark_accepted was refused (task BLOCKED after a review block, another
    attempt, reopened) is never delivered: §14 REVIEW -> DONE needs the scheduler's acceptance."""
    from sqlalchemy import select

    w, acc = accepted
    install_doctor(monkeypatch, "READY")
    _allow(monkeypatch)
    broker = FakeBroker(monkeypatch, head_sha=acc.head_commit)
    te = db.scalar(select(TaskExecution).where(TaskExecution.task_id == w.task.id))
    te.state = state
    te.accepted_run_id = w.run.id if accepted_run == "same" else "another-run"
    db.commit()
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "TASK_NOT_DONE" and not broker.calls


@pytest.mark.parametrize("status", ["BLOCKED", "UNKNOWN", "DEGRADED"])
def test_prepare_pr_requires_capability(db, accepted, monkeypatch, status):
    w, acc = accepted
    install_doctor(monkeypatch, status)
    _allow(monkeypatch)
    broker = FakeBroker(monkeypatch, head_sha=acc.head_commit)
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "CAPABILITY_NOT_READY" and not broker.calls


def test_prepare_pr_fails_closed_when_modules_missing(db, accepted, monkeypatch):
    w, _ = accepted
    remove_module(monkeypatch, "aeos.doctor.capabilities")
    with pytest.raises(Blocked):
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    install_doctor(monkeypatch, "READY")
    remove_module(monkeypatch, "aeos.policy.interlock")
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "INTERLOCK_UNAVAILABLE"
    _allow(monkeypatch)
    remove_module(monkeypatch, "aeos.broker.git_broker")
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "BROKER_PUSH_FAILED"


def test_prepare_pr_interlock_denial(db, accepted, monkeypatch):
    w, acc = accepted
    install_doctor(monkeypatch, "READY")
    _allow(monkeypatch, allowed=False, reasons=["bootstrap incomplete"])
    broker = FakeBroker(monkeypatch, head_sha=acc.head_commit)
    with pytest.raises(PolicyDenied) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.details["reasons"] == ["bootstrap incomplete"] and not broker.calls


def test_pushed_head_mismatch_invalidates_acceptance(db, accepted, monkeypatch):
    w, acc = accepted
    install_doctor(monkeypatch, "READY")
    _allow(monkeypatch)
    FakeBroker(monkeypatch, head_sha="7" * 40)
    with pytest.raises(Conflict) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "HEAD_MISMATCH"
    assert acc.acceptance_status == "STALE"


def test_task_branch_required(db, accepted, monkeypatch):
    w, _ = accepted
    w.run.branch = "main"
    db.commit()
    install_doctor(monkeypatch, "READY")
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "BRANCH_NOT_TASK_BRANCH"


def test_task_branch_moved_after_acceptance_is_refused_before_push(db, accepted, monkeypatch):
    """§50 "PR baru mengubah head": the broker pushes the branch tip, so a tip that moved after acceptance
    is refused before anything is pushed, and the acceptance becomes STALE."""
    w, acc = accepted
    install_doctor(monkeypatch, "READY")
    _allow(monkeypatch)
    broker = FakeBroker(monkeypatch, head_sha=acc.head_commit)
    cand = w.run.workspace_ref
    (Path(cand) / "backend" / "late.py").write_text("Y = 2\n", encoding="utf-8")
    git(Path(cand), "add", "-A")
    git(Path(cand), "commit", "-q", "-m", "late change")
    git(Path(cand), "branch", "-f", BRANCH, "HEAD")
    with pytest.raises(Conflict) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "HEAD_MISMATCH" and not broker.calls
    assert acc.acceptance_status == "STALE"


def test_unreadable_task_branch_is_blocked(db, accepted, monkeypatch):
    w, acc = accepted
    install_doctor(monkeypatch, "READY")
    broker = FakeBroker(monkeypatch, head_sha=acc.head_commit)
    w.run.branch = "aeos/does-not-exist"
    db.commit()
    with pytest.raises(Blocked) as e:
        pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert e.value.code == "BRANCH_UNRESOLVED" and not broker.calls


def test_broker_without_pushed_sha_is_reported_not_proven(db, accepted, monkeypatch):
    w, acc = accepted
    install_doctor(monkeypatch, "READY")
    _allow(monkeypatch)
    FakeBroker(monkeypatch, head_sha=None)
    out = pr_bundle.prepare_pr(db, w.task.id, actor_user_id="owner-id")
    assert any("NOT-PROVEN" in n for n in out["notes"])
    assert out["pull_request"]["observed_head_sha"] is None


def _pr(db, w, acc, **kw):
    pr = PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                           acceptance_id=acc.id, repo_full_name=REPO, number=12, head_ref=BRANCH,
                           base_ref="main", accepted_head_sha=acc.head_commit, initial_base_sha="b" * 40,
                           observed_head_sha=acc.head_commit, observed_base_sha="b" * 40, bundle_hash="sha256:x",
                           state="OPEN", **kw)
    db.add(pr)
    db.commit()
    return pr


API = provider.GITHUB_API


def _pull(head, base="b" * 40, merged=False, merged_by=None):
    return {"number": 12, "state": "closed" if merged else "open", "merged": merged, "merged_by": merged_by,
            "merged_at": "2026-10-18T10:00:00Z" if merged else None, "merge_commit_sha": "e" * 40 if merged else None,
            "head": {"sha": head, "ref": "aeos/t1-attempt-1"}, "base": {"sha": base, "ref": "main"},
            "html_url": f"https://github.com/{REPO}/pull/12"}


@respx.mock(assert_all_called=False)
def test_sync_new_commits_make_acceptance_stale(respx_mock, db, accepted, secrets_memory):
    w, acc = accepted
    secrets_memory.put("personal/github", "a-test-token-value", actor="t")
    pr = _pr(db, w, acc)
    new_head = "9" * 40
    respx_mock.get(f"{API}/repos/{REPO}/pulls/12").respond(200, json=_pull(head=new_head))
    respx_mock.get(f"{API}/repos/{REPO}/commits/{new_head}/check-runs").respond(200, json={"check_runs": []})
    out = sync.sync_pull_request(db, pr.id, actor="owner")
    db.commit()
    assert acc.acceptance_status == "STALE"
    assert any("acceptance STALE" in n for n in out["notes"])
    assert all(c.request.method == "GET" for c in respx_mock.calls)


@respx.mock(assert_all_called=False)
def test_sync_base_moved_makes_acceptance_stale(respx_mock, db, accepted, secrets_memory):
    w, acc = accepted
    secrets_memory.put("personal/github", "a-test-token-value", actor="t")
    pr = _pr(db, w, acc)
    respx_mock.get(f"{API}/repos/{REPO}/pulls/12").respond(200, json=_pull(acc.head_commit, base="c" * 40))
    respx_mock.get(f"{API}/repos/{REPO}/commits/{acc.head_commit}/check-runs").respond(200, json={"check_runs": []})
    sync.sync_pull_request(db, pr.id, actor="owner")
    assert acc.acceptance_status == "STALE" and "base moved" in acc.invalidated_reason


@respx.mock(assert_all_called=False)
def test_sync_human_merge_records_ci_on_merge_commit(respx_mock, db, accepted, secrets_memory, monkeypatch):
    w, acc = accepted
    secrets_memory.put("personal/github", "a-test-token-value", actor="t")
    pr = _pr(db, w, acc)
    respx_mock.get(f"{API}/repos/{REPO}/pulls/12").respond(200, json=_pull(
        acc.head_commit, merged=True, merged_by={"login": "fadhlillah-dli", "type": "User"}))
    ok = {"check_runs": [{"name": "backend (ubuntu-latest)", "status": "completed", "conclusion": "success"}]}
    respx_mock.get(f"{API}/repos/{REPO}/commits/{acc.head_commit}/check-runs").respond(200, json=ok)
    respx_mock.get(f"{API}/repos/{REPO}/commits/{'e' * 40}/check-runs").respond(200, json=ok)
    out = sync.sync_pull_request(db, pr.id, actor="owner")
    db.commit()
    assert pr.state == "MERGED" and pr.merged_by_type == "User"
    assert len(out["ci_evidence_ids"]) == 2
    assert acc.acceptance_status == "ACCEPTED"
    g = {x["gate"]: x for x in gates.compute_gates(db, w.task.id)["gates"]}
    assert g["G5"]["status"] == "PASS" and g["G7"]["status"] == "PASS"


def test_sync_without_credential_is_failed_not_assumed(db, accepted, secrets_memory):
    w, acc = accepted
    pr = _pr(db, w, acc)
    out = sync.sync_pull_request(db, pr.id, actor="owner")
    assert pr.sync_status == "FAILED" and "credential" in out["notes"][0]
    assert acc.acceptance_status == "ACCEPTED" and pr.state == "OPEN"


def test_branch_update_hook_invalidates(db, accepted):
    w, acc = accepted
    _pr(db, w, acc)
    assert sync.on_branch_updated(db, repo=REPO, branch=BRANCH, head_sha="8" * 40) == 1
    assert acc.acceptance_status == "STALE"
