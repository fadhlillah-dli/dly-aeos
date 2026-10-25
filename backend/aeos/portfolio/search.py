"""Metadata search (§11 "Pencarian metadata/FTS yang sederhana", §35).

SQLite ``LIKE`` over metadata columns of canonical records, scoped on the backend to the ACTIVE
domain and the principal's project scope. Counts, totals and pagination are computed with the same
filter, so the existence of other-domain rows never leaks (§35, §50 "Viewer mengakses detail/
search/export inventory Office"). Platform-global records (ADRs, global decisions, components
without a domain scope) are included; domain- or project-scoped components only when that domain /
project is visible. With ``project_id`` the search is project-scoped (that project's rows plus the
platform-global records), §35 "project-scoped/global metadata search".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.identity.facade import Principal
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.models.inventory import ComponentReference
from aeos.models.planning import Plan, TaskSpec
from aeos.models.portfolio import Attachment, Project, RepositoryReference
from aeos.models.publishing import DocumentRecord
from aeos.models.research import MemoryRecord

KINDS = ("project", "plan", "task", "component", "decision", "adr", "document", "attachment", "repository", "memory")
MAX_LIMIT = 100


def _like(q: str) -> str:
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _match(pattern: str, *cols: Any) -> Any:
    return or_(*[c.ilike(pattern, escape="\\") for c in cols])


def _snippet(text: str | None, q: str, width: int = 140) -> str:
    if not text:
        return ""
    i = text.lower().find(q.lower())
    if i < 0:
        return text[:width]
    a = max(0, i - width // 3)
    return ("…" if a else "") + text[a:a + width] + ("…" if a + width < len(text) else "")


@dataclass(frozen=True)
class _Visibility:
    domain: str
    project_refs: frozenset[str]  # ids and keys of the projects this search may show


@dataclass(frozen=True)
class _Kind:
    build: Callable[[str, str, list[str] | None], Select[Any]]
    row: Callable[[Any, str], dict[str, Any]]
    python_filter: Callable[[Any, _Visibility], bool] | None = None


def _scope(q: Select[Any], col: Any, project_ids: list[str] | None) -> Select[Any]:
    return q.where(col.in_(project_ids)) if project_ids is not None else q


def _projects(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    q = select(Project).where(Project.domain == domain,
                              _match(pat, Project.name, Project.project_key, Project.description, Project.idea))
    return _scope(q, Project.id, pids)


def _plans(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    return _scope(select(Plan).where(Plan.domain == domain, _match(pat, Plan.title, Plan.state_reason)),
                  Plan.project_id, pids)


def _tasks(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    q = select(TaskSpec).where(TaskSpec.domain == domain, _match(pat, TaskSpec.title, TaskSpec.goal, TaskSpec.task_key))
    return _scope(q, TaskSpec.project_id, pids)


def _components(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    return select(ComponentReference).where(
        ComponentReference.tombstoned_at.is_(None),
        _match(pat, ComponentReference.name, ComponentReference.friendly_name, ComponentReference.function,
               ComponentReference.component_id, ComponentReference.category))


def _component_visible(c: ComponentReference, vis: _Visibility) -> bool:
    scope = c.scope if isinstance(c.scope, dict) else {}
    domains = scope.get("domains")
    if isinstance(domains, list) and domains and vis.domain not in domains:
        return False
    if "domain" in scope and scope.get("domain") not in (None, "", vis.domain):
        return False
    refs: list[Any] = []
    for key in ("project_id", "project"):
        if scope.get(key):
            refs.append(scope[key])
    if isinstance(scope.get("project_ids"), list):
        refs.extend(scope["project_ids"])
    if refs:  # project-scoped component: only when one of its projects is visible (fail closed)
        return any(isinstance(r, str) and r in vis.project_refs for r in refs)
    return True  # platform-global component


def _decisions(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    visible = or_(Decision.domain == domain, (Decision.domain.is_(None) & Decision.project_id.is_(None)))
    q = select(Decision).where(visible, _match(pat, Decision.rationale, Decision.kind, Decision.target_type,
                                               Decision.target_id))
    if pids is not None:
        q = q.where(or_(Decision.project_id.is_(None), Decision.project_id.in_(pids)))
    return q


def _adrs(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    return select(ArchitectureDecisionRecord).where(_match(
        pat, ArchitectureDecisionRecord.title, ArchitectureDecisionRecord.adr_key, ArchitectureDecisionRecord.decision,
        ArchitectureDecisionRecord.context))


def _documents(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    q = select(DocumentRecord).where(DocumentRecord.domain == domain,
                                     _match(pat, DocumentRecord.title, DocumentRecord.logical_doc_id))
    return _scope(q, DocumentRecord.project_id, pids)


def _attachments(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    return _scope(select(Attachment).where(Attachment.domain == domain, _match(pat, Attachment.filename)),
                  Attachment.project_id, pids)


def _repositories(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    q = (select(RepositoryReference).join(Project, Project.id == RepositoryReference.project_id)
         .where(Project.domain == domain, _match(pat, RepositoryReference.full_name)))
    return _scope(q, RepositoryReference.project_id, pids)


def _memory(pat: str, domain: str, pids: list[str] | None) -> Select[Any]:
    q = select(MemoryRecord).where(MemoryRecord.domain == domain, _match(pat, MemoryRecord.content))
    if pids is not None:
        q = q.where(MemoryRecord.project_id.in_(pids))
    return q


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


KIND_SPECS: dict[str, _Kind] = {
    "project": _Kind(_projects, lambda r, q: {
        "id": r.id, "title": r.name, "subtitle": r.project_key, "snippet": _snippet(r.description or r.idea, q),
        "project_id": r.id, "status": r.lifecycle, "url": f"/projects/{r.id}", "updated_at": _iso(r.updated_at)}),
    "plan": _Kind(_plans, lambda r, q: {
        "id": r.id, "title": r.title, "subtitle": r.state, "snippet": _snippet(r.state_reason, q),
        "project_id": r.project_id, "status": r.state, "url": f"/plans/{r.id}", "updated_at": _iso(r.updated_at)}),
    "task": _Kind(_tasks, lambda r, q: {
        "id": r.id, "title": r.title, "subtitle": r.task_key, "snippet": _snippet(r.goal, q),
        "project_id": r.project_id, "status": None, "url": f"/dag?project={r.project_id}&task={r.id}",
        "updated_at": _iso(r.created_at)}),
    "component": _Kind(_components, lambda r, q: {
        "id": r.component_id, "title": r.friendly_name or r.name, "subtitle": r.kind, "snippet": _snippet(r.function, q),
        "project_id": None, "status": r.design_label, "url": f"/tools/{r.component_id}", "updated_at": _iso(r.updated_at)},
        _component_visible),
    "decision": _Kind(_decisions, lambda r, q: {
        "id": r.id, "title": f"{r.kind} · {r.target_type}", "subtitle": r.target_id, "snippet": _snippet(r.rationale, q),
        "project_id": r.project_id, "status": r.status, "url": f"/decisions?id={r.id}", "updated_at": _iso(r.created_at)}),
    "adr": _Kind(_adrs, lambda r, q: {
        "id": r.adr_key, "title": f"{r.adr_key} — {r.title}", "subtitle": r.status, "snippet": _snippet(r.decision, q),
        "project_id": None, "status": r.status, "url": f"/decisions?adr={r.adr_key}", "updated_at": _iso(r.updated_at)}),
    "document": _Kind(_documents, lambda r, q: {
        "id": r.id, "title": r.title, "subtitle": f"{r.node} · {r.logical_doc_id}", "snippet": "",
        "project_id": r.project_id, "status": r.status, "url": f"/publications?project={r.project_id}",
        "updated_at": _iso(r.updated_at)}),
    "attachment": _Kind(_attachments, lambda r, q: {
        "id": r.id, "title": r.filename, "subtitle": r.detected_type, "snippet": "",
        "project_id": r.project_id, "status": r.scan_status, "url": f"/projects/{r.project_id}#attachments",
        "updated_at": _iso(r.updated_at)}),
    "repository": _Kind(_repositories, lambda r, q: {
        "id": r.id, "title": r.full_name, "subtitle": r.provider, "snippet": "",
        "project_id": r.project_id, "status": r.verification_status, "url": f"/projects/{r.project_id}#repositories",
        "updated_at": _iso(r.updated_at)}),
    "memory": _Kind(_memory, lambda r, q: {
        "id": r.id, "title": _snippet(r.content, q, 80), "subtitle": r.scope, "snippet": _snippet(r.content, q),
        "project_id": r.project_id, "status": r.status, "url": f"/projects/{r.project_id}#memory",
        "updated_at": _iso(r.updated_at)}),
}


# Deterministic ordering so offset pagination is stable.
ORDERING: dict[str, tuple[Any, ...]] = {
    "project": (Project.is_platform.desc(), Project.name, Project.id),
    "plan": (Plan.updated_at.desc(), Plan.id),
    "task": (TaskSpec.title, TaskSpec.id),
    "component": (ComponentReference.name, ComponentReference.component_id),
    "decision": (Decision.created_at.desc(), Decision.id),
    "adr": (ArchitectureDecisionRecord.adr_key,),
    "document": (DocumentRecord.node, DocumentRecord.title, DocumentRecord.id),
    "attachment": (Attachment.created_at.desc(), Attachment.id),
    "repository": (RepositoryReference.full_name, RepositoryReference.id),
    "memory": (MemoryRecord.created_at.desc(), MemoryRecord.id),
}


def _visibility(session: Session, domain: str, pids: list[str] | None) -> _Visibility:
    q = select(Project.id, Project.project_key).where(Project.domain == domain)
    if pids is not None:
        q = q.where(Project.id.in_(pids))
    refs: set[str] = set()
    for pid, key in session.execute(q).all():
        refs.update((pid, key))
    return _Visibility(domain, frozenset(refs))


def search(
    session: Session, principal: Principal, domain: Domain, *, q: str, kind: str | None = None,
    limit: int = 20, offset: int = 0, project_id: str | None = None,
) -> dict[str, Any]:
    """*project_id* must already be authorised by the caller (``load_project``)."""
    query = (q or "").strip()
    if len(query) < 2:
        raise ValidationFailed("search query needs at least 2 characters", code="QUERY_TOO_SHORT")
    if len(query) > 200:
        raise ValidationFailed("search query is too long", code="QUERY_TOO_LONG")
    kinds = list(KINDS)
    if kind:
        wanted = [k.strip().lower() for k in kind.split(",") if k.strip()]
        bad = [k for k in wanted if k not in KIND_SPECS]
        if bad:
            raise ValidationFailed(f"unknown kind(s): {', '.join(bad)}", code="INVALID_KIND", details={"kinds": list(KINDS)})
        kinds = wanted
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    pat = _like(query)
    pids = [project_id] if project_id else principal.allowed_project_ids
    vis: _Visibility | None = None
    counts: dict[str, int] = {}
    collected: list[dict[str, Any]] = []
    need = offset + limit
    for k in kinds:
        spec = KIND_SPECS[k]
        stmt = spec.build(pat, domain.value, pids)
        if spec.python_filter is not None:
            vis = vis or _visibility(session, domain.value, pids)
            rows = [r for r in session.scalars(stmt.order_by(*ORDERING[k])) if spec.python_filter(r, vis)]
            counts[k] = len(rows)
            rows = rows[:need]
        else:
            counts[k] = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            rows = list(session.scalars(stmt.order_by(*ORDERING[k]).limit(need)))
        collected.extend({"kind": k, **spec.row(r, query)} for r in rows)
    page = collected[offset:offset + limit]
    total = sum(counts.values())
    return {
        "items": page,
        "total": total,
        "counts": counts,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
        "query": query,
        "domain": domain.value,
        "project_id": project_id,
        "source": "sqlite-like (metadata only, active domain)",
        "checked_at": utcnow().isoformat(),
        "mode": "LIVE",
    }
