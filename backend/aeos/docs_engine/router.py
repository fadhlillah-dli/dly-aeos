"""Documentation & Visuals API (§27-§29, §41): Product 1 structure, document records, previews.

Reads are domain-scoped (cross-domain → 404). Rendering into DocumentRecords happens only for the
APPROVED PlanVersion; human edits observed at the provider are listed as revision inputs and can be
acknowledged by an admin (never overwritten automatically).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, SettingsDep, load_project
from aeos.audit import service as audit
from aeos.core.errors import Conflict, NotFound
from aeos.core.timeutil import utcnow
from aeos.docs_engine import product1, visuals
from aeos.docs_engine import service as docs_service
from aeos.events import service as events
from aeos.models.planning import PlanVersion
from aeos.models.publishing import DocumentRecord, DocumentRevision, ExternalReference

router = APIRouter(tags=["docs_engine"])
SOURCE = "AEOS DocumentRecord (render dari PlanVersion yang disetujui)"


class RenderIn(BaseModel):
    plan_version_id: str = Field(min_length=1, max_length=64)
    render_visuals: bool = True


class AcknowledgeIn(BaseModel):
    note: str = Field(default="", max_length=2000)


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def document_view(rec: DocumentRecord, refs: list[ExternalReference], open_inputs: int) -> dict[str, Any]:
    stale = any(r.sync_status != "SYNCED" for r in refs)
    return {
        "id": rec.id, "project_id": rec.project_id, "domain": rec.domain, "logical_doc_id": rec.logical_doc_id,
        "node": rec.node, "title": rec.title, "kind": rec.kind, "doc_type": rec.doc_type,
        "parent_logical_id": rec.parent_logical_id, "owner": rec.owner, "status": rec.status,
        "status_reason": rec.status_reason, "content_type": rec.content_type, "timing": rec.timing,
        "template_key": rec.template_key, "source_plan_version_id": rec.source_plan_version_id,
        "content_hash": rec.content_hash, "external_revision": rec.external_revision,
        "last_reviewed_at": _iso(rec.last_reviewed_at), "last_synced_at": _iso(rec.last_synced_at),
        "rendered_at": _iso(rec.rendered_at), "links": rec.links or {}, "pending_sections": rec.pending_sections or [],
        "documentation_pending": bool(rec.pending_sections), "sections": rec.sections or [],
        "order_index": rec.order_index, "mode": rec.mode,
        "external": [{"provider": r.provider, "external_id": r.external_id, "url": r.external_url,
                      "revision": r.external_revision, "sync_status": r.sync_status,
                      "last_synced_at": _iso(r.last_synced_at)} for r in refs],
        "freshness": {"rendered_at": _iso(rec.rendered_at), "last_synced_at": _iso(rec.last_synced_at),
                      "external_stale": stale},
        "open_revision_inputs": open_inputs,
    }


@router.get("/api/docs/product1-structure")
def product1_structure(_principal: CurrentPrincipal) -> dict[str, Any]:
    return product1.structure() | {"source": "aeos.docs_engine.product1 (master §27/§28)", "mode": "DESIGN"}


@router.get("/api/docs/visuals/status")
def visuals_status(_principal: CurrentPrincipal) -> dict[str, Any]:
    info = visuals.detect_mmdc()
    return {"mmdc": info.__dict__, "kinds": visuals.VISUAL_KINDS, "screenshot": "NOT_PROVEN",
            "evidence_role": visuals.EVIDENCE_ROLE}


@router.get("/api/projects/{project_id}/documents")
def list_documents(project_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    recs = list(db.scalars(select(DocumentRecord).where(DocumentRecord.project_id == project.id)
                           .order_by(DocumentRecord.order_index, DocumentRecord.logical_doc_id)))
    refs: dict[str, list[ExternalReference]] = {}
    for r in db.scalars(select(ExternalReference).where(ExternalReference.project_id == project.id)):
        refs.setdefault(r.logical_id, []).append(r)
    open_inputs = dict(db.execute(select(DocumentRevision.document_id, func.count()).where(
        DocumentRevision.project_id == project.id, DocumentRevision.kind == "HUMAN_EDIT_INPUT",
        DocumentRevision.status == "OPEN").group_by(DocumentRevision.document_id)).all())
    checked = max((r.rendered_at for r in recs if r.rendered_at), default=None)
    return {
        "items": [document_view(r, refs.get(r.logical_doc_id, []), int(open_inputs.get(r.id, 0))) for r in recs],
        "product_title": product1.product_title(project.name), "source": SOURCE, "checked_at": _iso(checked),
        "mode": "LIVE",
        "summary": {"documents": sum(1 for r in recs if r.kind == "DOC"),
                    "folders": sum(1 for r in recs if r.kind == "FOLDER"),
                    "pending": sum(1 for r in recs if r.pending_sections),
                    "published": sum(1 for r in recs if r.status == "PUBLISHED"),
                    "conflicts": sum(int(v) for v in open_inputs.values())},
        "remedy": None if recs else "Buat preview publikasi dari PlanVersion yang disetujui, atau jalankan "
                                    "`aeos docs render <plan_version_id>` untuk preview lokal.",
    }


@router.post("/api/projects/{project_id}/documents/render")
def render_documents(project_id: str, body: RenderIn, db: DB, settings: SettingsDep, principal: AdminPrincipal,
                     domain: ActiveDomain) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    version = db.get(PlanVersion, body.plan_version_id)
    if version is None or version.project_id != project.id:
        raise NotFound("plan version not found")
    result, delta, state = docs_service.render_version(db, settings, version, persist=True,
                                                       render_visuals=body.render_visuals)
    if not state.approved:
        raise Conflict("only the APPROVED plan version updates documents; use `aeos docs render` for a local "
                       "preview", code="PLAN_NOT_APPROVED", details={"reason": state.reason})
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="documents.render", target_type="project",
                 target_id=project.id, project_id=project.id, domain=project.domain,
                 details={"plan_version_id": version.id, **(delta.to_dict() if delta else {})})
    events.emit(db, source="docs_engine", kind="documents.rendered", entity_type="project", entity_id=project.id,
                entity_version=version.id, project_id=project.id, domain=project.domain,
                payload={k: len(v) for k, v in (delta.to_dict() if delta else {}).items()})
    db.commit()
    return {"delta": delta.to_dict() if delta else {}, "visuals": [v.to_dict() for v in result.visuals],
            "checked_at": utcnow().isoformat()}


def _load_doc(db: DB, principal: CurrentPrincipal, domain: ActiveDomain, doc_id: str) -> DocumentRecord:
    rec = db.get(DocumentRecord, doc_id)
    if rec is None:
        raise NotFound("document not found")
    load_project(db, principal, rec.project_id, domain=domain)
    return rec


@router.get("/api/documents/{doc_id}/render")
def render_preview(doc_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    rec = _load_doc(db, principal, domain, doc_id)
    return {"id": rec.id, "logical_doc_id": rec.logical_doc_id, "title": rec.title, "kind": rec.kind,
            "status": rec.status, "content_type": rec.content_type, "content_hash": rec.content_hash,
            "markdown": rec.body_markdown if rec.kind == "DOC" else "", "sections": rec.sections or [],
            "pending_sections": rec.pending_sections or [], "source_plan_version_id": rec.source_plan_version_id,
            "rendered_at": _iso(rec.rendered_at), "mode": rec.mode, "format": "markdown"}


@router.get("/api/documents/{doc_id}/revisions")
def revisions(doc_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    rec = _load_doc(db, principal, domain, doc_id)
    rows = db.scalars(select(DocumentRevision).where(DocumentRevision.document_id == rec.id)
                      .order_by(DocumentRevision.created_at.desc()))
    return {"items": [{"id": r.id, "kind": r.kind, "status": r.status, "plan_version_id": r.plan_version_id,
                       "content_hash": r.content_hash, "provider": r.provider, "external_id": r.external_id,
                       "expected_revision": r.expected_revision, "observed_revision": r.observed_revision,
                       "publication_id": r.publication_id, "created_at": _iso(r.created_at),
                       "acknowledged_by": r.acknowledged_by, "acknowledged_at": _iso(r.acknowledged_at),
                       "excerpt": (r.body_markdown or "")[:2000]} for r in rows],
            "source": SOURCE, "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.post("/api/documents/{doc_id}/revision-inputs/{rev_id}/acknowledge")
def acknowledge(doc_id: str, rev_id: str, body: AcknowledgeIn, db: DB, principal: AdminPrincipal,
                domain: ActiveDomain) -> dict[str, Any]:
    """The human confirms the external edit was reviewed. The observed revision becomes the baseline for
    LATER approved versions only — an older snapshot still never overwrites it (§29)."""
    rec = _load_doc(db, principal, domain, doc_id)
    rev = db.get(DocumentRevision, rev_id)
    if rev is None or rev.document_id != rec.id or rev.kind != "HUMAN_EDIT_INPUT":
        raise NotFound("revision input not found")
    if rev.status != "OPEN":
        raise Conflict("revision input already acknowledged")
    rev.status = "ACKNOWLEDGED"
    rev.acknowledged_by = principal.user.id
    rev.acknowledged_at = utcnow()
    rev.details = {**(rev.details or {}), "note": body.note}
    for ref in db.scalars(select(ExternalReference).where(ExternalReference.project_id == rec.project_id,
                                                          ExternalReference.logical_id == rec.logical_doc_id)):
        if rev.observed_revision:
            ref.external_revision = rev.observed_revision
        ref.sync_status = "STALE"  # external holds human content not yet in an approved version
    rec.links = {**(rec.links or {}), "conflict": None}
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="document.revision_input.acknowledge",
                 target_type="document", target_id=rec.id, project_id=rec.project_id, domain=rec.domain,
                 details={"revision_input_id": rev.id, "observed_revision": rev.observed_revision})
    events.emit(db, source="docs_engine", kind="document.revision_input.acknowledged", entity_type="document",
                entity_id=rec.id, project_id=rec.project_id, domain=rec.domain, payload={"revision_input_id": rev.id})
    db.commit()
    return {"id": rev.id, "status": rev.status}
