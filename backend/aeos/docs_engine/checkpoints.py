"""Documentation checkpoints (master §28 "unified narrative/diagrams updated at an approved checkpoint", §41
"Narasi arsitektur/diagram: delta pada PR/checkpoint, finalisasi pada accepted milestone/release" and "Pada
checkpoint yang benar-benar diterima, Tech Writer/Diagram Author menyusun narasi dan visual terpadu; Claude
memeriksa paket perubahan material").

A checkpoint is recorded only when it is really accepted:

* ``ARCHITECTURE_SNAPSHOT`` - an inventory/tool checkpoint: an ArchitectureSnapshot with a human APPROVAL bound to
  its exact hash (platform project: DLY AEOS documents itself);
* ``MILESTONE`` - every TaskSpec of the milestone's features (approved PlanVersion) has a valid ACCEPTED acceptance;
  this also FINALISES the documentation of that milestone (a consistent document snapshot, like a release
  candidate).

The result is one immutable ``DocumentRevision`` (kind ``DOC_CHECKPOINT``) on the unified narrative
(``03-architecture-overview``) carrying the documentation change package: which documents changed since the previous
checkpoint (content hashes), the architecture delta (snapshot diff) or the change records of the milestone, the
diagram delta, whether the change is material, and a TaskSpec DRAFT for the Technical Writer / Diagram-Visual
Author with the changed-section list. The draft becomes executable work only after the owner approves it into a
plan version (§21: workers only run approved TaskSpecs). Claude's review of a material package is advisory
(``POST /api/claude/review-docs-checkpoint``). Deterministic: no LLM runs here. Services never commit.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import Settings
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash, sha256_text
from aeos.docs_engine import renderer as r
from aeos.docs_engine.plan_content import as_text, feature_slug
from aeos.events import service as events
from aeos.models.planning import PlanVersion
from aeos.models.portfolio import Project
from aeos.models.publishing import DocumentRecord, DocumentRevision

CHECKPOINT_KIND = "DOC_CHECKPOINT"
KINDS = ("ARCHITECTURE_SNAPSHOT", "MILESTONE")
NARRATIVE_DOC = "03-architecture-overview"
# A change in these document families is material for the unified narrative (architecture, security, operations,
# release); everything else (e.g. a PRD wording change) is reported but not material on its own.
MATERIAL_PREFIXES = ("03-", "04-", "08-", "07-")


def _previous(session: Session, project_id: str) -> DocumentRevision | None:
    return session.scalar(select(DocumentRevision).where(
        DocumentRevision.project_id == project_id, DocumentRevision.kind == CHECKPOINT_KIND)
        .order_by(DocumentRevision.created_at.desc(), DocumentRevision.id.desc()).limit(1))


def _milestone_facts(session: Session, version: PlanVersion, key: str) -> dict[str, Any]:
    from aeos.planning import facade as planning
    from aeos.verification import facade as verification

    content = version.content if isinstance(version.content, dict) else {}
    milestones = {as_text(m.get("key")): m for m in content.get("milestones") or [] if isinstance(m, dict)}
    if key not in milestones:
        raise NotFound(f"milestone {key!r} is not part of the approved plan version",
                       details={"milestones": sorted(milestones)})
    features = sorted(as_text(f.get("key")) for f in content.get("features") or []
                      if isinstance(f, dict) and as_text(f.get("milestone")) == key)
    slugs = {feature_slug(f) for f in features}
    tasks = [t for t in planning.tasks_for_version(session, version.id) if feature_slug(t.feature_key or "") in slugs]
    if not tasks:
        raise Conflict(f"milestone {key!r} has no TaskSpec in the approved plan version; nothing was accepted",
                       code="MILESTONE_HAS_NO_TASKS", details={"features": features})
    open_tasks = sorted(t.task_key for t in tasks
                        if not verification.acceptance_is_valid(session, t.id, plan_version_hash=t.plan_version_hash))
    if open_tasks:
        raise Conflict(f"milestone {key!r} is not accepted: {len(open_tasks)} task(s) without a valid ACCEPTED "
                       "acceptance", code="MILESTONE_NOT_ACCEPTED", details={"tasks": open_tasks})
    m = milestones[key]
    return {"milestone": key, "title": as_text(m.get("title")), "exit_evidence": as_text(m.get("exit_evidence")),
            "features": features, "tasks": sorted(t.task_key for t in tasks), "task_ids": sorted(t.id for t in tasks)}


def _snapshot_facts(session: Session, project: Project, snapshot_id: str,
                    previous: DocumentRevision | None) -> dict[str, Any]:
    from aeos.inventory import facade as inventory

    if not project.is_platform:
        raise ValidationFailed("an ArchitectureSnapshot checkpoint documents the platform itself (DLY AEOS); a "
                               "product project uses MILESTONE checkpoints", code="CHECKPOINT_PLATFORM_ONLY")
    meta = inventory.snapshot_checkpoint(session, snapshot_id, domain=project.domain)
    if not meta.get("approved"):
        raise Conflict("the snapshot has no human APPROVAL bound to its hash; only an approved checkpoint updates "
                       "the unified narrative", code="SNAPSHOT_NOT_APPROVED")
    before = ((previous.details or {}).get("checkpoint") or {}).get("snapshot_id") if previous is not None else None
    delta = (inventory.snapshot_delta(session, before, snapshot_id, domain=project.domain)
             if before and before != snapshot_id else None)
    return {"snapshot_id": snapshot_id, "snapshot_hash": meta.get("snapshot_hash"), "label": meta.get("label"),
            "approval": meta.get("approval"), "counts": meta.get("counts"), "previous_snapshot_id": before,
            "architecture_delta": delta}


# "Hash/diff menentukan bagian dokumentasi yang berubah" (§41): one hash per rendered section of a document.
_SECTION_RE = re.compile(r"<!-- aeos:section=([\w.-]+)[^>]*-->\n(.*?)(?=\n## |\Z)", re.S)


def section_hashes(body: str) -> dict[str, str]:
    return {m.group(1): sha256_text(m.group(2).strip())[:16] for m in _SECTION_RE.finditer(body or "")}


def _section_titles(doc: r.RenderedDocument, record: DocumentRecord | None, keys: set[str]) -> list[str]:
    # a reused (unchanged) document is not rebuilt: its section list comes from the stored record
    sections = [(s.key, s.title) for s in doc.sections] or [
        (str(x.get("key")), str(x.get("title"))) for x in ((record.sections if record is not None else None) or [])
        if isinstance(x, dict)]
    return [title for key, title in sections if key in keys]


def record_checkpoint(session: Session, settings: Settings, version: PlanVersion, *, kind: str, ref: str,
                      actor_user_id: str, actor_type: str = "user") -> dict[str, Any]:
    """Record an accepted checkpoint's documentation change package (idempotent per package hash)."""
    from aeos.docs_engine.service import change_infos, render_version

    kind = (kind or "").strip().upper()
    if kind not in KINDS:
        raise ValidationFailed(f"kind must be one of {list(KINDS)}", code="CHECKPOINT_KIND_INVALID")
    ref = (ref or "").strip()
    if not ref:
        raise ValidationFailed("ref (snapshot id or milestone key) is required", code="CHECKPOINT_REF_REQUIRED")
    project = session.get(Project, version.project_id)
    if project is None:
        raise NotFound("project not found")
    result, _delta, state = render_version(session, settings, version, persist=True, render_visuals=False)
    if not state.approved:
        raise Conflict("a documentation checkpoint needs the APPROVED plan version", code="PLAN_NOT_APPROVED",
                       details={"reason": state.reason})
    session.flush()
    previous = _previous(session, project.id)
    if kind == "MILESTONE":
        facts = _milestone_facts(session, version, ref)
        changes = [c for c in change_infos(session, project) if c.task_id in set(facts["task_ids"])]
        facts["change_records"] = [{"task_key": c.task_key, "pr": c.pr_url, "merge_commit": c.merge_commit_sha,
                                    "contract_deltas": list(c.contract_deltas),
                                    "dependency_deltas": list(c.dependency_deltas),
                                    "decisions": list(c.decision_refs)} for c in changes]
    else:
        facts = _snapshot_facts(session, project, ref, previous)
    records = {x.logical_doc_id: x for x in session.scalars(select(DocumentRecord).where(
        DocumentRecord.project_id == project.id))}
    narrative = records.get(NARRATIVE_DOC)
    if narrative is None:
        raise Conflict("the unified narrative (03-architecture-overview) is not rendered for this project",
                       code="NARRATIVE_MISSING")
    current = {d.logical_doc_id: d for d in result.documents if d.kind == "DOC"}
    from aeos.docs_engine.service import document_body

    now_docs: dict[str, dict[str, Any]] = {
        lid: {"hash": records[lid].content_hash if lid in records else None,
              "sections": section_hashes(document_body(session, project.id, current[lid]))}
        for lid in sorted(current)}
    before_docs: dict[str, Any] = dict((previous.details or {}).get("documents") or {}) if previous is not None else {}
    changed: list[dict[str, Any]] = []
    for lid, now in now_docs.items():
        before = before_docs.get(lid)
        if before is not None and before.get("hash") == now["hash"]:
            continue
        old: dict[str, str] = (before or {}).get("sections") or {}
        new: dict[str, str] = now["sections"]
        keys = {k for k, h in new.items() if old.get(k) != h} | (set(old) - set(new))
        changed.append({"logical_doc_id": lid, "title": current[lid].title,
                        "change": "ADDED" if before is None else "CHANGED",
                        "sections": _section_titles(current[lid], records.get(lid), keys)})
    removed = sorted(set(before_docs) - set(now_docs))
    visuals = {v.key: {"kind": v.kind, "source_hash": v.source_hash, "verification": v.verification_status}
               for v in result.visuals}
    before_visuals: dict[str, Any] = dict((previous.details or {}).get("visuals") or {}) if previous is not None else {}
    visual_delta = sorted(k for k in set(visuals) | set(before_visuals)
                          if (visuals.get(k) or {}).get("source_hash") != (before_visuals.get(k) or {}).get("source_hash"))
    arch = facts.get("architecture_delta") or {}
    contract_changes = sorted({p for c in facts.get("change_records") or [] for p in c["contract_deltas"]})
    material = (any(c["logical_doc_id"].startswith(MATERIAL_PREFIXES) for c in changed) or bool(visual_delta)
                or bool(contract_changes) or bool(removed)
                or any((arch.get(k) or {}).get(x) for k in ("nodes", "edges") for x in ("added", "removed", "changed"))
                or bool(arch.get("refs_changed")))
    targets = [c for c in changed if c["logical_doc_id"].startswith(MATERIAL_PREFIXES)] or changed
    writer_task = {
        "status": "DRAFT_FOR_NEXT_PLAN_VERSION",
        "note": "Draft TaskSpec; dijalankan hanya setelah pemilik menyetujuinya ke dalam PlanVersion (§21).",
        "key": f"DOC-{feature_slug(kind)[:4]}-{feature_slug(ref)[:12]}".upper(),
        "title": f"Perbarui narasi & visual terpadu setelah checkpoint {kind.lower()} {ref}"[:200],
        "role_key": "technical-writer", "supporting_roles": ["diagram-visual-author"],
        "goal": "Susun narasi arsitektur dan visual terpadu dari artefak yang diterima pada checkpoint ini; hanya "
                "menunjuk artefak, tanpa status runtime atau approval.",
        "input_artifacts": [f"documentation_checkpoint:{project.id}:{kind}:{ref}"],
        "expected_outputs": ["delta narasi per dokumen", "sumber diagram yang diperbarui"],
        "changed_sections": [{"document": c["logical_doc_id"], "sections": c["sections"]} for c in targets][:40],
        "diagrams": visual_delta[:40],
        "acceptance_criteria": ["Setiap bagian yang berubah menunjuk artefak/commit/evidence yang diterima",
                                "Diagram diperiksa terhadap kontrak/manifest (verification_status tidak MISMATCH)"],
    }
    # the same checkpoint over unchanged documents/diagrams is the same record (idempotent, nothing re-recorded)
    state_hash = content_hash({"kind": kind, "ref": ref, "plan_manifest_hash": version.manifest_hash,
                               "documents": now_docs, "visuals": visuals})
    if previous is not None and (previous.details or {}).get("state_hash") == state_hash:
        return {**checkpoint_view(previous), "created": False}
    package = {
        "state_hash": state_hash,
        "project_id": project.id, "plan_version_id": version.id, "plan_manifest_hash": version.manifest_hash,
        "checkpoint": {"kind": kind, **facts}, "previous_checkpoint_id": previous.id if previous is not None else None,
        "documents": now_docs, "visuals": visuals, "changed_documents": changed, "removed_documents": removed,
        "diagram_delta": visual_delta, "contract_deltas": contract_changes, "material": material,
        "finalised": kind == "MILESTONE", "writer_task": writer_task,
    }
    package_hash = content_hash(package)
    existing = session.scalar(select(DocumentRevision).where(DocumentRevision.document_id == narrative.id,
                                                             DocumentRevision.kind == CHECKPOINT_KIND,
                                                             DocumentRevision.content_hash == package_hash))
    if existing is not None:
        return {**checkpoint_view(existing), "created": False}
    lines = [f"# Documentation checkpoint — {kind} `{ref}`", "",
             f"PlanVersion `{version.id}` v{version.version} (`{version.manifest_hash}`)"
             + (" — **finalisasi milestone**" if kind == "MILESTONE" else ""), "",
             f"Material: **{'ya' if material else 'tidak'}** · dokumen berubah: {len(changed)} · dihapus: {len(removed)}"
             f" · diagram berubah: {len(visual_delta)}", ""]
    lines += [f"- `{c['logical_doc_id']}` ({c['change']}): {', '.join(c['sections'][:8]) or '—'}" for c in changed[:60]]
    rev = DocumentRevision(document_id=narrative.id, project_id=project.id, domain=project.domain,
                           logical_doc_id=NARRATIVE_DOC, kind=CHECKPOINT_KIND, status="RECORDED",
                           plan_version_id=version.id, content_hash=package_hash, body_markdown="\n".join(lines) + "\n",
                           details=package)
    session.add(rev)
    session.flush()
    narrative.links = {**(narrative.links or {}), "checkpoint": {"revision_id": rev.id, "kind": kind, "ref": ref,
                                                                 "package_hash": package_hash, "material": material}}
    audit.record(session, actor_type=actor_type, actor_id=actor_user_id, action="documents.checkpoint",
                 target_type="document", target_id=narrative.id, project_id=project.id, domain=project.domain,
                 details={"kind": kind, "ref": ref, "package_hash": package_hash, "material": material,
                          "changed_documents": len(changed), "plan_version_id": version.id})
    events.emit(session, source="docs_engine", kind="documents.checkpoint", entity_type="document",
                entity_id=narrative.id, entity_version=package_hash, project_id=project.id, domain=project.domain,
                payload={"kind": kind, "ref": ref, "material": material, "changed_documents": len(changed)})
    session.flush()
    return {**checkpoint_view(rev), "created": True}


