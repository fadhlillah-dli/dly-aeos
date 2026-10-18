"""Immutability invariant (AGENTS.md #4, §13, §14, §34): AuditEvent, Event, EvidenceArtifact,
PlanVersion and TaskSpec are append-only; Decision and AcceptanceRecord binding columns are
immutable. (Local append-only tables are not a WORM claim against a host administrator.)"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from aeos.audit import service as audit
from aeos.events import service as events
from aeos.models.decisions import Decision
from aeos.models.evidence import AcceptanceRecord
from aeos.verification import facade as verification
from tests.acceptance.support import make_records

pytestmark = pytest.mark.acceptance

GUARD_ERRORS = (IntegrityError, OperationalError)


def _refused(db, sql: str, **params) -> None:
    with pytest.raises(GUARD_ERRORS):
        db.execute(text(sql), params)
        db.commit()
    db.rollback()


def test_audit_and_events_append_only(db):
    audit.record(db, actor_type="system", actor_id="acc", action="acc.write")
    events.emit(db, source="acc", kind="acc.event")
    db.commit()
    _refused(db, "UPDATE audit_events SET action='tampered'")
    _refused(db, "DELETE FROM audit_events")
    _refused(db, "UPDATE events SET kind='tampered'")
    _refused(db, "DELETE FROM events")


def test_evidence_append_only(db):
    ev = verification.collect_evidence(db, kind="NOTE", origin="worker", collector_identity="w", claim="claim")
    db.commit()
    _refused(db, "UPDATE evidence_artifacts SET trusted=1 WHERE id=:i", i=ev.id)
    _refused(db, "UPDATE evidence_artifacts SET evidence_level='E4' WHERE id=:i", i=ev.id)
    _refused(db, "DELETE FROM evidence_artifacts WHERE id=:i", i=ev.id)


def test_plan_version_and_taskspec_append_only(db):
    rec = make_records(db)
    _refused(db, "UPDATE plan_versions SET manifest_hash='x' WHERE id=:i", i=rec.version.id)
    _refused(db, "DELETE FROM plan_versions WHERE id=:i", i=rec.version.id)
    _refused(db, "UPDATE task_specs SET allowed_paths='[\"**\"]' WHERE id=:i", i=rec.task.id)
    _refused(db, "DELETE FROM task_specs WHERE id=:i", i=rec.task.id)


def test_decision_and_acceptance_bindings_immutable(db):
    rec = make_records(db)
    d = Decision(kind="APPROVAL", actor_user_id="owner", target_type="plan_version", target_id=rec.version.id,
                 target_hash=rec.version.manifest_hash, project_id=rec.project.id, domain="PERSONAL")
    a = AcceptanceRecord(task_id=rec.task.id, run_id=rec.run.id, project_id=rec.project.id,
                         artifact_hash="sha256:acc", head_commit="a" * 40, base_commit="b" * 40)
    db.add_all([d, a])
    db.commit()
    _refused(db, "UPDATE decisions SET target_hash='sha256:other' WHERE id=:i", i=d.id)
    _refused(db, "UPDATE acceptance_records SET head_commit='c' WHERE id=:i", i=a.id)
    _refused(db, "UPDATE acceptance_records SET artifact_hash='sha256:x' WHERE id=:i", i=a.id)
    # lifecycle columns may change (invalidation/staleness)
    db.execute(text("UPDATE acceptance_records SET acceptance_status='STALE' WHERE id=:i"), {"i": a.id})
    db.commit()
