"""Human Decision Queue (U-12): one queue aggregated from canonical records; domain/project scoped
(§35, §50 "Dashboard Personal mencoba run/evidence Office")."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aeos.core.enums import Domain
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.publishing import Publication
from aeos.models.research import ChangeProposal
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.policy import decisions as registry
from aeos.policy import queue
from tests.conftest import create_admin
from tests.unit.test_planning_support import make_project, sample_content

REQUIRED_KEYS = {"kind", "title", "target", "why", "requested_decision", "link", "created_at", "age_seconds"}


@pytest.fixture
def owner(db):
    return create_admin(db)


def _plan(db, project, state, title):
    p = Plan(project_id=project.id, domain=project.domain, title=title, state=state, state_reason="alasan")
    db.add(p)
    db.flush()
    return p


def _populate(db, owner):
    personal = make_project(db)
    office = make_project(db, domain=Domain.OFFICE)
    ready = _plan(db, personal, "READY_FOR_HUMAN_REVIEW", "Rencana siap")
    content = sample_content()
    v = PlanVersion(plan_id=ready.id, project_id=personal.id, domain="PERSONAL", version=1, content=content,
                    manifest_hash=content_hash(content))
    db.add(v)
    db.flush()
    ready.current_version_id = v.id
    _plan(db, personal, "AWAITING_MANUAL_CLAUDE", "Rencana manual")
    _plan(db, personal, "NEEDS_DECISION", "Rencana kuota")
    _plan(db, personal, "PRIMARY_PLANNING", "Sedang berjalan")  # not waiting for a human
    _plan(db, office, "READY_FOR_HUMAN_REVIEW", "Rencana kantor rahasia")
    db.add(Publication(project_id=personal.id, domain="PERSONAL", plan_version_id=v.id, approved_hash=v.manifest_hash,
                       decision_id="d", status="PREVIEW"))
    db.add(ChangeProposal(kind="ADD", title="Tambah Serena", submitted_by=owner.id, status="REVIEWED"))
    db.add(ChangeProposal(kind="ADD", title="Usulan kantor", submitted_by=owner.id, status="REVIEWED",
                          scope_type="PROJECT", project_id=office.id, domain="OFFICE"))
    spec = TaskSpec(plan_version_id=v.id, plan_version_hash=v.manifest_hash, project_id=personal.id, domain="PERSONAL",
                    task_key="T1", title="Task", goal="g", spec_hash="sha256:x")
    db.add(spec)
    db.flush()
    db.add(TaskExecution(task_id=spec.id, project_id=personal.id, domain="PERSONAL", state="BLOCKED",
                         reason="NEEDS_DECISION: attempts exhausted"))
    db.add(WorkerRun(project_id=personal.id, domain="PERSONAL", execution_status="REVIEW", review_verdict="NOT-PROVEN"))
    db.add(WorkerRun(project_id=personal.id, domain="PERSONAL", execution_status="REVIEW"))  # automated review
    registry.record_decision(db, kind="EXCEPTION", actor_user_id=owner.id, target_type="repository", target_id="o/r",
                             domain="PERSONAL", auth_scope={"r": 1}, rationale="x",
                             expires_at=utcnow() + timedelta(days=2))
    registry.record_decision(db, kind="EXCEPTION", actor_user_id=owner.id, target_type="repository", target_id="o/far",
                             domain="PERSONAL", auth_scope={"r": 1}, rationale="x",
                             expires_at=utcnow() + timedelta(days=60))
    from aeos.policy import requests as reqs

    reqs.create_request(db, kind="RISK_ACCEPTANCE", title="Terima risiko X", reason="residual", requested_by="verifier",
                        domain="PERSONAL")
    db.commit()
    return personal, office


def test_queue_aggregates_canonical_sources(db, owner):
    personal, office = _populate(db, owner)
    items = queue.queue_items(db, "PERSONAL")
    kinds = queue.counts(items)
    assert kinds == {"PLAN": 3, "PUBLICATION": 1, "CHANGE_PROPOSAL": 1, "TASK_BLOCKED": 1, "RUN_REVIEW": 1,
                     "DECISION_EXPIRING": 1, "DECISION_REQUEST": 1}
    for i in items:
        assert set(i) >= REQUIRED_KEYS and i["link"].startswith("/")
    ready = next(i for i in items if i["title"] == "Rencana siap")
    assert ready["target"]["version"] == 1 and ready["target"]["hash"].startswith("sha256:")
    assert "Approve & Publish" in ready["requested_decision"]
    task = next(i for i in items if i["kind"] == "TASK_BLOCKED")
    assert task["link"].startswith("/dag?plan=")
    assert [i["priority"] for i in items] == sorted(i["priority"] for i in items)


def test_office_items_never_in_personal_queue_and_vice_versa(db, owner):
    _populate(db, owner)
    personal_titles = {i["title"] for i in queue.queue_items(db, "PERSONAL")}
    assert "Rencana kantor rahasia" not in personal_titles and "Usulan kantor" not in personal_titles
    office_items = queue.queue_items(db, "OFFICE")
    assert {i["title"] for i in office_items} == {"Rencana kantor rahasia", "Usulan kantor"}


def test_queue_respects_project_scope(db, owner):
    personal, _office = _populate(db, owner)
    items = queue.queue_items(db, "PERSONAL", project_ids=[])
    assert all(i["project_id"] is None for i in items)
    assert queue.counts(queue.queue_items(db, "PERSONAL", project_ids=[personal.id]))["PLAN"] == 3
