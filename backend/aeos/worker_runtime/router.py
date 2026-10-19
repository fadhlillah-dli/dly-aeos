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
from aeos.worker_runtime.sandbox import containment_setting_key, sandbox_readiness

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
    for kind, dom in (("process", None), ("container", "PERSONAL"), ("container", "OFFICE")):
        row = db.get(SystemSetting, containment_setting_key(kind, dom))
        if row is not None and isinstance(row.value, dict):
            items.append(row.value)
    return {"items": items, "source": "containment probes executed on this host", "checked_at": utcnow().isoformat(),
            "mode": "LIVE"}


class ContainmentBody(BaseModel):
    kind: str = Field(default="process", max_length=16)
    domain: str = Field(default="PERSONAL", max_length=16)


@router.post("/api/worker-runtime/containment")
async def run_containment(body: ContainmentBody, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.worker_runtime.containment import containment_report, record_containment_report

    if body.kind not in ("process", "container"):
        raise ValidationFailed("kind must be process or container")
    if body.domain not in ("PERSONAL", "OFFICE"):
        raise ValidationFailed("domain must be PERSONAL or OFFICE")
    report = await run_in_threadpool(lambda: containment_report(body.kind, domain=body.domain))
    record_containment_report(db, report, actor=f"user:{principal.user.id}")
    db.commit()
    return report


@router.post("/api/worker-runtime/contract-test")
async def run_contract(principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    """§08 worker contract: real OpenCode against a scripted loopback model; evidence WORKER_CONTRACT_TEST."""
    from aeos.worker_runtime.contract import record_contract_test, run_contract_test

    report = await run_in_threadpool(run_contract_test)
    report["evidence_id"] = record_contract_test(db, report, actor=f"user:{principal.user.id}")
    db.commit()
    return report
