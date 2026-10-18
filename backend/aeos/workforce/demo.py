"""DEMO fixtures for previewing the Workforce Console before the runtime exists (§35 "M1 dapat
menampilkan role catalog dan fixtures yang jelas berlabel MOCK/DEMO").

Rows are ``mode=DEMO``: excluded from task counts, active-run counts, cost/usage summaries and
acceptance; they never touch the usage ledger, leases, grants or reservations. ``remove`` deletes
the DEMO WorkerRun rows; DEMO events stay in the append-only event log, labelled DEMO.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import DataMode, Domain
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.workforce import WorkerRun

DEMO_PROJECT_ID = "demo-workforce-fixture"


def _project_id(session: Session, domain: str) -> str:
    from aeos.portfolio import facade as portfolio

    p = portfolio.platform_project(session)
    return p.id if p is not None and p.domain == domain else DEMO_PROJECT_ID


def insert(session: Session, *, domain: str = Domain.PERSONAL.value, actor: str = "cli:local-operator") -> dict[str, Any]:
    if session.scalar(select(func.count()).select_from(WorkerRun).where(WorkerRun.mode == DataMode.DEMO.value,
                                                                         WorkerRun.domain == domain)):
        return {"inserted": 0, "note": "DEMO fixtures already present"}
    now = utcnow()
    pid = _project_id(session, domain)
    specs = [
        dict(execution_status="RUNNING", role_key="feature-implementer", agent_key="opencode-implementer",
             task_key="demo-doctor-ui", current_action="DEMO: Mengubah file frontend/src/pages/readiness/index.tsx",
             touched_paths=["frontend/src/pages/readiness/index.tsx"], last_heartbeat_at=now - timedelta(seconds=5),
             started_at=now - timedelta(minutes=4), verifier_status="NOT_RUN", review_verdict="PENDING",
             acceptance_status="PENDING", merged_status="NOT_MERGED", worker_reported_status=None),
        dict(execution_status="REVIEW", role_key="backend-api-engineer", agent_key="opencode-implementer",
             task_key="demo-api-health", current_action="DEMO: Selesai: REVIEW", touched_paths=["backend/aeos/api/x.py"],
             last_heartbeat_at=now - timedelta(minutes=9), started_at=now - timedelta(minutes=20),
             finished_at=now - timedelta(minutes=9), worker_reported_status="EXITED_0", verifier_status="NOT_RUN",
             review_verdict="PENDING", acceptance_status="PENDING", merged_status="NOT_MERGED"),
        dict(execution_status="RUNNING", role_key="frontend-engineer", agent_key="opencode-implementer",
             task_key="demo-stale-heartbeat", current_action="DEMO: Menjalankan perintah (tidak ada heartbeat)",
             last_heartbeat_at=now - timedelta(minutes=7), started_at=now - timedelta(minutes=12),
             verifier_status="NOT_RUN", review_verdict="PENDING", acceptance_status="PENDING"),
        dict(execution_status="RUNNING", role_key="qa-test-designer", agent_key="opencode-implementer",
             task_key="demo-quota-wait", current_action="DEMO: WAITING_QUOTA: upstream 429 (retry-after 60s)",
             quota_status="RATE_LIMITED", last_heartbeat_at=now - timedelta(seconds=10),
             started_at=now - timedelta(minutes=2), verifier_status="NOT_RUN"),
    ]
    ids = []
    for i, spec in enumerate(specs, start=1):
        run = WorkerRun(project_id=pid, domain=domain, attempt=1, run_type="IMPLEMENTATION", mode=DataMode.DEMO.value,
                        model_route_key="DEMO-route (bukan route nyata)", model_resolved="UNKNOWN", engine="opencode",
                        engine_version="DEMO", sandbox_kind="process", caps={"demo": True},
                        usage_summary={"note": "DEMO - tidak tercatat di ledger"}, **spec)
        session.add(run)
        session.flush()
        ids.append(run.id)
        events.emit(session, source="demo-fixture", kind="run.state_changed", entity_type="run", entity_id=run.id,
                    run_id=run.id, attempt=1, project_id=pid, domain=domain, mode=DataMode.DEMO.value,
                    payload={"to": run.execution_status, "demo": True, "fixture": i})
    audit.record(session, actor_type="cli", actor_id=actor, action="workforce.demo.insert", target_type="worker_run",
                 target_id="*", domain=domain, details={"count": len(ids), "mode": "DEMO"})
    return {"inserted": len(ids), "run_ids": ids, "mode": DataMode.DEMO.value}


def remove(session: Session, *, actor: str = "cli:local-operator") -> dict[str, Any]:
    res = session.execute(delete(WorkerRun).where(WorkerRun.mode == DataMode.DEMO.value))
    audit.record(session, actor_type="cli", actor_id=actor, action="workforce.demo.remove", target_type="worker_run",
                 target_id="*", details={"removed": res.rowcount})
    return {"removed": res.rowcount}
