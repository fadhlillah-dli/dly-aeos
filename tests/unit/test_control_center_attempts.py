"""History attempts timeline (master §35 History/Audit/Recovery: accepted/failed/cancelled attempts)."""

from __future__ import annotations

from datetime import timedelta

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun


def test_attempt_timeline_is_scoped_ordered_and_filterable(personal_client, db):
    mine = Project(project_key="att-p", name="Att P", domain="PERSONAL")
    office = Project(project_key="att-o", name="Att O", domain="OFFICE")
    db.add_all([mine, office])
    db.flush()
    now = utcnow()
    db.add_all([
        WorkerRun(project_id=mine.id, domain="PERSONAL", attempt=1, execution_status="DONE", acceptance_status="ACCEPTED",
                  verifier_status="PASS", finished_at=now - timedelta(minutes=5)),
        WorkerRun(project_id=mine.id, domain="PERSONAL", attempt=2, execution_status="FAILED", verifier_status="FAILED",
                  worker_reported_status="EXITED_0", finished_at=now - timedelta(minutes=1)),
        WorkerRun(project_id=mine.id, domain="PERSONAL", attempt=1, execution_status="CANCELLED",
                  finished_at=now - timedelta(minutes=10)),
        WorkerRun(project_id=office.id, domain="OFFICE", attempt=1, execution_status="FAILED", finished_at=now),
    ])
    db.commit()
    body = personal_client.get("/api/control-center/history/attempts").json()
    assert body["total"] == 3 and body["source"] and body["checked_at"]
    assert [i["execution_status"] for i in body["items"]] == ["FAILED", "DONE", "CANCELLED"]  # newest first
    failed = body["items"][0]
    assert failed["worker_reported_status"] == "EXITED_0" and failed["verifier_status"] == "FAILED"
    assert failed["acceptance_status"] is None  # separate statuses, never merged into one "done"
    assert all(i["project_id"] == mine.id for i in body["items"])  # Office attempts never in a Personal view
    for outcome, expected in (("ACCEPTED", ["DONE"]), ("FAILED", ["FAILED"]), ("CANCELLED", ["CANCELLED"])):
        got = personal_client.get(f"/api/control-center/history/attempts?outcome={outcome}").json()["items"]
        assert [i["execution_status"] for i in got] == expected, outcome
    personal_client.select_domain(Domain.OFFICE)
    assert personal_client.get("/api/control-center/history/attempts").json()["total"] == 1
