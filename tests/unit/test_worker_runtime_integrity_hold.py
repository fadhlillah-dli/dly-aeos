"""Wave 7 (W6-000/W6-005 d): a controller/config/bytecode/DB tamper finding raises the host-wide integrity hold;
worker admission, the executor's start check and CAN_RUN_WORKER fail closed until the owner reconciles with a
HUMAN decision bound to exactly that hold (§09 Zone 1, AGENTS.md invariants 3 and 8)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aeos.core.enums import Capability
from aeos.core.timeutil import utcnow
from aeos.models.audit import Event
from aeos.models.decisions import Decision
from aeos.worker_runtime import integrity, sandbox
from tests.unit.test_workforce_support import process_sandbox_exception

FINDING = [{"kind": "controller", "path": "backend/aeos/core/config.py", "change": "modified"}]


def _reconcile(db, hold_id: str, **kw) -> Decision:
    d = Decision(kind=kw.pop("kind", "RISK_ACCEPTANCE"), actor_user_id="owner", actor_kind=kw.pop("actor_kind", "HUMAN"),
                 target_type=integrity.HOLD_TARGET_TYPE, target_id=hold_id, domain="PERSONAL",
                 rationale="restored the checkout with git and reviewed the run", **kw)
    db.add(d)
    db.flush()
    return d


def test_hold_blocks_every_sandbox_kind_with_its_own_code(db):
    process_sandbox_exception(db)
    assert sandbox.sandbox_readiness(db, domain="PERSONAL")["status"] == "READY"
    hold = integrity.raise_hold(db, source="worker run", subject_id="run-1", findings=FINDING)
    db.commit()
    for domain in ("PERSONAL", "OFFICE"):
        sb = sandbox.sandbox_readiness(db, domain=domain)
        assert sb["status"] == "BLOCKED" and sb["code"] == "INTEGRITY_HOLD" and sb["kind"] is None
        assert sb["integrity_hold"]["id"] == hold["id"]
        assert "aeos policy decide --kind RISK_ACCEPTANCE" in sb["remedies"][0] and hold["id"] in sb["remedies"][0]
    task = sandbox.task_sandbox_readiness(db, domain="PERSONAL", risk_class="R0", tools_allowed=[], egress_allowed=[])
    assert task["code"] == "INTEGRITY_HOLD"
    alert = db.scalar(select(Event).where(Event.kind == "worker_runtime.alert", Event.entity_id == hold["id"]))
    assert alert is not None and alert.payload["severity"] == "HIGH" and alert.payload["code"] == "INTEGRITY_HOLD"


def test_only_a_human_reconciliation_of_exactly_this_hold_clears_it(db):
    process_sandbox_exception(db)
    hold = integrity.raise_hold(db, source="worker run", subject_id="run-1", findings=FINDING)
    _reconcile(db, "another-hold-id")
    _reconcile(db, hold["id"], actor_kind="AGENT")  # a model/worker can never reconcile
    _reconcile(db, hold["id"], kind="POLICY")
    db.commit()
    assert integrity.hold_status(db) is not None
    revoked = _reconcile(db, hold["id"])
    revoked.status = "INVALIDATED"
    db.commit()
    assert integrity.hold_status(db) is not None
    _reconcile(db, hold["id"], expires_at=utcnow() + timedelta(days=1))
    db.commit()
    assert integrity.hold_status(db) is None
    assert integrity.hold_view(db)["status"] == "CLEAR"
    assert sandbox.sandbox_readiness(db, domain="PERSONAL")["status"] == "READY"


def test_a_new_finding_needs_a_new_reconciliation(db):
    first = integrity.raise_hold(db, source="worker run", subject_id="run-1", findings=FINDING)
    _reconcile(db, first["id"])
    db.commit()
    assert integrity.hold_status(db) is None
    second = integrity.raise_hold(db, source="verification", subject_id="run-2",
                                  findings=[{"kind": "db", "path": "decisions:x", "change": "added"}])
    db.commit()
    assert second["id"] != first["id"] and second["previous_id"] == first["id"]
    active = integrity.hold_status(db)
    assert active is not None and [f["path"] for f in active["findings"]] == ["decisions:x"]  # reconciled one dropped
    # an unreconciled hold keeps its findings when widened
    third = integrity.raise_hold(db, source="worker run", subject_id="run-3", findings=FINDING)
    db.commit()
    assert [f["path"] for f in third["findings"]] == ["decisions:x", "backend/aeos/core/config.py"]


def test_a_reconciliation_recorded_before_the_hold_does_not_clear_it(db):
    hold = integrity.raise_hold(db, source="worker run", subject_id="run-1", findings=FINDING)
    _reconcile(db, hold["id"], created_at=utcnow() - timedelta(hours=1))
    db.commit()
    assert integrity.hold_status(db) is not None


def test_the_stated_remedy_records_a_decision_that_clears_exactly_this_hold(db):
    """The reconcile command the product prints (``aeos policy decide --kind RISK_ACCEPTANCE --target-type
    worker_integrity_hold --target-id <hold id> ...``) goes through the policy recording path; that decision lifts
    the hold."""
    from aeos.core.enums import Domain
    from aeos.policy import recording
    from tests.conftest import create_admin

    hold = integrity.raise_hold(db, source="worker run", subject_id="run-1", findings=FINDING)
    db.commit()
    command = integrity.reconcile_command(hold["id"])
    assert f"--target-type {integrity.HOLD_TARGET_TYPE} --target-id {hold['id']}" in command
    assert "--kind RISK_ACCEPTANCE" in command and "--days" in command
    owner = create_admin(db)
    d = recording.record_generic_decision(db, actor_user_id=owner.id, domain=Domain.PERSONAL, kind="RISK_ACCEPTANCE",
                                          rationale="checked git status, restored the checkout",
                                          auth_scope={"recorded_via": "aeos policy decide"},
                                          target_type=integrity.HOLD_TARGET_TYPE, target_id=hold["id"],
                                          expires_at=utcnow() + timedelta(days=1))
    db.commit()
    assert d.actor_kind == "HUMAN"
    view = integrity.hold_view(db)
    assert view["status"] == "CLEAR" and view["reconciled_by_decision"] == d.id


def test_can_run_worker_gate_is_blocked_by_the_hold(db):
    from aeos.doctor import facade as doctor

    process_sandbox_exception(db)
    integrity.raise_hold(db, source="worker run", subject_id="run-1", findings=FINDING)
    db.commit()
    state = doctor.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert state.status == "BLOCKED"
    assert any("integrity hold" in r for r in state.reasons), state.reasons