def checkpoint_view(rev: DocumentRevision) -> dict[str, Any]:
    d = rev.details or {}
    cp = d.get("checkpoint") or {}
    return {"id": rev.id, "kind": cp.get("kind"), "ref": cp.get("snapshot_id") or cp.get("milestone"),
            "package_hash": rev.content_hash, "plan_version_id": rev.plan_version_id, "material": d.get("material"),
            "finalised": d.get("finalised"), "changed_documents": d.get("changed_documents") or [],
            "removed_documents": d.get("removed_documents") or [], "diagram_delta": d.get("diagram_delta") or [],
            "contract_deltas": d.get("contract_deltas") or [], "architecture_delta": cp.get("architecture_delta"),
            "writer_task": d.get("writer_task"), "previous_checkpoint_id": d.get("previous_checkpoint_id"),
            "created_at": rev.created_at.isoformat() if rev.created_at else None}


def list_checkpoints(session: Session, project_id: str, *, with_planning: bool = False) -> list[dict[str, Any]]:
    rows = session.scalars(select(DocumentRevision).where(
        DocumentRevision.project_id == project_id, DocumentRevision.kind == CHECKPOINT_KIND)
        .order_by(DocumentRevision.created_at.desc()).limit(100))
    if not with_planning:
        return [checkpoint_view(x) for x in rows]
    return [{**checkpoint_view(x), "writer_task_planned": _writer_planned(session, x)} for x in rows]


