"""Documentation & Visuals service layer: DB rows → renderer inputs, persistence, local previews.

Approval is read only through ``aeos.planning.facade.get_approved_version`` (the version bound by an
ACTIVE approval decision). If the planning service is unavailable the version is treated as NOT
approved (fail closed): a local preview is still possible, DocumentRecords are not updated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.errors import NotFound
from aeos.core.paths import ensure_within, safe_segment
from aeos.docs_engine import renderer as r
from aeos.docs_engine import visuals as vis
from aeos.docs_engine.docx import markdown_to_docx
from aeos.docs_engine.plan_content import build_view
from aeos.models.identity import User
from aeos.models.planning import PlanVersion
from aeos.models.portfolio import Project
from aeos.models.publishing import DocumentRecord
from aeos.planning import facade as planning
from aeos.portfolio import facade as portfolio

log = logging.getLogger("aeos.docs_engine")


@dataclass(frozen=True)
class ApprovalState:
    approved: bool
    reason: str
    approved_version_id: str | None = None


def approval_state(session: Session, version: PlanVersion) -> ApprovalState:
    try:
        approved = planning.get_approved_version(session, version.plan_id)
    except (ImportError, ModuleNotFoundError):
        return ApprovalState(False, "planning service unavailable — approval cannot be verified (fail closed)")
    if approved is None:
        return ApprovalState(False, "plan has no approved version")
    if approved.id != version.id:
        return ApprovalState(False, f"approved version is {approved.id}, not this version", approved.id)
    if approved.manifest_hash != version.manifest_hash:
        return ApprovalState(False, "approved hash does not match the version content", approved.id)
    return ApprovalState(True, "", approved.id)


def project_identity(session: Session, project: Project) -> r.ProjectIdentity:
    owner = session.get(User, project.owner_user_id) if project.owner_user_id else None
    repos = tuple(
        r.RepoInfo(provider=x.provider, full_name=x.full_name, url=x.url or "", default_branch=x.default_branch)
        for x in sorted(portfolio.repositories(session, project.id), key=lambda x: (x.role != "project", x.full_name))
    )
    return r.ProjectIdentity(
        id=project.id, key=project.project_key, name=project.name, domain=project.domain,
        data_classification=project.data_classification, lifecycle=project.lifecycle,
        description=project.description or "", owner_name=(owner.display_name or owner.username) if owner else None,
        is_platform=bool(project.is_platform), project_kind=project.project_kind, repositories=repos,
    )


def task_infos(session: Session, plan_version_id: str) -> list[r.TaskInfo]:
    out = []
    for t in sorted(planning.tasks_for_version(session, plan_version_id), key=lambda x: x.task_key):
        out.append(r.TaskInfo(
            id=t.id, task_key=t.task_key, title=t.title, goal=t.goal or "", feature_key=t.feature_key,
            acceptance_criteria=tuple(str(x) if not isinstance(x, dict) else str(x.get("text") or x.get("title") or x)
                                      for x in (t.acceptance_criteria or [])),
            depends_on=tuple(t.depends_on or []), base_commit=t.base_commit,
        ))
    return out


def visuals_dir(settings: Settings, project: Project) -> Path:
    return settings.layout.publications(Domain(project.domain), project.project_key) / "visuals"


def render_version(session: Session, settings: Settings, version: PlanVersion, *, persist: bool,
                   render_visuals: bool = True, decision_id: str | None = None,
                   mmdc: vis.MmdcInfo | None = None) -> tuple[r.RenderResult, r.DeltaReport | None, ApprovalState]:
    """Render the document set for *version*. ``persist`` upserts DocumentRecords (approved only)."""
    project = session.get(Project, version.project_id)
    if project is None:
        raise NotFound("project not found")
    state = approval_state(session, version)
    view = build_view(version.content)
    visuals = vis.render_visuals(view.diagrams, visuals_dir(settings, project), enabled=render_visuals, mmdc=mmdc)
    previous = r.previous_input_hashes(session, project.id) if (persist and state.approved) else {}
    result = r.render_document_set(
        project=project_identity(session, project),
        version=r.VersionIdentity(id=version.id, plan_id=version.plan_id, version=version.version,
                                  manifest_hash=version.manifest_hash, approved=state.approved,
                                  approval_decision_id=decision_id, created_at=version.created_at),
        content=version.content, tasks=task_infos(session, version.id), visuals=visuals,
        previous_input_hashes=previous,
    )
    delta = r.sync_document_records(session, result, domain=project.domain) if (persist and state.approved) else None
    return result, delta, state


def document_body(session: Session, project_id: str, doc: r.RenderedDocument) -> str:
    """Body for a rendered doc; reused (unchanged) documents come from the stored record."""
    if doc.body_markdown is not None:
        return doc.body_markdown
    rec = session.scalar(select(DocumentRecord).where(DocumentRecord.project_id == project_id,
                                                      DocumentRecord.logical_doc_id == doc.logical_doc_id))
    return rec.body_markdown if rec else ""


def image_resolver(result: r.RenderResult):
    by_key = {v.key: v for v in result.visuals}

    def resolve(key: str) -> Path | None:
        v = by_key.get(key)
        if v is None or v.status != vis.STATUS_RENDERED or not v.png_path:
            return None
        return Path(v.png_path)

    return resolve


def docx_snapshot(result: r.RenderResult, doc: r.RenderedDocument, body: str) -> bytes:
    return markdown_to_docx(body, title=doc.title, logical_id=doc.logical_doc_id, image_resolver=image_resolver(result))


def _segments(result: r.RenderResult) -> dict[str, list[str]]:
    """Path segments per logical id, following ``parent_logical_id`` so the local preview mirrors the
    Product 1 folder tree (e.g. ``04-security-privacy/04-threat-models/...``). Every segment is a
    validated safe segment; the outer folder (``root``) is the output directory itself."""
    by_id = result.by_id()
    out: dict[str, list[str]] = {}

    def resolve(lid: str, depth: int = 0) -> list[str]:
        if lid in out:
            return out[lid]
        if lid == "root" or depth > 32:
            return []
        doc = by_id.get(lid)
        parent = doc.parent_logical_id if doc else None
        own = safe_segment(lid.rsplit("/", 1)[-1])
        segs = [*(resolve(parent, depth + 1) if parent else []), own]
        out[lid] = segs
        return segs

    for d in result.documents:
        resolve(d.logical_doc_id)
    return out


def _doc_path(out_dir: Path, segments: list[str], suffix: str) -> Path:
    if not segments:
        return ensure_within(out_dir, out_dir)
    path = out_dir.joinpath(*segments[:-1], segments[-1] + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    return ensure_within(out_dir, path)


def write_preview(session: Session, result: r.RenderResult, out_dir: Path, *, docx: bool = False) -> dict[str, Any]:
    """Write a local preview tree (Markdown + optional DOCX) — never an external publication."""
    out_dir.mkdir(parents=True, exist_ok=True)
    segs = _segments(result)
    written: list[str] = []
    for doc in result.documents:
        if doc.logical_doc_id == "root":
            continue  # out_dir itself is the project folder
        if doc.is_folder:
            folder = _doc_path(out_dir, segs[doc.logical_doc_id], "")
            folder.mkdir(parents=True, exist_ok=True)
            continue
        body = document_body(session, result.project_id, doc)
        md = _doc_path(out_dir, segs[doc.logical_doc_id], ".md")
        md.write_text(body, encoding="utf-8", newline="\n")
        written.append(md.relative_to(out_dir.resolve()).as_posix())
        if docx:
            dx = _doc_path(out_dir, segs[doc.logical_doc_id], ".docx")
            dx.write_bytes(docx_snapshot(result, doc, body))
    index = ["# " + result.product_title, "",
             "PREVIEW LOKAL — bukan publikasi eksternal." + ("" if result.approved else " Versi BELUM DISETUJUI."), ""]
    for doc in result.documents:
        depth = len(segs.get(doc.logical_doc_id, []))
        marker = "[folder]" if doc.is_folder else "[doc]"
        index.append(f"{'  ' * depth}- {marker} {doc.title} — {doc.status}"
                     + (f" ({len(doc.pending_sections)} section pending)" if doc.pending_sections else ""))
    (out_dir / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8", newline="\n")
    return {"out_dir": str(out_dir), "files": written, "visuals": [v.to_dict() for v in result.visuals]}
