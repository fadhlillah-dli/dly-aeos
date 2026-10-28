"""Seam group W2 contracts (integration 2026-10-18): scheduler <-> cost <-> routing <-> broker <-> runtime <->
verification <-> delivery. Each test pins one reconciled contract (see the ADR amendments).

§14 reopen on STALE acceptance, §21/§50 fixtures never live work and scope re-check at lease time,
§22/§42 run-level reservation with partial commits, §26 credential namespaces and secret purposes,
§31 sha-pinned push / delivery grants / reconciliation before retry, §34 verifier vocabulary and units."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from aeos.broker import facade as broker
from aeos.broker import git_broker, model_proxy
from aeos.core.errors import Blocked, Conflict, PolicyDenied
from aeos.core.hashing import content_hash
from aeos.cost import facade as cost
from aeos.cost import service as cost_service

# Imported at module load (before any FakeModules fixture swaps sys.modules entries), so these real
# modules bind the real verification collector/acceptance.
from aeos.delivery import pr_bundle, sync
from aeos.models.cost import BudgetReservation, QuotaState
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.routing import CapabilityGrant, ModelRoute
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.verification import acceptance
from aeos.verification import scope as verification_scope
from aeos.workforce import scheduler
from aeos.workforce.states import is_legal
from tests.unit.review_gate_support import record_claude_review
from tests.unit.test_workforce_support import (
    FakeModules,
    init_git_repo,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)


@pytest.fixture
def fakes(monkeypatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def _task_in(db, fakes, state: str, *, spec: dict[str, Any] | None = None, attempts_used: int = 0):
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "t1", **(spec or {})}])
    fakes.approved[plan.id] = pv.id
    scheduler.ensure_executions(db, pv.id, actor="t")
    te = scheduler.get_te(db, specs[0].id)
    db.execute(update(TaskExecution).where(TaskExecution.id == te.id).values(state=state, attempts_used=attempts_used))
    db.flush()
    return plan, pv, specs[0]


# ----------------------------------------------------------------------------- 1. reopen (§14, §50)
def test_reopen_done_task_to_ready_while_attempts_remain(db, fakes):
    _plan, _pv, task = _task_in(db, fakes, "DONE", attempts_used=1)
    te = scheduler.reopen_for_reverification(db, task.id, reason="PR head changed", actor="verifier")
    assert te.state == "READY" and te.reason_code == "ACCEPTANCE_STALE" and te.active_run_id is None
    assert "re-verification attempt 2/2" in te.reason
    assert te.pending_findings and te.pending_findings[0]["source"] == "acceptance"


def test_reopen_blocks_at_attempt_cap_or_when_plan_no_longer_approved(db, fakes):
    _plan, _pv, task = _task_in(db, fakes, "DONE", attempts_used=2)
    te = scheduler.reopen_for_reverification(db, task.id, reason="base moved", actor="delivery")
    assert te.state == "BLOCKED" and te.reason_code == "ACCEPTANCE_STALE" and "attempt cap" in te.reason
    plan2, _pv2, task2 = _task_in(db, fakes, "DONE", attempts_used=1)
    fakes.approved.pop(plan2.id)
    te2 = scheduler.reopen_for_reverification(db, task2.id, reason="config changed", actor="verifier")
    assert te2.state == "BLOCKED" and "no longer approved" in te2.reason


def test_done_is_left_only_through_reopen(db, fakes):
    _plan, _pv, task = _task_in(db, fakes, "DONE", attempts_used=1)
    with pytest.raises(Conflict) as exc:
        scheduler.transition_task(db, task.id, "READY", reason="x", actor="worker:x", reason_code="ACCEPTANCE_STALE")
    assert exc.value.code == "REOPEN_REQUIRES_STALE_ACCEPTANCE"
    assert not is_legal("DONE", "READY") and not is_legal("DONE", "BLOCKED", reason_code="LEASE_LOST")
    _plan2, _pv2, ready = _task_in(db, fakes, "READY")
    assert scheduler.reopen_for_reverification(db, ready.id, reason="x", actor="t").state == "READY"  # idempotent


# ----------------------------------------------------------------------------- 2. fixtures never live (§35, §50)
def _mock_plan(db, project, mode: str = "MOCK"):
    plan = Plan(project_id=project.id, domain=project.domain, title="fixture plan", state="APPROVED")
    db.add(plan)
    db.flush()
    content = {"tasks": ["t1"], "fixture": True}
    pv = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                     manifest_hash=content_hash(content), mode=mode)
    db.add(pv)
    db.flush()
    spec = TaskSpec(plan_version_id=pv.id, plan_version_hash=pv.manifest_hash, project_id=project.id,
                    domain=project.domain, task_key="t1", title="t1", goal="g", allowed_paths=["src/**"],
                    capability_ttl_seconds=600, spec_hash=content_hash({"t": 1, "m": mode}), mode=mode)
    db.add(spec)
    db.flush()
    plan.approved_version_id = pv.id
    return plan, pv, spec


def test_mock_taskspec_is_never_admitted_as_live_work(db, fakes):
    make_route(db)
    fakes.route_key = "worker.test-free"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    plan, pv, spec = _mock_plan(db, make_project(db))
    fakes.approved[plan.id] = pv.id
    tes = scheduler.ensure_executions(db, pv.id, actor="t")
    assert tes[0].mode == "MOCK"  # the execution keeps the fixture label
    scheduler.refresh_readiness(db, pv.id)
    adm = scheduler.admission_check(db, spec.id)
    assert not adm.admitted and ("NON_LIVE_TASKSPEC" in [c for c, _ in adm.blocks])
    with pytest.raises(Blocked):
        scheduler.acquire_lease(db, spec.id, actor="t")
    assert scheduler.admit_ready_tasks(db) == [] and fakes.calls.get("reserve", 0) == 0


# ----------------------------------------------------------------------------- 3. scope re-check at lease time (§21)
@pytest.mark.parametrize("allowed,forbidden,code", [
    (["../outside/**"], [], "UNSAFE_PATH"),
    (["C:/Windows/**"], [], "UNSAFE_PATH"),
    (["src/**"], ["~/secrets/**"], "UNSAFE_PATH"),
    (["Policy/**"], ["policy/**"], "FORBIDDEN_PATH_ALLOWED"),  # case-insensitive file systems
    (["SRC/Secret/keys.py"], ["src/secret/**"], "FORBIDDEN_PATH_ALLOWED"),
])
def test_scope_contract_is_rechecked_at_lease_time(db, fakes, allowed, forbidden, code):
    p = make_project(db)
    _plan, _pv, specs = make_plan(db, p, [{"task_key": "t1", "allowed_paths": allowed, "forbidden_paths": forbidden}])
    assert code in [c for c, _ in scheduler.scope_contract_problems(db, specs[0])]


def test_scope_contract_allows_carve_outs(db, fakes):
    p = make_project(db)
    _plan, _pv, specs = make_plan(db, p, [{"task_key": "t1", "allowed_paths": ["src/**"],
                                           "forbidden_paths": ["src/secret/**", ".github/workflows/**"]}])
    assert scheduler.scope_contract_problems(db, specs[0]) == []


# ----------------------------------------------------------------------------- 4. owner-only blocks (§21, §14)
@pytest.mark.parametrize("code", ["QUARANTINED", "REVIEW_BLOCKED"])
def test_quarantine_and_review_blocks_wait_for_an_owner_action(db, fakes, code):
    _plan, pv, task = _task_in(db, fakes, "REVIEW", attempts_used=1)
    old = WorkerRun(task_id=task.id, project_id=task.project_id, domain="PERSONAL", execution_status="REVIEW",
                    head_commit="c" * 40)
    db.add(old)
    db.flush()
    db.execute(update(TaskExecution).where(TaskExecution.task_id == task.id).values(active_run_id=old.id))
    scheduler.transition_task(db, task.id, "BLOCKED", reason=f"{code}: special review", actor="verifier",
                              reason_code=code)
    scheduler.refresh_readiness(db, pv.id)
    assert scheduler.get_te(db, task.id).state == "BLOCKED", "never retried by a scheduler tick"
    assert old.execution_status == "REVIEW"
    te = scheduler.retry_task(db, task.id, actor="user:owner", rationale="discard patch, new attempt")
    assert te.state == "READY"
    # the replaced candidate can never be reviewed, accepted or published later
    assert old.execution_status == "FAILED" and old.outcome_code == "DISCARDED_BY_OWNER"


# ----------------------------------------------------------------------------- 5. run-level reservation (§22, §42)
def test_partial_commits_keep_the_unused_hold_and_release_settles(db):
    db.add(ModelRoute(route_key="free-flash", pool="WORKER", provider="opencode-zen", model_id="flash", is_free="YES"))
    db.commit()
    res = cost.reserve(db, cost.ReservationRequest(run_id="run-a", project_id="p1", domain="PERSONAL",
                                                   route_key="free-flash", est_usd=0.0, est_tokens=1000,
                                                   est_requests=10))
    db.commit()
    for _ in range(2):
        cost.record_usage(db, provider="opencode-zen", category="WORKER_API", source="broker", run_id="run-a",
                          route_key="free-flash", input_tokens=100, output_tokens=50, reservation_id=res.id,
                          partial=True)
        db.commit()
    res = db.get(BudgetReservation, res.id)
    assert res.status == "RESERVED" and res.committed_tokens == 300 and res.committed_requests == 2
    budget = cost_service.ensure_default_budget(db)
    held = cost_service.held_usage(db, budget)
    assert held.tokens == 700 and held.requests == 8, "only the unused part is still held (no double count)"
    cost.release(db, res.id)
    db.commit()
    res = db.get(BudgetReservation, res.id)
    assert res.status == "COMMITTED" and cost_service.held_usage(db, budget).tokens == 0
    with pytest.raises(Conflict):  # a settled hold takes no further commits
        cost.record_usage(db, provider="opencode-zen", category="WORKER_API", source="broker", run_id="run-a",
                          route_key="free-flash", input_tokens=1, output_tokens=1, reservation_id=res.id,
                          partial=True)


def test_partial_unknown_cost_charges_the_remaining_estimate_once(db, monkeypatch):
    """UNKNOWN cost on a paid route charges the reserved estimate (ADR-0080) — once per run-level hold."""
    from tests.unit.test_cost_fakes import install_fake_policy

    install_fake_policy(monkeypatch)
    db.add(ModelRoute(route_key="paid-pro", pool="WORKER", provider="openrouter", model_id="pro", is_free="NO",
                      pricing={"input_per_mtok": 1.0}, pricing_checked_at=cost_service.utcnow()))
    db.commit()
    cost_service.set_budget(db, actor_user_id="owner-1", scope_type="GLOBAL", scope_id="*", period="MONTH",
                            limit_usd=1.0, rationale="owner budget for the test")
    db.commit()
    res = cost.reserve(db, cost.ReservationRequest(run_id="run-p", project_id="p1", domain="PERSONAL",
                                                   route_key="paid-pro", est_usd=0.5, est_tokens=100, est_requests=5))
    db.commit()
    first = cost.record_usage(db, provider="openrouter", category="WORKER_API", source="broker", run_id="run-p",
                              route_key="paid-pro", input_tokens=10, output_tokens=5, cost_usd=None,
                              reservation_id=res.id, partial=True)
    second = cost.record_usage(db, provider="openrouter", category="WORKER_API", source="broker", run_id="run-p",
                               route_key="paid-pro", input_tokens=10, output_tokens=5, cost_usd=None,
                               reservation_id=res.id, partial=True)
    db.commit()
    assert first.charged_usd == pytest.approx(0.5) and second.charged_usd == pytest.approx(0.0)
    assert first.cost_usd is None and second.cost_usd is None  # never shown as an actual cost
    with pytest.raises(Blocked):  # 0.5 charged (hold now empty) + 0.6 > 1.0
        cost.reserve(db, cost.ReservationRequest(run_id="run-q", project_id="p2", domain="PERSONAL",
                                                 route_key="paid-pro", est_usd=0.6, est_tokens=1, est_requests=1))
    db.rollback()


# ----------------------------------------------------------------------------- 6. broker usage + quota (§42, §50)
def test_broker_ledger_commits_live_hold_partially_and_releases_mock_hold(db, fakes, monkeypatch):
    import sys

    p = make_project(db)
    cost_impl = sys.modules["aeos.cost.service"]  # the FakeModules stand-in
    for mode in ("LIVE", "MOCK"):
        run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="RUNNING", mode=mode,
                        budget_reservation_id=f"res-{mode}")
        db.add(run)
        db.flush()
        prep = model_proxy.Prepared(grant_id="g", run_id=run.id, project_id=p.id, domain="PERSONAL", exposed="m",
                                    upstream=model_proxy.Upstream("https://x", None, "opencode-zen", "r", "m", False),
                                    body={}, request_id="q", stream=False)
        calls: list[dict[str, Any]] = []
        released: list[str] = []
        monkeypatch.setattr(cost_impl, "record_usage", lambda session, **kw: calls.append(kw))
        monkeypatch.setattr(cost_impl, "release", lambda session, rid: released.append(rid))
        assert model_proxy._record_ledger(db, prep, run, prompt=3, completion=2, cost=None, model="m") == "RECORDED"
        if mode == "LIVE":
            assert calls[0]["reservation_id"] == "res-LIVE" and calls[0]["partial"] is True and not released
        else:
            assert calls[0].get("reservation_id") is None and calls[0]["mode"] == "MOCK" and released == ["res-MOCK"]


def test_observed_429_is_written_through_the_cost_module(db, fakes):
    prep = model_proxy.Prepared(grant_id="g", run_id="r", project_id="p", domain="PERSONAL", exposed="m",
                                upstream=model_proxy.Upstream("https://x", None, "opencode-zen", "zen/route", "m",
                                                              False), body={}, request_id="q", stream=False)
    model_proxy._record_quota(db, prep, 90)
    assert fakes.calls.get("set_quota_state") == 1
    q = db.scalar(select(QuotaState).where(QuotaState.scope == "zen/route"))
    assert q.state == "EXHAUSTED" and q.source == "observed-429" and q.resets_at is not None


# ----------------------------------------------------------------------------- 7. credential namespaces (§26)
@pytest.mark.parametrize("domain,ref,project,ok", [
    ("PERSONAL", "workers/opencode-zen", None, True),
    ("PERSONAL", "workers/opencode-go", None, True),
    ("OFFICE", "workers/opencode-go", None, False),
    ("PERSONAL", "personal/openrouter", None, True),
    ("PERSONAL", "projects/p1/zen", "p1", True),
    ("PERSONAL", "projects/p2/zen", "p1", False),
    ("PERSONAL", "platform/openbao", None, False),
    ("PERSONAL", "office/zen", None, False),
    ("OFFICE", "workers/opencode-zen", None, False),
    ("OFFICE", "office/zen", None, True),
])
def test_credential_namespace_rule(domain, ref, project, ok):
    assert (broker.credential_ref_problem(domain, ref, project_id=project) is None) is ok


def test_secret_purposes_follow_the_secrets_contract():
    from aeos.delivery import provider
    from aeos.routing import catalog
    from aeos.secrets.provider import check_purpose

    check_purpose("workers/opencode-zen", model_proxy.secret_purpose("workers/opencode-zen"))
    check_purpose("workers/opencode-go", model_proxy.secret_purpose("workers/opencode-go"))
    check_purpose(*catalog.OPENCODE_GO_KEY_REF)  # the Go model list is read in the workers: purpose scope
    check_purpose("projects/p1/zen", model_proxy.secret_purpose("projects/p1/zen"))
    check_purpose(git_broker.GITHUB_SECRET_REF, git_broker.GITHUB_SECRET_PURPOSE)
    check_purpose(provider.SECRET_REF, provider.SECRET_PURPOSE)


def test_routing_rejects_routes_the_broker_could_not_authenticate(db, worker_keys):
    from aeos.routing.service import credential_rejections

    zen = ModelRoute(route_key="z", pool="WORKER", provider="opencode-zen",
                     endpoint="https://opencode.ai/inference/openai/v1",  # S07 (ADR-0041 amendment 2026-10-25)
                     model_id="m", mode_params={})
    assert credential_rejections(zen, domain="PERSONAL")
    zen.mode_params = {"credential_ref": "workers/opencode-zen"}
    assert credential_rejections(zen, domain="PERSONAL") == []
    assert credential_rejections(zen, domain="OFFICE")  # an OFFICE call never uses a workers/ key
    loop = ModelRoute(route_key="l", pool="WORKER", provider="local-test", endpoint="http://127.0.0.1:9/v1",
                      model_id="m", mode_params={})
    assert credential_rejections(loop, domain="PERSONAL") == []


def test_seed_sets_and_backfills_credential_ref(db):
    from aeos.routing import seed
    from aeos.routing.service import compute_config_hash

    seed.run(db)
    zen = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "opencode-zen/deepseek-v4.1-flash-free"))
    assert zen.mode_params["credential_ref"] == "workers/opencode-zen"
    zen.mode_params = {}
    zen.config_hash = compute_config_hash(zen)
    db.flush()
    out = seed.run(db)
    assert out["credential_ref_backfilled"] >= 1
    assert zen.mode_params["credential_ref"] == "workers/opencode-zen" and zen.config_hash == compute_config_hash(zen)
    # OpenCode Go (ADR-0041 amendment 2026-10-26): its own reference, never the Zen key
    go = db.scalars(select(ModelRoute).where(ModelRoute.provider == "opencode-go")).all()
    assert go and {r.mode_params["credential_ref"] for r in go} == {"workers/opencode-go"}
    go[0].mode_params = {k: v for k, v in go[0].mode_params.items() if k != "credential_ref"}
    go[0].config_hash = compute_config_hash(go[0])
    db.flush()
    seed.run(db)
    assert go[0].mode_params["credential_ref"] == "workers/opencode-go"
    assert go[0].config_hash == compute_config_hash(go[0])


# ----------------------------------------------------------------------------- 8. sha-pinned push (§31)
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()


def test_refspec_accepts_only_full_sha_pinned_sources():
    sha = "a" * 40
    assert git_broker.validate_refspec(f"{sha}:refs/heads/aeos/t-1")[0] == sha
    for bad in (f"+{sha}:refs/heads/aeos/t-1", "abc1234:refs/heads/aeos/t-1", f"{sha}:refs/heads/main",
                f"{sha}:refs/tags/v1", "HEAD:refs/heads/aeos/t-1"):
        with pytest.raises(PolicyDenied):
            git_broker.validate_refspec(bad)


def test_push_is_sha_pinned_returns_head_sha_and_notifies_delivery(db, fakes, tmp_path):
    branch = "aeos/task-a-1234abcd"
    src = tmp_path / "candidate"
    base = init_git_repo(src)
    _git(src, "checkout", "-q", "-b", branch)
    (src / "feature.txt").write_text("work\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "candidate")
    head = _git(src, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, timeout=60)
    p = make_project(db)
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="REVIEW", workspace_ref=str(src),
                    branch=branch, head_commit=head, base_commit=base)
    db.add(run)
    db.flush()
    record_claude_review(db, run)  # owner rule R44: the Claude review PASS of this head (ADR-0132)
    pr = PullRequestRecord(project_id=p.id, domain="PERSONAL", task_id="t", run_id=run.id, acceptance_id="a",
                           repo_full_name="owner/repo", number=3, head_ref=branch, base_ref="main",
                           accepted_head_sha="9" * 40, state="OPEN", bundle_hash="sha256:x")
    db.add(pr)
    db.flush()
    seen: list[dict[str, Any]] = []

    real = sync.on_branch_updated

    def spy(session, **kw):
        seen.append(kw)
        return real(session, **kw)

    sync.on_branch_updated = spy  # type: ignore[assignment]
    try:
        broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="GIT_BRANCH",
                           scope={"repo": "owner/repo", "branch": branch}, ttl_seconds=300, max_requests=2)
        with pytest.raises(PolicyDenied) as exc:
            git_broker.push_task_branch(db, run_id=run.id, worktree=str(src), branch=branch,
                                        repo_full_name="owner/repo", remote_url=str(bare), expected_head_sha="1" * 40)
        assert exc.value.code == "HEAD_MISMATCH"
        res = git_broker.push_task_branch(db, run_id=run.id, worktree=str(src), branch=branch,
                                          repo_full_name="owner/repo", remote_url=str(bare), expected_head_sha=head)
    finally:
        sync.on_branch_updated = real  # type: ignore[assignment]
    assert res["status"] == "PUSHED" and res["head_sha"] == head
    assert _git(bare, "rev-parse", f"refs/heads/{branch}") == head
    assert seen == [{"repo": "owner/repo", "branch": branch, "head_sha": head, "actor": "broker"}]
    assert pr.observed_head_sha == head  # the open PR re-observed the pushed head


# ----------------------------------------------------------------------------- 9. delivery result handling (§14, §31)
class _Broker:
    """Minimal broker facade stand-in recording the delivery calls."""

    def __init__(self, push_status: str, reconcile_status: str = "SUCCEEDED", head: str = "h" * 40):
        self.calls: list[str] = []
        self.push_status, self.reconcile_status, self.head = push_status, reconcile_status, head

    def issue_grant(self, session, **kw):
        self.calls.append(f"grant:{kw['kind']}")
        return broker.issue_grant(session, **kw)

    def revoke_grants(self, session, **kw):
        self.calls.append("revoke")
        return broker.revoke_grants(session, **kw)

    def push_task_branch(self, session, **kw):
        self.calls.append(f"push:{kw.get('expected_head_sha')}")
        return {"status": self.push_status, "ok": self.push_status == "PUSHED", "head_sha": self.head,
                "side_effect_id": "se-1", "error": "remote rejected"}

    def reconcile_side_effect(self, session, **kw):
        self.calls.append(f"reconcile:{kw['side_effect_id']}")
        return {"id": kw["side_effect_id"], "status": self.reconcile_status}


def _run_for_delivery(db):
    p = make_project(db)
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="DONE", branch="aeos/t-1",
                    workspace_ref="/x", head_commit="h" * 40)
    db.add(run)
    db.flush()
    return run


@pytest.mark.parametrize("status,reconciled,code", [
    ("FAILED", "SUCCEEDED", "BROKER_PUSH_FAILED"),
    ("UNKNOWN", "UNKNOWN", "BROKER_PUSH_UNKNOWN"),
    ("UNKNOWN", "FAILED", "BROKER_PUSH_UNKNOWN"),
])
def test_delivery_treats_failed_and_unknown_push_as_refusal(db, status, reconciled, code):

    run = _run_for_delivery(db)
    fake = _Broker(status, reconciled)
    with pytest.raises(Blocked) as exc:
        pr_bundle._push_branch(db, fake, run, repo="owner/repo", accepted_head=run.head_commit, notes=[])
    assert exc.value.code == code
    if status == "UNKNOWN":
        assert "reconcile:se-1" in fake.calls, "an UNKNOWN push is reconciled before anything else"


def test_delivery_continues_after_unknown_push_reconciled_as_landed(db):

    run = _run_for_delivery(db)
    fake = _Broker("UNKNOWN", "SUCCEEDED")
    res = pr_bundle._push_branch(db, fake, run, repo="owner/repo", accepted_head=run.head_commit, notes=[])
    assert res["status"] == "PUSHED" and fake.calls == [f"push:{run.head_commit}", "reconcile:se-1"]


def test_delivery_reconciles_open_side_effects_before_retrying(db):
    from aeos.models.broker_ext import BrokerSideEffect

    run = _run_for_delivery(db)
    db.add(BrokerSideEffect(id="se-old", run_id=run.id, project_id=run.project_id, domain="PERSONAL",
                            kind="PUSH_BRANCH", idempotency_key="k-old", status="UNKNOWN", target={}))
    db.flush()
    fake = _Broker("PUSHED", reconcile_status="UNKNOWN")
    with pytest.raises(Blocked) as exc:
        pr_bundle._push_branch(db, fake, run, repo="owner/repo", accepted_head=run.head_commit, notes=[])
    assert exc.value.code == "BROKER_PUSH_UNKNOWN" and fake.calls == ["reconcile:se-old"]  # no push


def test_delivery_issues_scoped_grants_and_returns_their_expiry(db):

    run = _run_for_delivery(db)
    fake = _Broker("PUSHED")
    expires = pr_bundle._issue_delivery_grants(db, fake, run, repo="owner/repo", base="main")
    grants = {g.kind: g for g in db.scalars(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id))}
    assert set(grants) == {"GIT_BRANCH", "PR_CREATE"} and expires == min(g.expires_at for g in grants.values())
    assert grants["PR_CREATE"].scope == {"repo": "owner/repo", "head": "aeos/t-1", "base": "main"}


# ----------------------------------------------------------------------------- 10. verification side (§34)
def test_workforce_accepts_the_verifier_vocabulary(db, fakes):
    p = make_project(db)
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="REVIEW")
    db.add(run)
    db.flush()
    for status in ("PASS", "FAIL", "QUARANTINED", "NOT_PROVEN", "BLOCKED"):
        assert scheduler.update_run(db, run.id, actor="verifier", verifier_status=status).verifier_status == status
    scheduler.update_run(db, run.id, actor="reviewer", quota_status="WAITING_QUOTA")
    assert run.quota_status == "WAITING_QUOTA"


def test_acceptance_invalidation_reopens_the_task(db, monkeypatch):
    from tests.unit.test_verification_support import (
        FakeWorkforce,
        make_review,
        make_world,
        record_verification,
    )

    wf = FakeWorkforce().install(monkeypatch)
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "ACCEPTED"
    assert rec.head_commit == w.run.head_commit and rec.base_commit == w.run.base_commit
    acceptance.invalidate(db, task_id=w.task.id, reason="PR head changed", actor="delivery", head_commit="9" * 40)
    reopened = wf.of("reopen_for_reverification")
    assert len(reopened) == 1 and reopened[0]["task_id"] == w.task.id and "PR head changed" in reopened[0]["reason"]
    acceptance.invalidate(db, task_id=w.task.id, reason="again", actor="delivery", head_commit="8" * 40)
    assert len(wf.of("reopen_for_reverification")) == 1, "only an ACCEPTED record reopens"


def test_verifier_loop_persists_drift_and_reopens_but_not_on_unknown_alone(db, monkeypatch):
    from aeos.verification import loop
    from tests.unit.test_verification_support import (
        FakeWorkforce,
        make_review,
        make_world,
        record_verification,
    )

    wf = FakeWorkforce().install(monkeypatch)
    w = make_world(db)
    make_review(db, w, record_verification(db, w))
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    assert rec.acceptance_status == "ACCEPTED"
    original = acceptance.current_config_hash
    monkeypatch.setattr(acceptance, "current_config_hash", lambda task: None)  # suite transiently unreadable
    assert loop.recheck_accepted() == [] and not wf.of("reopen_for_reverification")
    monkeypatch.setattr(acceptance, "current_config_hash", original)
    w.run.head_commit = "7" * 40  # the runtime recorded another head for this run
    db.commit()
    out = loop.recheck_accepted()
    assert [o["status"] for o in out] == ["STALE"]
    assert [c["task_id"] for c in wf.of("reopen_for_reverification")] == [w.task.id]


def test_binary_scan_finds_tokens_glued_to_other_bytes():
    scope = verification_scope

    token = "gh" + "p_" + "Q7w" * 12
    assert scope.scan_binary_secrets("blob.bin", b"\x00PK" + token.encode() + b"\x00")


# ----------------------------------------------------------------------------- 11. review hardening (2026-10-19)
def test_owner_block_survives_a_transient_co_reason(db, fakes):
    """A quarantine/review block keeps its code while e.g. a side effect is unreconciled; once that
    co-reason clears the task must still wait for the owner, never become READY on a tick."""
    from aeos.models.broker_ext import BrokerSideEffect

    _plan, pv, task = _task_in(db, fakes, "REVIEW", attempts_used=1)
    scheduler.transition_task(db, task.id, "BLOCKED", reason="QUARANTINED: special review", actor="verifier",
                              reason_code="QUARANTINED")
    run = WorkerRun(task_id=task.id, project_id=task.project_id, domain="PERSONAL", execution_status="REVIEW")
    db.add(run)
    db.flush()
    se = BrokerSideEffect(run_id=run.id, project_id=task.project_id, domain="PERSONAL", kind="PUSH_BRANCH",
                          idempotency_key="k-transient", status="UNKNOWN", target={})
    db.add(se)
    db.flush()
    scheduler.refresh_readiness(db, pv.id)
    te = scheduler.get_te(db, task.id)
    assert te.state == "BLOCKED" and te.reason_code == "QUARANTINED"
    se.status = "SUCCEEDED"
    db.flush()
    for _ in range(2):
        scheduler.refresh_readiness(db, pv.id)
    te = scheduler.get_te(db, task.id)
    assert te.state == "BLOCKED" and te.reason_code == "QUARANTINED" and te.reason == "QUARANTINED: special review"


def test_review_quota_is_scoped_per_family_and_lifted_by_the_next_completed_review(db, monkeypatch):
    """§42/§50: an observed Max limit waits the review; it must not stay EXHAUSTED forever (scope ``*``
    without a reset time was never cleared) and the run stops showing WAITING_QUOTA once a review got through."""
    from aeos.verification import review
    from tests.unit.test_verification_support import (
        FakeBridge,
        FakeWorkforce,
        claude_result,
        install_doctor,
        make_world,
        record_verification,
        review_payload,
    )

    FakeWorkforce().install(monkeypatch)
    install_doctor(monkeypatch)
    w = make_world(db)
    record_verification(db, w)
    FakeBridge(result=claude_result(None, ok=False, quota=True, model=None)).install(monkeypatch)
    out = review.review_run(db, w.run.id, actor="t")
    assert out.review.status == "WAITING_QUOTA" and w.run.quota_status == "WAITING_QUOTA"
    rows = list(db.scalars(select(QuotaState).where(QuotaState.provider == "claude-native")))
    assert [(r.scope, r.state, r.source) for r in rows] == [("fable", "EXHAUSTED", "observed-limit")]
    FakeBridge(result=claude_result(review_payload())).install(monkeypatch)
    out = review.review_run(db, w.run.id, actor="t")
    assert out.review.status == "COMPLETED" and w.run.quota_status is None
    q = db.scalar(select(QuotaState).where(QuotaState.provider == "claude-native", QuotaState.scope == "fable"))
    assert q.state == "UNKNOWN" and q.source == "observed-successful-call"  # never "OK" for Max
    assert review.native_quota_scope("claude-opus-5-5") == "opus" and review.native_quota_scope(None) == "max"


def test_branch_update_observation_failure_never_hides_a_landed_push(db, monkeypatch):
    def refuse(session, **kw):
        raise Conflict("observation refused", code="OBSERVATION_REFUSED")

    monkeypatch.setattr(sync, "on_branch_updated", refuse)
    assert git_broker._branch_updated(db, "owner/repo", "aeos/t-1", "a" * 40) == \
        "OBSERVATION_FAILED (OBSERVATION_REFUSED)"


def test_routing_credential_rule_matches_the_broker(db, worker_keys):
    """Routing must not select a route the broker refuses: host parsing for loopback (not a prefix
    match) and no default OpenRouter credential for OFFICE (§26)."""
    from aeos.routing.service import credential_rejections

    look_alike = ModelRoute(route_key="e", pool="WORKER", provider="local-test", model_id="m", mode_params={},
                            endpoint="http://localhost.attacker.example/v1")
    assert credential_rejections(look_alike, domain="PERSONAL")
    assert model_proxy._is_loopback_http("http://localhost.attacker.example/v1") is False
    real_loop = ModelRoute(route_key="l2", pool="WORKER", provider="local-test", model_id="m", mode_params={},
                           endpoint="http://localhost:8099/v1")
    assert credential_rejections(real_loop, domain="PERSONAL") == []
    openrouter = ModelRoute(route_key="o", pool="WORKER", provider="openrouter", model_id="m", mode_params={})
    assert credential_rejections(openrouter, domain="PERSONAL") == []
    assert credential_rejections(openrouter, domain="OFFICE")