def get_checkpoint(session: Session, revision_id: str) -> DocumentRevision:
    rev = session.get(DocumentRevision, revision_id)
    if rev is None or rev.kind != CHECKPOINT_KIND:
        raise NotFound("documentation checkpoint not found")
    return rev


# ---------------------------------------------------------------------------------------------------------------
# Control Center / CLI support: which checkpoints are ready to record (an offer, never an automatic record), and the
# owner-chosen hand-off of the writer draft into the NEXT plan version (it becomes an executable TaskSpec only when
# the owner approves that version, §21).


def _approved_version(session: Session, project: Project) -> PlanVersion | None:
    from aeos.models.planning import Plan
    from aeos.planning import facade as planning

    plans = session.scalars(select(Plan).where(Plan.project_id == project.id, Plan.domain == project.domain)
                            .order_by(Plan.created_at.desc()))
    for plan in plans:
        v = planning.get_approved_version(session, plan.id)
        if v is not None:
            return v
    return None


def checkpoint_candidates(session: Session, project: Project) -> dict[str, Any]:
    """§41 "finalisasi pada accepted milestone": per milestone of the APPROVED plan version how many TaskSpecs are
    ACCEPTED, approved ArchitectureSnapshots (platform project), and an OFFER for every fully accepted milestone /
    approved snapshot that has no checkpoint yet. Read-only; recording stays an explicit admin action."""
    from aeos.models.decisions import ArchitectureSnapshot
    from aeos.planning import facade as planning
    from aeos.verification import facade as verification

    recorded = {(c["kind"], str(c["ref"])): c["id"] for c in list_checkpoints(session, project.id)}
    version = _approved_version(session, project)
    out: dict[str, Any] = {"plan_version": None, "milestones": [], "snapshots": [], "offers": []}
    if version is not None:
        out["plan_version"] = {"id": version.id, "version": version.version, "manifest_hash": version.manifest_hash}
        content = version.content if isinstance(version.content, dict) else {}
        tasks = planning.tasks_for_version(session, version.id)
        for m in content.get("milestones") or []:
            if not isinstance(m, dict) or not as_text(m.get("key")):
                continue
            key = as_text(m.get("key"))
            slugs = {feature_slug(as_text(f.get("key"))) for f in content.get("features") or []
                     if isinstance(f, dict) and as_text(f.get("milestone")) == key}
            mine = [t for t in tasks if feature_slug(t.feature_key or "") in slugs]
            accepted = [t for t in mine if verification.acceptance_is_valid(session, t.id,
                                                                             plan_version_hash=t.plan_version_hash)]
            row = {"key": key, "title": as_text(m.get("title")), "tasks_total": len(mine),
                   "tasks_accepted": len(accepted), "accepted": bool(mine) and len(accepted) == len(mine),
                   "checkpoint_id": recorded.get(("MILESTONE", key))}
            out["milestones"].append(row)
            if row["accepted"] and not row["checkpoint_id"]:
                out["offers"].append({"kind": "MILESTONE", "ref": key, "plan_version_id": version.id,
                                      "reason": f"Semua {len(mine)} task milestone {key} ACCEPTED; catat checkpoint "
                                                "untuk memfinalisasi dokumentasinya."})
    if project.is_platform:
        from aeos.inventory import facade as inventory

        for snap in session.scalars(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at.desc())
                                    .limit(20)):
            try:
                meta = inventory.snapshot_checkpoint(session, snap.id, domain=project.domain)
            except (NotFound, ValidationFailed):
                continue  # another domain's snapshot is never listed
            row = {"id": snap.id, "label": meta.get("label"), "snapshot_hash": meta.get("snapshot_hash"),
                   "approved": bool(meta.get("approved")), "created_at": meta.get("created_at"),
                   "checkpoint_id": recorded.get(("ARCHITECTURE_SNAPSHOT", snap.id))}
            out["snapshots"].append(row)
            if row["approved"] and not row["checkpoint_id"] and version is not None:
                out["offers"].append({"kind": "ARCHITECTURE_SNAPSHOT", "ref": snap.id, "plan_version_id": version.id,
                                      "reason": f"Snapshot arsitektur {meta.get('label') or snap.id} disetujui; catat "
                                                "checkpoint agar narasi terpadu memuat deltanya."})
    return out


