"""Research & Technology Registry API (§45, §47, §52). Read for any signed-in user in the active domain;
writes, owner edits and the (network) upstream/dependency checks are admin-only and never install or
activate anything."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal
from aeos.core.timeutil import utcnow
from aeos.research import service

router = APIRouter(tags=["research"])


@router.get("/api/research")
def list_research(
    db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
    lifecycle: str | None = None, category: str | None = None, evidence: str | None = None,
    q: str | None = Query(None, max_length=200), kind: str | None = Query(None, pattern="^(reference|candidate)$"),
) -> dict[str, Any]:
    items = service.list_entries(db, domain=domain.value, lifecycle=lifecycle, category=category, evidence=evidence,
                                 q=q, kind=kind)
    # Freshness only from rows visible in this domain (no metadata from the other domain, §35/§50).
    checked = [i["checked_at"] for i in service.list_entries(db, domain=domain.value) if i["checked_at"]]
    return {
        "items": items, "source": "research_entries (katalog master §45/§47/§52 + entri manual)",
        "checked_at": max(checked) if checked else None, "mode": "DESIGN",
        "lifecycles": list(service.LIFECYCLES),
        "evidence_limit": "Tahap riset bukan bukti instalasi/koneksi/penggunaan; E1 = kemampuan terdokumentasi, "
                          "E0 = klaim komunitas; hasil platform ini (E2/E3) hanya dari trial/outcome tercatat. "
                          + service.COVERAGE_LIMIT,
        "conclusion": service.CONCLUSION,
        "coverage_limit": service.COVERAGE_LIMIT,
        "generated_at": utcnow().isoformat(),
    }


@router.get("/api/research/{entry_id}")
def get_research(entry_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return service.entry_view(service.get_entry(db, entry_id, domain=domain.value), detail=True)


class AddEntryIn(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=4000)


class UpdateEntryIn(BaseModel):
    maintainer: str | None = Field(default=None, max_length=200)
    source_version: str | None = Field(default=None, max_length=128)
    data_terms: str | None = Field(default=None, max_length=4000)
    compatibility: str | None = Field(default=None, max_length=4000)
    limitations: str | None = Field(default=None, max_length=4000)
    distribution: str | None = Field(default=None, max_length=256)
    superseded_by: str | None = Field(default=None, max_length=36)
    terms_checked: bool | None = None


class ReviewNoteIn(BaseModel):
    note: str = Field(min_length=10, max_length=2000)


class DependencyDiffIn(BaseModel):
    base_ref: str = Field(min_length=1, max_length=128)
    head_ref: str = Field(min_length=1, max_length=128)


@router.post("/api/research")
def add_research(body: AddEntryIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    entry, duplicate = service.add_by_url(db, url=body.url, name=body.name, category=body.category, notes=body.notes,
                                          actor_user_id=principal.user.id, domain=domain.value)
    db.commit()
    return {"entry": service.entry_view(entry, detail=True), "duplicate": duplicate,
            "note": "Entri DISCOVERED: tidak ada instalasi, aktivasi atau pengiriman data proyek."}


@router.patch("/api/research/{entry_id}")
def update_research(entry_id: str, body: UpdateEntryIn, db: DB, principal: AdminPrincipal,
                    domain: ActiveDomain) -> dict[str, Any]:
    entry = service.get_entry(db, entry_id, domain=domain.value)
    service.update_entry(db, entry, fields=body.model_dump(exclude_unset=True), actor_user_id=principal.user.id,
                         domain=domain.value)
    db.commit()
    return service.entry_view(entry, detail=True)


@router.post("/api/research/{entry_id}/confirm-source")
def confirm_source(entry_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    entry = service.get_entry(db, entry_id, domain=domain.value)
    service.confirm_source(db, entry, actor_user_id=principal.user.id, domain=domain.value)
    db.commit()
    return service.entry_view(entry, detail=True)


@router.post("/api/research/{entry_id}/mark-reviewed")
def mark_reviewed(entry_id: str, body: ReviewNoteIn, db: DB, principal: AdminPrincipal,
                  domain: ActiveDomain) -> dict[str, Any]:
    entry = service.get_entry(db, entry_id, domain=domain.value)
    service.mark_reviewed(db, entry, note=body.note, actor_user_id=principal.user.id, domain=domain.value)
    db.commit()
    return service.entry_view(entry, detail=True)


@router.post("/api/research/{entry_id}/check-upstream")
def check_upstream(entry_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    entry = service.get_entry(db, entry_id, domain=domain.value)
    view = service.check_upstream(db, entry, actor_type="user", actor_id=principal.user.id, domain=domain.value)
    db.commit()
    return view


@router.post("/api/research/{entry_id}/dependency-diff")
def dependency_diff(entry_id: str, body: DependencyDiffIn, db: DB, principal: AdminPrincipal,
                    domain: ActiveDomain) -> dict[str, Any]:
    from aeos.research.dependencies import dependency_diff as impl

    entry = service.get_entry(db, entry_id, domain=domain.value)
    result = impl(db, entry, base_ref=body.base_ref, head_ref=body.head_ref, actor_type="user",
                  actor_id=principal.user.id, domain=domain.value)
    db.commit()
    return {"dependency_diff": result, "entry": service.entry_view(entry, detail=True)}
