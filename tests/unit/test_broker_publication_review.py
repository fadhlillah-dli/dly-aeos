"""Owner rule R44 (2026-10-28, ADR-0132): a worker candidate is published only after a Claude review PASS of its exact
head (paraphrased: all OpenCode and OpenRouter work must pass a Claude review; a worker has no right to commit, push,
let alone merge).

Every remote write of a worker run - the push of its ``aeos/*`` branch and pull-request creation (the Bitbucket path:
tests/unit/test_office_bitbucket_delivery.py) - is refused with ``REVIEW_REQUIRED`` unless the run's latest Claude
checkpoint review is COMPLETED, non-MOCK, bound to exactly the head being published through its checkpoint package
evidence, a PASS not contradicted by its own findings, and answered by a Claude model of the review policy's family.
Each refusal below asserts its own message; each has a near miss that publishes. Local bare remotes, mocked HTTP."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.broker import git_broker
from aeos.core.errors import PolicyDenied
from aeos.models.audit import AuditEvent
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.decisions import Decision
from aeos.models.evidence import ReviewRecord
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun
from aeos.verification import publication_review as gate
from aeos.verification import review
from aeos.workforce.integrations import QUARANTINE_TARGET_TYPE
from tests.unit.review_gate_support import MISSING, record_claude_review, record_push
from tests.unit.test_verification_support import (
    FakeBridge,
    FakeWorkforce,
    claude_envelope,
    claude_result,
    install_doctor,
    make_world,
    record_verification,
    review_payload,
)
from tests.unit.test_workforce_support import FakeModules, init_git_repo, make_project

BRANCH = "aeos/task-r-1234abcd"
REPO = "owner/repo"
TOKEN = "ghp_" + "r" * 36


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(db, tmp_path):
    """A worker run in REVIEW whose runtime head is one commit above the base; no review yet."""
    src = tmp_path / "candidate"
    base = init_git_repo(src)
    _git(src, "checkout", "-q", "-b", BRANCH)
    (src / "feature.txt").write_text("work\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "candidate")
    head = _git(src, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    p = make_project(db)
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="REVIEW", workspace_ref=str(src),
                    branch=BRANCH, head_commit=head, base_commit=base)
    db.add(run)
    db.flush()
    broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="GIT_BRANCH",
                       scope={"repo": REPO, "branch": BRANCH}, ttl_seconds=300, max_requests=3)
    return {"src": src, "bare": bare, "run": run, "head": head, "base": base}


def _push(db, env):
    return git_broker.push_task_branch(db, run_id=env["run"].id, worktree=str(env["src"]), branch=BRANCH,
                                       repo_full_name=REPO, remote_url=str(env["bare"]))


def _remote_refs(env) -> str:
    return _git(env["bare"], "for-each-ref", "--format=%(refname)")


def _grant_uses(db, run, kind="GIT_BRANCH") -> int:
    g = db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id, CapabilityGrant.kind == kind))
    return g.used_requests or 0


def _refused_push(db, env, message: str, reason: str) -> PolicyDenied:
    """The push is refused with REVIEW_REQUIRED and *message*, audited, nothing pushed, no grant consumed."""
    with pytest.raises(PolicyDenied) as e:
        _push(db, env)
    assert e.value.code == "REVIEW_REQUIRED", e.value.code
    assert message in e.value.message, e.value.message
    assert e.value.message.startswith(f"no Claude review PASS for head {env['head'][:12]}: ")
    assert e.value.details["reason"] == reason and e.value.details["head_commit"] == env["head"]
    assert _remote_refs(env) == "", "nothing reached the remote"
    assert _grant_uses(db, env["run"]) == 0, "the capability is not consumed by a refused write"
    assert db.scalar(select(BrokerSideEffect).where(BrokerSideEffect.run_id == env["run"].id)) is None
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.git.push_branch",
                                             AuditEvent.result == "DENIED").order_by(AuditEvent.seq.desc()))
    assert row is not None and row.details["code"] == "REVIEW_REQUIRED" and row.details["reason"] == reason
    return e.value


def _pushed(db, env) -> None:
    res = _push(db, env)
    assert res["ok"] and res["status"] == "PUSHED" and res["head_sha"] == env["head"], res
    assert _git(env["bare"], "rev-parse", f"refs/heads/{BRANCH}") == env["head"]


# ----------------------------------------------------------------------------- push: the four required cases
def test_push_without_a_claude_review_is_refused(db, env):
    _refused_push(db, env, "no Claude checkpoint review exists for this run", "NO_REVIEW")
    record_claude_review(db, env["run"])  # near miss: the same run once Claude passed this head
    _pushed(db, env)


def test_push_is_refused_when_the_review_covers_an_older_head(db, env):
    record_claude_review(db, env["run"], head=env["base"])  # a PASS - of the head before the worker's last commit
    err = _refused_push(db, env, f"the latest Claude checkpoint review covers head {env['base'][:12]}, not this head",
                        "OTHER_HEAD")
    assert err.details["reviewed_head"] == env["base"]
    record_claude_review(db, env["run"])  # near miss: a new review of exactly this head
    _pushed(db, env)


@pytest.mark.parametrize("verdict", ["FAIL", "NOT-PROVEN", "BLOCKED"])
def test_push_is_refused_on_a_non_pass_verdict(db, env, verdict):
    record_claude_review(db, env["run"], verdict=verdict,
                         findings=[{"severity": "MEDIUM", "title": "t", "detail": "d", "required_action": "fix"}])
    _refused_push(db, env, f"the latest Claude checkpoint review verdict is {verdict}", "NOT_PASS")


def test_push_is_allowed_with_a_pass_bound_to_the_head(db, env):
    rec = record_claude_review(db, env["run"])
    _pushed(db, env)
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.git.push_branch",
                                             AuditEvent.result == "SUCCESS"))
    assert row is not None
    assert gate.publication_review(db, env["run"], env["head"]).review_id == rec.id


# ----------------------------------------------------------------------------- push: the review's own properties
def test_a_later_fail_supersedes_an_earlier_pass_of_the_same_head(db, env):
    record_claude_review(db, env["run"])
    record_claude_review(db, env["run"], verdict="FAIL")
    _refused_push(db, env, "verdict is FAIL", "NOT_PASS")


def test_a_pass_contradicted_by_its_own_high_findings_is_refused(db, env):
    high = {"severity": "HIGH", "title": "token logged", "detail": "d", "required_action": "redact"}
    record_claude_review(db, env["run"], findings=[high])
    _refused_push(db, env, "is contradicted by its own HIGH/CRITICAL findings", "FINDINGS")
    low = {"severity": "LOW", "title": "naming", "detail": "d", "required_action": "rename"}
    record_claude_review(db, env["run"], findings=[low])  # near miss: a LOW finding does not contradict a PASS
    _pushed(db, env)


OPUS_ONLY = "only an Opus review opens publication (owner rule 2026-10-28: the checkpoint review is Opus, never Fable)"


def test_a_fable_pass_bound_to_the_head_is_refused(db, env):
    """Owner rule 2026-10-28: never Fable. A Fable request validates nothing - the resolved model must be Opus."""
    record_claude_review(db, env["run"], model_requested="fable", model_resolved="claude-fable-5-1")
    _refused_push(db, env, f"answered by claude-fable-5-1 (family fable); {OPUS_ONLY}", "MODEL")
    record_claude_review(db, env["run"])  # near miss: an Opus PASS of the same head publishes
    _pushed(db, env)


@pytest.mark.parametrize("resolved,family", [("claude-sonnet-5-5", "sonnet"), ("claude-haiku-4-5", "haiku"),
                                             (None, "UNKNOWN"), ("gpt-5", "UNKNOWN")])
def test_a_review_by_any_other_model_is_refused(db, env, resolved, family):
    record_claude_review(db, env["run"], model_requested="opus", model_resolved=resolved)
    _refused_push(db, env, f"(family {family}); {OPUS_ONLY}", "MODEL")


def test_an_opus_review_also_satisfies_the_review_policy(db, env):
    """An Opus answer to a request of another family is still not a proven policy identity (acceptance's rule)."""
    record_claude_review(db, env["run"], model_requested="fable", model_resolved="claude-opus-5-5")
    _refused_push(db, env, "does not satisfy the Claude review policy", "MODEL")
    record_claude_review(db, env["run"], model_requested="opus", model_resolved="claude-opus-6-0")  # another Opus
    _pushed(db, env)


def test_a_review_of_a_truncated_package_is_refused(db, env):
    """The reviewer sees at most DIFF_EXCERPT_CHARS of the diff; a cut package never opens publication."""
    record_claude_review(db, env["run"], diff_truncated=True)
    _refused_push(db, env, "the reviewer saw only part of the diff (the checkpoint package excerpt was truncated); "
                           "split the task into smaller changes and review each one", "TRUNCATED")
    record_claude_review(db, env["run"])  # near miss: the whole diff was in the package
    _pushed(db, env)


# ----------------------------------------------------------------------------- push: the history that leaves
def _commit_all(src: Path, message: str) -> str:
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", message)
    return _git(src, "rev-parse", "HEAD")


def _history_refused(db, env, message: str) -> PolicyDenied:
    with pytest.raises(PolicyDenied) as e:
        _push(db, env)
    assert e.value.code == "REVIEW_REQUIRED" and e.value.details["reason"] == "HISTORY", e.value.details
    assert message in e.value.message and "worker-made commits are never published" in e.value.message
    assert e.value.message.startswith(f"no Claude review PASS for head {env['run'].head_commit[:12]}: HISTORY - ")
    assert _remote_refs(env) == "", "nothing reached the remote"
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.git.push_branch",
                                             AuditEvent.result == "DENIED").order_by(AuditEvent.seq.desc()))
    assert row is not None and row.details["code"] == "REVIEW_REQUIRED" and row.details["reason"] == "HISTORY"
    return e.value


