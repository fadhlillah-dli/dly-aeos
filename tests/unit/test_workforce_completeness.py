"""Completeness-review fixes for module group E, mapped to master §14/§19/§21/§22/§35/§50 rows."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta

import psutil
import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked, Conflict, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.cost import QuotaState
from aeos.models.decisions import Decision
from aeos.models.evidence import AcceptanceRecord
from aeos.models.identity import User
from aeos.models.workforce import AgentDefinition, RoleSkillPack, TaskExecution, WorkerRun
from aeos.workforce import scheduler, seed, views
from tests.unit.test_workforce_support import (
    FakeModules,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)


@pytest.fixture(name="fakes")
def _fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def _ready(db, fakes, tasks=None, *, project=None):
    project = project or make_project(db)
    plan, pv, specs = make_plan(db, project, tasks or [{"task_key": "a", "allowed_paths": ["src/**"]}])
    fakes.approved[plan.id] = pv.id
    if fakes.route_key is None:
        make_route(db)
        fakes.route_key = "worker.test-free"
        seed_and_enable_implementer(db)
        process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="test", requested_by="owner")
    scheduler.refresh_readiness(db, pv.id)
    return project, specs


def _to_review(db, fakes, *, head="c0ffee" * 6 + "abcd"):
    _p, specs = _ready(db, fakes)
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    scheduler.transition_task(db, tid, "RUNNING", reason="b", actor="t")
    scheduler.set_run_status(db, run, "RUNNING", reason="b", actor="t")
    scheduler.transition_task(db, tid, "REVIEW", reason="exit 0", actor="t")
    scheduler.set_run_status(db, run, "REVIEW", reason="exit 0", actor="t")
    run.head_commit = head
    db.flush()
    return tid, run


def _codes(adm) -> set[str]:
    return {c for c, _ in [*adm.waits, *adm.blocks]}


# ----------------------------------------------------------------------------- admission (§19, §14, §22)
def test_admission_waits_without_verifier(db, monkeypatch):
    """§19 'siapa verifier dan penerima hasilnya': no worker is started whose result could never be
    accepted (verification module absent -> fail closed, no run/cost)."""
    fk = FakeModules().install(monkeypatch, skip=("aeos.verification.acceptance",))
    _p, specs = _ready(db, fk)
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted and "VERIFIER_UNAVAILABLE" in _codes(adm)
    with pytest.raises(Blocked):
        scheduler.acquire_lease(db, specs[0].id, actor="t")
    assert db.scalar(select(WorkerRun)) is None


def test_admission_waits_on_unreconciled_side_effect(db, fakes):
    """§14 'Crash recovery membaca ... publication records dan status eksternal sebelum retry'."""
    p, specs = _ready(db, fakes)
    old = WorkerRun(task_id=specs[0].id, project_id=p.id, domain="PERSONAL", execution_status="FAILED")
    db.add(old)
    db.flush()
    se = BrokerSideEffect(run_id=old.id, project_id=p.id, domain="PERSONAL", kind="PUSH_BRANCH",
                          target={"branch": "aeos/a-1"}, idempotency_key="k1", status="UNKNOWN")
    db.add(se)
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    assert "SIDE_EFFECT_UNRECONCILED" in _codes(adm) and not adm.admitted
    se.status = "SUCCEEDED"
    db.flush()
    assert scheduler.admission_check(db, specs[0].id).admitted


def test_admission_waits_while_previous_worker_process_alive(db, fakes):
    """§50 'Controller crash dan worker lama masih aktif' -> no duplicate before reconciliation."""
    p, specs = _ready(db, fakes)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        old = WorkerRun(task_id=specs[0].id, project_id=p.id, domain="PERSONAL", execution_status="FAILED",
                        pid=proc.pid, pid_create_time=psutil.Process(proc.pid).create_time())
        db.add(old)
        db.flush()
        adm = scheduler.admission_check(db, specs[0].id)
        assert "LEASE_LOST_PROCESS_ALIVE" in _codes(adm) and not adm.admitted
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert scheduler.admission_check(db, specs[0].id).admitted


def test_admission_waits_on_exhausted_route_quota_without_fallback(db, fakes):
    """§50 'Free quota habis / 429' -> backoff/queue on the same approved route, never a paid one."""
    _p, specs = _ready(db, fakes)
    make_route(db, key="worker.paid", model_id="vendor/paid", is_free="NO")
    q = QuotaState(provider="openrouter", scope="worker.test-free", state="EXHAUSTED", source="observed-429",
                   resets_at=utcnow() + timedelta(minutes=5))
    db.add(q)
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    assert "QUOTA_EXHAUSTED" in _codes(adm) and adm.route.route_key == "worker.test-free"
    assert scheduler.admit_ready_tasks(db) == []
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "READY" and views.derive_ui_state(None, te) == "WAITING_QUOTA"
    q.resets_at = utcnow() - timedelta(seconds=1)
    db.flush()
    assert scheduler.admission_check(db, specs[0].id).admitted


def test_lease_records_role_and_agent_revisions(db, fakes):
    """§50 'Role/model/mode diganti' -> configuration is versioned per attempt; history kept."""
    _p, specs = _ready(db, fakes, [{"task_key": "a", "role_key": "backend-api-engineer"}])
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer"))
    role = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "backend-api-engineer"))
    assert run.agent_revision_hash == agent.revision_hash and run.role_revision_hash == role.revision_hash
    old = run.agent_revision_hash
    agent.revision_hash = "sha256:changed"
    db.flush()
    assert db.get(WorkerRun, run.id).agent_revision_hash == old  # history not rewritten


# ----------------------------------------------------------------------------- repair & acceptance (§19, §21, §22)
def test_repair_closes_reviewed_attempt_as_failed(db, fakes):
    """Reviewer findings: the reviewed attempt ends FAILED (REVIEW_FINDINGS) and can never be
    accepted later; the repair is a new attempt (§19 'repair dilakukan worker baru/attempt baru')."""
    tid, run = _to_review(db, fakes)
    te = scheduler.request_repair(db, tid, findings=["fix x"], review_record_id=None, actor="reviewer")
    assert te.state == "READY" and te.active_run_id is None
    old = db.get(WorkerRun, run.id)
    assert old.execution_status == "FAILED" and old.outcome_code == "REVIEW_FINDINGS"
    assert views.derive_ui_state(old, te) == "FAILED"  # not "waiting for review"
    rec = AcceptanceRecord(task_id=tid, run_id=run.id, project_id=run.project_id, artifact_hash="sha256:x",
                           acceptance_status="ACCEPTED")
    db.add(rec)
    db.flush()
    fakes.accepted.add(tid)
    with pytest.raises(Conflict):
        scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")


def test_quarantined_patch_needs_owner_release_bound_to_head(db, fakes):
    """§21 'patch karantina yang melalui review khusus sebelum dipromosikan' + human-only AUTH."""
    tid, run = _to_review(db, fakes)
    run.quarantined = True
    run.quarantine_reasons = [{"path": "policy/x", "rule": "protected-path", "detail": "policy/**"}]
    db.flush()
    rec = AcceptanceRecord(task_id=tid, run_id=run.id, project_id=run.project_id, artifact_hash="sha256:x",
                           acceptance_status="ACCEPTED")
    db.add(rec)
    db.flush()
    fakes.accepted.add(tid)
    with pytest.raises(Blocked) as exc:
        scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    assert exc.value.code == "QUARANTINE_REVIEW_REQUIRED"
    with pytest.raises(ValidationFailed):
        scheduler.release_quarantine(db, run.id, actor="user:o", actor_user_id="o", rationale="  ")
    d = scheduler.release_quarantine(db, run.id, actor="user:o", actor_user_id="owner-1",
                                     rationale="special review of policy/x done; change is intended")
    assert d.kind == "EXCEPTION" and d.actor_kind == "HUMAN" and d.target_hash == run.head_commit
    # A different patch (new head) is not covered by the old decision.
    run.head_commit = "f" * 40
    db.flush()
    with pytest.raises(Blocked):
        scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    run.head_commit = d.target_hash
    db.flush()
    te = scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    assert te.state == "DONE"


def test_release_quarantine_guards(db, fakes, monkeypatch):
    tid, run = _to_review(db, fakes)
    with pytest.raises(Conflict):  # not quarantined
        scheduler.release_quarantine(db, run.id, actor="user:o", actor_user_id="o", rationale="reviewed ok")
    run.quarantined = True
    db.flush()
    monkeypatch.delitem(sys.modules, "aeos.policy.decisions")
    with pytest.raises(Blocked) as exc:  # policy module missing -> fail closed, no decision
        scheduler.release_quarantine(db, run.id, actor="user:o", actor_user_id="o", rationale="reviewed ok")
    assert exc.value.code == "POLICY_UNAVAILABLE"
    assert db.scalar(select(Decision).where(Decision.target_type == "run_quarantine")) is None


# ----------------------------------------------------------------------------- views honesty + scope (§35, §50)
def test_derived_ui_state_never_borrows_another_attempt(db, fakes):
    """§50 'Worker selesai tetapi verifier gagal -> UI menunjukkan kegagalan ... bukan accepted' and
    'Lease lama ... tidak menimpa state attempt baru'."""
    p, specs = _ready(db, fakes)
    te = scheduler.get_te(db, specs[0].id)
    failed = WorkerRun(task_id=specs[0].id, project_id=p.id, domain="PERSONAL", execution_status="FAILED")
    old = WorkerRun(task_id=specs[0].id, project_id=p.id, domain="PERSONAL", execution_status="CANCELLED")
    db.add_all([failed, old])
    db.flush()
    db.execute(TaskExecution.__table__.update().where(TaskExecution.id == te.id)
               .values(state="REVIEW", active_run_id=failed.id))
    db.flush()
    te = scheduler.get_te(db, specs[0].id)
    assert views.derive_ui_state(failed, te) == "FAILED"
    assert views.derive_ui_state(old, te) == "CANCELLED"
    assert views.derive_ui_state(None, te) == "WAITING_REVIEW"


def test_counts_follow_project_scope(personal_client, db):
    """§50 'Viewer mengakses ... backend scope diterapkan termasuk counts': active-run counts in the
    summary, role and agent views only include projects the principal may see."""
    seed.run(db)
    allowed, hidden = make_project(db), make_project(db)
    for proj in (allowed, hidden):
        db.add(WorkerRun(project_id=proj.id, domain="PERSONAL", execution_status="RUNNING", mode="LIVE",
                         role_key="backend-api-engineer", agent_key="opencode-implementer"))
    owner = db.scalar(select(User).where(User.username == "owner"))
    owner.project_scope = [allowed.id]
    db.commit()
    s = personal_client.get("/api/workforce/summary").json()
    assert s["active_runs"] == 1
    roles = [r for g in personal_client.get("/api/workforce/roles").json()["items"] for r in g["roles"]]
    assert {r["role_key"]: r["active_runs"] for r in roles}["backend-api-engineer"] == 1
    agents = {a["agent_key"]: a for a in personal_client.get("/api/workforce/agents").json()["items"]}
    assert agents["opencode-implementer"]["active_runs"] == 1
    assert len(personal_client.get("/api/runs").json()["items"]) == 1


def test_quota_rows_follow_domain(personal_client, db):
    make_route(db, key="worker.office-only", domains=["OFFICE"])
    make_route(db, key="worker.personal", domains=["PERSONAL"])
    db.add_all([QuotaState(provider="openrouter", scope="worker.office-only", state="EXHAUSTED"),
                QuotaState(provider="openrouter", scope="worker.personal", state="EXHAUSTED")])
    db.commit()
    quota = personal_client.get("/api/workforce/summary").json()["cost"]["quota"]
    assert {q["scope"] for q in quota} == {"worker.personal"}


# ----------------------------------------------------------------------------- quarantine release API (§21, §35)
def test_quarantine_release_api_rbac_and_scope(personal_client, client, db, fakes):
    tid, run = _to_review(db, fakes)
    run.quarantined = True
    office = WorkerRun(project_id="office-p", domain="OFFICE", execution_status="REVIEW", quarantined=True,
                       head_commit="a" * 40)
    db.add(office)
    db.commit()
    short = personal_client.post(f"/api/runs/{run.id}/quarantine-release", json={"rationale": "ok"})
    assert short.status_code == 422  # a real rationale is required
    r = personal_client.post(f"/api/runs/{run.id}/quarantine-release",
                             json={"rationale": "special review done: protected change intended"})
    assert r.status_code == 200 and r.json()["target_hash"] == run.head_commit
    assert personal_client.post(f"/api/runs/{office.id}/quarantine-release",
                                json={"rationale": "special review done here"}).status_code == 404
    d = db.scalar(select(Decision).where(Decision.target_type == "run_quarantine"))
    assert d is not None and d.actor_user_id and d.target_id == run.id


# ----------------------------------------------------------------------------- CLI output is ASCII (AGENTS.md)
def test_cli_status_output_is_ascii_even_with_foreign_reasons(settings, db):
    """Reasons from other modules may contain non-ASCII ("§", "—") or brackets; the CLI output must
    stay ASCII for legacy Windows consoles and must not be interpreted as markup."""
    from typer.testing import CliRunner

    from aeos.workforce.cli import app as work_app

    p = make_project(db)
    _plan, _pv, specs = make_plan(db, p, [{"task_key": "a"}])
    db.add(TaskExecution(task_id=specs[0].id, project_id=p.id, domain="PERSONAL", state="BLOCKED",
                         reason="menunggu keputusan §21 — [bold]bukan markup[/bold]",
                         reason_code="NEEDS_DECISION", task_key="a"))
    db.commit()
    r = CliRunner().invoke(work_app, ["status"])
    assert r.exit_code == 0, r.output
    assert all(ord(c) < 128 for c in r.output)
    flat = " ".join(r.output.split())  # the console wraps long lines
    assert "[bold]bukan markup[/bold]" in flat and "s21 - " in flat


def test_acceptance_bound_to_runtime_head_and_head_not_writable(db, fakes):
    """§14 'Perubahan head/base yang relevan membatalkan acceptance/approval lama'."""
    tid, run = _to_review(db, fakes)
    fakes.accepted.add(tid)
    rec = AcceptanceRecord(task_id=tid, run_id=run.id, project_id=run.project_id, artifact_hash="sha256:x",
                           acceptance_status="ACCEPTED", head_commit="9" * 40)
    db.add(rec)
    db.flush()
    with pytest.raises(Conflict) as exc:
        scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    assert exc.value.code == "ACCEPTANCE_HEAD_MISMATCH"
    with pytest.raises(ValidationFailed):  # the runtime head is a binding, not an observed field
        scheduler.update_run(db, run.id, actor="x", head_commit=rec.head_commit)
    rec2 = AcceptanceRecord(task_id=tid, run_id=run.id, project_id=run.project_id, artifact_hash="sha256:y",
                            acceptance_status="ACCEPTED", head_commit=run.head_commit)
    db.add(rec2)
    db.flush()
    assert scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec2.id, actor="v").state == "DONE"


# ----------------------------------------------------------------------------- §22 stop when policy changes
def test_running_attempt_stops_when_plan_approval_is_withdrawn(db, fakes):
    """§22 'Stop berlaku saat ... policy berubah': capability revoked, lease revoked, task BLOCKED."""
    from aeos.broker import facade as broker
    from aeos.models.routing import CapabilityGrant

    _p, specs = _ready(db, fakes)
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain="PERSONAL", kind="MODEL_PROXY",
                       scope={"models": {"m": "worker.test-free"}}, ttl_seconds=600)
    scheduler.transition_task(db, tid, "RUNNING", reason="bound", actor="t")
    scheduler.set_run_status(db, run, "RUNNING", reason="bound", actor="t")
    assert scheduler.enforce_stop_conditions(db)["stopped"] == []  # still authorised
    fakes.approved.clear()  # the owner's approval decision was invalidated
    out = scheduler.enforce_stop_conditions(db)
    assert out["stopped"] == [run.id]
    r = db.get(WorkerRun, run.id)
    te = scheduler.get_te(db, tid)
    assert r.execution_status == "FAILED" and r.outcome_code == "POLICY_CHANGED"
    assert te.state == "BLOCKED" and te.reason_code == "POLICY_CHANGED"
    assert db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id)).active is False


def test_running_attempt_stops_when_sandbox_exception_revoked(db, fakes):
    _p, specs = _ready(db, fakes)
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    for d in db.scalars(select(Decision).where(Decision.target_type == "worker_sandbox")):
        d.status = "INVALIDATED"
    db.flush()
    assert scheduler.enforce_stop_conditions(db)["stopped"] == [run.id]
    assert scheduler.get_te(db, specs[0].id).state == "BLOCKED"  # LEASED -> BLOCKED


def test_bash_under_process_sandbox_needs_explicit_exception(db, fakes):
    """ADR-0051: a shell under the WEAK process sandbox acts as the same OS user (OS credential
    helpers included), so the owner exception must allow it explicitly."""
    _p, specs = _ready(db, fakes, [{"task_key": "a", "tools_allowed": ["bash:pytest *"]}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted and any("allow_bash" in m for c, m in adm.waits if c == "SANDBOX_NOT_READY")
    d = db.scalar(select(Decision).where(Decision.target_type == "worker_sandbox"))
    d.details = {"allow_process_sandbox": True, "allow_bash": True}
    db.flush()
    assert scheduler.admission_check(db, specs[0].id).admitted