def writer_task_spec(rev: DocumentRevision, project: Project) -> dict[str, Any]:
    """The Tech Writer / Diagram Author draft as a complete plan TASK (planning schema). Write scope stays EMPTY:
    which paths the writer may change is the owner's decision in the plan version, and the scheduler never runs a
    TaskSpec without allowed paths."""
    d = rev.details or {}
    w = d.get("writer_task") or {}
    sections = [f"{c.get('document')}: {', '.join(c.get('sections') or []) or '-'}" for c in w.get("changed_sections")
                or [] if isinstance(c, dict)]
    return {
        "key": str(w.get("key") or "DOC-CHECKPOINT")[:64], "feature_key": "", "title": str(w.get("title") or ""),
        "goal": str(w.get("goal") or ""), "role_key": str(w.get("role_key") or "technical-writer"),
        "input_artifacts": [str(x) for x in w.get("input_artifacts") or []],
        "expected_outputs": [*[str(x) for x in w.get("expected_outputs") or []],
                             *[f"bagian berubah: {s}" for s in sections[:20]],
                             *[f"diagram berubah: {k}" for k in (w.get("diagrams") or [])[:20]]],
        "acceptance_criteria": [str(x) for x in w.get("acceptance_criteria") or []],
        "depends_on": [], "allowed_paths": [], "forbidden_paths": [], "tools_allowed": [], "egress_allowed": [],
        "capability_scope": {"operations": [], "paths": [],
                             "notes": "Path tulis ditetapkan pemilik di versi rencana sebelum approval."},
        "capability_ttl_seconds": 0, "data_classification": project.data_classification or "INTERNAL",
        "model_route_policy": {"pool": "WORKER", "requires_tools": True, "min_context_tokens": 0, "notes": ""},
        "reviewer_policy": {"required": True, "reviewer_role": "", "checkpoint": ""},
        "verifier_policy": {"required": True, "suites": [], "negative_tests_required": False},
        "limits": {"max_attempts": 0, "timeout_seconds": 0, "max_output_tokens": 0},
        "stop_conditions": [], "evidence_requirements": [f"paket checkpoint {rev.content_hash}"],
        "risk_class": "R1", "artifact_schema_version": "1",
    }