def test_worker_commits_that_add_then_delete_a_file_are_never_published(db, env):
    """The reviewer saw the net diff base..head. Two worker commits that add a file and delete it again leave no
    trace in that diff, but the history would carry the file to the remote: refused, although Claude passed the
    head."""
    src, run = env["src"], env["run"]
    _git(src, "reset", "-q", "--hard", env["base"])
    (src / "notes.txt").write_text("not for the remote\n", encoding="utf-8")
    _commit_all(src, "wip: notes")
    (src / "notes.txt").unlink()
    (src / "feature.txt").write_text("work\n", encoding="utf-8")
    run.head_commit = _commit_all(src, "aeos: candidate")
    record_claude_review(db, run)  # an Opus PASS of exactly this head (its net diff has no notes.txt)
    _history_refused(db, env, f"base {env['base'][:12]}..head is 2 commit(s)")
    # near miss: the runtime's single candidate commit of the same tree on the base is published
    _git(src, "reset", "-q", "--soft", env["base"])
    run.head_commit = _commit_all(src, "aeos: candidate")
    record_claude_review(db, run)
    res = _push(db, env)
    assert res["ok"] and res["head_sha"] == run.head_commit
    assert _git(env["bare"], "rev-list", f"refs/heads/{BRANCH}", "--not", env["base"]) == run.head_commit
    assert _git(env["bare"], "cat-file", "-t", f"refs/heads/{BRANCH}:feature.txt") == "blob"


