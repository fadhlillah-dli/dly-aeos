"""§21 "workers may not change final audit/evidence" / §34 append-only ledger: the Doctor detects what the DB
triggers alone cannot - a guard trigger dropped at the database level, or audit/event rows deleted below the
application (a hole in the seq order). Detection only; isolation of verifier/worker code is §20."""

from __future__ import annotations

from sqlalchemy import text

from aeos.audit import service as audit
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.runner import make_context, run_check

CHECK = "evidence.append_only_guard"


def _run(settings, db):
    load_all()
    return run_check(REGISTRY[CHECK], make_context(settings, db))


def _audit(db, n: int) -> None:
    for i in range(n):
        audit.record(db, actor_type="system", actor_id="t", action=f"test.{i}")
    db.commit()


def test_intact_guards_and_contiguous_audit_trail_verify(settings, db):
    _audit(db, 3)
    res = _run(settings, db)
    assert res.status == "VERIFIED", res.summary
    assert res.observed_evidence["triggers_missing"] == [] and res.observed_evidence["seq_gaps"] == {}


def test_a_dropped_trigger_is_detected(settings, db):
    _audit(db, 2)
    db.execute(text("DROP TRIGGER trg_audit_events_no_delete"))
    db.commit()
    res = _run(settings, db)
    assert res.status == "FAILED" and res.error_code == "APPEND_ONLY_GUARD_MISSING"
    assert "trg_audit_events_no_delete" in res.observed_evidence["triggers_missing"]


def test_rows_deleted_below_the_application_leave_a_detected_gap(settings, db):
    _audit(db, 5)
    db.execute(text("DROP TRIGGER trg_audit_events_no_delete"))
    db.execute(text("DELETE FROM audit_events WHERE seq = 2"))
    db.commit()
    from aeos.core.db import install_append_only_triggers

    install_append_only_triggers(db.get_bind())  # guard re-installed (e.g. next start): the hole stays visible
    res = _run(settings, db)
    assert res.status == "FAILED" and res.error_code == "AUDIT_SEQ_GAP"
    assert res.observed_evidence["seq_gaps"] == {"audit_events": 1}
