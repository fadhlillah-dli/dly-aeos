"""End-to-end execution chain of seam group W2 (master §14, §19, §21, §22, §25, §26, §31, §34, §42, §50).

Scheduler -> Cost -> Routing -> Broker -> Worker runtime -> Verifier -> Review -> Acceptance -> Delivery with
the real modules; only the edges are faked (see ``tests/e2e/chain_support.py``):

* an approved plan version (owner decision through the API) with one TaskSpec for a temporary git
  repository registered as a PERSONAL project, its trusted acceptance suite registered by the owner;
* owner decisions through the API: TRIAL activation of a free route, the process-sandbox exception, a
  project budget, write enablement — then ``POST /api/plans/{id}/execute``;
* admission + lease + budget reservation, the scripted ``opencode`` child process calling
  ``/broker/v1/chat/completions`` with its capability token, the broker forwarding to a respx upstream;
* run REVIEW (never DONE) -> independent verifier on the project's trusted suite -> checkpoint review
  (first WAITING_QUOTA, then PASS) -> AcceptanceRecord ACCEPTED -> task DONE -> prepare-pr pushes the
  exact accepted sha to a local bare remote and opens a draft PR via respx GitHub -> a new commit on the
  branch makes the acceptance STALE and the task is reopened.

§50 rows covered: "Worker exit 0 -> REVIEW", "PR baru mengubah head atau base -> acceptance stale",
"Cancel saat ada child process", "Free quota habis / 429 -> no paid fallback", "Fable Max quota habis ->
review menunggu", "Worker mengubah grader/policy/pipeline -> quarantined, not published".
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import psutil
import pytest
import respx
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.core.db import session_factory
from aeos.core.errors import PolicyDenied
from aeos.models.broker_ext import BrokerRequest, BrokerSideEffect
from aeos.models.cost import BudgetReservation, QuotaState, UsageLedgerEntry
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun
from tests.e2e.chain_support import FAKE_ZEN_KEY, REPO, ROUTE_KEY, build_chain, git, remote_head

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required for the execution chain")


@pytest.fixture
def chain(settings, monkeypatch, tmp_path):
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as rmock:
        yield from build_chain(settings, monkeypatch, tmp_path, rmock)


def _reservation(run: WorkerRun) -> BudgetReservation:
    with session_factory()() as s:
        res = s.get(BudgetReservation, run.budget_reservation_id)
        s.expunge_all()
        return res


def _wait(predicate, timeout: float = 30.0, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")


def _dead(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_full_chain_plan_to_pr_then_new_commit_reopens(chain):
    chain.setup("ok", ["backend/e2ecalc/**"])
    assert chain.te().state == "READY"

    # ---- admission + atomic lease + reservation (free route, $0 GLOBAL default, owner PROJECT budget)
    leased = chain.tick()
    assert len(leased) == 1
    run_id = leased[0]
    run = chain.run(run_id)
    assert chain.te().state == "LEASED" and run.model_route_key == ROUTE_KEY
    res = _reservation(run)
    assert res.status == "RESERVED" and res.pricing_basis == "FREE_ROUTE"

    # ---- worker: real child process, broker-mediated model calls
    out = chain.execute(run_id)
    assert out["status"] == "REVIEW", out
    run = chain.run(run_id)
    te = chain.te()
    assert run.execution_status == "REVIEW" and te.state == "REVIEW", "exit 0 is REVIEW, never DONE"
    assert run.worker_reported_status == "EXITED_0" and run.head_commit and not run.quarantined
    assert run.diff_summary["paths"][0]["path"] == "backend/e2ecalc/__init__.py"
    # broker forwarded the exact route model with the upstream key; the capability token stayed local
    assert len(chain.upstream_calls) == 2
    assert {c["model"] for c in chain.upstream_calls} == {chain.route().model_id}
    assert all(c["auth"] == f"Bearer {FAKE_ZEN_KEY}" for c in chain.upstream_calls)
    with session_factory()() as s:
        reqs = list(s.scalars(select(BrokerRequest).where(BrokerRequest.run_id == run_id)))
        assert [r.outcome for r in reqs] == ["OK", "OK"]
        ledger = list(s.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id == run_id,
                                                                UsageLedgerEntry.category == "WORKER_API")))
        assert len(ledger) == 2 and all(e.reservation_id == run.budget_reservation_id for e in ledger)
        assert all(e.cost_usd is None and e.mode == "LIVE" for e in ledger)  # UNKNOWN cost, free route
        grant = s.get(CapabilityGrant, run.capability_id)
        assert grant.active is False and grant.used_requests == 2
    res = _reservation(run)
    assert res.status == "COMMITTED" and res.committed_requests == 2 and res.committed_tokens == 38

    # ---- independent verifier with the project's trusted suite (registered outside the candidate)
    v = chain.ok(chain.client.post(f"/api/runs/{run_id}/verify", json={}))
    assert v["status"] == "PASS", v
    assert v["checks"]["trusted_suite"]["status"] == "PASS"
    run = chain.run(run_id)
    assert run.verifier_status == "PASS"
    with session_factory()() as s:
        compute = s.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id == run_id,
                                                          UsageLedgerEntry.category == "COMPUTE"))
        assert compute is not None and compute.unit == "seconds" and compute.cost_usd is None

    # ---- checkpoint review: Max quota exhausted -> waits BLOCKED, nothing opens, task stays REVIEW
    chain.bridge.review_quota_exhausted = True
    waiting = chain.ok(chain.client.post(f"/api/runs/{run_id}/review", json={}))
    assert waiting["review"]["status"] == "WAITING_QUOTA" and waiting["review"]["verdict"] == "BLOCKED"
    assert waiting["acceptance"] is None
    assert chain.te().state == "REVIEW"
    run = chain.run(run_id)
    assert run.review_verdict == "BLOCKED" and run.quota_status == "WAITING_QUOTA"
    with session_factory()() as s:
        assert s.scalar(select(AcceptanceRecord).where(AcceptanceRecord.task_id == chain.task_id)) is None
        q = s.scalar(select(QuotaState).where(QuotaState.provider == "claude-native"))
        assert q.state == "EXHAUSTED" and q.source == "observed-limit"
    run_view = chain.ok(chain.client.get(f"/api/runs/{run_id}"))
    assert run_view["ui_state"] == "WAITING_QUOTA"

    # ---- quota back: review PASS -> AcceptanceRecord ACCEPTED (bound to the run's head/base) -> DONE
    chain.bridge.review_quota_exhausted = False
    reviewed = chain.ok(chain.client.post(f"/api/runs/{run_id}/review", json={}))
    assert reviewed["review"]["verdict"] == "PASS-CONTROLLED"
    assert reviewed["acceptance"]["acceptance_status"] == "ACCEPTED", reviewed
    run = chain.run(run_id)
    assert reviewed["acceptance"]["head_commit"] == run.head_commit
    assert reviewed["acceptance"]["base_commit"] == run.base_commit
    te = chain.te()
    assert te.state == "DONE" and te.accepted_run_id == run_id and run.execution_status == "DONE"

    # ---- delivery: grants -> sha-pinned push to the bare remote -> draft PR via the broker only
    pr = chain.ok(chain.client.post(f"/api/tasks/{chain.task_id}/prepare-pr", json={}))
    assert pr["merge_authority"] == "HUMAN_ONLY"
    assert remote_head(chain.bare, run.branch) == run.head_commit
    assert len(chain.pr_posts) == 1
    post = chain.pr_posts[0]
    assert post["draft"] is True and post["head"] == run.branch and post["base"] == "main"
    assert "Merge authority: human only" in post["body"]
    with session_factory()() as s:
        rec = s.get(PullRequestRecord, pr["pull_request"]["id"])
        assert rec.number == 7 and rec.state == "OPEN" and rec.accepted_head_sha == run.head_commit
        assert rec.observed_head_sha == run.head_commit
        bundle = s.get(EvidenceArtifact, rec.bundle_evidence_id)
        assert bundle.kind == "PR_BUNDLE" and bundle.trusted
        effects = {se.kind: se for se in s.scalars(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run_id))}
        assert effects["PUSH_BRANCH"].status == "SUCCEEDED" and effects["PUSH_BRANCH"].commit_sha == run.head_commit
        assert effects["CREATE_PR"].status == "SUCCEEDED" and effects["CREATE_PR"].external_id == "7"
        live = list(s.scalars(select(CapabilityGrant).where(CapabilityGrant.run_id == run_id,
                                                            CapabilityGrant.active.is_(True))))
        assert live == [], "delivery capabilities are revoked after the step"

    # ---- a new commit lands on the PR branch -> acceptance STALE -> task reopened for re-verification
    clone = chain.repo.parent / "human-clone"
    git(chain.repo.parent, "clone", "-q", str(chain.bare), str(clone))
    git(clone, "checkout", "-q", run.branch)
    (clone / "README.md").write_text("# e2e calc\n\nedited on the PR by a human\n", encoding="utf-8")
    git(clone, "commit", "-q", "-am", "follow-up edit on the PR")
    git(clone, "push", "-q", "origin", run.branch)
    new_head = remote_head(chain.bare, run.branch)
    synced = chain.ok(chain.client.post(f"/api/delivery/prs/{pr['pull_request']['id']}/sync"))
    assert any("acceptance STALE" in n for n in synced["notes"]), synced
    with session_factory()() as s:
        acc = s.get(AcceptanceRecord, reviewed["acceptance"]["id"])
        assert acc.acceptance_status == "STALE" and new_head[:12] in acc.invalidated_reason
    te = chain.te()
    assert te.state == "READY" and te.reason_code == "ACCEPTANCE_STALE", (te.state, te.reason)
    assert chain.run(run_id).acceptance_status == "STALE"
    # the reopened task is admitted again as a new, bounded attempt (2/2)
    again = chain.tick()
    assert len(again) == 1 and chain.run(again[0]).attempt == 2


def test_cancel_mid_run_kills_process_tree_and_releases_reservation(chain):
    chain.setup("hang", ["backend/e2ecalc/**"])
    run_id = chain.tick()[0]
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(chain.execute(run_id)), daemon=True)
    worker.start()
    run = _wait(lambda: (r := chain.run(run_id)) and r.execution_status == "RUNNING" and r.sandbox_ref and r)
    marker = _wait(lambda: next(iter(Path(run.sandbox_ref).rglob("aeos-fake-pids.json")), None))
    pids = _wait(lambda: json.loads(marker.read_text(encoding="utf-8") or "null"))
    assert psutil.pid_exists(pids["grandchild"])

    chain.ok(chain.client.post(f"/api/runs/{run_id}/cancel", json={"reason": "owner stops the run"}))
    worker.join(60)
    assert not worker.is_alive()
    run = chain.run(run_id)
    assert run.execution_status == "CANCELLED" and chain.te().state == "CANCELLED"
    _wait(lambda: _dead(pids["engine"]) and _dead(pids["grandchild"]), timeout=20)
    res = _reservation(run)
    assert res.status != "RESERVED", "the hold is closed (released, or settled with the usage already made)"
    with session_factory()() as s:
        grants = list(s.scalars(select(CapabilityGrant).where(CapabilityGrant.run_id == run_id)))
        assert grants and all(not g.active for g in grants)
    budgets = chain.ok(chain.client.get("/api/budgets"))["items"]
    project_budget = next(b for b in budgets if b["scope_type"] == "PROJECT")
    assert project_budget["held_tokens"] == 0 and project_budget["held_requests"] == 0


def test_upstream_429_waits_for_quota_without_fallback(chain):
    chain.setup("quota", ["backend/e2ecalc/**"])
    chain.upstream_status = 429
    run_id = chain.tick()[0]
    out = chain.execute(run_id, quota_stop_after=3)
    assert out["stop_reason"] == "QUOTA_EXHAUSTED", out
    run = chain.run(run_id)
    te = chain.te()
    assert run.execution_status == "FAILED" and run.outcome_code == "QUOTA_EXHAUSTED"
    assert te.state == "BLOCKED" and te.reason_code == "QUOTA_EXHAUSTED"
    # every upstream call went to the same approved route/model: no fallback model, no paid route
    assert len(chain.upstream_calls) >= 3
    assert {c["model"] for c in chain.upstream_calls} == {chain.route().model_id}
    with session_factory()() as s:
        q = s.scalar(select(QuotaState).where(QuotaState.scope == ROUTE_KEY))
        assert q.state == "EXHAUSTED" and q.source == "observed-429" and q.provider == "opencode-zen"
        assert {r.route_key for r in s.scalars(select(BrokerRequest).where(BrokerRequest.run_id == run_id))} == {ROUTE_KEY}
        assert s.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id == run_id)) is None
    assert _reservation(run).status == "RELEASED"
    # the scheduler keeps the task waiting for the quota window; nothing else is leased
    assert chain.tick() == []
    te = chain.te()
    assert te.state == "BLOCKED" and te.reason_code == "QUOTA_EXHAUSTED"
    with session_factory()() as s:
        assert [r.id for r in s.scalars(select(WorkerRun).where(WorkerRun.task_id == chain.task_id))] == [run_id]
    run_view = chain.ok(chain.client.get(f"/api/runs/{run_id}"))
    assert run_view["usage"]["quota_status"] == "RATE_LIMITED"


def test_protected_path_change_is_quarantined_and_never_published(chain):
    # The TaskSpec explicitly allows a protected pipeline path: the patch is a candidate for special
    # review only (§21) — the scheduler never retries it on its own and the broker never publishes it.
    chain.setup("protected", ["backend/e2ecalc/**", ".github/workflows/**"])
    run_id = chain.tick()[0]
    out = chain.execute(run_id)
    assert out["status"] == "REVIEW" and out["quarantined"], out
    run = chain.run(run_id)
    assert run.quarantined and any(q["rule"] == "protected-path" for q in run.quarantine_reasons)
    assert chain.te().reason_code == "QUARANTINED"

    v = chain.ok(chain.client.post(f"/api/runs/{run_id}/verify", json={}))
    assert v["status"] == "QUARANTINED", v
    te = chain.te()
    assert te.state == "BLOCKED" and te.reason_code == "QUARANTINED"
    assert chain.tick() == [], "a quarantined patch waits for the owner's special review"
    assert chain.te().state == "BLOCKED"
    with session_factory()() as s:
        with pytest.raises(PolicyDenied) as exc:
            broker.push_task_branch(s, run_id=run_id, worktree=run.workspace_ref, branch=run.branch,
                                    repo_full_name=REPO, expected_head_sha=run.head_commit)
        assert exc.value.code == "QUARANTINED"
        s.rollback()
    refused = chain.client.post(f"/api/tasks/{chain.task_id}/prepare-pr", json={})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "ACCEPTANCE_REQUIRED"
    assert chain.pr_posts == []
    assert git(chain.bare, "branch", "--list", "aeos/*") == ""