def test_a_run_without_a_recorded_base_is_never_published(db, env):
    env["run"].base_commit = None
    record_claude_review(db, env["run"])
    _history_refused(db, env, "the run has no runtime-recorded base commit")


@pytest.mark.parametrize("status", ["WAITING_QUOTA", "NEEDS_DECISION", "FAILED"])
def test_a_review_that_did_not_complete_is_refused(db, env, status):
    record_claude_review(db, env["run"], status=status)
    _refused_push(db, env, f"the latest Claude checkpoint review is {status}, not COMPLETED", "NOT_COMPLETED")


def test_a_mock_review_is_refused(db, env):
    record_claude_review(db, env["run"], mode="MOCK")
    _refused_push(db, env, "the latest Claude checkpoint review is a MOCK", "MOCK")


def test_a_review_not_by_the_claude_reviewer_is_refused(db, env):
    record_claude_review(db, env["run"], reviewer_kind="HUMAN")
    _refused_push(db, env, "is by 'HUMAN', not the Claude checkpoint reviewer", "NOT_CLAUDE")


def test_a_review_without_its_package_binding_is_refused(db, env):
    """A PASS whose head cannot be proven (no package evidence, or package evidence of another review) never counts."""
    db.add(ReviewRecord(target_type="worker_run", target_id=env["run"].id, target_hash="sha256:" + "a" * 64,
                        project_id=env["run"].project_id, domain="PERSONAL", run_id=env["run"].id,
                        reviewer_kind="CLAUDE", model_requested="opus", model_resolved="claude-opus-5-5",
                        verdict="PASS-CONTROLLED", status="COMPLETED"))
    db.flush()
    _refused_push(db, env, "is not bound to a recorded head commit", "UNBOUND")
    other = record_claude_review(db, env["run"], artifact_hash="sha256:" + "9" * 64)  # another verified artifact
    rec = record_claude_review(db, env["run"])
    rec.package_evidence_id = other.package_evidence_id  # the package of another review: its hashes differ
    db.flush()
    _refused_push(db, env, "is not bound to a recorded head commit", "UNBOUND")


