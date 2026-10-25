"""Control Center read projections: activity feed, integrations, metadata search, history (§35, §41).

All queries enforce the backend scope: the session's active domain (plus platform rows without a
domain) and the user's project scope. Hiding a name in the UI is not enforcement (§35), so counts,
search hits and timelines are filtered here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.redaction import redact
from aeos.core.timeutil import utcnow
from aeos.identity.facade import Principal
from aeos.models.audit import AuditEvent, Event
from aeos.models.decisions import ArchitectureDecisionRecord, ArchitectureSnapshot, Decision
from aeos.models.identity import User
from aeos.models.inventory import CheckResult, ComponentReference
from aeos.models.planning import Plan, TaskSpec
from aeos.models.portfolio import Project, ProjectIntegration
from aeos.models.publishing import DocumentRecord, Publication, PublicationOperation
from aeos.models.research import ChangeProposal
from aeos.models.workforce import WorkerRun


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def scoped_project_ids(session: Session, principal: Principal, domain: Domain) -> list[str]:
    ids = list(session.scalars(select(Project.id).where(Project.domain == domain.value)))
    if principal.allowed_project_ids is not None:
        ids = [i for i in ids if i in principal.allowed_project_ids]
    return ids


def scope_visible(scope: dict[str, Any] | None, domain: Domain, project_ids: list[str] | set[str]) -> bool:
    """Inventory-style ``scope`` dicts (``domain``/``domains``/``project_id``): a row scoped to another
    domain or to a project outside the user's scope is not visible (same rule as exports, §35/§41)."""
    scope = scope or {}
    domains: set[str] = set()
    if isinstance(scope.get("domains"), list):
        domains |= {str(d).upper() for d in scope["domains"]}
    if isinstance(scope.get("domain"), str):
        domains.add(scope["domain"].upper())
    if domains and domain.value not in domains:
        return False
    pid = scope.get("project_id")
    return not (pid and pid not in project_ids)


# ----------------------------------------------------------------------------------------- activity


def activity(session: Session, principal: Principal, domain: Domain, *, before: int | None, limit: int,
             kind: str | None, project_id: str | None) -> dict[str, Any]:
    pids = scoped_project_ids(session, principal, domain)
    q = (select(Event).where(or_(Event.domain == domain.value, Event.domain.is_(None)))
         .where(or_(Event.project_id.is_(None), Event.project_id.in_(pids)))
         .order_by(Event.seq.desc()).limit(min(limit, 200) + 1))
    if before:
        q = q.where(Event.seq < before)
    if kind:
        q = q.where(Event.kind.startswith(kind))
    if project_id:
        q = q.where(Event.project_id == project_id)
    rows = list(session.scalars(q))
    more = len(rows) > limit
    rows = rows[:limit]
    latest = session.scalar(select(func.max(Event.seq)).where(or_(Event.domain == domain.value, Event.domain.is_(None))))
    return {
        "items": [{"seq": e.seq, "at": e.at.isoformat(), "source": e.source, "kind": e.kind,
                   "entity_type": e.entity_type, "entity_id": e.entity_id, "entity_version": e.entity_version,
                   "project_id": e.project_id, "domain": e.domain, "run_id": e.run_id, "attempt": e.attempt,
                   "mode": e.mode, "payload": redact(e.payload or {})} for e in rows],
        "next_before": rows[-1].seq if more and rows else None,
        "latest_seq": int(latest or 0),
        "source": "events (persisted first, then projected; cursor = seq)",
        "checked_at": utcnow().isoformat(),
        "mode": "LIVE",
        "evidence_limit": "Aktivitas = tindakan teramati yang tercatat; narasi AI tidak menjadi status.",
    }


# ----------------------------------------------------------------------------------------- integrations


def _secret_status(ref: str | None) -> dict[str, Any]:
    if not ref:
        return {"status": "NOT_APPLICABLE"}
    try:
        from aeos.secrets import facade as secrets

        st = secrets.secret_status(ref)
        return {"status": "CONFIGURED" if st.exists else "MISSING", "backend": st.backend,
                "updated_at": _iso(st.updated_at), "expires_at": _iso(st.expires_at)}
    except ImportError as exc:
        return {"status": "UNKNOWN", "reason": f"modul secrets belum tersedia ({exc.name or exc})"}
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        return {"status": "UNKNOWN", "reason": f"secret store tidak tersedia ({code})"}


