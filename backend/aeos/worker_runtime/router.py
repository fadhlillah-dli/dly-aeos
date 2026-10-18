"""Worker runtime status API: sandbox levels/readiness, OpenCode engine, containment reports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal
from aeos.core.errors import ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.identity import SystemSetting
from aeos.worker_runtime.sandbox import CONTAINMENT_SETTING_PREFIX, sandbox_readiness

router = APIRouter(tags=["worker_runtime"])


@router.get("/api/worker-runtime/sandbox")
def sandbox(principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    return sandbox_readiness(db, domain=domain.value)


@router.get("/api/worker-runtime/engine")
def engine(principal: CurrentPrincipal) -> dict[str, Any]:
    from aeos.worker_runtime.opencode import opencode_status

    return opencode_status()


@router.get("/api/worker-runtime/containment")
def containment_reports(principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    items = []
    for kind in ("process", "container"):
        row = db.get(SystemSetting, CONTAINMENT_SETTING_PREFIX + kind)
        if row is not None and isinstance(row.value, dict):
            items.append(row.value)
    return {"items": items, "source": "containment probes executed on this host", "checked_at": utcnow().isoformat(),
            "mode": "LIVE"}


class ContainmentBody(BaseModel):
    kind: str = Field(default="process", max_length=16)


@router.post("/api/worker-runtime/containment")
async def run_containment(body: ContainmentBody, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.worker_runtime.containment import containment_report, record_containment_report

    if body.kind not in ("process", "container"):
        raise ValidationFailed("kind must be process or container")
    report = await run_in_threadpool(containment_report, body.kind)
    record_containment_report(db, report, actor=f"user:{principal.user.id}")
    db.commit()
    return report