def test_quarantine_is_still_checked_first_and_still_needs_its_release(db, env):
    """The special-review rule is unchanged: a quarantined patch with a Claude PASS still needs the owner's release
    bound to the head; with both it publishes."""
    run = env["run"]
    run.quarantined = True
    run.quarantine_reasons = [{"path": ".github/workflows/ci.yml", "rule": "protected-path", "detail": "d"}]
    with pytest.raises(PolicyDenied) as e:
        _push(db, env)
    assert e.value.code == "QUARANTINED"  # without a review too: quarantine is decided first
    record_claude_review(db, run)
    with pytest.raises(PolicyDenied) as e:
        _push(db, env)
    assert e.value.code == "QUARANTINED"
    db.add(Decision(kind="EXCEPTION", actor_user_id="owner", target_type=QUARANTINE_TARGET_TYPE, target_id=run.id,
                    target_hash=run.head_commit, domain="PERSONAL", rationale="special review done"))
    db.flush()
    _pushed(db, env)


def test_the_gate_unavailable_refuses_the_push(db, env, absent_modules):
    record_claude_review(db, env["run"])
    absent_modules("aeos.verification.publication_review")
    with pytest.raises(PolicyDenied) as e:
        _push(db, env)
    assert e.value.code == "REVIEW_REQUIRED" and "review gate is unavailable (fail closed)" in e.value.message
    assert _remote_refs(env) == ""


# ----------------------------------------------------------------------------- pull request creation (GitHub)
def _pr_env(db, monkeypatch, secrets_memory, env) -> list[str]:
    FakeModules(interlock_allowed=True).install(monkeypatch)
    secrets_memory.put("personal/github", TOKEN, actor="test")
    reads: list[str] = []
    real = secrets_memory.get

    def get(ref, **kw):
        reads.append(ref)
        return real(ref, **kw)

    monkeypatch.setattr(secrets_memory, "get", get)
    broker.issue_grant(db, run_id=env["run"].id, project_id=env["run"].project_id, domain="PERSONAL",
                       kind="PR_CREATE", scope={"repo": REPO, "head": BRANCH, "base": "main"}, ttl_seconds=300,
                       max_requests=3)
    record_push(db, env["run"], repo=REPO)  # the broker pushed this head first (ADR-0132 decision 3)
    return reads


def _create_pr(db, env):
    return git_broker.create_pull_request(db, run_id=env["run"].id, repo_full_name=REPO, head=BRANCH, base="main",
                                          title="Task R", body="evidence")


def _refused_pr(db, env, reads, message: str, reason: str) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as e:
            _create_pr(db, env)
        assert not mock.calls, "no request reached GitHub"
    assert e.value.code == "REVIEW_REQUIRED" and message in e.value.message, e.value.message
    assert e.value.details["reason"] == reason
    assert reads == [], "the GitHub credential is never read for a refused PR"
    assert _grant_uses(db, env["run"], "PR_CREATE") == 0
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.github.create_pr",
                                             AuditEvent.result == "DENIED").order_by(AuditEvent.seq.desc()))
    assert row is not None and row.details["reason"] == reason


def _pr_created(db, env) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(f"https://api.github.com/repos/{REPO}/pulls").mock(return_value=httpx.Response(
            201, json={"number": 4, "html_url": f"https://github.com/{REPO}/pull/4", "head": {"sha": env["head"]}}))
        res = _create_pr(db, env)
        assert route.called
    assert res["ok"] and res["status"] == "CREATED" and res["head_sha"] == env["head"]


def test_pr_without_a_claude_review_is_refused(db, env, monkeypatch, secrets_memory):
    reads = _pr_env(db, monkeypatch, secrets_memory, env)
    _refused_pr(db, env, reads, "no Claude checkpoint review exists for this run", "NO_REVIEW")
    record_claude_review(db, env["run"])  # near miss
    _pr_created(db, env)