def _writer_planned(session: Session, rev: DocumentRevision) -> dict[str, Any] | None:
    """The newest plan version that already carries this checkpoint's writer task (by input-artifact label)."""
    version = session.get(PlanVersion, rev.plan_version_id) if rev.plan_version_id else None
    labels = set(((rev.details or {}).get("writer_task") or {}).get("input_artifacts") or [])
    if version is None or not labels:
        return None
    for v in session.scalars(select(PlanVersion).where(PlanVersion.plan_id == version.plan_id)
                             .order_by(PlanVersion.version.desc())):
        for t in (v.content or {}).get("tasks") or [] if isinstance(v.content, dict) else []:
            if isinstance(t, dict) and labels & set(t.get("input_artifacts") or []):
                return {"plan_version_id": v.id, "version": v.version, "task_key": t.get("key")}
    return None


def plan_writer_task(session: Session, revision_id: str, project: Project, *, actor_user_id: str,
                     rationale: str) -> dict[str, Any]:
    """The owner adds the checkpoint's writer draft to a NEW plan version (a human edit of the current version).
    That version is DRAFT (READY_FOR_HUMAN_REVIEW): the task becomes executable only after the owner approves it,
    with its write paths set (§21). Refused when the draft is already planned or the package is not material."""
    import copy

    from aeos.planning import facade as planning

    rev = get_checkpoint(session, revision_id)
    if rev.project_id != project.id or rev.domain != project.domain:
        raise NotFound("documentation checkpoint not found")
    if not (rev.details or {}).get("material"):
        raise Conflict("the package has no material change; there is nothing for the Tech Writer to integrate",
                       code="PACKAGE_NOT_MATERIAL")
    planned = _writer_planned(session, rev)
    if planned is not None:
        raise Conflict("the writer draft of this checkpoint is already part of a plan version",
                       code="WRITER_TASK_ALREADY_PLANNED", details=planned)
    base = session.get(PlanVersion, rev.plan_version_id) if rev.plan_version_id else None
    plan = planning.get_plan(session, base.plan_id) if base is not None else None
    latest = session.get(PlanVersion, plan.current_version_id) if plan is not None and plan.current_version_id else None
    if latest is None or plan is None:
        raise NotFound("the plan of this checkpoint has no current version")
    # the planning service upgrades older content (additive schema revisions) and validates schema + DAG
    content = copy.deepcopy(latest.content if isinstance(latest.content, dict) else {})
    task = writer_task_spec(rev, project)
    if any(isinstance(t, dict) and t.get("key") == task["key"] for t in content.get("tasks") or []):
        raise Conflict(f"task key {task['key']} already exists in the current plan version",
                       code="WRITER_TASK_KEY_TAKEN")
    content["tasks"] = [*(content.get("tasks") or []), task]
    version = planning.create_human_version(
        session, plan.id, content=content, parent_version_id=latest.id, actor_user_id=actor_user_id,
        rationale=(rationale or "").strip() or f"Tambahkan draft Tech Writer dari checkpoint {revision_id}")
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="documents.checkpoint.writer_task_planned",
                 target_type="document_revision", target_id=rev.id, project_id=project.id, domain=project.domain,
                 details={"plan_version_id": version.id, "task_key": task["key"], "package_hash": rev.content_hash})
    return {"plan_version_id": version.id, "version": version.version, "task_key": task["key"],
            "status": "READY_FOR_HUMAN_REVIEW",
            "note": "Versi rencana baru menunggu keputusan pemilik; path tulis task harus ditetapkan sebelum approval."}


