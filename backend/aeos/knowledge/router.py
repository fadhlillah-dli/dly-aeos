"""Knowledge & Context API (§24): ContextPacks and MemoryRecords, scoped to the active domain."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.core.errors import NotFound
from aeos.core.timeutil import utcnow
from aeos.knowledge import memory as memory_mod
from aeos.knowledge import packs
from aeos.models.research import MemoryRecord

router = APIRouter(tags=["knowledge"])


class PackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str | None = Field(default=None, max_length=36)
    role_key: str | None = Field(default=None, max_length=64)
    repository_id: str | None = Field(default=None, max_length=36)
    base_commit: str | None = Field(default=None, max_length=200)
    query: list[str] | str | None = None
    paths: list[str] | None = None
    forbidden_paths: list[str] | None = None
    token_budget: int | None = None
    include: list[str] | None = None
    ttl_hours: int | None = None


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8000)
    source_type: str = Field(min_length=1, max_length=32)
    source_id: str | None = Field(default=None, max_length=128)
    scope: str = "PROJECT"
    classification: str | None = None
    expires_at: datetime | None = None
    supersedes_id: str | None = Field(default=None, max_length=36)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str = Field(default="", max_length=2000)


# ---------------------------------------------------------------- context packs


@router.get("/api/projects/{project_id}/context-packs")
def list_context_packs(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    items = [packs.pack_view(p, m, include_content=False) for p, m in packs.list_packs(db, project)]
    return {"items": items, "source": "aeos-knowledge", "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.post("/api/projects/{project_id}/context-packs")
def create_context_pack(project_id: str, body: PackIn, principal: AdminPrincipal, domain: ActiveDomain,
                        db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    pack, meta = packs.build_pack(db, principal, project, **body.model_dump())
    db.commit()
    return {"context_pack": packs.pack_view(pack, meta, freshness="CURRENT")}


@router.get("/api/context-packs/{pack_id}")
def get_context_pack(pack_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    pack, meta = packs.get_scoped(db, domain.value, pack_id)
    load_project(db, principal, pack.project_id, domain=domain)  # project scope -> 404 otherwise
    was_stale = pack.stale
    freshness = packs.check_freshness(db, pack, meta)
    view = packs.pack_view(pack, meta, freshness=freshness)
    if pack.stale != was_stale or meta is not None:
        db.commit()  # persist the observed staleness / check time (monotonic: STALE is sticky)
    return {"context_pack": view}


# ---------------------------------------------------------------- memory


@router.get("/api/projects/{project_id}/memory")
def list_memory(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                status: str | None = Query(None, max_length=16)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    rows = memory_mod.list_for_project(db, project, status=status)
    return {"items": [memory_mod.memory_view(r) for r in rows], "source": "aeos-knowledge",
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.post("/api/projects/{project_id}/memory")
def propose_memory(project_id: str, body: MemoryIn, principal: AdminPrincipal, domain: ActiveDomain,
                   db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    rec = memory_mod.propose(db, principal, project, **body.model_dump())
    db.commit()
    return {"memory": memory_mod.memory_view(rec)}


def _scoped_memory(db: Any, principal: Any, domain: Any, memory_id: str) -> tuple[Any, MemoryRecord]:
    rec = db.get(MemoryRecord, memory_id)
    if rec is None or rec.project_id is None or rec.domain != domain.value:
        raise NotFound("memory record not found")
    project = load_project(db, principal, rec.project_id, domain=domain)
    return project, rec


class MemoryAcceptIn(DecisionIn):
    # §41/§43 system-change evaluation of this memory (COMPLETED runs, every gate PASS); empty = NOT_EVALUATED
    eval_run_ids: list[str] = Field(default_factory=list, max_length=20)


@router.post("/api/memory/{memory_id}/accept")
def accept_memory(memory_id: str, body: MemoryAcceptIn, principal: AdminPrincipal, domain: ActiveDomain,
                  db: DB) -> dict[str, Any]:
    project, rec = _scoped_memory(db, principal, domain, memory_id)
    memory_mod.accept(db, principal, project, rec, rationale=body.rationale, eval_run_ids=body.eval_run_ids)
    db.commit()
    return {"memory": memory_mod.memory_view(rec)}


@router.post("/api/memory/{memory_id}/reject")
def reject_memory(memory_id: str, body: DecisionIn, principal: AdminPrincipal, domain: ActiveDomain,
                  db: DB) -> dict[str, Any]:
    project, rec = _scoped_memory(db, principal, domain, memory_id)
    memory_mod.reject(db, principal, project, rec, rationale=body.rationale)
    db.commit()
    return {"memory": memory_mod.memory_view(rec)}


@router.post("/api/memory/{memory_id}/expire")
def expire_memory(memory_id: str, body: DecisionIn, principal: AdminPrincipal, domain: ActiveDomain,
                  db: DB) -> dict[str, Any]:
    project, rec = _scoped_memory(db, principal, domain, memory_id)
    memory_mod.expire(db, principal, project, rec, reason=body.rationale)
    db.commit()
    return {"memory": memory_mod.memory_view(rec)}