def test_pr_is_refused_when_the_review_covers_an_older_head(db, env, monkeypatch, secrets_memory):
    reads = _pr_env(db, monkeypatch, secrets_memory, env)
    record_claude_review(db, env["run"], head=env["base"])
    _refused_pr(db, env, reads, f"covers head {env['base'][:12]}, not this head", "OTHER_HEAD")


def test_pr_is_refused_on_a_fail_verdict(db, env, monkeypatch, secrets_memory):
    reads = _pr_env(db, monkeypatch, secrets_memory, env)
    record_claude_review(db, env["run"], verdict="FAIL")
    _refused_pr(db, env, reads, "the latest Claude checkpoint review verdict is FAIL", "NOT_PASS")


def test_pr_is_allowed_with_a_pass_bound_to_the_head(db, env, monkeypatch, secrets_memory):
    _pr_env(db, monkeypatch, secrets_memory, env)
    record_claude_review(db, env["run"])
    _pr_created(db, env)


def _refused_not_pushed(db, env, reads, message: str) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as e:
            _create_pr(db, env)
        assert not mock.calls, "no request reached GitHub"
    assert e.value.code == "REVIEW_REQUIRED" and e.value.details["reason"] == "NOT_PUSHED", e.value.details
    assert message in e.value.message and "push the branch through the broker first" in e.value.message
    assert reads == [] and _grant_uses(db, env["run"], "PR_CREATE") == 0


def test_pr_needs_a_recorded_push_of_exactly_this_head(db, env, monkeypatch, secrets_memory):
    """The push is where the history is checked: a PR is opened only for a branch the broker pushed with this
    head - not for a branch pushed by anyone else, nor for another head of the run."""
    reads = _pr_env(db, monkeypatch, secrets_memory, env)
    from aeos.models.broker_ext import BrokerSideEffect

    db.query(BrokerSideEffect).delete()  # no push record at all
    record_claude_review(db, env["run"])
    _refused_not_pushed(db, env, reads, f"no reviewed push of head {env['head'][:12]} to {REPO}:{BRANCH}")
    record_push(db, env["run"], repo=REPO, head=env["base"])  # a push of another head of the run
    record_push(db, env["run"], repo="owner/other", head=env["head"])  # this head, another repository
    _refused_not_pushed(db, env, reads, "NOT_PUSHED - the broker has no successful push of exactly this head")
    res = git_broker.push_task_branch(db, run_id=env["run"].id, worktree=str(env["src"]), branch=BRANCH,
                                      repo_full_name=REPO, remote_url=str(env["bare"]))  # near miss: the real push
    assert res["ok"]
    _pr_created(db, env)


def test_a_pr_whose_reported_head_is_not_the_reviewed_head_is_not_counted(db, env, monkeypatch, secrets_memory):
    _pr_env(db, monkeypatch, secrets_memory, env)
    record_claude_review(db, env["run"])
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(f"https://api.github.com/repos/{REPO}/pulls").mock(return_value=httpx.Response(
            201, json={"number": 5, "html_url": f"https://github.com/{REPO}/pull/5", "head": {"sha": "f" * 40}}))
        res = _create_pr(db, env)
    assert not res["ok"] and res["status"] == "FAILED"
    assert res["error"].startswith(f"HEAD_MISMATCH: the pull request's head {'f' * 12} is not the reviewed head")
    from aeos.models.broker_ext import BrokerSideEffect

    se = db.get(BrokerSideEffect, res["side_effect_id"])
    assert se.status == "FAILED" and se.kind == "CREATE_PR"


# ----------------------------------------------------------------------------- binding edge cases (L3)
def test_a_merge_commit_head_is_never_published(db, env):
    """A merge whose first parent is the base still carries a second history the reviewer did not see."""
    src, run = env["src"], env["run"]
    _git(src, "checkout", "-q", "-b", "side", env["base"])
    (src / "side.txt").write_text("side\n", encoding="utf-8")
    _commit_all(src, "side work")
    _git(src, "checkout", "-q", BRANCH)
    _git(src, "reset", "-q", "--hard", env["base"])
    _git(src, "merge", "-q", "--no-ff", "-m", "aeos: candidate", "side")
    run.head_commit = _git(src, "rev-parse", "HEAD")
    record_claude_review(db, run)
    err = _history_refused(db, env, "the head's parents are")
    assert f"{env['base'][:12]}, " in err.message  # two parents, the first one being the base


