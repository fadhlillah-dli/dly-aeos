"""Publication jobs: preview → human Approve & Publish → outbox execution → reconciliation (§14, §26-§30, §41).

Lifecycle

1. ``create_preview`` — only from the PlanVersion bound by an ACTIVE approval decision
   (``planning.facade.get_approved_version``; never "latest"). Renders the Product 1 set, resolves the
   domain's targets and stores the full list of concrete operations (outbox rows) with targets and an
   idempotency key ``hash(project, provider, logical_id, operation, approved_hash)``. Unchanged
   documents/existing folders/issues are ``SKIPPED``; documents with an open human edit are planned
   as ``CONFLICT`` (a stale snapshot never overwrites newer human content).
2. ``execute_publication`` — the human confirms the exact plan version, approved hash and preview hash.
   That records ONE ``PUBLICATION`` decision covering the concrete operations of this run (U-10);
   ``policy.facade.check_external_write`` must allow EVERY operation (bootstrap, AUTH, domain,
   provider, target, write enablement, approved version/hash, unexpired) or nothing runs.
3. ``process_publication`` — runs ops in dependency order (folder before documents, labels before
   issues). Per-op states PENDING/IN_FLIGHT/SUCCEEDED/FAILED/UNKNOWN/CONFLICT/SKIPPED. A timeout or
   unknown result becomes UNKNOWN and is reconciled (find by marker) BEFORE any retry; retries are
   bounded with exponential backoff. Partial failure keeps every success (PARTIAL), and the job always
   uses the snapshot captured at preview time, even when a newer plan version appears (§50).
   The interlock is re-evaluated (non-interactively) before each write so a revoked/expired approval
   stops the run. This function commits after each operation (outbox durability) — documented
   exception to "services never commit".

There is no global transaction across Drive/Docs/GitHub/Confluence/Jira; the outbox + reconciliation
provides at-least-once *attempts* with duplicate prevention, not exactly-once delivery (§14, §41).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import Settings
from aeos.core.enums import PlanningState as PS
from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.docs_engine import renderer as rnd
from aeos.docs_engine import service as docs_service
from aeos.docs_engine.docx import markdown_to_docx
from aeos.docs_engine.plan_content import as_text, build_view
from aeos.events import service as events
from aeos.models.decisions import ArchitectureSnapshot
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.publishing import (
    DocumentRecord,
    DocumentRevision,
    ExternalReference,
    OperationalWorkProjection,
    Publication,
    PublicationOperation,
)
from aeos.models.workforce import TaskExecution
from aeos.planning import facade as planning
from aeos.policy import facade as policy
from aeos.publisher import plan_state
from aeos.publisher.providers import FAMILY_OF, REF_PROVIDER, build_provider
from aeos.publisher.providers.atlassian import attachment_filename
from aeos.publisher.providers.base import (
    OpContext,
    OpResult,
    Provider,
    ProviderBlocked,
    ProviderConflict,
    ProviderError,
    ProviderTimeout,
)
from aeos.publisher.providers.github_issues import marker, project_marker
from aeos.publisher.targets import Target, resolve_targets

log = logging.getLogger("aeos.publisher")

# Publication statuses
PREVIEW, RUNNING, PUBLISHED, PARTIAL, FAILED, CONFLICT, CANCELLED = (
    "PREVIEW", "RUNNING", "PUBLISHED", "PARTIAL", "FAILED", "CONFLICT", "CANCELLED")
# Operation statuses
OP_PENDING, OP_IN_FLIGHT, OP_SUCCEEDED, OP_FAILED, OP_UNKNOWN, OP_CONFLICT, OP_SKIPPED = (
    "PENDING", "IN_FLIGHT", "SUCCEEDED", "FAILED", "UNKNOWN", "CONFLICT", "SKIPPED")
DONE_OPS = frozenset({OP_SUCCEEDED, OP_SKIPPED})

DEFAULT_MAX_ATTEMPTS = 3
RETRY_BUDGET = 3
BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 900
MAX_RECONCILE_FAILURES = 5
DECISION_TTL = timedelta(hours=24)
SERVICE_ACTOR = "publisher"

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock(pub_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(pub_id, threading.Lock())


def idempotency_key(project_id: str, provider: str, logical_id: str, operation: str, approved_hash: str) -> str:
    """Stable outbox key: the same side effect for the same approved hash always has the same key."""
    return content_hash({"project": project_id, "provider": provider, "logical_id": logical_id,
                         "operation": operation, "approved_hash": approved_hash})


def backoff_seconds(attempts: int) -> int:
    return int(min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempts - 1))))


# ---------------------------------------------------------------------------------------------
# Approval lookups (fail closed)


def approved_version(session: Session, plan: Plan) -> PlanVersion:
    try:
        version = planning.get_approved_version(session, plan.id)
    except (ImportError, ModuleNotFoundError) as exc:
        raise Blocked("planning service unavailable — cannot verify the approved version (fail closed)",
                      code="PLANNING_UNAVAILABLE") from exc
    if version is None:
        raise Conflict("plan has no APPROVED version; publication requires an approved PlanVersion",
                       code="PLAN_NOT_APPROVED")
    return version


def approval_decision(session: Session, version: PlanVersion):
    """The ACTIVE plan APPROVAL decision bound to this exact version hash."""
    for target_type, target_id in (("plan_version", version.id), ("plan", version.plan_id)):
        try:
            d = policy.find_active_decision(session, kind="APPROVAL", target_type=target_type, target_id=target_id,
                                            target_hash=version.manifest_hash)
        except (ImportError, ModuleNotFoundError) as exc:
            raise Blocked("policy/decision registry unavailable (fail closed)", code="POLICY_UNAVAILABLE") from exc
        if d is not None:
            return d
    raise Blocked("no ACTIVE human approval decision is bound to this plan version hash",
                  code="APPROVAL_DECISION_MISSING")


# ---------------------------------------------------------------------------------------------
# Planning operations


@dataclass
class PlannedOp:
    provider: str
    operation: str
    logical_id: str
    title: str
    kind: str
    target: dict[str, Any]
    payload: dict[str, Any]
    parent_ref: tuple[str, str] | None = None  # (ref provider, parent logical id)
    dep_refs: list[tuple[str, str]] = field(default_factory=list)  # (ref provider, logical id) of prerequisites
    status: str = OP_PENDING
    reason: str = ""


def _ref(session: Session, project_id: str, provider: str, logical_id: str) -> ExternalReference | None:
    return session.scalar(select(ExternalReference).where(
        ExternalReference.project_id == project_id, ExternalReference.provider == provider,
        ExternalReference.logical_id == logical_id))


def _human_edit_block(session: Session, rec: DocumentRecord | None, version: PlanVersion) -> str:
    if rec is None:
        return ""
    for rev in session.scalars(select(DocumentRevision).where(
            DocumentRevision.document_id == rec.id, DocumentRevision.kind == "HUMAN_EDIT_INPUT")):
        if rev.status == "OPEN":
            return (f"human edit {rev.observed_revision or ''} belum ditinjau (revision input {rev.id}); "
                    "tidak overwrite")
        detected = ensure_aware(rev.created_at)
        created = ensure_aware(version.created_at)
        if detected and created and created < detected:
            return ("snapshot PlanVersion lebih lama dari human edit yang tercatat; snapshot lama tidak menimpa "
                    "konten baru (§29) — perlu PlanVersion baru")
    return ""


def _doc_ops(session: Session, project: Project, version: PlanVersion, result: rnd.RenderResult,
             target: Target) -> list[PlannedOp]:
    family = target.family
    ref_provider = "google-drive" if family == "google" else "confluence"
    create_provider = "google-drive" if family == "google" else "confluence"
    update_provider = "google-docs" if family == "google" else "confluence"
    records = {r.logical_doc_id: r for r in session.scalars(
        select(DocumentRecord).where(DocumentRecord.project_id == project.id))}
    visuals = {v.key: v for v in result.visuals}
    ops: list[PlannedOp] = []
    for doc in result.documents:
        parent = (ref_provider, doc.parent_logical_id) if doc.parent_logical_id else None
        base_target = {"family": family, "logical_id": doc.logical_doc_id, "title": doc.title,
                       "parent_logical_id": doc.parent_logical_id, "root_parent_id": target.config.get(
                           "root_folder_id") if family == "google" else None,
                       "doc_type": doc.doc_type}
        ref = _ref(session, project.id, ref_provider, doc.logical_doc_id)
        title = doc.title if doc.logical_doc_id != "root" else project.name
        if doc.is_folder:
            op = PlannedOp(create_provider, "create_folder", doc.logical_doc_id, title, "folder", base_target,
                           {"title": title}, parent_ref=parent)
            if ref is not None:
                op.status, op.reason = OP_SKIPPED, f"folder sudah ada ({ref.external_id})"
            ops.append(op)
            continue
        rec = records.get(doc.logical_doc_id)
        body = docs_service.document_body(session, project.id, doc)
        chash = doc.content_hash or (rec.content_hash if rec else None) or content_hash({"markdown": body})
        used = sorted(k for k in visuals if f"(visual:{k})" in body)
        rendered = [k for k in used if visuals[k].status == "RENDERED" and visuals[k].png_path]
        payload = {"markdown": body, "content_hash": chash, "doc_type": doc.doc_type, "status": doc.status,
                   "rendered_visuals": rendered, "visual_paths": {k: visuals[k].png_path for k in rendered},
                   "visual_titles": {k: visuals[k].title for k in used}}
        if ref is None:
            op = PlannedOp(create_provider, "create_doc", doc.logical_doc_id, title, "doc", base_target, payload,
                           parent_ref=parent)
        elif ref.content_hash == chash:
            op = PlannedOp(update_provider, "update_doc", doc.logical_doc_id, title, "doc", base_target,
                           {"content_hash": chash}, status=OP_SKIPPED, reason="konten tidak berubah (hash sama)")
        else:
            op = PlannedOp(update_provider, "update_doc", doc.logical_doc_id, title, "doc",
                           {**base_target, "external_id": ref.external_id, "baseline_revision": ref.external_revision},
                           payload)
            why = _human_edit_block(session, rec, version)
            if not why and ref.sync_status == "CONFLICT":
                why = "konflik revisi sebelumnya belum diselesaikan"
            if why:
                op.status, op.reason = OP_CONFLICT, why
        ops.append(op)
        if family == "confluence" and op.status == OP_PENDING:
            for k in rendered:
                ops.append(PlannedOp("confluence", "upload_attachment", f"{doc.logical_doc_id}#{k}",
                                     attachment_filename(k), "attachment",
                                     {"family": family, "page_logical_id": doc.logical_doc_id},
                                     {"filename": attachment_filename(k), "path": visuals[k].png_path},
                                     parent_ref=(ref_provider, doc.logical_doc_id)))
    return ops


def _issue_body(project: Project, version: PlanVersion, logical_id: str, *, summary: str, acs: list[str],
                links: list[str]) -> str:
    lines = [summary.strip() or "_(ringkasan tidak tersedia di rencana)_", "", "### Acceptance criteria"]
    lines += [f"- [ ] {a}" for a in acs] or ["- _(belum ada AC di rencana)_"]
    lines += ["", "### Tautan", f"- AEOS project: `{project.project_key}` — PlanVersion v{version.version} "
                                f"(`{version.manifest_hash[:19]}`)"]
    lines += [f"- {x}" for x in links]
    lines += ["", "> Status issue ini adalah proyeksi operasional. Closed/Done tidak membuka dependency runtime "
                  "tanpa bukti penerimaan AEOS (verifier independen).", "", marker(logical_id),
              project_marker(project.id)]
    return "\n".join(lines) + "\n"


def _label(name: str) -> str:
    return name[:50]


def _issue_ops(session: Session, project: Project, version: PlanVersion, target: Target,
               options: dict[str, Any]) -> tuple[list[PlannedOp], list[str]]:
    family = target.family
    notes: list[str] = []
    view = build_view(version.content)
    tasks = sorted(planning.tasks_for_version(session, version.id), key=lambda t: t.task_key)
    granularity = options.get("issue_granularity") or target.config.get("issue_granularity") or "task"
    if granularity not in ("task", "feature"):
        raise ValidationFailed("issue_granularity must be 'task' or 'feature'")
    if granularity == "task" and not tasks:
        notes.append("Belum ada TaskSpec pada versi ini; satu issue per fitur dibuat sebagai gantinya.")
        granularity = "feature"
    ops: list[PlannedOp] = []
    features = {f.key: f for f in view.features}
    label_refs: list[tuple[str, str]] = []

    def ensure_label(name: str, desc: str) -> tuple[str, str]:
        lid = f"label:{_label(name)}"
        if family == "github-issues" and all(o.logical_id != lid for o in ops):
            ref = _ref(session, project.id, "github-issues", lid)
            op = PlannedOp("github-issues", "ensure_label", lid, _label(name), "label", {"family": family},
                           {"name": _label(name), "description": desc, "color": "2f5bd3" if name == "aeos" else "5b6270"})
            if ref is not None:
                op.status, op.reason = OP_SKIPPED, "label sudah ada"
            ops.append(op)
        return ("github-issues", lid)

    if family == "github-issues":
        label_refs.append(ensure_label("aeos", "Dikelola oleh DLY AEOS (proyeksi operasional)"))
    with_milestones = bool(options.get("milestone_labels", target.config.get("milestone_labels", False)))
    ref_provider = "github-issues" if family == "github-issues" else "jira"
    provider = ref_provider

    def issue(lid: str, title: str, body: str, labels: list[str], deps: list[tuple[str, str]],
              meta: dict[str, Any]) -> None:
        ref = _ref(session, project.id, ref_provider, lid)
        op = PlannedOp(provider, "create_issue", lid, title, "issue", {"family": family, **meta},
                       {"title": title, "body": body, "labels": labels, **meta}, dep_refs=deps)
        if ref is not None:
            op.status, op.reason = OP_SKIPPED, f"issue sudah ada ({ref.external_id}); body tidak disinkronkan di V1"
        ops.append(op)

    if granularity == "task":
        for t in tasks:
            lid = f"issue:task:{t.task_key}"
            labels, deps = ["aeos"], list(label_refs)
            if t.feature_key:
                labels.append(_label(f"feature:{t.feature_key}"))
                deps.append(ensure_label(f"feature:{t.feature_key}", f"Fitur {t.feature_key}"))
                f = features.get(t.feature_key)
                if with_milestones and f and f.milestone:
                    labels.append(_label(f"milestone:{f.milestone}"))
                    deps.append(ensure_label(f"milestone:{f.milestone}", f"Milestone {f.milestone}"))
            links = [f"TaskSpec: `{t.task_key}` (`{t.id}`)"]
            if t.feature_key:
                links += [f"PRD: `02-requirements/prd-{t.feature_key}`", f"Test Plan: `06-testing/test-plan-{t.feature_key}`"]
            acs = [as_text(a) for a in (t.acceptance_criteria or [])]
            body = _issue_body(project, version, lid, summary=t.goal or t.title, acs=acs, links=links)
            issue(lid, f"[{project.project_key}] {t.title}"[:250], body, labels, deps,
                  {"task_id": t.id, "task_key": t.task_key, "feature_key": t.feature_key})
    else:
        for f in view.features:
            lid = f"issue:feature:{f.key}"
            labels = ["aeos", _label(f"feature:{f.key}")]
            deps = [*label_refs, ensure_label(f"feature:{f.key}", f"Fitur {f.title}"[:100])]
            if with_milestones and f.milestone:
                labels.append(_label(f"milestone:{f.milestone}"))
                deps.append(ensure_label(f"milestone:{f.milestone}", f"Milestone {f.milestone}"))
            ft = [t for t in tasks if t.feature_key == f.key]
            links = [f"PRD: `02-requirements/prd-{f.key}`", f"Test Plan: `06-testing/test-plan-{f.key}`"]
            links += [f"TaskSpec: `{t.task_key}`" for t in ft]
            body = _issue_body(project, version, lid, summary=f.summary or f.title,
                               acs=[as_text(a) for a in f.acceptance], links=links)
            issue(lid, f"[{project.project_key}] {f.title}"[:250], body, labels, deps,
                  {"task_id": None, "task_key": None, "feature_key": f.key})
    if family == "github-issues":
        notes.append("GitHub Projects (roadmap) opsional dan tidak diimplementasikan oleh adapter ini.")
    return ops, notes


# ---------------------------------------------------------------------------------------------
# Preview


def _ops_for(session: Session, pub_id: str) -> list[PublicationOperation]:
    return list(session.scalars(select(PublicationOperation).where(PublicationOperation.publication_id == pub_id)
                                .order_by(PublicationOperation.order_index, PublicationOperation.created_at)))


def create_preview(session: Session, settings: Settings, *, plan_id: str, actor_user_id: str,
                   options: dict[str, Any] | None = None, render_visuals: bool = True) -> Publication:
    options = dict(options or {})
    plan = planning.get_plan(session, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    project = session.get(Project, plan.project_id)
    if project is None:
        raise NotFound("project not found")
    version = approved_version(session, plan)
    if version.plan_id != plan.id or version.project_id != project.id:
        raise Conflict("approved version does not belong to this plan/project", code="APPROVAL_MISMATCH")
    if version.domain != project.domain or plan.domain != project.domain:
        raise Conflict("plan/version domain differs from the project's human-chosen domain", code="DOMAIN_MISMATCH")
    decision = approval_decision(session, version)

    result, delta, state = docs_service.render_version(session, settings, version, persist=True,
                                                       render_visuals=render_visuals, decision_id=decision.id)
    if not state.approved:  # defensive: approval changed between lookups
        raise Conflict(state.reason or "version not approved", code="PLAN_NOT_APPROVED")
    targets = resolve_targets(session, project, options.get("targets"))
    planned: list[PlannedOp] = []
    notes: list[str] = []
    for t in targets:
        if t.status != "READY":
            continue
        if t.purpose == "docs":
            planned += _doc_ops(session, project, version, result, t)
        else:
            iops, inotes = _issue_ops(session, project, version, t, options)
            planned += iops
            notes += inotes

    pub = Publication(project_id=project.id, domain=project.domain, plan_id=plan.id, plan_version_id=version.id,
                      plan_version_number=version.version, approved_hash=version.manifest_hash, decision_id=decision.id,
                      targets=[t.to_dict() for t in targets], status=PREVIEW, options=redact(options),
                      created_by=actor_user_id, mode="LIVE")
    session.add(pub)
    session.flush()

    # Supersede earlier PREVIEW publications of the same version (never executed → no side effects).
    for old in session.scalars(select(Publication).where(Publication.plan_version_id == version.id,
                                                         Publication.status == PREVIEW, Publication.id != pub.id)):
        old.status = CANCELLED
        old.status_reason = f"superseded by preview {pub.id}"

    by_ref: dict[tuple[str, str], PublicationOperation] = {}
    planned_by_op: dict[str, PlannedOp] = {}
    already_done: list[dict[str, Any]] = []
    order = 0
    for p in planned:
        key = idempotency_key(project.id, p.provider, p.logical_id, p.operation, version.manifest_hash)
        payload_hash = content_hash(p.payload)
        existing = session.scalar(select(PublicationOperation).where(PublicationOperation.idempotency_key == key))
        if existing is not None:
            prev = session.get(Publication, existing.publication_id)
            if existing.status in DONE_OPS and prev is not None and prev.status != CANCELLED:
                already_done.append({"logical_id": p.logical_id, "operation": p.operation, "provider": p.provider,
                                     "status": existing.status, "publication_id": existing.publication_id,
                                     "external_id": existing.external_id})
                by_ref[(REF_PROVIDER[p.provider], p.logical_id)] = existing
                continue
            if prev is not None and prev.status == RUNNING and prev.id != pub.id:
                raise Conflict(f"publication {prev.id} for this approved version is still running; reconcile or "
                               "retry it instead of creating a new preview", code="PUBLICATION_RUNNING")
            # Adopt the outbox row (one row per side effect, forever): history is kept in the response.
            hist = list((existing.response or {}).get("history", []))
            hist.append({"publication_id": existing.publication_id, "status": existing.status,
                         "attempts": existing.attempts, "at": utcnow().isoformat()})
            existing.response = {**(existing.response or {}), "history": hist}
            existing.publication_id = pub.id
            if existing.status in (OP_UNKNOWN, OP_IN_FLIGHT):
                existing.status = OP_UNKNOWN  # reconcile before any retry
            elif p.status in (OP_SKIPPED, OP_CONFLICT):
                existing.status = p.status
            else:
                existing.status = OP_PENDING
                existing.max_attempts = existing.attempts + DEFAULT_MAX_ATTEMPTS
            existing.target, existing.payload, existing.payload_hash = p.target, p.payload, payload_hash
            existing.error = p.reason
            existing.next_attempt_at = None
            op = existing
        else:
            op = PublicationOperation(publication_id=pub.id, provider=p.provider, operation=p.operation,
                                      logical_id=p.logical_id, target=p.target, idempotency_key=key,
                                      payload_hash=payload_hash, payload=p.payload, status=p.status, error=p.reason,
                                      max_attempts=DEFAULT_MAX_ATTEMPTS, title=p.title[:256])
            session.add(op)
        order += 1
        op.order_index = order
        session.flush()
        by_ref[(REF_PROVIDER[p.provider], p.logical_id)] = op
        planned_by_op[op.id] = p

    # Resolve dependencies (parent folders, labels) to op ids within this publication.
    for op in _ops_for(session, pub.id):
        p = planned_by_op.get(op.id)
        if p is None:
            continue
        deps: list[str] = []
        for ref in ([p.parent_ref] if p.parent_ref else []) + list(p.dep_refs):
            dep = by_ref.get(ref)
            if dep is not None and dep.publication_id == pub.id and dep.id != op.id:
                deps.append(dep.id)
        op.depends_on = deps

    ops = _ops_for(session, pub.id)
    blocked = [t.to_dict() for t in targets if t.status != "READY"]
    executable = [o for o in ops if o.status not in (OP_SKIPPED, OP_CONFLICT)]
    counts: dict[str, int] = {}
    for o in ops:
        counts[f"{o.operation}:{o.status}"] = counts.get(f"{o.operation}:{o.status}", 0) + 1
    pub.preview = {
        "operations_total": len(ops),
        "operations_executable": len(executable),
        "counts": counts,
        "targets": [t.to_dict() for t in targets],
        "blocked_targets": blocked,
        "already_done": already_done,
        "documents_delta": delta.to_dict() if delta else {},
        "documents_total": sum(1 for d in result.documents if not d.is_folder),
        "folders_total": sum(1 for d in result.documents if d.is_folder),
        "visuals": [{"key": v.key, "title": v.title, "status": v.status, "reason": v.reason} for v in result.visuals],
        "notes": [*notes, "Dokumen SUPERSEDED tidak dihapus di provider eksternal."],
        "product_title": result.product_title,
    }
    pub.preview_hash = content_hash({"plan_version_id": version.id, "approved_hash": version.manifest_hash,
                                     "ops": [(o.idempotency_key, o.status, o.payload_hash) for o in ops],
                                     "blocked": [b["family"] for b in blocked]})
    pub.preview["approval_statement"] = approval_statement(pub, len(executable))
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="publication.preview",
                 target_type="publication", target_id=pub.id, project_id=project.id, domain=project.domain,
                 details={"plan_version_id": version.id, "operations": len(ops), "blocked_targets": len(blocked)})
    events.emit(session, source="publisher", kind="publication.previewed", entity_type="publication",
                entity_id=pub.id, entity_version=pub.preview_hash, project_id=project.id, domain=project.domain,
                payload={"plan_version_id": version.id, "operations": len(ops), "status": PREVIEW})
    session.flush()
    return pub


def approval_statement(pub: Publication, executable: int) -> str:
    return (f"Satu persetujuan mencakup {executable} operasi konkret pada publication ini (U-10) untuk PlanVersion "
            f"v{pub.plan_version_number} ({pub.plan_version_id}) dengan approved hash {pub.approved_hash} dan preview "
            f"hash {pub.preview_hash}. Tidak ada permintaan ulang per panggilan API; versi/target lain memerlukan "
            "preview dan persetujuan baru.")


# ---------------------------------------------------------------------------------------------
# Execute (human Approve & Publish)


@dataclass
class ExecuteOutcome:
    allowed: bool
    reasons: list[str]
    publication: Publication


def _intent(pub: Publication, op: PublicationOperation, decision_expires: datetime | None) -> policy.WriteIntent:
    return policy.WriteIntent(
        actor_user_id=pub.executed_by or "", project_id=pub.project_id, domain=pub.domain, provider=op.provider,
        operation=op.operation, target=dict(op.target or {}), approved_plan_version_id=pub.plan_version_id,
        approved_hash=pub.approved_hash, decision_id=pub.publication_decision_id,
        capability_expires_at=decision_expires,
        extra={"publication_id": pub.id, "operation_id": op.id, "idempotency_key": op.idempotency_key,
               "preview_hash": pub.preview_hash, "logical_id": op.logical_id},
    )


def _check_all(session: Session, pub: Publication, ops: list[PublicationOperation],
               decision_expires: datetime | None) -> list[str]:
    reasons: list[str] = []
    for op in ops:
        try:
            res = policy.check_external_write(session, _intent(pub, op, decision_expires))
        except (ImportError, ModuleNotFoundError):
            return ["external-write interlock unavailable (policy module missing) — fail closed"]
        if not res.allowed:
            why = "; ".join(res.reasons) or "denied"
            reasons.append(f"{op.provider}/{op.operation} {op.logical_id}: {why}")
    return reasons


def _record_publication_decision(session: Session, pub: Publication, actor_user_id: str,
                                 ops: list[PublicationOperation], rationale: str):
    try:
        return policy.record_decision(
            session, kind="PUBLICATION", actor_user_id=actor_user_id, target_type="publication", target_id=pub.id,
            target_version=pub.plan_version_id, target_hash=pub.approved_hash,
            allowed_operations=sorted({o.operation for o in ops}),
            auth_scope={"publication_id": pub.id, "preview_hash": pub.preview_hash, "project_id": pub.project_id,
                        "domain": pub.domain, "providers": sorted({o.provider for o in ops}),
                        "operation_ids": [o.id for o in ops],
                        "targets": [{"family": t.get("family"), "purpose": t.get("purpose")} for t in pub.targets]},
            project_id=pub.project_id, domain=pub.domain, rationale=rationale,
            expires_at=utcnow() + DECISION_TTL,
            details={"plan_version_number": pub.plan_version_number, "operation_count": len(ops)},
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise Blocked("decision registry unavailable — publication approval cannot be recorded (fail closed)",
                      code="POLICY_UNAVAILABLE") from exc


def _verify_confirmation(pub: Publication, confirm: dict[str, Any]) -> None:
    mismatched = [k for k, v in (("plan_version_id", pub.plan_version_id), ("approved_hash", pub.approved_hash),
                                 ("preview_hash", pub.preview_hash)) if confirm.get(k) != v]
    if mismatched:
        raise Conflict("confirmation does not match the publication preview (stale view?)", code="STALE_PREVIEW",
                       details={"mismatched": mismatched})


def _still_approved(session: Session, pub: Publication) -> None:
    plan = session.get(Plan, pub.plan_id) if pub.plan_id else None
    if plan is None:
        raise NotFound("plan not found")
    current = approved_version(session, plan)
    if current.id != pub.plan_version_id or current.manifest_hash != pub.approved_hash:
        raise Conflict("the approved plan version changed after this preview; create a new preview",
                       code="APPROVAL_STALE", details={"approved_version_id": current.id})


def execute_publication(session: Session, pub_id: str, *, actor_user_id: str, confirm: dict[str, Any]) -> ExecuteOutcome:
    pub = session.get(Publication, pub_id)
    if pub is None:
        raise NotFound("publication not found")
    if pub.status != PREVIEW:
        raise Conflict(f"publication is {pub.status}; only a PREVIEW can be executed (use retry/reconcile)",
                       code="NOT_A_PREVIEW")
    _verify_confirmation(pub, confirm)
    blocked = (pub.preview or {}).get("blocked_targets") or []
    if blocked:
        raise Blocked("some requested targets are not ready; fix them or preview without them",
                      code="TARGET_NOT_READY", details={"reasons": [r for b in blocked for r in b.get("reasons", [])],
                                                        "remedies": [r for b in blocked for r in b.get("remedies", [])]})
    _still_approved(session, pub)
    ops = [o for o in _ops_for(session, pub.id) if o.status not in (OP_SKIPPED, OP_CONFLICT, OP_SUCCEEDED)]
    decision = _record_publication_decision(session, pub, actor_user_id, ops,
                                            rationale=f"Approve & Publish PlanVersion v{pub.plan_version_number}")
    pub.publication_decision_id = decision.id
    pub.executed_by = actor_user_id
    reasons = _check_all(session, pub, ops, decision.expires_at)
    if reasons:
        try:
            policy.invalidate_decisions(session, target_type="publication", target_id=pub.id,
                                        reason="external-write interlock denied")
        except (ImportError, ModuleNotFoundError):
            pass
        pub.publication_decision_id = None
        pub.status_reason = "interlock denied: " + "; ".join(reasons)[:2000]
        audit.record(session, actor_type="user", actor_id=actor_user_id, action="publication.execute",
                     result="DENIED", target_type="publication", target_id=pub.id, project_id=pub.project_id,
                     domain=pub.domain, reason=pub.status_reason)
        session.flush()
        return ExecuteOutcome(False, reasons, pub)
    now = utcnow()
    pub.status = RUNNING if ops else PUBLISHED
    pub.started_at = now
    pub.status_reason = "" if ops else "tidak ada perubahan untuk dipublikasikan"
    if not ops:
        pub.finished_at = now
    applied, note = plan_state.set_plan_state(session, pub.plan_id, PS.PUBLISHING if ops else PS.PUBLISHED,
                                              reason=f"publication {pub.id}")
    pub.summary = {**(pub.summary or {}), "plan_state": {"applied": applied, "note": note}}
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="publication.execute",
                 target_type="publication", target_id=pub.id, project_id=pub.project_id, domain=pub.domain,
                 details={"decision_id": decision.id, "operations": len(ops), "approved_hash": pub.approved_hash})
    events.emit(session, source="publisher", kind="publication.started", entity_type="publication", entity_id=pub.id,
                entity_version=pub.preview_hash, project_id=pub.project_id, domain=pub.domain,
                payload={"status": pub.status, "operations": len(ops)})
    session.flush()
    return ExecuteOutcome(True, [], pub)


# ---------------------------------------------------------------------------------------------
# Processing


@dataclass
class _Run:
    session: Session
    pub: Publication
    project: Project
    now: datetime
    providers: dict[str, Provider] = field(default_factory=dict)
    provider_errors: dict[str, ProviderBlocked] = field(default_factory=dict)
    decision_expires: datetime | None = None
    denied: list[str] = field(default_factory=list)
    denied_ids: set[str] = field(default_factory=set)

    def provider(self, op: PublicationOperation) -> Provider:
        family = FAMILY_OF[op.provider]
        if family in self.provider_errors:
            raise self.provider_errors[family]
        if family not in self.providers:
            cfg = next((t.get("config", {}) for t in self.pub.targets if t.get("family") == family), {})
            try:
                self.providers[family] = build_provider(family, cfg)
            except ProviderBlocked as exc:
                self.provider_errors[family] = exc
                raise
            except Blocked as exc:  # secret store unavailable / secret missing
                err = ProviderBlocked(exc.message, remedy="lihat Integrations / secrets", code=exc.code)
                self.provider_errors[family] = err
                raise err from exc
        return self.providers[family]

    def close(self) -> None:
        for p in self.providers.values():
            try:
                p.close()
            except Exception:  # pragma: no cover
                log.exception("provider close failed")


def _context(run: _Run, op: PublicationOperation) -> OpContext:
    s, pub = run.session, run.pub
    tgt = dict(op.target or {})
    ref_provider = REF_PROVIDER[op.provider]
    parent_external = None
    parent_lid = tgt.get("parent_logical_id") or tgt.get("page_logical_id")
    if parent_lid:
        parent = _ref(s, pub.project_id, ref_provider, parent_lid)
        if parent is None:
            # Never fall back to another location (e.g. the Drive root): wait for the real parent.
            raise ProviderBlocked(f"parent {parent_lid} is not published yet", code="PARENT_NOT_PUBLISHED",
                                  remedy="publikasikan/rekonsiliasi folder induk lalu retry")
        parent_external = parent.external_id
    elif op.operation == "create_folder" and tgt.get("root_parent_id") not in (None, "", "root"):
        parent_external = tgt["root_parent_id"]
    existing = _ref(s, pub.project_id, ref_provider, op.logical_id)
    attachments: dict[str, bytes] = {}
    payload = dict(op.payload or {})
    if op.operation == "create_doc" and op.provider == "google-drive":
        paths = payload.get("visual_paths") or {}

        def resolve(key: str) -> Path | None:
            p = paths.get(key)
            return Path(p) if p else None

        attachments["document.docx"] = markdown_to_docx(payload.get("markdown", ""), title=op.title,
                                                        logical_id=op.logical_id, image_resolver=resolve)
    if op.operation == "upload_attachment":
        path = Path(payload.get("path") or "")
        if path.is_file():
            attachments[payload["filename"]] = path.read_bytes()
    return OpContext(
        operation_id=op.id, publication_id=pub.id, project_id=pub.project_id, project_key=run.project.project_key,
        project_name=run.project.name, domain=pub.domain, provider=op.provider, operation=op.operation,
        logical_id=op.logical_id, idempotency_key=op.idempotency_key, title=op.title or tgt.get("title", ""),
        target=tgt, payload=payload, parent_external_id=parent_external,
        existing_external_id=existing.external_id if existing else tgt.get("external_id"),
        existing_revision=existing.external_revision if existing else tgt.get("baseline_revision"),
        attachments=attachments,
    )


def _deps_state(op: PublicationOperation, by_id: dict[str, PublicationOperation]) -> tuple[bool, str]:
    """(ready, blocking_reason). blocking_reason non-empty means the dependency failed permanently."""
    waiting = False
    for dep_id in op.depends_on or []:
        dep = by_id.get(dep_id)
        if dep is None or dep.status in DONE_OPS:
            continue
        if dep.status in (OP_FAILED, OP_CONFLICT):
            return False, f"dependency {dep.operation} {dep.logical_id} is {dep.status}"
        waiting = True
    return not waiting, ""


def _record_success(run: _Run, op: PublicationOperation, res: OpResult) -> None:
    s, pub, now = run.session, run.pub, run.now
    op.status = OP_SUCCEEDED
    op.external_id, op.external_url = res.external_id, res.external_url
    op.external_revision = res.external_revision
    op.response = {**(op.response or {}), **redact(res.response or {})}
    op.error = ""
    op.next_attempt_at = None
    ref_provider = REF_PROVIDER[op.provider]
    ref = _ref(s, pub.project_id, ref_provider, op.logical_id)
    if ref is None:
        ref = ExternalReference(project_id=pub.project_id, domain=pub.domain, provider=ref_provider,
                                logical_id=op.logical_id, external_id=res.external_id)
        s.add(ref)
    ref.external_id = res.external_id
    ref.external_url = res.external_url or ref.external_url
    ref.external_revision = res.external_revision
    ref.source_plan_version_id = pub.plan_version_id
    ref.last_synced_at = now
    ref.sync_status = "SYNCED" if not (res.response or {}).get("revision_unknown") else "UNKNOWN"
    ref.kind = res.kind or op.target.get("doc_type", "")
    ref.title = (op.title or "")[:256]
    ref.content_hash = (op.payload or {}).get("content_hash") or ref.content_hash
    ref.last_publication_id = pub.id
    if op.operation in ("create_doc", "update_doc", "create_folder"):
        rec = s.scalar(select(DocumentRecord).where(DocumentRecord.project_id == pub.project_id,
                                                    DocumentRecord.logical_doc_id == op.logical_id))
        if rec is not None:
            if rec.status in (rnd.ST_APPROVED, rnd.ST_DRAFT):
                rec.status = rnd.ST_PUBLISHED
            rec.external_revision = res.external_revision
            rec.last_synced_at = now
            rec.links = {**(rec.links or {}), "external_url": res.external_url, "external_id": res.external_id,
                         "external_provider": ref_provider}
    if op.operation == "create_issue":
        tgt = op.target or {}
        proj = s.scalar(select(OperationalWorkProjection).where(
            OperationalWorkProjection.project_id == pub.project_id, OperationalWorkProjection.provider == op.provider,
            OperationalWorkProjection.issue_id == res.external_id))
        if proj is None:
            proj = OperationalWorkProjection(project_id=pub.project_id, provider=op.provider, issue_id=res.external_id)
            s.add(proj)
        proj.task_id = tgt.get("task_id")
        proj.domain = pub.domain
        proj.logical_id = op.logical_id
        proj.feature_key = tgt.get("feature_key")
        proj.title = (op.payload or {}).get("title", "")[:256]
        proj.issue_url = res.external_url
        proj.external_status = str((res.response or {}).get("state") or "open")
        proj.last_synced_at = now
        proj.sync_status = "SYNCED"


def _record_conflict(run: _Run, op: PublicationOperation, exc: ProviderConflict) -> None:
    s, pub = run.session, run.pub
    op.status = OP_CONFLICT
    op.error = exc.message
    op.next_attempt_at = None
    op.response = {**(op.response or {}), "conflict": {"expected_revision": exc.expected_revision,
                                                       "observed_revision": exc.observed_revision}}
    ref = _ref(s, pub.project_id, REF_PROVIDER[op.provider], op.logical_id)
    if ref is not None:
        ref.sync_status = "CONFLICT"
    rec = s.scalar(select(DocumentRecord).where(DocumentRecord.project_id == pub.project_id,
                                                DocumentRecord.logical_doc_id == op.logical_id))
    if rec is not None:
        rev = DocumentRevision(document_id=rec.id, project_id=rec.project_id, domain=rec.domain,
                               logical_doc_id=rec.logical_doc_id, kind="HUMAN_EDIT_INPUT", status="OPEN",
                               plan_version_id=pub.plan_version_id, body_markdown=exc.current_text,
                               provider=op.provider, external_id=ref.external_id if ref else None,
                               expected_revision=exc.expected_revision, observed_revision=exc.observed_revision,
                               publication_id=pub.id, details={"reason": exc.message})
        s.add(rev)
        s.flush()
        rec.links = {**(rec.links or {}), "conflict": {"revision_input_id": rev.id, "publication_id": pub.id,
                                                       "observed_revision": exc.observed_revision}}
    events.emit(s, source="publisher", kind="document.conflict", entity_type="publication_operation", entity_id=op.id,
                project_id=pub.project_id, domain=pub.domain,
                payload={"logical_id": op.logical_id, "observed_revision": exc.observed_revision})


def _fail(op: PublicationOperation, message: str, *, blocked: ProviderBlocked | None = None) -> None:
    op.status = OP_FAILED
    op.error = redact_text(message)[:4000]
    op.next_attempt_at = None
    if blocked is not None:
        op.response = {**(op.response or {}), "blocked": {"code": blocked.code, "remedy": blocked.remedy}}


def _audit_op(run: _Run, op: PublicationOperation, result: str, reason: str = "") -> None:
    audit.record(run.session, actor_type="service", actor_id=SERVICE_ACTOR, action=f"publication.op.{op.operation}",
                 result=result, target_type="publication_operation", target_id=op.id, project_id=run.pub.project_id,
                 domain=run.pub.domain, reason=reason,
                 details={"publication_id": run.pub.id, "provider": op.provider, "logical_id": op.logical_id,
                          "status": op.status, "external_id": op.external_id, "attempts": op.attempts})
    events.emit(run.session, source="publisher", kind="publication.operation", entity_type="publication_operation",
                entity_id=op.id, project_id=run.pub.project_id, domain=run.pub.domain,
                payload={"publication_id": run.pub.id, "status": op.status, "operation": op.operation,
                         "logical_id": op.logical_id})


def _claim(session: Session, op: PublicationOperation, now: datetime) -> bool:
    res = session.execute(update(PublicationOperation).where(
        PublicationOperation.id == op.id, PublicationOperation.status == op.status,
        PublicationOperation.attempts == op.attempts,
    ).values(status=OP_IN_FLIGHT, attempts=op.attempts + 1, last_attempt_at=now, updated_at=now)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    session.refresh(op)
    return True


def _interlock_ok(run: _Run, op: PublicationOperation) -> bool:
    try:
        res = policy.check_external_write(run.session, _intent(run.pub, op, run.decision_expires))
    except (ImportError, ModuleNotFoundError):
        res = policy.InterlockResult(False, ["external-write interlock unavailable — fail closed"])
    if res.allowed:
        return True
    why = "; ".join(res.reasons) or "denied"
    op.error = f"interlock denied before write: {why}"[:4000]
    run.denied.append(f"{op.logical_id}: {why}")
    run.denied_ids.add(op.logical_id)
    return False


def _reconcile_one(run: _Run, op: PublicationOperation) -> str:
    """Reconcile an op; returns 'found' | 'absent' | 'unknown' | 'conflict'."""
    try:
        provider = run.provider(op)
        found = provider.reconcile(_context(run, op))
    except ProviderConflict as exc:
        _record_conflict(run, op, exc)
        return "conflict"
    except ProviderBlocked as exc:
        op.error = f"reconcile blocked: {exc.message}"
        return "unknown"
    except ProviderError as exc:
        fails = int((op.response or {}).get("reconcile_failures", 0)) + 1
        op.response = {**(op.response or {}), "reconcile_failures": fails}
        op.error = f"reconcile failed ({fails}x): {exc.message}"[:4000]
        op.next_attempt_at = run.now + timedelta(seconds=backoff_seconds(fails))
        return "unknown"
    op.reconciled_at = run.now
    if found is not None:
        _record_success(run, op, found)
        op.response = {**(op.response or {}), "reconciled": True}
        return "found"
    return "absent"


def process_publication(session: Session, pub_id: str, *, now: datetime | None = None,
                        max_ops: int | None = None) -> Publication:
    """Execute due operations of a RUNNING publication (commits per operation)."""
    lock = _lock(pub_id)
    if not lock.acquire(blocking=False):
        pub = session.get(Publication, pub_id)
        if pub is None:
            raise NotFound("publication not found")
        return pub
    try:
        return _process(session, pub_id, now=now, max_ops=max_ops)
    finally:
        lock.release()


def _decision_expiry(session: Session, pub: Publication) -> datetime | None:
    try:
        d = policy.find_active_decision(session, kind="PUBLICATION", target_type="publication", target_id=pub.id,
                                        target_hash=pub.approved_hash)
    except (ImportError, ModuleNotFoundError):
        return None
    if d is None or d.status not in (None, "ACTIVE"):
        return None
    expires = ensure_aware(d.expires_at)
    if expires is None or expires <= utcnow():
        return None  # an approval without expiry is not accepted as an unexpired capability (§26)
    return expires


def _process(session: Session, pub_id: str, *, now: datetime | None, max_ops: int | None) -> Publication:
    pub = session.get(Publication, pub_id)
    if pub is None:
        raise NotFound("publication not found")
    if pub.status != RUNNING:
        return pub
    project = session.get(Project, pub.project_id)
    if project is None:
        raise NotFound("project not found")
    run = _Run(session=session, pub=pub, project=project, now=now or utcnow())
    run.decision_expires = _decision_expiry(session, pub)
    done = 0
    try:
        ops = _ops_for(session, pub.id)
        by_id = {o.id: o for o in ops}
        for op in ops:
            if max_ops is not None and done >= max_ops:
                break
            if op.status in (OP_SUCCEEDED, OP_SKIPPED, OP_CONFLICT, OP_FAILED):
                continue
            nxt = ensure_aware(op.next_attempt_at)
            if nxt is not None and nxt > run.now:
                continue
            ready, blocking = _deps_state(op, by_id)
            if blocking:
                op.error = f"waiting: {blocking}"
                session.commit()
                continue
            if not ready:
                continue
            if op.status == OP_IN_FLIGHT:  # crashed mid-call earlier → outcome unknown
                op.status = OP_UNKNOWN
            if op.status == OP_UNKNOWN:
                outcome = _reconcile_one(run, op)
                session.commit()
                if outcome in ("found", "conflict"):
                    _audit_op(run, op, "SUCCESS" if outcome == "found" else "FAILED", "reconciled")
                    session.commit()
                    continue
                if outcome == "unknown":
                    continue
                op.status = OP_PENDING  # definitively absent → safe to (re)create
                if op.attempts >= op.max_attempts:
                    _fail(op, "not found after reconciliation and attempt budget exhausted")
                session.commit()
                if op.status == OP_FAILED:
                    continue
            if op.attempts >= op.max_attempts:
                _fail(op, op.error or "attempt budget exhausted")
                session.commit()
                continue
            if not _interlock_ok(run, op):
                session.commit()
                continue
            try:
                provider = run.provider(op)
                ctx = _context(run, op)
            except ProviderBlocked as exc:
                _fail(op, f"BLOCKED: {exc.message}", blocked=exc)
                _audit_op(run, op, "FAILED", exc.message)
                session.commit()
                continue
            if not _claim(session, op, run.now):
                continue
            done += 1
            try:
                res = provider.execute(ctx)
            except ProviderConflict as exc:
                _record_conflict(run, op, exc)
                _audit_op(run, op, "FAILED", "revision conflict — not overwritten")
            except ProviderBlocked as exc:
                _fail(op, f"BLOCKED: {exc.message}", blocked=exc)
                _audit_op(run, op, "FAILED", exc.message)
            except ProviderTimeout as exc:
                op.status = OP_UNKNOWN
                op.error = exc.message
                op.next_attempt_at = run.now + timedelta(seconds=backoff_seconds(op.attempts))
                _audit_op(run, op, "FAILED", "outcome unknown — reconcile before retry")
            except ProviderError as exc:
                if exc.retryable and op.attempts < op.max_attempts:
                    op.status = OP_PENDING
                    op.error = exc.message
                    op.next_attempt_at = run.now + timedelta(seconds=backoff_seconds(op.attempts))
                else:
                    _fail(op, exc.message)
                _audit_op(run, op, "FAILED", exc.message)
            except Exception as exc:  # unexpected: the side effect may have happened → UNKNOWN
                log.exception("publication op %s crashed", op.id)
                op.status = OP_UNKNOWN
                op.error = f"unexpected error ({exc.__class__.__name__}) — outcome unknown, reconcile first"
                op.next_attempt_at = run.now + timedelta(seconds=backoff_seconds(op.attempts))
                _audit_op(run, op, "FAILED", op.error)
            else:
                _record_success(run, op, res)
                _audit_op(run, op, "SUCCESS")
            session.commit()
        _finalize(run)
        session.commit()
    finally:
        run.close()
    return pub


def _progressable_ids(ops: list[PublicationOperation], denied: set[str]) -> set[str]:
    """Ops that can still make progress without a human: their own budget is left and every
    in-publication dependency is done or itself progressable (ops are in dependency order)."""
    by_id = {o.id: o for o in ops}
    ok: set[str] = set()
    for op in ops:
        if op.status not in (OP_PENDING, OP_UNKNOWN, OP_IN_FLIGHT) or op.logical_id in denied:
            continue
        if op.status == OP_UNKNOWN:
            if int((op.response or {}).get("reconcile_failures", 0)) >= MAX_RECONCILE_FAILURES:
                continue
        elif op.attempts >= op.max_attempts:
            continue
        deps = [by_id[d] for d in (op.depends_on or []) if d in by_id]
        if all(d.status in DONE_OPS or d.id in ok for d in deps):
            ok.add(op.id)
    return ok


def _finalize(run: _Run) -> None:
    s, pub = run.session, run.pub
    ops = _ops_for(s, pub.id)
    if _progressable_ids(ops, run.denied_ids):
        pub.summary = {**(pub.summary or {}), "counts": _counts(ops)}
        return  # keep RUNNING; the worker comes back when retries/reconciliation are due
    pub.status = final_status(ops)
    pub.finished_at = run.now
    reasons = []
    if run.denied:
        reasons.append("interlock denied: " + "; ".join(run.denied)[:1000])
    if any(o.status == OP_UNKNOWN for o in ops):
        reasons.append("UNKNOWN operations need manual reconciliation")
    if any(o.status == OP_FAILED and (o.response or {}).get("blocked") for o in ops):
        reasons.append("BLOCKED: needs action — " + "; ".join(
            o.error for o in ops if o.status == OP_FAILED and (o.response or {}).get("blocked"))[:1000])
    pub.status_reason = " | ".join(reasons)
    target_state = {PUBLISHED: PS.PUBLISHED, PARTIAL: PS.PUBLICATION_PARTIAL, CONFLICT: PS.PUBLICATION_PARTIAL,
                    FAILED: PS.PUBLICATION_FAILED}[pub.status]
    plan = s.get(Plan, pub.plan_id) if pub.plan_id else None
    if plan is not None and plan.state == PS.PUBLISHING:
        applied, note = plan_state.set_plan_state(s, pub.plan_id, target_state, reason=f"publication {pub.id} "
                                                                                     f"{pub.status}")
    else:
        applied, note = False, (f"plan state is {plan.state if plan else 'missing'}; not overwritten (newer revision "
                                "may be in progress)")
    pub.summary = {**(pub.summary or {}), "counts": _counts(ops), "plan_state": {"applied": applied, "note": note,
                                                                                  "target": target_state}}
    audit.record(s, actor_type="service", actor_id=SERVICE_ACTOR, action="publication.complete",
                 result="SUCCESS" if pub.status == PUBLISHED else "FAILED", target_type="publication",
                 target_id=pub.id, project_id=pub.project_id, domain=pub.domain, reason=pub.status_reason,
                 details={"status": pub.status, "counts": _counts(ops)})
    events.emit(s, source="publisher", kind="publication.completed", entity_type="publication", entity_id=pub.id,
                project_id=pub.project_id, domain=pub.domain, payload={"status": pub.status, "counts": _counts(ops)})


def _counts(ops: list[PublicationOperation]) -> dict[str, int]:
    out: dict[str, int] = {}
    for o in ops:
        out[o.status] = out.get(o.status, 0) + 1
    return out


def final_status(ops: list[PublicationOperation]) -> str:
    """PUBLISHED only when every op succeeded/was skipped; successes are never hidden by failures."""
    real = [o for o in ops if o.status != OP_SKIPPED]
    if all(o.status in DONE_OPS for o in ops):
        return PUBLISHED
    succeeded = [o for o in real if o.status == OP_SUCCEEDED]
    conflicts = [o for o in real if o.status == OP_CONFLICT]
    if succeeded:
        return PARTIAL
    if conflicts and len(conflicts) == len(real):
        return CONFLICT
    if conflicts:
        return PARTIAL
    return FAILED


# ---------------------------------------------------------------------------------------------
# Retry / reconcile (human-triggered)


def retry_publication(session: Session, pub_id: str, *, actor_user_id: str,
                      confirm: dict[str, Any] | None = None) -> ExecuteOutcome:
    pub = session.get(Publication, pub_id)
    if pub is None:
        raise NotFound("publication not found")
    if pub.status not in (PARTIAL, FAILED, CONFLICT, RUNNING):
        raise Conflict(f"publication is {pub.status}; retry applies to RUNNING/PARTIAL/FAILED/CONFLICT",
                       code="NOT_RETRYABLE")
    ops = _ops_for(session, pub.id)
    retry_ops = [o for o in ops if o.status in (OP_FAILED, OP_PENDING, OP_UNKNOWN, OP_IN_FLIGHT)]
    if not retry_ops:
        raise Conflict("nothing to retry (CONFLICT operations need a new approved version / human resolution)",
                       code="NOTHING_TO_RETRY")
    expires = _decision_expiry(session, pub)
    if expires is None:
        # The run's approval is gone or expired: the human re-confirms the exact version/hash/preview.
        _verify_confirmation(pub, confirm or {})
        _still_approved(session, pub)
        decision = _record_publication_decision(session, pub, actor_user_id, retry_ops,
                                                rationale=f"Retry publication {pub.id}")
        pub.publication_decision_id = decision.id
        pub.executed_by = actor_user_id
        expires = decision.expires_at
    reasons = _check_all(session, pub, retry_ops, expires)
    if reasons:
        audit.record(session, actor_type="user", actor_id=actor_user_id, action="publication.retry", result="DENIED",
                     target_type="publication", target_id=pub.id, project_id=pub.project_id, domain=pub.domain,
                     reason="; ".join(reasons)[:2000])
        session.flush()
        return ExecuteOutcome(False, reasons, pub)
    now = utcnow()
    for o in retry_ops:
        if o.status == OP_FAILED:
            o.status = OP_PENDING
            o.response = {**(o.response or {}), "previous_error": o.error}
        if o.status in (OP_UNKNOWN, OP_IN_FLIGHT):
            o.status = OP_UNKNOWN  # reconcile first
            o.response = {**(o.response or {}), "reconcile_failures": 0}
        o.max_attempts = max(o.max_attempts, o.attempts + RETRY_BUDGET)
        o.next_attempt_at = now
        o.error = ""
    pub.status = RUNNING
    pub.finished_at = None
    pub.status_reason = ""
    applied, note = plan_state.set_plan_state(session, pub.plan_id, PS.PUBLISHING, reason=f"retry {pub.id}")
    pub.summary = {**(pub.summary or {}), "plan_state": {"applied": applied, "note": note}}
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="publication.retry",
                 target_type="publication", target_id=pub.id, project_id=pub.project_id, domain=pub.domain,
                 details={"operations": len(retry_ops)})
    events.emit(session, source="publisher", kind="publication.retry", entity_type="publication", entity_id=pub.id,
                project_id=pub.project_id, domain=pub.domain, payload={"operations": len(retry_ops)})
    session.flush()
    return ExecuteOutcome(True, [], pub)


def reconcile_publication(session: Session, pub_id: str, *, actor_user_id: str,
                          now: datetime | None = None) -> dict[str, Any]:
    """Read-only reconciliation of uncertain/failed ops (find by marker). No external writes."""
    pub = session.get(Publication, pub_id)
    if pub is None:
        raise NotFound("publication not found")
    if pub.status == PREVIEW:
        raise Conflict("a PREVIEW has no side effects to reconcile", code="NOTHING_TO_RECONCILE")
    project = session.get(Project, pub.project_id)
    if project is None:
        raise NotFound("project not found")
    run = _Run(session=session, pub=pub, project=project, now=now or utcnow())
    report: dict[str, list[str]] = {"found": [], "absent": [], "unknown": [], "conflict": [], "skipped": []}
    try:
        for op in _ops_for(session, pub.id):
            if op.status not in (OP_UNKNOWN, OP_IN_FLIGHT, OP_FAILED, OP_PENDING) or \
                    (op.status == OP_PENDING and op.attempts == 0):
                report["skipped"].append(op.id)
                continue
            outcome = _reconcile_one(run, op)
            if outcome == "absent" and op.status in (OP_UNKNOWN, OP_IN_FLIGHT):
                op.status = OP_PENDING  # proven absent → a retry may create it
            report[outcome].append(op.id)
        if pub.status != RUNNING:
            ops = _ops_for(session, pub.id)
            pub.status = final_status(ops)
            pub.summary = {**(pub.summary or {}), "counts": _counts(ops)}
    finally:
        run.close()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="publication.reconcile",
                 target_type="publication", target_id=pub.id, project_id=pub.project_id, domain=pub.domain,
                 details={k: len(v) for k, v in report.items()})
    events.emit(session, source="publisher", kind="publication.reconciled", entity_type="publication",
                entity_id=pub.id, project_id=pub.project_id, domain=pub.domain,
                payload={k: len(v) for k, v in report.items()})
    session.flush()
    return {k: len(v) for k, v in report.items()} | {"status": pub.status}


def recover_in_flight(session: Session) -> int:
    """Startup crash recovery: IN_FLIGHT ops of an earlier process are UNKNOWN (reconcile first, §14)."""
    n = 0
    for op in session.scalars(select(PublicationOperation).where(PublicationOperation.status == OP_IN_FLIGHT)):
        op.status = OP_UNKNOWN
        op.error = "controller restarted during the call — outcome unknown, reconcile before retry"
        n += 1
    session.flush()
    return n


def due_publications(session: Session) -> list[str]:
    return list(session.scalars(select(Publication.id).where(Publication.status == RUNNING)
                                .order_by(Publication.started_at)))


# ---------------------------------------------------------------------------------------------
# Views


def _dimension(status: str, detail: str, source: str) -> dict[str, str]:
    return {"status": status, "detail": detail, "source": source}


def status_dimensions(session: Session, pub: Publication) -> dict[str, dict[str, str]]:
    """§41: APPROVED / IMPLEMENTED / ACTIVE / VERIFIED / ARCHITECTURE_SOURCE_UPDATED / DOCS_PUBLISHED
    are separate fields — never collapsed into one green badge."""
    dims: dict[str, dict[str, str]] = {}
    plan = session.get(Plan, pub.plan_id) if pub.plan_id else None
    try:
        current = planning.get_approved_version(session, plan.id) if plan else None
        if current is None:
            dims["APPROVED"] = _dimension("UNKNOWN", "tidak ada versi yang disetujui saat ini", "planning")
        elif current.id == pub.plan_version_id:
            dims["APPROVED"] = _dimension("APPROVED", f"Decision {pub.decision_id}", "Decision registry")
        else:
            dims["APPROVED"] = _dimension("SUPERSEDED", f"versi disetujui sekarang {current.id}", "Decision registry")
    except (ImportError, ModuleNotFoundError):
        dims["APPROVED"] = _dimension("UNKNOWN", "planning service unavailable", "planning")
    task_ids = list(session.scalars(select(TaskSpec.id).where(TaskSpec.plan_version_id == pub.plan_version_id)))
    if not task_ids:
        dims["IMPLEMENTED"] = _dimension("UNKNOWN", "belum ada TaskSpec untuk versi ini", "workforce")
        dims["VERIFIED"] = _dimension("UNKNOWN", "belum ada TaskSpec untuk versi ini", "verification")
    else:
        states = list(session.scalars(select(TaskExecution.state).where(TaskExecution.task_id.in_(task_ids))))
        done = sum(1 for x in states if x == "DONE")
        status = "DONE" if done == len(task_ids) else ("IN_PROGRESS" if states else "NOT_STARTED")
        dims["IMPLEMENTED"] = _dimension(status, f"{done}/{len(task_ids)} task DONE (runtime state)", "workforce")
        try:
            from aeos.verification import facade as verification

            ok = sum(1 for t in task_ids if verification.acceptance_is_valid(session, t,
                                                                             plan_version_hash=pub.approved_hash))
            dims["VERIFIED"] = _dimension("VERIFIED" if ok == len(task_ids) else "NOT_PROVEN",
                                          f"{ok}/{len(task_ids)} task dengan acceptance valid", "verification")
        except (ImportError, ModuleNotFoundError):
            dims["VERIFIED"] = _dimension("UNKNOWN", "verification service unavailable", "verification")
    dims["ACTIVE"] = _dimension("UNKNOWN", "tidak ada observasi runtime/deployment yang ditautkan ke versi ini",
                                "operations")
    snaps = [s for s in session.scalars(select(ArchitectureSnapshot)) if pub.plan_version_id in str(s.refs)]
    dims["ARCHITECTURE_SOURCE_UPDATED"] = _dimension(
        "UPDATED" if snaps else "NOT_RECORDED",
        f"{len(snaps)} ArchitectureSnapshot merujuk versi ini" if snaps else "belum ada ArchitectureSnapshot",
        "decisions")
    docs_map = {PUBLISHED: "PUBLISHED", PARTIAL: "PARTIAL", CONFLICT: "CONFLICT", RUNNING: "RUNNING",
                PREVIEW: "NOT_PUBLISHED", FAILED: "FAILED", CANCELLED: "NOT_PUBLISHED"}
    dims["DOCS_PUBLISHED"] = _dimension(docs_map.get(pub.status, "UNKNOWN"), pub.status_reason or pub.status,
                                        "publisher")
    return dims


def operation_view(op: PublicationOperation) -> dict[str, Any]:
    payload = op.payload or {}
    return {
        "id": op.id, "order_index": op.order_index, "provider": op.provider, "operation": op.operation,
        "logical_id": op.logical_id, "title": op.title, "target": redact(op.target or {}), "status": op.status,
        "attempts": op.attempts, "max_attempts": op.max_attempts, "external_id": op.external_id,
        "external_url": op.external_url, "external_revision": op.external_revision, "error": redact_text(op.error or ""),
        "next_attempt_at": op.next_attempt_at.isoformat() if op.next_attempt_at else None,
        "last_attempt_at": op.last_attempt_at.isoformat() if op.last_attempt_at else None,
        "reconciled_at": op.reconciled_at.isoformat() if op.reconciled_at else None,
        "depends_on": list(op.depends_on or []), "idempotency_key": op.idempotency_key,
        "payload_hash": op.payload_hash,
        "payload_summary": {"bytes": len(payload.get("markdown", "") or payload.get("body", "") or ""),
                            "labels": payload.get("labels"), "rendered_visuals": payload.get("rendered_visuals")},
        "response": redact({k: v for k, v in (op.response or {}).items() if k != "history"}),
        "history": (op.response or {}).get("history", []),
    }


def publication_view(session: Session, pub: Publication, *, with_ops: bool = True) -> dict[str, Any]:
    ops = _ops_for(session, pub.id)
    executable = [o for o in ops if o.status not in (OP_SKIPPED, OP_CONFLICT)]
    out: dict[str, Any] = {
        "id": pub.id, "project_id": pub.project_id, "domain": pub.domain, "plan_id": pub.plan_id,
        "plan_version_id": pub.plan_version_id, "plan_version_number": pub.plan_version_number,
        "approved_hash": pub.approved_hash, "preview_hash": pub.preview_hash, "decision_id": pub.decision_id,
        "publication_decision_id": pub.publication_decision_id, "status": pub.status,
        "status_reason": pub.status_reason, "targets": redact(pub.targets or []),
        "blocked_targets": (pub.preview or {}).get("blocked_targets", []), "mode": pub.mode,
        "created_at": pub.created_at.isoformat(), "updated_at": pub.updated_at.isoformat(),
        "started_at": pub.started_at.isoformat() if pub.started_at else None,
        "finished_at": pub.finished_at.isoformat() if pub.finished_at else None,
        "created_by": pub.created_by, "executed_by": pub.executed_by, "counts": _counts(ops),
        "summary": pub.summary or {}, "preview": {k: v for k, v in (pub.preview or {}).items() if k != "targets"},
        "status_dimensions": status_dimensions(session, pub),
        "approval_statement": approval_statement(pub, len(executable)),
        "attention": [o.id for o in ops if o.status in (OP_UNKNOWN, OP_CONFLICT, OP_FAILED)],
    }
    if with_ops:
        out["operations"] = [operation_view(o) for o in ops]
    return out


def list_publications(session: Session, *, project_ids: list[str], domain: str) -> list[Publication]:
    if not project_ids:
        return []
    return list(session.scalars(select(Publication).where(Publication.project_id.in_(project_ids),
                                                          Publication.domain == domain)
                                .order_by(Publication.created_at.desc()).limit(200)))