def integrations(session: Session, principal: Principal, domain: Domain, project_id: str | None) -> dict[str, Any]:
    pids = scoped_project_ids(session, principal, domain)
    if project_id:
        pids = [p for p in pids if p == project_id]
    rows = list(session.scalars(select(ProjectIntegration).where(ProjectIntegration.domain == domain.value,
                                                                 ProjectIntegration.project_id.in_(pids))))
    projects = {p.id: p for p in session.scalars(select(Project).where(Project.id.in_(pids)))} if pids else {}
    checks = list(session.scalars(select(CheckResult).order_by(CheckResult.last_checked_at.desc()).limit(2000)))
    latest: dict[str, CheckResult] = {}
    for c in checks:
        scope = c.scope or {}
        if scope.get("domain") and str(scope["domain"]).upper() != domain.value:
            continue
        if scope.get("project_id") and scope["project_id"] not in pids:
            continue
        latest.setdefault(c.check_id, c)
    items: list[dict[str, Any]] = []
    from aeos.portfolio import providers as pv

    for integ in rows:
        # One provider vocabulary (aeos.portfolio.providers): the Doctor checks that probe this provider.
        # "github-issues" is probed by adapter.github_token, which no substring of the provider id matches.
        probes = set(pv.DOCTOR_CHECKS.get(pv.normalize(integ.provider), ()))
        related = [c for c in latest.values() if c.check_id in probes
                   or integ.provider.lower() in (c.check_id or "").lower()
                   or integ.provider.lower() in (c.component_id or "").lower()]
        items.append({
            "id": integ.id, "project_id": integ.project_id,
            "project_name": projects[integ.project_id].name if integ.project_id in projects else None,
            "provider": integ.provider, "purpose": integ.purpose, "tenant_id": integ.tenant_id, "root_id": integ.root_id,
            "capability_status": redact(integ.capability_status or {}), "config": redact(integ.config or {}),
            "credential_ref": integ.credential_ref, "credential": _secret_status(integ.credential_ref),
            "last_verified_at": _iso(integ.last_verified_at),
            "verification": "NOT_TESTED" if integ.last_verified_at is None else "RECORDED",
            "checks": [{"check_id": c.check_id, "status": c.status, "severity": c.severity, "summary": c.summary,
                        "last_checked_at": _iso(c.last_checked_at)} for c in related],
        })
    adapter_checks = [c for c in latest.values() if any(k in (c.group or "").lower()
                                                        for k in ("adapter", "integration", "delivery", "project"))]
    return {
        "items": items,
        "checks": [{"check_id": c.check_id, "group": c.group, "status": c.status, "severity": c.severity,
                    "summary": c.summary, "last_checked_at": _iso(c.last_checked_at)} for c in adapter_checks],
        "source": "project_integrations + check_results + secret store status",
        "checked_at": utcnow().isoformat(),
        "mode": "LIVE",
        "evidence_limit": "Credential hanya status/ref/expiry - tanpa nilai. CONNECTED bukan VERIFIED.",
    }


# ----------------------------------------------------------------------------------------- search