def test_a_head_equal_to_the_base_is_never_published(db, env):
    src, run = env["src"], env["run"]
    _git(src, "reset", "-q", "--hard", env["base"])
    run.head_commit = env["base"]
    record_claude_review(db, run)
    _history_refused(db, env, f"base {env['base'][:12]}..head is 0 commit(s) and the head's parents are none")


def test_a_tampered_package_is_unbound(db, env, settings):
    rec = record_claude_review(db, env["run"])
    from aeos.models.evidence import EvidenceArtifact

    ev = db.get(EvidenceArtifact, rec.package_evidence_id)
    stored = settings.layout.root / ev.location
    stored.write_bytes(stored.read_bytes().replace(b'"diff_truncated": false', b'"diff_truncated": true '))
    _refused_push(db, env, "is not bound to a recorded head commit (no trusted, intact checkpoint package", "UNBOUND")


@pytest.mark.parametrize("overrides", [{"diff_truncated": MISSING}, {"diff_truncated": "no"},
                                       {"diff_available": MISSING}, {"diff_available": 1},
                                       {"binary_paths": MISSING}, {"binary_paths": "assets/a.png"},
                                       {"run": {"head_commit": "0" * 40}}])
def test_a_package_without_well_formed_coverage_fields_is_unbound(db, env, overrides):
    record_claude_review(db, env["run"], package_overrides=overrides)
    _refused_push(db, env, "is not bound to a recorded head commit", "UNBOUND")


# ----------------------------------------------------------------------------- what the reviewer could see (M1, M2)
def test_a_review_whose_diff_was_unavailable_is_refused(db, env):
    record_claude_review(db, env["run"], diff_available=False)
    _refused_push(db, env, "the reviewer saw no diff (the verifier's diff evidence was missing or failed its "
                           "content hash when the package was built)", "DIFF_UNAVAILABLE")
    record_claude_review(db, env["run"])  # near miss
    _pushed(db, env)


def test_a_change_with_binary_files_is_refused(db, env):
    record_claude_review(db, env["run"], binary_paths=["assets/logo.png"])
    _refused_push(db, env, "the change adds or modifies binary file(s) the reviewer could not read "
                           "(assets/logo.png)", "BINARY")
    record_claude_review(db, env["run"], binary_paths=[])  # near miss: a text-only change publishes
    _pushed(db, env)


# ----------------------------------------------------------------------------- the real review paths bind the head
@pytest.fixture
def reviewed_world(db, monkeypatch):
    """A verified run whose checkpoint review is requested from Opus (owner rule 2026-10-28; TaskSpecs are
    append-only, so the default the review uses is set, as the Opus switch of the stage models does)."""
    FakeWorkforce().install(monkeypatch)
    monkeypatch.setattr(review, "DEFAULT_REVIEW_MODEL", "opus")
    w = make_world(db)
    record_verification(db, w)  # the verifier's head for this run
    return w


OPUS = "claude-opus-5-5"


def test_a_native_review_records_the_binding_the_gate_reads(db, reviewed_world, monkeypatch):
    """What ``review_run`` writes through the bridge is what the broker checks (not only the test helper)."""
    w = reviewed_world
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload("PASS-CONTROLLED"), model=OPUS)).install(monkeypatch)
    out = review.review_run(db, w.run.id, actor="t")
    assert out.review.status == "COMPLETED" and out.review.verdict == "PASS-CONTROLLED", out.review.status_reason
    binding = gate.review_binding(db, out.review)
    assert binding == gate.ReviewBinding(head=w.run.head_commit, diff_truncated=False)
    ok = gate.publication_review(db, w.run, w.run.head_commit)
    assert ok.allowed and ok.reason == "PASS" and ok.review_id == out.review.id, ok
    moved = gate.publication_review(db, w.run, "e" * 40)  # a later worker commit is not covered
    assert not moved.allowed and moved.reason == "OTHER_HEAD"


