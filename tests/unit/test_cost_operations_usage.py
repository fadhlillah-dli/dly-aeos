"""§42 "Satuan dipantau terpisah: ... latency, jumlah repair": the cost summary reports worker-run latency
(p50/p95 with sample size) and repair attempts separately from money/tokens, scoped to the active domain."""

from __future__ import annotations

from datetime import timedelta

from aeos.core.timeutil import utcnow
from aeos.cost import service
from aeos.models.workforce import TaskExecution, WorkerRun
from tests.unit.test_workforce_support import make_plan, make_project


def test_summary_reports_latency_and_repairs_per_domain(db):
    personal, office = make_project(db), make_project(db, domain="OFFICE")
    _, _, specs = make_plan(db, personal, [{"task_key": "A"}, {"task_key": "B"}])
    _, _, ospecs = make_plan(db, office, [{"task_key": "O"}])
    now = utcnow()
    for i, minutes in enumerate((2, 4, 10)):
        db.add(WorkerRun(task_id=specs[0].id, project_id=personal.id, domain="PERSONAL", attempt=i + 1,
                         started_at=now - timedelta(minutes=minutes), finished_at=now))
    db.add(WorkerRun(task_id=specs[1].id, project_id=personal.id, domain="PERSONAL", started_at=now))  # still running
    db.add(WorkerRun(task_id=ospecs[0].id, project_id=office.id, domain="OFFICE", started_at=now - timedelta(hours=5),
                     finished_at=now))
    db.add(TaskExecution(task_id=specs[0].id, project_id=personal.id, domain="PERSONAL", state="DONE", attempts_used=3))
    db.add(TaskExecution(task_id=specs[1].id, project_id=personal.id, domain="PERSONAL", state="RUNNING", attempts_used=1))
    db.add(TaskExecution(task_id=ospecs[0].id, project_id=office.id, domain="OFFICE", state="DONE", attempts_used=4))
    db.commit()
    ops = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=False)["operations"]
    assert ops["latency"]["samples"] == 3 and ops["latency"]["runs"] == 4
    assert ops["latency"]["p50_s"] == 240.0 and ops["latency"]["p95_s"] == 600.0  # Office's 5 h run never counts
    assert ops["repairs"] == {"repair_attempts": 2, "tasks": 2, "tasks_repaired": 1,
                              "basis": ops["repairs"]["basis"]}
    empty = service.summary(db, domain="PERSONAL", allowed_project_ids=[], cross_domain_visible=False)["operations"]
    assert empty["latency"]["status"] == "NO_FINISHED_RUNS" and empty["repairs"]["repair_attempts"] == 0