def search(session: Session, principal: Principal, domain: Domain, q: str, limit: int = 30) -> dict[str, Any]:
    term = q.strip()
    pids = scoped_project_ids(session, principal, domain)
    hits: list[dict[str, Any]] = []

    def like(col: Any) -> Any:
        return func.lower(col).contains(term.lower(), autoescape=True)

    if pids:
        for p in session.scalars(select(Project).where(Project.id.in_(pids), or_(
                like(Project.name), like(Project.project_key), like(Project.description))).limit(limit)):
            hits.append({"type": "project", "id": p.id, "title": p.name, "subtitle": p.project_key,
                         "link": f"/projects/{p.id}"})
        for pl in session.scalars(select(Plan).where(Plan.project_id.in_(pids), like(Plan.title)).limit(limit)):
            hits.append({"type": "plan", "id": pl.id, "title": pl.title, "subtitle": pl.state, "link": f"/plans/{pl.id}"})
        for t in session.scalars(select(TaskSpec).where(TaskSpec.project_id.in_(pids), or_(
                like(TaskSpec.title), like(TaskSpec.task_key))).limit(limit)):
            hits.append({"type": "task", "id": t.id, "title": t.title, "subtitle": t.task_key, "link": "/dag"})
        for doc in session.scalars(select(DocumentRecord).where(DocumentRecord.project_id.in_(pids),
                                                                like(DocumentRecord.title)).limit(limit)):
            hits.append({"type": "document", "id": doc.id, "title": doc.title, "subtitle": doc.status,
                         "link": "/publications"})
        for r in session.scalars(select(WorkerRun).where(WorkerRun.project_id.in_(pids), or_(
                WorkerRun.id.startswith(term, autoescape=True), like(WorkerRun.role_key))).limit(limit)):
            hits.append({"type": "run", "id": r.id, "title": r.role_key or r.id, "subtitle": r.execution_status,
                         "link": f"/runs/{r.id}"})
    for c in session.scalars(select(ComponentReference).where(or_(
            like(ComponentReference.name), like(ComponentReference.friendly_name),
            like(ComponentReference.component_id))).limit(limit)):
        if not scope_visible(c.scope, domain, pids):
            continue
        hits.append({"type": "component", "id": c.component_id, "title": c.friendly_name or c.name,
                     "subtitle": c.design_label, "link": f"/tools/{c.component_id}"})
    for a in session.scalars(select(ArchitectureDecisionRecord).where(or_(
            like(ArchitectureDecisionRecord.title), like(ArchitectureDecisionRecord.adr_key))).limit(limit)):
        hits.append({"type": "adr", "id": a.adr_key, "title": a.title, "subtitle": a.status, "link": "/evidence"})
    for cp in session.scalars(select(ChangeProposal).where(
            like(ChangeProposal.title),
            or_(ChangeProposal.domain.is_(None), ChangeProposal.domain == domain.value),
            or_(ChangeProposal.project_id.is_(None), ChangeProposal.project_id.in_(pids))).limit(limit)):
        hits.append({"type": "proposal", "id": cp.id, "title": cp.title, "subtitle": cp.status,
                     "link": f"/proposals/{cp.id}"})
    return {"query": term, "items": hits[:limit], "truncated": len(hits) > limit,
            "scope": {"domain": domain.value, "projects": len(pids)}, "source": "metadata search (scoped)",
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


# ----------------------------------------------------------------------------------------- history


def attempt_timeline(session: Session, principal: Principal, domain: Domain, *, outcome: str | None = None,
                     page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """§35 History: accepted / failed / cancelled attempts across runs (domain + project scoped). Each row
    keeps worker-reported, verifier, acceptance and merge status separate; finished_at orders the timeline."""
    pids = scoped_project_ids(session, principal, domain)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    q = select(WorkerRun).where(WorkerRun.domain == domain.value, WorkerRun.project_id.in_(pids))
    if outcome == "ACCEPTED":
        q = q.where(WorkerRun.acceptance_status == "ACCEPTED")
    elif outcome == "FAILED":
        q = q.where(WorkerRun.execution_status.in_(["FAILED", "TIMED_OUT"]))
    elif outcome == "CANCELLED":
        q = q.where(WorkerRun.execution_status == "CANCELLED")
    total = int(session.scalar(select(func.count()).select_from(q.subquery())) or 0)
    rows = list(session.scalars(q.order_by(WorkerRun.finished_at.desc().nulls_last(), WorkerRun.created_at.desc())
                                .offset((page - 1) * page_size).limit(page_size)))
    return {
        "items": [{"run_id": r.id, "task_id": r.task_id, "project_id": r.project_id, "attempt": r.attempt,
                   "run_type": r.run_type, "execution_status": r.execution_status,
                   "worker_reported_status": r.worker_reported_status, "verifier_status": r.verifier_status,
                   "acceptance_status": r.acceptance_status, "merged_status": r.merged_status,
                   "started_at": r.started_at.isoformat() if r.started_at else None,
                   "finished_at": r.finished_at.isoformat() if r.finished_at else None, "mode": r.mode,
                   "link": f"/runs/{r.id}"} for r in rows],
        "page": page, "page_size": page_size, "total": total,
        "source": "worker_runs (separate worker/verifier/acceptance/merge statuses)",
        "checked_at": utcnow().isoformat(), "mode": "LIVE",
    }


def audit_timeline(session: Session, principal: Principal, domain: Domain, *, action: str | None,
                   target_type: str | None, target_id: str | None, result: str | None, before: int | None,
                   limit: int) -> dict[str, Any]:
    pids = scoped_project_ids(session, principal, domain)
    q = (select(AuditEvent).where(or_(AuditEvent.domain == domain.value, AuditEvent.domain.is_(None)))
         .where(or_(AuditEvent.project_id.is_(None), AuditEvent.project_id.in_(pids)))
         .order_by(AuditEvent.seq.desc()).limit(min(limit, 500) + 1))
    if action:
        q = q.where(AuditEvent.action.startswith(action))
    if target_type:
        q = q.where(AuditEvent.target_type == target_type)
    if target_id:
        q = q.where(AuditEvent.target_id == target_id)
    if result:
        q = q.where(AuditEvent.result == result)
    if before:
        q = q.where(AuditEvent.seq < before)
    rows = list(session.scalars(q))
    more = len(rows) > limit
    rows = rows[:limit]
    from aeos.operations import facade as operations

    retention = operations.RetentionMarkers(session)  # §35: the policy rule of each record, never a deletion
    return {
        "items": [{"seq": r.seq, "at": r.at.isoformat(), "actor_type": r.actor_type, "actor_id": r.actor_id,
                   "action": r.action, "target_type": r.target_type, "target_id": r.target_id,
                   "project_id": r.project_id, "domain": r.domain, "result": r.result, "reason": redact(r.reason or ""),
                   "details": redact(r.details or {}), "retention": retention.audit(r)} for r in rows],
        "retention_source": "policy/retention.yaml (owner decisions; protected path)",
        "next_before": rows[-1].seq if more and rows else None,
        "source": "audit_events (append-only local table; not a WORM guarantee - §34)",
        "checked_at": utcnow().isoformat(),
        "mode": "LIVE",
    }


def decision_timeline(session: Session, principal: Principal, domain: Domain, *, limit: int = 200) -> dict[str, Any]:
    pids = scoped_project_ids(session, principal, domain)
    q = (select(Decision).where(or_(Decision.domain == domain.value, Decision.domain.is_(None)))
         .where(or_(Decision.project_id.is_(None), Decision.project_id.in_(pids)))
         .order_by(Decision.created_at.desc()).limit(min(limit, 500)))
    rows = list(session.scalars(q))
    users = {u.id: u.username for u in session.scalars(select(User).where(User.id.in_({r.actor_user_id for r in rows})))} \
        if rows else {}
    return {
        "items": [{"id": d.id, "kind": d.kind, "status": d.status, "actor": users.get(d.actor_user_id, d.actor_user_id),
                   "actor_kind": d.actor_kind, "target_type": d.target_type, "target_id": d.target_id,
                   "target_version": d.target_version, "target_hash": d.target_hash,
                   "allowed_operations": d.allowed_operations, "rationale": redact(d.rationale or ""),
                   "project_id": d.project_id, "created_at": _iso(d.created_at), "expires_at": _iso(d.expires_at),
                   "invalidated_at": _iso(d.invalidated_at), "invalidation_reason": d.invalidation_reason,
                   "superseded_by": d.superseded_by} for d in rows],
        "source": "decisions (human AUTH bound to target + hash)",
        "checked_at": utcnow().isoformat(),
        "mode": "LIVE",
    }


def _diff(before: Any, after: Any, path: str = "", out: list[dict[str, Any]] | None = None,
          max_items: int = 200) -> list[dict[str, Any]]:
    out = [] if out is None else out
    if len(out) >= max_items:
        return out
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            p = f"{path}.{key}" if path else str(key)
            if key not in before:
                out.append({"path": p, "change": "added", "after": after[key]})
            elif key not in after:
                out.append({"path": p, "change": "removed", "before": before[key]})
            else:
                _diff(before[key], after[key], p, out, max_items)
    elif before != after:
        out.append({"path": path or "(root)", "change": "changed", "before": before, "after": after})
    return out[:max_items]


def snapshot_history(session: Session, *, limit: int = 50) -> dict[str, Any]:
    rows = list(session.scalars(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at.asc())))
    items: list[dict[str, Any]] = []
    prev: ArchitectureSnapshot | None = None
    for s in rows:
        items.append({"id": s.id, "label": s.label, "snapshot_hash": s.snapshot_hash, "created_at": _iso(s.created_at),
                      "created_by": s.created_by, "previous_id": prev.id if prev else None,
                      "diff": redact(_diff(prev.refs or {}, s.refs or {})) if prev else [],
                      "ref_keys": sorted((s.refs or {}).keys())})
        prev = s
    items.reverse()
    return {"items": items[:limit], "source": "architecture_snapshots (immutable refs/hashes; before -> after diff)",
            "checked_at": utcnow().isoformat(), "mode": "LIVE",
            "evidence_limit": "Snapshot rancangan/konfigurasi yang disetujui, bukan observasi runtime."}


def publication_history(session: Session, principal: Principal, domain: Domain, *, limit: int = 100) -> dict[str, Any]:
    pids = scoped_project_ids(session, principal, domain)
    rows = list(session.scalars(select(Publication).where(Publication.domain == domain.value,
                                                          Publication.project_id.in_(pids))
                                .order_by(Publication.created_at.desc()).limit(min(limit, 300)))) if pids else []
    op_counts: dict[str, dict[str, int]] = {}
    if rows:
        for pub_id, status, n in session.execute(
                select(PublicationOperation.publication_id, PublicationOperation.status, func.count())
                .where(PublicationOperation.publication_id.in_([r.id for r in rows]))
                .group_by(PublicationOperation.publication_id, PublicationOperation.status)):
            op_counts.setdefault(pub_id, {})[status] = int(n)
    return {
        "items": [{"id": p.id, "project_id": p.project_id, "status": p.status, "plan_version_id": p.plan_version_id,
                   "approved_hash": p.approved_hash, "decision_id": p.decision_id, "targets": len(p.targets or []),
                   "operations": op_counts.get(p.id, {}), "mode": p.mode, "created_at": _iso(p.created_at),
                   "updated_at": _iso(p.updated_at)} for p in rows],
        "source": "publications + publication_operations (outbox)",
        "checked_at": utcnow().isoformat(),
        "mode": "LIVE",
        "evidence_limit": "PARTIAL/CONFLICT tetap terlihat; sukses sebagian tidak disebut PUBLISHED.",
    }
