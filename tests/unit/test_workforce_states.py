"""§14 TaskExecution state machine: every legal and illegal transition, optimistic concurrency,
DONE only via valid acceptance (master §14, §50 "Worker exit 0 ... REVIEW/NOT-PROVEN")."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from aeos.core.enums import ExecutionState
from aeos.core.errors import Conflict
from aeos.models.audit import AuditEvent, Event
from aeos.models.workforce import TaskExecution
from aeos.workforce import scheduler
from aeos.workforce.states import LEGAL_TRANSITIONS, RUNNING_STOP_CODES, is_legal
from tests.unit.test_workforce_support import make_plan, make_project

S = ExecutionState
ALL = list(S)


def _task(db, state: str):
    p = make_project(db)
    _plan, pv, specs = make_plan(db, p, [{"task_key": "t1"}])
    scheduler.ensure_executions(db, pv.id, actor="test")
    db.execute(update(TaskExecution).where(TaskExecution.task_id == specs[0].id).values(state=state))
    db.flush()
    return specs[0]


EXPECTED = {
    (S.QUEUED, S.READY), (S.QUEUED, S.BLOCKED), (S.BLOCKED, S.READY), (S.READY, S.LEASED), (S.LEASED, S.RUNNING),
    (S.RUNNING, S.REVIEW), (S.RUNNING, S.FAILED), (S.RUNNING, S.CANCELLED), (S.REVIEW, S.READY),
    (S.REVIEW, S.DONE), (S.REVIEW, S.BLOCKED), (S.LEASED, S.BLOCKED), (S.READY, S.CANCELLED),
    # cancellation from every non-terminal state (§14 text)
    (S.QUEUED, S.CANCELLED), (S.BLOCKED, S.CANCELLED), (S.LEASED, S.CANCELLED), (S.REVIEW, S.CANCELLED),
    # lease expiry / stop condition while RUNNING (only with a stop reason code)
    (S.RUNNING, S.BLOCKED),
    # acceptance STALE after DONE (§14 re-verification): only via reopen_for_reverification
    (S.DONE, S.READY), (S.DONE, S.BLOCKED),
}


def test_transition_table_matches_master_diagram_exactly():
    actual = {(a, b) for a, targets in LEGAL_TRANSITIONS.items() for b in targets}
    assert actual == EXPECTED
    for terminal in (S.FAILED, S.CANCELLED):
        assert LEGAL_TRANSITIONS[terminal] == frozenset()
    # DONE is left only with the ACCEPTANCE_STALE reopen code (never a free exit)
    assert not is_legal("DONE", "READY") and is_legal("DONE", "READY", reason_code="ACCEPTANCE_STALE")
    assert not is_legal("DONE", "CANCELLED", reason_code="ACCEPTANCE_STALE")


@pytest.mark.parametrize("src,dst", [(a, b) for a in ALL for b in ALL])
def test_every_pair_legal_or_conflict(db, src, dst):
    task = _task(db, src.value)
    # DONE is entered only via mark_accepted and left only via reopen_for_reverification
    legal = (src, dst) in EXPECTED and dst is not S.DONE and src is not S.DONE
    code = "LEASE_LOST" if (src, dst) == (S.RUNNING, S.BLOCKED) else ""
    if legal:
        te = scheduler.transition_task(db, task.id, dst.value, reason="test", actor="test", reason_code=code)
        assert te.state == dst.value
        assert te.row_version == 2
        assert db.scalar(select(Event).where(Event.kind == "task.state_changed", Event.entity_id == task.id,
                                             Event.payload["to"].as_string() == dst.value)) is not None
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "workforce.task.transition",
                                                  AuditEvent.target_id == task.id)) is not None
    else:
        with pytest.raises(Conflict):
            scheduler.transition_task(db, task.id, dst.value, reason="test", actor="test", reason_code=code)
        assert db.get(TaskExecution, scheduler.get_te(db, task.id).id).state == src.value


def test_running_to_blocked_requires_stop_code(db):
    task = _task(db, S.RUNNING.value)
    with pytest.raises(Conflict):
        scheduler.transition_task(db, task.id, S.BLOCKED.value, reason="x", actor="t", reason_code="WHATEVER")
    for code in RUNNING_STOP_CODES:
        assert is_legal("RUNNING", "BLOCKED", reason_code=code)
    te = scheduler.transition_task(db, task.id, S.BLOCKED.value, reason="lease expired", actor="t",
                                   reason_code="LEASE_LOST")
    assert te.state == "BLOCKED" and te.reason_code == "LEASE_LOST"


def test_done_never_via_transition_task(db):
    """§14 REVIEW --> DONE requires valid acceptance: transition_task refuses DONE outright."""
    task = _task(db, S.REVIEW.value)
    with pytest.raises(Conflict) as exc:
        scheduler.transition_task(db, task.id, S.DONE.value, reason="worker says done", actor="worker:x")
    assert exc.value.code == "DONE_REQUIRES_ACCEPTANCE"


def test_expected_from_mismatch(db):
    task = _task(db, S.READY.value)
    with pytest.raises(Conflict):
        scheduler.transition_task(db, task.id, S.LEASED.value, reason="x", actor="t", expected_from="QUEUED")


def test_optimistic_concurrency_row_version_guard(db, monkeypatch):
    """A writer holding a stale row_version loses (conditional UPDATE ... WHERE row_version=?)."""
    task = _task(db, S.QUEUED.value)
    real = scheduler.get_te
    stale = {"done": False}

    def get_te_stale(session, task_id, fresh=True):
        te = real(session, task_id, fresh=fresh)
        if not stale["done"]:
            stale["done"] = True
            # Another writer bumps the version between our read and our UPDATE.
            session.execute(update(TaskExecution).where(TaskExecution.id == te.id)
                            .values(row_version=TaskExecution.row_version + 1),
                            execution_options={"synchronize_session": False})
        return te

    monkeypatch.setattr(scheduler, "get_te", get_te_stale)
    with pytest.raises(Conflict) as exc:
        scheduler.transition_task(db, task.id, S.READY.value, reason="x", actor="t")
    assert exc.value.code == "CONCURRENT_UPDATE"


def test_unknown_state_rejected(db):
    from aeos.core.errors import ValidationFailed

    task = _task(db, S.QUEUED.value)
    with pytest.raises(ValidationFailed):
        scheduler.transition_task(db, task.id, "FINISHED", reason="x", actor="t")
