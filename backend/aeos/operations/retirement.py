"""Project retirement checklist (§33 retirement, §10 Operations "retirement workflow").

§33: "Retirement memetakan consumer/dependency, data retention/disposal, credential/IAM/repo/
integration/job/infra closure, legal hold, backup closure, communication, dan verifikasi final oleh
pemilik yang sah. Tidak ada agent yang menghapus data, resource produksi atau backup secara otonom."

This is a read-only projection: it maps what exists for a project and which owner actions remain.
It never deletes, revokes or archives anything.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.errors import NotFound, ValidationFailed
from aeos.models.evidence import EvidenceArtifact
from aeos.models.identity import SystemSetting
from aeos.models.portfolio import Attachment, Project, ProjectIntegration, RepositoryReference
from aeos.models.publishing import ExternalReference, Publication
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import TaskExecution

TERMINAL = ("DONE", "FAILED", "CANCELLED")


def _count(session: Session, model: Any, *conds: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(*conds)) or 0)


def checklist(session: Session, settings: Settings, project: Project) -> dict[str, Any]:
    pid = project.id
    repos = list(session.scalars(select(RepositoryReference).where(RepositoryReference.project_id == pid)))
    integrations = list(session.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == pid)))
    open_tasks = _count(session, TaskExecution, TaskExecution.project_id == pid, TaskExecution.state.not_in(TERMINAL))
    active_grants = _count(session, CapabilityGrant, CapabilityGrant.project_id == pid, CapabilityGrant.active.is_(True),
                           CapabilityGrant.revoked_at.is_(None))
    pending_pubs = _count(session, Publication, Publication.project_id == pid,
                          Publication.status.in_(["PREVIEW", "PENDING", "RUNNING", "PARTIAL", "CONFLICT"]))
    external_refs = _count(session, ExternalReference, ExternalReference.project_id == pid)
    attachments = _count(session, Attachment, Attachment.project_id == pid)
    evidence = _count(session, EvidenceArtifact, EvidenceArtifact.project_id == pid)
    legal_hold = session.get(SystemSetting, f"retirement.legal_hold.{pid}")
    from aeos.operations import backup

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

    def item(key: str, title: str, status: str, detail: dict[str, Any], action: str) -> dict[str, Any]:
        return {"key": key, "title": title, "status": status, "detail": detail, "owner_action": action}

    items = [
        item("consumers", "Consumer dan dependency", "REVIEW_REQUIRED" if repos or integrations else "NONE_RECORDED",
             {"repositories": [r.full_name for r in repos], "integrations": [f"{i.provider}:{i.purpose}" for i in integrations]},
             "Petakan consumer/downstream setiap repo dan integrasi sebelum penutupan."),
        item("jobs", "Pekerjaan dan job aktif", "BLOCKING" if open_tasks or pending_pubs else "NONE_RECORDED",
             {"open_tasks": open_tasks, "pending_publications": pending_pubs},
             "Selesaikan/batalkan task dan publikasi yang masih berjalan."),
        item("credentials", "Credential/IAM/capability", "BLOCKING" if active_grants else (
            "OWNER_ACTION" if any(i.credential_ref for i in integrations) else "NONE_RECORDED"),
             {"active_capability_grants": active_grants,
              "credential_refs": sorted({i.credential_ref for i in integrations if i.credential_ref})},
             "Cabut capability aktif; rotasi/cabut credential pada provider secara manual (nilai tidak ditampilkan)."),
        item("data", "Retensi dan disposal data", "OWNER_DECISION_REQUIRED",
             {"attachments": attachments, "evidence_artifacts": evidence, "external_references": external_refs},
             "Tetapkan retensi/disposal sesuai kebijakan; evidence bersifat append-only dan tidak dihapus otomatis."),
        item("legal_hold", "Legal hold", "RECORDED" if legal_hold and legal_hold.value else "UNKNOWN",
             {"legal_hold": legal_hold.value if legal_hold else None},
             "Pastikan apakah ada legal hold sebelum disposal apa pun."),
        item("backups", "Penutupan backup", "OWNER_DECISION_REQUIRED" if backups_with_project else "NONE_RECORDED",
             {"backups_containing_project_files": backups_with_project},
             "Backup tidak pernah dihapus otomatis; putuskan penanganannya secara manual."),
        item("communication", "Komunikasi", "OWNER_ACTION", {},
             "Informasikan stakeholder dan pemilik integrasi tentang retirement."),
    ]
    project_view = {"id": project.id, "name": project.name, "domain": project.domain, "lifecycle": project.lifecycle}
    # §33 final verification: an ACTIVE RETIREMENT decision bound to the hash of exactly these items
    # (aeos.policy.retirement). A checklist that changed after the owner verified it is STALE, not VERIFIED.
    from aeos.policy import retirement as owner_verification

    current_hash = owner_verification.checklist_hash({"project": project_view, "items": items})
    final = owner_verification.binding_status(session, project, current_hash=current_hash)
    items.append(item("final_verification", "Verifikasi akhir pemilik", final["status"],
                      {"decision_id": final["decision_id"], "bound_hash": final["bound_hash"]},
                      "Pemilik yang sah mencatat keputusan RETIREMENT (terikat hash daftar periksa ini) setelah semua "
                      "butir di atas selesai; RETIRED hanya dapat dicapai setelahnya."))
    blocking = [i["key"] for i in items if i["status"] == "BLOCKING"]
    return {"project": project_view, "items": items, "blocking": blocking, "checklist_hash": current_hash,
            "notice": "Daftar periksa read-only. Tidak ada data, resource atau backup yang dihapus otomatis (§33)."}
