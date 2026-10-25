"""Documentation & Visuals service layer: DB rows → renderer inputs, persistence, local previews.

Approval is read only through ``aeos.planning.facade.get_approved_version`` (the version bound by an
ACTIVE approval decision). If the planning service is unavailable the version is treated as NOT
approved (fail closed): a local preview is still possible, DocumentRecords are not updated.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.paths import ensure_within, safe_segment
from aeos.core.redaction import redact_text
from aeos.docs_engine import product1 as p1
from aeos.docs_engine import renderer as r
from aeos.docs_engine import visuals as vis
from aeos.docs_engine.docx import markdown_to_docx
from aeos.docs_engine.plan_content import build_view, feature_slug
from aeos.events import service as events
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact
from aeos.models.identity import User
from aeos.models.planning import PlanVersion, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.publishing import DocumentRecord, DocumentRevision
from aeos.models.research import ChangeProposal
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


# Trusted, project-scoped evidence kinds that §28 timings depend on (referenced by id, never copied as status).
# RUNTIME_OBSERVATION: the deployment observer's read-only check of the registered production/staging target
# (delivery.deployment.observe) - the production-validation signal of the 08 Operations documents.
SIGNAL_EVIDENCE_KINDS = ("PRODUCTION_VALIDATION", "RUNTIME_OBSERVATION", "RESTORE_TEST", "SCREENSHOT",
                         *r.VA_EVIDENCE_KINDS, *r.PT_EVIDENCE_KINDS)
_SUMMARY_KEYS = ("scope", "method", "tools", "remediation", "target", "environment", "status", "observed_at",
                 "expected_commit", "reported_commit", "route", "viewport", "provenance", "tester", "performed_on")


def evidence_refs(session: Session, project: Project) -> list[r.EvidenceRef]:
    rows = session.scalars(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project.id, EvidenceArtifact.domain == project.domain,
        EvidenceArtifact.kind.in_(SIGNAL_EVIDENCE_KINDS), EvidenceArtifact.trusted.is_(True),
        EvidenceArtifact.origin != "worker").order_by(EvidenceArtifact.created_at))
    out = []
    for ev in rows:
        inline = ev.inline if isinstance(ev.inline, dict) else {}
        # small JSON evidence keeps its (redacted) content inline, a larger one its scalar summary; top-level
        # inline keys win
        content, summ = inline.get("content"), inline.get("summary")
        body: dict[str, Any] = {**(summ if isinstance(summ, dict) else {}),
                                **(content if isinstance(content, dict) else {}), **inline}
        env = ev.environment if isinstance(ev.environment, dict) else {}
        summary = tuple((k, redact_text(str(body[k]))[:500]) for k in _SUMMARY_KEYS
                        if isinstance(body.get(k), (str, int, float)) and str(body[k]).strip())
        environment = env.get("name") or env.get("environment") or env.get("deployment_environment") or ""
        out.append(r.EvidenceRef(id=ev.id, kind=ev.kind, claim=redact_text(ev.claim or "")[:200], origin=ev.origin,
                                 evidence_level=ev.evidence_level, commit=str(env.get("commit") or "") or None,
                                 environment=str(environment) or None,
                                 created_at=ev.created_at.isoformat() if ev.created_at else None, summary=summary))
    return out


def repo_runbooks(project: Project) -> list[r.RunbookSource]:
    """The reviewed runbooks of the project's own repository: for the platform project ``docs/runbooks/*.md``
    (Trigger / Authority / Steps / Verification / rollback sections). Product repositories are not read here."""
    if not project.is_platform:
        return []
    from aeos.core.paths import repo_root

    out = []
    for path in sorted((repo_root() / "docs" / "runbooks").glob("*.md")):
        if path.stem.lower() == "readme":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        title = path.stem
        sections: list[tuple[str, str]] = []
        head: str | None = None
        buf: list[str] = []
        for line in text.splitlines():
            if line.startswith("# ") and title == path.stem:
                title = line[2:].strip().removeprefix("Runbook:").strip()
            elif line.startswith("## "):
                if head is not None:
                    sections.append((head, "\n".join(buf).strip()))
                head, buf = line[3:].strip().lower(), []
            elif head is not None:
                buf.append(line)
        if head is not None:
            sections.append((head, "\n".join(buf).strip()))
        out.append(r.RunbookSource(key=path.stem, title=title, path=f"docs/runbooks/{path.name}",
                                   sections=tuple((h, redact_text(b)) for h, b in sections if b)))
    return out


def _incident_info(v: dict[str, Any]) -> r.IncidentInfo:
    def strs(key: str) -> tuple[str, ...]:
        return tuple(str(x) for x in v.get(key) or [] if str(x).strip())

    timeline = tuple((str(e.get("at") or ""), str(e.get("kind") or ""), redact_text(str(e.get("note") or ""))[:500],
                      tuple(str(x) for x in e.get("evidence_ids") or []))
                     for e in v.get("timeline") or [] if isinstance(e, dict))
    def radius(d: str, br: dict[str, Any]) -> str:
        caps = ", ".join(br.get("revoked_capability_kinds") or []) or "-"
        comps = ", ".join(br.get("components") or []) or "-"
        return (f"{d}: proyek {len(br.get('projects') or [])}, run {len(br.get('runs') or [])}, side effect "
                f"{len(br.get('side_effect_targets') or [])}, capability {caps}, komponen {comps}")

    derived = tuple(radius(d, br) for d, br in sorted((v.get("derived_blast_radius") or {}).items())
                    if isinstance(br, dict) and br)
    follow = [f for f in v.get("follow_ups") or [] if isinstance(f, dict)]
    return r.IncidentInfo(
        id=str(v["id"]), title=str(v.get("title") or ""), status=str(v.get("status") or ""),
        owner=str(v.get("owner") or ""), source=str(v.get("source") or ""), source_ref=v.get("source_ref"),
        created_at=v.get("created_at"), resolved_at=v.get("resolved_at"), impact=str(v.get("impact") or ""),
        declared_blast_radius=str(v.get("declared_blast_radius") or ""), derived_blast_radius=derived,
        timeline=timeline, hypotheses=strs("hypotheses"), root_cause=str(v.get("root_cause") or ""),
        root_cause_evidence=strs("root_cause_evidence_refs"), root_cause_ref=v.get("root_cause_ref"),
        corrective_actions=tuple(str(a.get("description") or "") + (f" ({a['ref']})" if a.get("ref") else "")
                                 for a in v.get("corrective_actions") or [] if isinstance(a, dict)),
        regression_tests=strs("regression_test_ids"), eval_cases=strs("eval_case_refs"),
        follow_ups=tuple(f"{f.get('kind')} `{f.get('id')}`" for f in follow),
        follow_up_ids=tuple(str(f.get("id")) for f in follow if f.get("id")),
        evidence_ids=strs("evidence_ids"), resolution_missing=strs("resolution_requirements"))


def platform_ops_facts(project: Project) -> dict[str, str]:
    """For the platform project only: the 08 Operations facts of this controller (operations facade)."""
    if not project.is_platform:
        return {}
    from aeos.operations import facade as operations

    return operations.platform_operations_facts(project.domain)


def incident_infos(session: Session, project: Project) -> list[r.IncidentInfo]:
    """The incidents that concern this project (AEOS Operations records), already scoped to its domain."""
    from aeos.operations import facade as operations

    return [_incident_info(v) for v in operations.project_incidents(session, project)]


def _changed_files(session: Session, acc: AcceptanceRecord | None) -> list[str]:
    """Changed paths of an accepted run (the verifier's SCOPE_CHECK), read-only; [] when unknown."""
    if acc is None or not acc.verification_evidence_id:
        return []
    try:
        from aeos.verification import facade as verification
        from aeos.verification.verifier import related_evidence

        report = session.get(EvidenceArtifact, acc.verification_evidence_id)
        scope = related_evidence(session, report, "SCOPE_CHECK")
        return [str(p) for p in ((verification.read_json(scope) or {}).get("changed_files") or [])][:200]
    except Exception:  # evidence unreadable: the change record says documentation pending, never guesses
        log.warning("changed files of acceptance %s unavailable", acc.id)
        return []


def change_infos(session: Session, project: Project) -> list[r.ChangeInfo]:
    """One ChangeInfo per pull request the broker opened for this project (newest first per task).

    Delivery is a hard dependency here (it owns the rollback/contract rules): an import failure surfaces instead of
    silently rendering empty rollback notes and "no contract delta" (fail closed, never a quiet pass)."""
    from aeos.delivery.governance import OWNER_DECISION_KINDS, change_deltas
    from aeos.delivery.pr_bundle import _rollback_notes as rollback_notes
    from aeos.models.decisions import Decision

    out: list[r.ChangeInfo] = []
    prs = session.scalars(select(PullRequestRecord).where(PullRequestRecord.project_id == project.id,
                                                          PullRequestRecord.domain == project.domain)
                          .order_by(PullRequestRecord.created_at.desc()))
    for pr in prs:
        task = session.get(TaskSpec, pr.task_id)
        if task is None:
            continue
        acc = session.get(AcceptanceRecord, pr.acceptance_id) if pr.acceptance_id else None
        changed = _changed_files(session, acc)
        # §28/§32 deltas with the same categories the verifier uses (AEOS *and* product layouts: openapi, proto,
        # graphql, schema/**, lockfiles); unknown changed set -> no delta claimed
        deltas = change_deltas(changed) if changed else {"contracts": [], "dependencies": []}
        pv = session.get(PlanVersion, task.plan_version_id) if task.plan_version_id else None
        targets = [t for t in (task.id, task.plan_version_id, pv.plan_id if pv else None) if t]
        decisions = session.scalars(select(Decision).where(
            Decision.project_id == project.id, Decision.target_id.in_(targets), Decision.status == "ACTIVE",
            Decision.actor_kind == "HUMAN", Decision.kind.in_(OWNER_DECISION_KINDS)))
        out.append(r.ChangeInfo(
            task_id=task.id, task_key=task.task_key, title=task.title, goal=task.goal or "",
            feature_key=task.feature_key, risk_class=getattr(task, "risk_class", "") or "",
            acceptance_criteria=tuple(str(x) if not isinstance(x, dict) else str(x.get("text") or x.get("title") or x)
                                      for x in (task.acceptance_criteria or [])),
            allowed_paths=tuple(str(p) for p in (getattr(task, "allowed_paths", None) or [])),
            pr_id=pr.id, pr_number=pr.number, pr_url=pr.url, pr_state=pr.state, repo=pr.repo_full_name,
            head_ref=pr.head_ref, base_ref=pr.base_ref, accepted_head_sha=pr.accepted_head_sha,
            merge_commit_sha=pr.merge_commit_sha, merged=bool(pr.merged), acceptance_id=pr.acceptance_id or "",
            acceptance_status=acc.acceptance_status if acc else "",
            evidence_ids=tuple(acc.verifier_evidence_refs or []) if acc else (),
            bundle_evidence_id=pr.bundle_evidence_id,
            # unknown changed set ([]) -> no notes: "no migration changed" would be a claim without evidence
            rollback_notes=tuple(rollback_notes(changed)) if changed else (),
            contract_deltas=tuple(deltas["contracts"]), dependency_deltas=tuple(deltas["dependencies"]),
            decision_refs=tuple(sorted(f"{d.kind}:{d.id}" for d in decisions)),
        ))
    return out


def improvement_infos(session: Session, project: Project) -> list[r.ImprovementInfo]:
    out = []
    for p in session.scalars(select(ChangeProposal).where(ChangeProposal.scope_type == "PROJECT",
                                                          ChangeProposal.project_id == project.id,
                                                          ChangeProposal.domain == project.domain)):
        pkg = p.review_package if isinstance(p.review_package, dict) else {}
        owner = session.get(User, p.submitted_by) if p.submitted_by else None
        out.append(r.ImprovementInfo(
            id=p.id, title=p.title, kind=p.kind, status=p.status, problem=redact_text(p.problem or ""),
            expected_benefit=redact_text(p.expected_benefit or ""),
            risk=redact_text(str(pkg.get("risk") or pkg.get("risks") or ""))[:2000],
            owner=(owner.display_name or owner.username) if owner else "",
            source_refs=tuple(str(x) for x in (p.source_refs or [])), task_ids=tuple(p.task_ids or []),
            evidence_ids=tuple(p.evidence_ids or []), decision_ids=tuple(p.decision_ids or [])))
    return out


def render_signals(session: Session, settings: Settings, project: Project) -> r.RenderSignals:
    """Read-only inputs for documents whose §28 timing is not plan approval (PR, release, production, retirement)."""
    retirement = None
    if (project.lifecycle or "").upper() in ("RETIRING", "RETIRED"):
        from aeos.operations import retirement as retirement_mod

        retirement = retirement_mod.checklist(session, settings, project)
    return r.RenderSignals(evidence=evidence_refs(session, project), changes=change_infos(session, project),
                           improvements=improvement_infos(session, project), retirement=retirement,
                           incidents=incident_infos(session, project), runbooks=repo_runbooks(project),
                           platform_ops=platform_ops_facts(project))


def known_schema_names(project: Project) -> set[str] | None:
    """The schema an ERD of this project must match. Only the platform project's schema is known to AEOS
    (SQLAlchemy metadata at the running commit); other projects → None (ERD stays UNVERIFIED)."""
    if not project.is_platform:
        return None
    from aeos.core.db import Base

    names = set(Base.metadata.tables)
    names |= {m.class_.__name__ for m in Base.registry.mappers}
    return names


def known_schema_columns(project: Project) -> dict[str, set[str]] | None:
    """Columns per table (and per mapped class name) of the platform schema at the running commit."""
    if not project.is_platform:
        return None
    from aeos.core.db import Base

    out: dict[str, set[str]] = {name: {c.name for c in t.columns} for name, t in Base.metadata.tables.items()}
    for m in Base.registry.mappers:
        out.setdefault(m.class_.__name__, set()).update(c.name for c in m.columns)
    return out


def known_component_names(session: Session, project: Project) -> set[str] | None:
    """What a context/component diagram of this project must name: for the platform project the registered
    InstallManifest/inventory components (ids, names, friendly names); other projects → None (UNVERIFIED)."""
    if not project.is_platform:
        return None
    from aeos.models.inventory import ComponentReference

    rows = session.execute(select(ComponentReference.component_id, ComponentReference.name,
                                  ComponentReference.friendly_name).where(ComponentReference.tombstoned_at.is_(None)))
    return {str(x) for row in rows for x in row if x} or None


def known_route_paths(project: Project) -> set[str] | None:
    """API paths a sequence diagram may use: the platform's route contract (contracts/api/routes.json)."""
    if not project.is_platform:
        return None
    import json

    from aeos.core.paths import repo_root

    try:
        routes = json.loads((repo_root() / "contracts" / "api" / "routes.json").read_text(encoding="utf-8"))["routes"]
    except (OSError, ValueError, KeyError):
        return None
    return {str(r).split(" ", 1)[1] for r in routes if " " in str(r)} or None


def deployment_environments(session: Session, project: Project) -> list[str]:
    """Environments of this project with a registered (observe-only) deployment target."""
    from aeos.delivery import facade as delivery

    return [env for env in ("staging", "production") if delivery.deployment_target(session, project, env)]


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
    threat_model = view.security.get("threat_model")
    tm = threat_model if isinstance(threat_model, dict) else {}
    data_items = [str(x.get("data_type") or "") for x in (view.data.get("items") or []) if isinstance(x, dict)]
    vis.verify_visuals(visuals, trust_boundaries=[str(x) for x in (tm.get("trust_boundaries") or [])],
                       known_tables=known_schema_names(project), known_columns=known_schema_columns(project),
                       known_components=known_component_names(session, project),
                       known_routes=known_route_paths(project), data_items=data_items,
                       deployment_environments=deployment_environments(session, project))
    previous = r.previous_input_hashes(session, project.id) if (persist and state.approved) else {}
    result = r.render_document_set(
        project=project_identity(session, project),
        version=r.VersionIdentity(id=version.id, plan_id=version.plan_id, version=version.version,
                                  manifest_hash=version.manifest_hash, approved=state.approved,
                                  approval_decision_id=decision_id, created_at=version.created_at),
        content=version.content, tasks=task_infos(session, version.id), visuals=visuals,
        previous_input_hashes=previous, signals=render_signals(session, settings, project),
    )
    delta = r.sync_document_records(session, result, domain=project.domain) if (persist and state.approved) else None
    return result, delta, state


# §41 "Kriteria kesiapan menentukan apakah bagian dokumentasi wajib lengkap sebelum tahap berikutnya": the
# PROJECT_CREATION and FEATURE_APPROVAL required sections (product1.REQUIRED_SECTIONS) come from the approved plan
# content only, so their readiness is a pure function of the immutable PlanVersion (cached per version + hash).
_STAGE_CACHE: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = {}
_STAGE_CACHE_MAX = 64
PRE_IMPLEMENTATION_STAGES = (p1.Timing.PROJECT_CREATION, p1.Timing.FEATURE_APPROVAL)


def _stage_pending(session: Session, version: PlanVersion) -> dict[str, list[dict[str, Any]]]:
    key = (version.id, version.manifest_hash or "", r.RENDERER_VERSION)
    hit = _STAGE_CACHE.get(key)
    if hit is not None:
        return hit
    project = session.get(Project, version.project_id)
    if project is None:
        raise NotFound("project not found")
    result = r.render_document_set(
        project=project_identity(session, project),
        version=r.VersionIdentity(id=version.id, plan_id=version.plan_id, version=version.version,
                                  manifest_hash=version.manifest_hash, approved=True,
                                  created_at=version.created_at),
        content=version.content, tasks=task_infos(session, version.id),
        visuals=vis.source_only(build_view(version.content).diagrams), signals=r.RenderSignals())
    out: dict[str, list[dict[str, Any]]] = {}
    for stage in PRE_IMPLEMENTATION_STAGES:
        docs = {d.logical_doc_id: d for d in result.documents}
        items = p1.required_pending(result.documents, stage)
        for it in items:  # who the pending document is about (feature keys / task ids), for per-task gating
            links = docs[it["logical_doc_id"]].links or {}
            it["features"] = sorted({feature_slug(x) for x in links.get("requirement") or [] if "/" not in str(x)})
            it["task_ids"] = [str(x) for x in links.get("task_ids") or []]
        out[stage.value] = items
    if len(_STAGE_CACHE) >= _STAGE_CACHE_MAX:
        _STAGE_CACHE.clear()
    _STAGE_CACHE[key] = out
    return out


def stage_blockers(session: Session, version: PlanVersion, *, task_id: str | None = None,
                   feature_key: str | None = None) -> list[dict[str, Any]]:
    """Required documentation still pending before implementation of *task_id* / *feature_key* starts: every
    PROJECT_CREATION required section, and the FEATURE_APPROVAL ones (PRD, test plan) of that feature/task.

    Applies to Product 1 plans (``product_home`` plus at least one feature); content without them (fixtures,
    task-only DAGs) has no Product 1 documents to be ready. Never invents a section; only reports pending ones."""
    content = version.content if isinstance(version.content, dict) else {}
    if not isinstance(content.get("product_home"), dict) or not content.get("features"):
        return []
    pending = _stage_pending(session, version)
    out = [{**it, "stage": p1.Timing.PROJECT_CREATION.value} for it in pending[p1.Timing.PROJECT_CREATION.value]]
    for it in pending[p1.Timing.FEATURE_APPROVAL.value]:
        if (feature_key and feature_slug(feature_key) in it["features"]) or (task_id and task_id in it["task_ids"]):
            out.append({**it, "stage": p1.Timing.FEATURE_APPROVAL.value})
    return out


_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
RC_KIND = "RELEASE_CANDIDATE"


def record_release_candidate(session: Session, settings: Settings, version: PlanVersion, *, release_key: str,
                             commit: str, actor_user_id: str, actor_type: str = "user") -> dict[str, Any]:
    """§28 release candidate: a CONSISTENT SNAPSHOT of the Product 1 set for one release.

    Binds the exact commit, the APPROVED PlanVersion (id + manifest hash) and the content hash of every current
    document (Release Plan incl. Release Notes, runbooks, ...) into one immutable DocumentRevision of kind
    RELEASE_CANDIDATE on the Release Plan. Refused (Conflict) while a required release section is documentation
    pending (§41 readiness). It records documentation state only — no readiness, deployment or sign-off claim."""
    commit = (commit or "").strip().lower()
    if not _SHA_RE.match(commit):
        raise ValidationFailed("commit must be a full git SHA (40 or 64 hex characters)", code="COMMIT_INVALID")
    project = session.get(Project, version.project_id)
    if project is None:
        raise NotFound("project not found")
    result, _delta, state = render_version(session, settings, version, persist=True, render_visuals=False)
    if not state.approved:
        raise Conflict("a release candidate needs the APPROVED plan version", code="PLAN_NOT_APPROVED",
                       details={"reason": state.reason})
    session.flush()
    key = feature_slug(release_key)
    lid = f"07-release-deployment/release-plan-{key}"
    records = {x.logical_doc_id: x for x in session.scalars(select(DocumentRecord).where(
        DocumentRecord.project_id == project.id))}
    rel = records.get(lid)
    if rel is None or rel.status == r.ST_SUPERSEDED:
        raise NotFound(f"release plan {release_key!r} is not part of the approved plan version",
                       details={"release_plans": sorted(k for k in records if k.startswith("07-release-deployment/"))})
    missing = p1.required_pending([rel], p1.Timing.RELEASE_CANDIDATE)
    if missing:
        raise Conflict("required Release Plan sections are still documentation pending", code="RELEASE_DOCS_INCOMPLETE",
                       details={"pending": missing})
    current = {d.logical_doc_id for d in result.documents}
    docs = sorted(((x.logical_doc_id, x.content_hash, x.status) for x in records.values()
                   if x.kind == "DOC" and x.logical_doc_id in current), key=lambda t: t[0])
    snapshot = {"project_id": project.id, "release": key, "commit": commit, "plan_version_id": version.id,
                "plan_manifest_hash": version.manifest_hash, "documents": [list(d) for d in docs]}
    snapshot_hash = content_hash(snapshot)
    existing = session.scalar(select(DocumentRevision).where(DocumentRevision.document_id == rel.id,
                                                             DocumentRevision.kind == RC_KIND,
                                                             DocumentRevision.content_hash == snapshot_hash))
    if existing is not None:
        return {"id": existing.id, "snapshot_hash": snapshot_hash, "documents": len(docs), "created": False,
                "release_plan": lid, "commit": commit}
    lines = [f"# Release candidate {key} @ {commit}", "",
             f"PlanVersion `{version.id}` v{version.version} (`{version.manifest_hash}`)", "",
             "| Dokumen | Content hash | Status |", "|---|---|---|"]
    lines += [f"| `{d[0]}` | `{d[1] or '-'}` | {d[2]} |" for d in docs]
    rev = DocumentRevision(document_id=rel.id, project_id=project.id, domain=project.domain, logical_doc_id=lid,
                           kind=RC_KIND, status="RECORDED", plan_version_id=version.id, content_hash=snapshot_hash,
                           body_markdown="\n".join(lines) + "\n", details=snapshot)
    session.add(rev)
    session.flush()
    rel.links = {**(rel.links or {}), "release_candidate": {"revision_id": rev.id, "commit": commit,
                                                             "snapshot_hash": snapshot_hash}}
    audit.record(session, actor_type=actor_type, actor_id=actor_user_id, action="documents.release_candidate",
                 target_type="document", target_id=rel.id, project_id=project.id, domain=project.domain,
                 details={"release": key, "commit": commit, "snapshot_hash": snapshot_hash, "documents": len(docs),
                          "plan_version_id": version.id})
    events.emit(session, source="docs_engine", kind="documents.release_candidate", entity_type="document",
                entity_id=rel.id, entity_version=snapshot_hash, project_id=project.id, domain=project.domain,
                payload={"release": key, "commit": commit, "documents": len(docs)})
    session.flush()
    return {"id": rev.id, "snapshot_hash": snapshot_hash, "documents": len(docs), "created": True,
            "release_plan": lid, "commit": commit}


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