def checkpoint_artifacts(session: Session, project_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Input artifacts a planner may name: ``documentation_checkpoint:<project>:<kind>:<ref>`` -> the recorded package
    (hash-bound DATA, newest first). The compiled TaskSpec carries the content, so the writer reads exactly what
    was recorded."""
    out: list[tuple[str, dict[str, Any]]] = []
    for rev in session.scalars(select(DocumentRevision).where(
            DocumentRevision.project_id == project_id, DocumentRevision.kind == CHECKPOINT_KIND)
            .order_by(DocumentRevision.created_at.desc()).limit(50)):
        view = checkpoint_view(rev)
        writer = view.get("writer_task") or {}
        content = {k: view.get(k) for k in ("kind", "ref", "package_hash", "material", "finalised",
                                             "diagram_delta", "contract_deltas", "architecture_delta")}
        content["changed_documents"] = [{"logical_doc_id": c.get("logical_doc_id"), "change": c.get("change"),
                                         "sections": (c.get("sections") or [])[:12]}
                                        for c in view.get("changed_documents") or []][:40]
        content["writer_task"] = {k: writer.get(k) for k in ("goal", "changed_sections", "diagrams")}
        entry = {"ref": f"doc_checkpoint:{rev.id}", "kind": "documentation_checkpoint", "sha256": rev.content_hash,
                 "name": f"Documentation checkpoint {view.get('kind')} {view.get('ref')}", "content": content}
        for label in writer.get("input_artifacts") or []:
            out.append((str(label), entry))
    return out