def test_a_native_fable_review_never_opens_publication(db, monkeypatch):
    """A review requested from Fable (the default before the Opus switch of the stage models) is recorded as a
    PASS, but it never publishes (owner rule 2026-10-28)."""
    FakeWorkforce().install(monkeypatch)
    monkeypatch.setattr(review, "DEFAULT_REVIEW_MODEL", "fable")
    w = make_world(db)
    record_verification(db, w)
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload(), model="claude-fable-5-1")).install(monkeypatch)
    out = review.review_run(db, w.run.id, actor="t")
    assert out.review.status == "COMPLETED" and out.review.verdict == "PASS-CONTROLLED"
    res = gate.publication_review(db, w.run, w.run.head_commit)
    assert not res.allowed and res.reason == "MODEL" and OPUS_ONLY in res.message


def test_the_package_lists_the_binary_files_and_the_gate_refuses_them(db, monkeypatch):
    FakeWorkforce().install(monkeypatch)
    monkeypatch.setattr(review, "DEFAULT_REVIEW_MODEL", "opus")
    w = make_world(db)
    record_verification(db, w, changed=["backend/extra.py", "assets/logo.png", "assets/old.png"],
                        numstat={"backend/extra.py": {"added": 1, "deleted": 0},
                                 "assets/logo.png": {"added": None, "deleted": None},
                                 "assets/old.png": {"added": None, "deleted": None}})
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload(), model=OPUS)).install(monkeypatch)
    out = review.review_run(db, w.run.id, actor="t")
    binding = gate.review_binding(db, out.review)
    assert binding is not None and binding.binary_paths == ("assets/logo.png", "assets/old.png")
    res = gate.publication_review(db, w.run, w.run.head_commit)
    assert not res.allowed and res.reason == "BINARY" and "(assets/logo.png, assets/old.png)" in res.message


def test_a_package_built_without_the_diff_evidence_is_refused(db, reviewed_world, monkeypatch, settings):
    """M2: a DIFF evidence that fails its content hash gives the reviewer an empty excerpt - recorded, refused."""
    from aeos.models.evidence import EvidenceArtifact
    from aeos.verification.verifier import latest_report, related_evidence

    w = reviewed_world
    diff_ev = related_evidence(db, latest_report(db, w.run.id), "DIFF")
    stored = settings.layout.root / diff_ev.location
    stored.write_bytes(stored.read_bytes() + b"tampered")
    assert isinstance(db.get(EvidenceArtifact, diff_ev.id), EvidenceArtifact)
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload(), model=OPUS)).install(monkeypatch)
    out = review.review_run(db, w.run.id, actor="t")
    binding = gate.review_binding(db, out.review)
    assert binding is not None and binding.diff_available is False and binding.diff_truncated is False
    res = gate.publication_review(db, w.run, w.run.head_commit)
    assert not res.allowed and res.reason == "DIFF_UNAVAILABLE", res


def test_a_manual_package_review_keeps_the_binding(db, reviewed_world, monkeypatch):
    w = reviewed_world
    install_doctor(monkeypatch, status="UNKNOWN")
    FakeBridge().install(monkeypatch, manual=False)
    waiting = review.review_run(db, w.run.id, actor="t").review
    assert not gate.publication_review(db, w.run, w.run.head_commit).allowed  # waiting for the manual result
    out = review.collect_manual_review(db, waiting.id, actor="owner",
                                       structured=claude_envelope(review_payload(), model=OPUS))
    assert out.review.status == "COMPLETED" and out.review.mode == "MANUAL_FALLBACK", out.review.status_reason
    assert gate.publication_review(db, w.run, w.run.head_commit).allowed


def test_a_native_fail_review_refuses(db, reviewed_world, monkeypatch):
    w = reviewed_world
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload("FAIL", [{"severity": "HIGH", "title": "bug", "detail": "d",
                                                              "required_action": "fix"}]), model=OPUS)).install(monkeypatch)
    out = review.review_run(db, w.run.id, actor="t")
    assert out.review.verdict == "FAIL"
    res = gate.publication_review(db, w.run, w.run.head_commit)
    assert not res.allowed and res.reason == "NOT_PASS"


# ----------------------------------------------------------------------------- merge stays outside the runtime
def test_the_broker_still_has_no_merge_operation():
    """R44 "apalagi sampai merge": nothing in the broker integrates a pull request (U-09)."""
    names = dir(git_broker) + dir(broker)
    assert not [n for n in names if "merge" in n.lower()]
    assert "REVIEW_REQUIRED" == git_broker.REVIEW_REQUIRED == gate.REVIEW_REQUIRED
