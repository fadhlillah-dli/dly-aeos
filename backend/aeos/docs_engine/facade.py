"""Cross-module contract of ``aeos.docs_engine`` (§27 Product 1 structure, §28 document timing, §29 visuals).
Keep signatures stable.

The publisher renders and delivers Product 1 documents through this contract: render an approved PlanVersion,
read a document body, convert Markdown to DOCX, and check the readiness sections of a stage. Document content
rules (content types, DYNAMIC references, owners) stay inside docs_engine.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.docs_engine.product1 import DOC_TYPES, Timing
from aeos.docs_engine.renderer import (
    ST_APPROVED,
    ST_DRAFT,
    ST_NOT_STARTED,
    ST_PUBLISHED,
    ST_SUPERSEDED,
    RenderedDocument,
    RenderResult,
)
from aeos.models.planning import PlanVersion

__all__ = [
    "DOC_TYPES",
    "ST_APPROVED",
    "ST_DRAFT",
    "ST_NOT_STARTED",
    "ST_PUBLISHED",
    "ST_SUPERSEDED",
    "RenderResult",
    "RenderedDocument",
    "Timing",
    "as_text",
    "build_view",
    "checkpoint_artifacts",
    "document_body",
    "documentation_checkpoint",
    "feature_slug",
    "markdown_to_docx",
    "render_version",
    "required_pending",
    "stage_blockers",
]


def render_version(session: Session, settings: Settings, version: PlanVersion, *, persist: bool,
                   render_visuals: bool = True, decision_id: str | None = None) -> tuple[Any, Any, Any]:
    """(RenderResult, DeltaReport | None, ApprovalState) of the Product 1 set for *version* (persist: approved)."""
    from aeos.docs_engine.service import render_version as impl

    return impl(session, settings, version, persist=persist, render_visuals=render_visuals, decision_id=decision_id)


def document_body(session: Session, project_id: str, doc: RenderedDocument) -> str:
    """Markdown body of a rendered document (reused documents come from the stored record)."""
    from aeos.docs_engine.service import document_body as impl

    return impl(session, project_id, doc)


def markdown_to_docx(markdown: str, *, title: str = "", logical_id: str = "", image_resolver: Any = None) -> bytes:
    from aeos.docs_engine.docx import markdown_to_docx as impl

    return impl(markdown, title=title, logical_id=logical_id, image_resolver=image_resolver)


def required_pending(documents: Any, stage: Timing) -> list[dict[str, Any]]:
    """Required sections of *stage* still documentation pending, per rendered document (§41 readiness)."""
    from aeos.docs_engine.product1 import required_pending as impl

    return impl(documents, stage)


def stage_blockers(session: Session, version: PlanVersion, *, task_id: str | None = None,
                   feature_key: str | None = None) -> list[dict[str, Any]]:
    """§41 readiness: required PROJECT_CREATION / FEATURE_APPROVAL sections still pending before implementation of
    *task_id* (its feature) may start. ``[]`` = ready (or not a Product 1 plan). Each item: logical_doc_id, title,
    sections, stage."""
    from aeos.docs_engine.service import stage_blockers as impl

    return impl(session, version, task_id=task_id, feature_key=feature_key)


def documentation_checkpoint(session: Session, revision_id: str) -> dict[str, Any]:
    """View of one recorded documentation checkpoint package (§41) with its ``project_id``/``domain`` scope;
    ``NotFound`` when *revision_id* is not a checkpoint. Read-only."""
    from aeos.docs_engine.checkpoints import checkpoint_view, get_checkpoint

    rev = get_checkpoint(session, revision_id)
    return {**checkpoint_view(rev), "project_id": rev.project_id, "domain": rev.domain}


def checkpoint_artifacts(session: Session, project_id: str) -> list[tuple[str, dict[str, Any]]]:
    """(label, hash-bound artifact) pairs of the project's recorded documentation checkpoint packages, newest first:
    ``doc_checkpoint:<revision id>`` (exactly that package) and ``documentation_checkpoint:<project>:<kind>:<ref>``
    (the newest package of that checkpoint) -> {ref, kind, sha256, name, content}. Read-only; used by the task
    compiler to resolve a writer task's input artifact (§21, §41)."""
    from aeos.docs_engine.checkpoints import checkpoint_artifacts as impl

    return impl(session, project_id)


def build_view(content: dict[str, Any] | None) -> Any:
    """Read-only view over approved plan content (the renderer's own reading of the schema)."""
    from aeos.docs_engine.plan_content import build_view as impl

    return impl(content)


def feature_slug(raw: Any) -> str:
    from aeos.docs_engine.plan_content import feature_slug as impl

    return impl(raw)


def evidence_chart(*, title: str, unit: str, source: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    """§29 evidence chart from recorded data only (each point bound to its record id; gaps stay UNKNOWN)."""
    from aeos.docs_engine.visuals import evidence_chart as impl

    return impl(title=title, unit=unit, source=source, points=points)


def as_text(value: Any) -> str:
    from aeos.docs_engine.plan_content import as_text as impl

    return impl(value)
