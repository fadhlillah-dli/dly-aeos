"""Project retirement workflow (§33 retirement, §10 Operations "retirement workflow", §16 Retirement
Assistant).

§33: "Retirement memetakan consumer/dependency, data retention/disposal, credential/IAM/repo/
integration/job/infra closure, legal hold, backup closure, communication, dan verifikasi final oleh
pemilik yang sah. Tidak ada agent yang menghapus data, resource produksi atau backup secara otonom."

* ``checklist`` is a read-only projection of what exists for a project and which owner actions remain;
  it never deletes, revokes or archives anything. Its ``checklist_hash`` covers every item except the
  final verification itself.
* The tracked workflow (``ops_retirements``) records per-step status/owner/evidence ids and the legal
  hold. A legal hold makes the data and backup items BLOCKING.
* ``record_decision`` records the owner's final verification through ``aeos.policy.retirement`` (one
  decision mechanism: kind RETIREMENT on ``project/<id>``, bound to the checklist hash, refused while any
  item is BLOCKING or the project is not RETIRING) and links it to the workflow record. ``portfolio``
  refuses lifecycle RETIRED without an ACTIVE decision for the *current* hash
  (``require_retirement_decision``), so any checklist change after the decision invalidates it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.errors import NotFound, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.identity import SystemSetting
from aeos.models.operations_ext import OpsRetirement
from aeos.models.portfolio import Attachment, Project, ProjectIntegration, RepositoryReference
from aeos.models.publishing import ExternalReference, Publication
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import TaskExecution

TERMINAL = ("DONE", "FAILED", "CANCELLED")
STEP_STATUSES = ("IN_PROGRESS", "DONE", "NOT_APPLICABLE")
CI_PROVIDERS = ("github-actions", "bitbucket-pipelines")


def _count(session: Session, model: Any, *conds: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(*conds)) or 0)


def record_of(session: Session, project_id: str) -> OpsRetirement | None:
    return session.get(OpsRetirement, project_id)


def _legal_hold(session: Session, project_id: str) -> dict[str, Any] | None:
    rec = record_of(session, project_id)
    if rec is not None and isinstance(rec.legal_hold, dict):
        return rec.legal_hold
    legacy = session.get(SystemSetting, f"retirement.legal_hold.{project_id}")
    if legacy is not None and legacy.value:
        return legacy.value if isinstance(legacy.value, dict) else {"active": bool(legacy.value)}
    return None


def _open_prs(session: Session, project_id: str) -> list[dict[str, Any]]:
    try:
        from aeos.models.delivery_ext import PullRequestRecord
    except ImportError:
        return []
    rows = session.scalars(select(PullRequestRecord).where(PullRequestRecord.project_id == project_id,
                                                           PullRequestRecord.state.in_(["PREPARED", "OPEN"])))
    return [{"repo": r.repo_full_name, "number": r.number, "state": r.state, "head_ref": r.head_ref} for r in rows]


def _pushed_branches(session: Session, project_id: str) -> list[dict[str, Any]]:
    try:
        from aeos.models.broker_ext import BrokerSideEffect
    except ImportError:
        return []
    rows = session.scalars(select(BrokerSideEffect).where(BrokerSideEffect.project_id == project_id,
                                                          BrokerSideEffect.kind == "PUSH_BRANCH",
                                                          BrokerSideEffect.status == "SUCCEEDED"))
    out = []
    for r in rows:
        tgt = r.target or {}
        out.append({"repo": tgt.get("repo"), "branch": tgt.get("branch")})
    return out


def _write_enablements(session: Session, integration_ids: list[str]) -> dict[str, str]:
    if not integration_ids:
        return {}
    from aeos.core.timeutil import ensure_aware

    now = utcnow()
    rows = session.scalars(select(Decision).where(Decision.kind == "WRITE_ENABLEMENT", Decision.status == "ACTIVE",
                                                  Decision.target_id.in_(integration_ids)))
    return {d.target_id: d.id for d in rows if d.expires_at is None or ensure_aware(d.expires_at) > now}


def _workspaces(settings: Settings, project: Project) -> int:
    from aeos.core.enums import Domain

    try:
        root = settings.layout.task_data(Domain(project.domain), project.project_key)
    except ValueError:
        return 0
    return sum(1 for p in root.iterdir() if p.is_dir()) if root.is_dir() else 0


def checklist(session: Session, settings: Settings, project: Project) -> dict[str, Any]:
    pid = project.id
    repos = list(session.scalars(select(RepositoryReference).where(RepositoryReference.project_id == pid)))
    integrations = list(session.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == pid)))
    open_tasks = _count(session, TaskExecution, TaskExecution.project_id == pid, TaskExecution.state.not_in(TERMINAL))
    active_grants = _count(session, CapabilityGrant, CapabilityGrant.project_id == pid, CapabilityGrant.active.is_(True),
                           CapabilityGrant.revoked_at.is_(None))
    pending_pubs = _count(session, Publication, Publication.project_id == pid,
                          Publication.status.in_(["PREVIEW", "PENDING", "RUNNING", "PARTIAL", "CONFLICT"]))
    ext_refs = list(session.scalars(select(ExternalReference).where(ExternalReference.project_id == pid)))
    publications = list(session.scalars(select(Publication).where(Publication.project_id == pid)))
    attachments = _count(session, Attachment, Attachment.project_id == pid)
    evidence = _count(session, EvidenceArtifact, EvidenceArtifact.project_id == pid)
    hold = _legal_hold(session, pid)
    hold_active = bool(hold and hold.get("active"))
    rec = record_of(session, pid)
    steps = (rec.steps if rec is not None else None) or {}
    from aeos.operations import backup, retention

    key_prefix = f"projects/{project.project_key}/"
    backups_with_project = 0
    for b in backup.list_backups(settings):
        try:
            manifest = backup.load_manifest(settings, str(b.get("backup_id")))
        except (NotFound, ValidationFailed):
            continue
        if any(f.get("path", "").startswith(key_prefix)
               for a in manifest.get("archives") or [] if a.get("domain") == project.domain
               for f in a.get("files") or []):
            backups_with_project += 1
    policy = retention.load_policy()
    data_rules = {c: retention.resolve(policy, c, project.domain) for c in retention.DATA_CLASSES}
    open_prs = _open_prs(session, pid)
    branches = _pushed_branches(session, pid)
    enablements = _write_enablements(session, [i.id for i in integrations])
    ci_integrations = [f"{i.provider}:{i.purpose}" for i in integrations
                       if i.purpose in ("ci", "staging", "deploy") or i.provider in CI_PROVIDERS]
    workspaces = _workspaces(settings, project)

    def item(key: str, title: str, status: str, detail: dict[str, Any], action: str) -> dict[str, Any]:
        recorded = steps.get(key)
        return {"key": key, "title": title, "status": status, "detail": detail, "owner_action": action,
                "recorded": recorded}

    items = [
        item("consumers", "Consumer dan dependency",
             "REVIEW_REQUIRED" if repos or integrations or ext_refs or publications else "NONE_RECORDED",
             {"repositories": [r.full_name for r in repos],
              "integrations": [f"{i.provider}:{i.purpose}" for i in integrations],
              "external_targets": sorted({f"{e.provider}:{e.logical_id}" for e in ext_refs}),
              "publication_targets": sorted({str(t.get("provider") or t.get("target") or t)
                                             for p in publications for t in (p.targets or [])
                                             if isinstance(t, dict)})},
             "Petakan consumer/downstream setiap repo, integrasi dan target publikasi sebelum penutupan."),
        item("jobs", "Pekerjaan dan job aktif", "BLOCKING" if open_tasks or pending_pubs else "NONE_RECORDED",
             {"open_tasks": open_tasks, "pending_publications": pending_pubs},
             "Selesaikan/batalkan task dan publikasi yang masih berjalan."),
        item("credentials", "Credential/IAM/capability", "BLOCKING" if active_grants else (
            "OWNER_ACTION" if any(i.credential_ref for i in integrations) else "NONE_RECORDED"),
             {"active_capability_grants": active_grants,
              "credential_refs": sorted({i.credential_ref for i in integrations if i.credential_ref})},
             "Cabut capability aktif; rotasi/cabut credential pada provider secara manual (nilai tidak ditampilkan)."),
        item("repositories", "Penutupan repository",
             "BLOCKING" if open_prs else ("OWNER_ACTION" if repos else "NONE_RECORDED"),
             {"repositories": [r.full_name for r in repos], "open_pull_requests": open_prs,
              "aeos_branches": branches},
             "Tutup/merge PR yang masih terbuka; arsipkan atau pindahkan repository di provider secara manual."),
        item("integrations", "Penutupan integrasi",
             "BLOCKING" if enablements else ("OWNER_ACTION" if integrations else "NONE_RECORDED"),
             {"integrations": [{"id": i.id, "provider": i.provider, "purpose": i.purpose,
                                "write_enablement_decision": enablements.get(i.id)} for i in integrations]},
             "Biarkan keputusan WRITE_ENABLEMENT kedaluwarsa atau batalkan; nonaktifkan integrasi di provider."),
        item("infra", "Penutupan infra/runtime", "OWNER_ACTION" if workspaces or ci_integrations else "NONE_RECORDED",
             {"worker_workspaces": workspaces, "ci_or_staging_integrations": ci_integrations},
             "Hapus workspace worker dan pipeline/staging proyek secara manual bila sudah tidak diperlukan."),
        item("data", "Retensi dan disposal data", "BLOCKING" if hold_active else "OWNER_DECISION_REQUIRED",
             {"attachments": attachments, "evidence_artifacts": evidence, "external_references": len(ext_refs),
              "retention_policy": policy.get("status"), "rules": data_rules, "legal_hold_active": hold_active},
             "Disposal hanya manual sesuai aturan retensi; evidence append-only tidak dihapus otomatis."
             + (" Legal hold aktif: tidak ada disposal." if hold_active else "")),
        item("legal_hold", "Legal hold", "RECORDED" if hold else "UNKNOWN",
             {"legal_hold": hold},
             "Catat apakah ada legal hold sebelum disposal apa pun (aeos ops retirement legal-hold)."),
        item("backups", "Penutupan backup",
             "BLOCKING" if hold_active and backups_with_project else (
                 "OWNER_DECISION_REQUIRED" if backups_with_project else "NONE_RECORDED"),
             {"backups_containing_project_files": backups_with_project, "legal_hold_active": hold_active},
             "Backup tidak pernah dihapus otomatis; putuskan penanganannya secara manual."),
        item("communication", "Komunikasi", "OWNER_ACTION", {},
             "Informasikan stakeholder dan pemilik integrasi tentang retirement."),
    ]
    project_view = {"id": project.id, "name": project.name, "domain": project.domain, "lifecycle": project.lifecycle}
    # §33 final verification: an ACTIVE RETIREMENT decision bound to the hash of exactly these items
    # (aeos.policy.retirement). A checklist that changed after the owner verified it is STALE, not VERIFIED.
    # Recording a workflow step (``recorded``) is not part of the hash; a status/detail change is.
    from aeos.policy import retirement as owner_verification

    current_hash = owner_verification.checklist_hash({"project": project_view, "items": items})
    final = owner_verification.binding_status(session, project, current_hash=current_hash)
    items.append(item("final_verification", "Verifikasi akhir pemilik", final["status"],
                      {"decision_id": final["decision_id"], "bound_hash": final["bound_hash"]},
                      "Pemilik yang sah mencatat keputusan RETIREMENT (terikat hash daftar periksa ini) setelah semua "
                      "butir di atas selesai; RETIRED hanya dapat dicapai setelahnya."))
    blocking = [i["key"] for i in items if i["status"] == "BLOCKING"]
    return {"project": project_view, "items": items, "blocking": blocking, "checklist_hash": current_hash,
            "workflow": {"status": rec.status if rec else "NOT_STARTED", "decision_id": rec.decision_id if rec else None},
            "notice": "Daftar periksa read-only. Tidak ada data, resource atau backup yang dihapus otomatis (§33)."}


def _record(session: Session, project_id: str) -> OpsRetirement:
    rec = record_of(session, project_id)
    if rec is None:
        rec = OpsRetirement(project_id=project_id, status="IN_PROGRESS", steps={}, legal_hold=None, updated_by="")
        session.add(rec)
        session.flush()
    return rec


def set_legal_hold(session: Session, project: Project, *, active: bool, reason: str, actor: str) -> dict[str, Any]:
    if len((reason or "").strip()) < 5:
        raise ValidationFailed("a legal hold change needs a reason (at least 5 characters)")
    from aeos.audit import service as audit

    rec = _record(session, project.id)
    previous = rec.legal_hold
    rec.legal_hold = {"active": bool(active), "reason": redact_text(reason.strip(), pii=True)[:1000],
                      "actor": actor[:128], "at": utcnow().isoformat()}
    rec.updated_by = actor[:128]
    session.flush()
    audit.record(session, actor_type="user" if not actor.startswith("cli") else "cli", actor_id=actor,
                 action="ops.retirement.legal_hold", target_type="project", target_id=project.id,
                 project_id=project.id, domain=project.domain,
                 details={"active": bool(active), "previous_active": bool(previous and previous.get("active"))})
    return rec.legal_hold


def record_step(session: Session, project: Project, key: str, *, status: str, actor: str, owner: str = "",
                evidence_ids: list[str] | None = None, note: str = "") -> dict[str, Any]:
    keys = {"consumers", "jobs", "credentials", "repositories", "integrations", "infra", "data", "legal_hold",
            "backups", "communication"}
    if key not in keys:
        raise ValidationFailed(f"unknown retirement step {key!r}", details={"steps": sorted(keys)})
    if status not in STEP_STATUSES:
        raise ValidationFailed(f"status must be one of {STEP_STATUSES}")
    for ev_id in evidence_ids or []:
        ev = session.get(EvidenceArtifact, ev_id)
        # only evidence of the project's own domain (or platform evidence) can back a step: the other domain's
        # records never enter this project's retirement record (domain-first)
        if ev is None or ev.domain not in (None, project.domain):
            raise ValidationFailed(f"evidence {ev_id} does not exist", code="EVIDENCE_NOT_FOUND")
    from aeos.audit import service as audit

    rec = _record(session, project.id)
    entry = {"status": status, "owner": redact_text(owner or actor, pii=True)[:200],
             "evidence_ids": list(evidence_ids or []), "note": redact_text(note or "", pii=True)[:1000],
             "by": actor[:128], "at": utcnow().isoformat()}
    rec.steps = {**(rec.steps or {}), key: entry}
    rec.updated_by = actor[:128]
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor, action="ops.retirement.step", target_type="project",
                 target_id=project.id, project_id=project.id, domain=project.domain,
                 details={"step": key, "status": status, "evidence_ids": entry["evidence_ids"]})
    return entry


def record_decision(session: Session, settings: Settings, project: Project, *, actor_user_id: str,
                    checklist_hash_seen: str | None, rationale: str) -> Decision:
    """The owner's final retirement verification (``aeos.policy.retirement.record_retirement``: refused while an
    item is BLOCKING, the project is not RETIRING or the checklist changed since it was shown), linked to the
    tracked workflow record. Never commits."""
    from aeos.policy import retirement as owner_verification

    decision = owner_verification.record_retirement(session, project=project, actor_user_id=actor_user_id,
                                                    rationale=rationale, expected_hash=checklist_hash_seen,
                                                    settings=settings)
    rec = _record(session, project.id)
    rec.checklist_hash = decision.target_hash
    rec.decision_id = decision.id
    rec.updated_by = actor_user_id[:128]
    session.flush()
    return decision


def require_retirement_decision(session: Session, settings: Settings, project: Project) -> Decision:
    """Refuse lifecycle RETIRED unless an ACTIVE owner RETIREMENT decision is bound to the *current* checklist
    (``RETIREMENT_NOT_VERIFIED``); the workflow record then shows RETIRED."""
    from aeos.policy import retirement as owner_verification

    decision = owner_verification.require_retirement_verified(session, project, settings=settings)
    rec = _record(session, project.id)
    rec.status = "RETIRED"
    rec.decision_id = decision.id
    return decision
