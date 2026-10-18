"""Research & Technology Registry API (§45, §47). Read for any signed-in user in the active domain;
writes and the (network) upstream check are admin-only and never install or activate anything."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal
from aeos.core.timeutil import utcnow
from aeos.models.research import ResearchEntry
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
    last = db.scalar(select(func.max(ResearchEntry.checked_at)))
    return {
        "items": items, "source": "research_entries (katalog master §45/§47/§52 + entri manual)",
        "checked_at": last.isoformat() if last else None, "mode": "DESIGN",
        "lifecycles": list(service.LIFECYCLES),
        "evidence_limit": "Tahap riset bukan bukti instalasi/koneksi/penggunaan; E1 = kemampuan terdokumentasi, "
                          "E0 = klaim komunitas.",
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


@router.post("/api/research")
def add_research(body: AddEntryIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    entry, duplicate = service.add_by_url(db, url=body.url, name=body.name, category=body.category, notes=body.notes,
                                          actor_user_id=principal.user.id, domain=domain.value)
    db.commit()
    return {"entry": service.entry_view(entry, detail=True), "duplicate": duplicate,
            "note": "Entri DISCOVERED: tidak ada instalasi, aktivasi atau pengiriman data proyek."}


@router.post("/api/research/{entry_id}/check-upstream")
def check_upstream(entry_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    entry = service.get_entry(db, entry_id, domain=domain.value)
    view = service.check_upstream(db, entry, actor_type="user", actor_id=principal.user.id, domain=domain.value)
    db.commit()
    return view
