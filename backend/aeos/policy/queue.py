"""Human Decision Queue (U-12): one queue aggregated from canonical records — never a second
tracker. Each item says what waits, why, which decision is requested and where to act.

Sources: plans waiting for a human, publication previews awaiting approval, change proposals
awaiting a decision, task executions BLOCKED with NEEDS_DECISION, worker runs in REVIEW that need a
human, ACTIVE decisions about to expire, and open decision requests. Scope is the active domain
(and the principal's project scope); Office items never appear in a Personal queue (§35, §50).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.timeutil import age_seconds, ensure_aware, utcnow
from aeos.models.decisions import Decision
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.publishing import Publication
from aeos.models.research import ChangeProposal
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.policy import requests as decision_requests

EXPIRING_WITHIN = timedelta(days=7)

PLAN_WAITING: dict[str, tuple[str, str, int]] = {
    "READY_FOR_HUMAN_REVIEW": (
        "Empat tahap perencanaan selesai; versi rencana menunggu keputusan pemilik.",
        "Approve & Publish, Approve, Request Revision, atau Reject versi yang ditampilkan (terikat hash).", 1),
    "NEEDS_DECISION": (
        "Perencanaan berhenti dan menunggu keputusan manusia.",
        "Lanjutkan dari checkpoint setelah blocker diselesaikan, minta revisi, atau tolak.", 1),
    "AWAITING_MANUAL_CLAUDE": (
        "Bridge Claude native belum terbukti/tersedia; paket manual menunggu dijalankan di Claude Code resmi.",
        "Jalankan paket manual di Claude Code resmi, lalu impor hasilnya.", 2),
    "REVIEW_FAILED": (
        "Tahap review perencanaan gagal atau verdict FAIL; tidak ada tahap yang dilewati.",
        "Jalankan ulang review, minta revisi, atau tolak rencana.", 2),
    "PLANNING_FAILED": (
        "Tahap perencanaan gagal (output tidak valid, model tidak sesuai policy, atau error).",
        "Jalankan ulang dari checkpoint, minta revisi, atau tolak rencana.", 2),
    "PUBLICATION_PARTIAL": (
        "Sebagian target publikasi gagal; hasil yang berhasil tetap tercatat.",
        "Tinjau rekonsiliasi lalu putuskan retry terbatas atau hentikan.", 1),
    "PUBLICATION_FAILED": (
        "Publikasi gagal.", "Tinjau penyebab lalu putuskan retry atau revisi.", 2),
}

PROPOSAL_WAITING: dict[str, tuple[str, str]] = {
    "SUBMITTED": ("Usulan perubahan baru belum ditriase.", "Tinjau usulan dan tentukan apakah perlu review sumber/dampak."),
    "REVIEWED": ("Paket review usulan selesai (REVIEW/VERIFY/VERDICT bukan persetujuan).",
                 "Putuskan KEEP, REJECT, DEFER, REQUEST_MORE_EVIDENCE atau setujui scope trial."),
    "PROMOTION_PENDING": ("Trial selesai; promosi menunggu keputusan terikat hash kandidat/baseline.",
                          "Setujui atau tolak promosi untuk hash yang ditampilkan."),
    # §40: states that still need the owner although no button is "pending" — never silently finished.
    "UNDER_REVIEW": ("Usulan kembali ke review (binding baseline/kandidat berubah; keputusan lama dibatalkan).",
                     "Jalankan review ulang lalu putuskan lagi atas hash yang baru."),
    "TRIAL_APPROVED": ("Trial disetujui; menunggu bukti trial tepercaya sebelum promosi.",
                       "Lampirkan bukti trial (sandbox/eval) atau hentikan usulan."),
    "PROMOTION_APPROVED": ("Promosi disetujui; hasil implementasi (outcome) belum dicatat.",
                           "Catat outcome ACCEPTED/PARTIAL/FAILED/ROLLED_BACK dengan bukti tepercaya."),
    "PARTIAL": ("Sebagian target gagal (PARTIAL); bagian yang gagal belum direkonsiliasi.",
                "Rekonsiliasi setiap bagian gagal dengan resolusi + bukti tepercaya, atau catat ROLLED_BACK/FAILED. "
                "'Selesai' tidak membuka gate."),
}


def _iso(dt: datetime | None) -> str | None:
    dt = ensure_aware(dt)
    return dt.isoformat() if dt else None


def _item(*, kind: str, title: str, target: dict[str, Any], why: str, requested: str, link: str,
          created_at: datetime | None, project_id: str | None, domain: str | None, priority: int,
          extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": f"{kind}:{target.get('id')}",
        "kind": kind,
        "title": title,
        "target": target,
        "why": why,
        "requested_decision": requested,
        "link": link,
        "created_at": _iso(created_at),
        "age_seconds": age_seconds(created_at),
        "project_id": project_id,
        "domain": domain,
        "priority": priority,
        **(extra or {}),
    }



def queue_items(session: Session, domain: str | Domain, project_ids: list[str] | None = None) -> list[dict[str, Any]]:
    domain = Domain(domain).value
    items: list[dict[str, Any]] = []

    # Plans waiting for a human.
    q = select(Plan).where(Plan.domain == domain, Plan.state.in_(list(PLAN_WAITING)))
    if project_ids is not None:
        q = q.where(Plan.project_id.in_(project_ids))
    for plan in session.scalars(q):
        why, requested, prio = PLAN_WAITING[plan.state]
        version = session.get(PlanVersion, plan.current_version_id) if plan.current_version_id else None
        reason = plan.state_reason.strip() if plan.state_reason else ""
        items.append(_item(
            kind="PLAN", title=plan.title,
            target={"type": "plan", "id": plan.id, "state": plan.state,
                    "version_id": version.id if version else None, "version": version.version if version else None,
                    "hash": version.manifest_hash if version else None},
            why=f"{why} {reason}".strip(), requested=requested, link=f"/plans/{plan.id}",
            created_at=plan.updated_at, project_id=plan.project_id, domain=plan.domain, priority=prio,
        ))

    # Publication previews awaiting human approval.
    q = select(Publication).where(Publication.domain == domain, Publication.status == "PREVIEW")
    if project_ids is not None:
        q = q.where(Publication.project_id.in_(project_ids))
    for pub in session.scalars(q):
        items.append(_item(
            kind="PUBLICATION", title="Pratinjau publikasi menunggu persetujuan",
            target={"type": "publication", "id": pub.id, "version_id": pub.plan_version_id, "hash": pub.approved_hash},
            why="Pratinjau target eksternal sudah dibuat; belum ada efek eksternal.",
            requested="Tinjau pratinjau target/operasi lalu setujui atau batalkan publikasi.",
            link=f"/publications?id={pub.id}", created_at=pub.created_at, project_id=pub.project_id,
            domain=pub.domain, priority=1, extra={"mode": pub.mode},
        ))

    # Change proposals awaiting a decision (platform proposals belong to the PERSONAL platform project).
    q = select(ChangeProposal).where(ChangeProposal.status.in_(list(PROPOSAL_WAITING)))
    if domain == Domain.PERSONAL.value:
        q = q.where(or_(ChangeProposal.domain == domain, ChangeProposal.domain.is_(None)))
    else:
        q = q.where(ChangeProposal.domain == domain)
    if project_ids is not None:
        q = q.where(or_(ChangeProposal.project_id.in_(project_ids), ChangeProposal.project_id.is_(None)))
    for cp in session.scalars(q):
        why, requested = PROPOSAL_WAITING[cp.status]
        items.append(_item(
            kind="CHANGE_PROPOSAL", title=cp.title,
            target={"type": "change_proposal", "id": cp.id, "status": cp.status, "hash": cp.candidate_hash},
            why=why, requested=requested, link=f"/proposals/{cp.id}", created_at=cp.updated_at,
            project_id=cp.project_id, domain=cp.domain, priority=3, extra={"proposal_kind": cp.kind},
        ))

    # Task executions blocked on a human decision.
    q = (select(TaskExecution, TaskSpec, PlanVersion)
         .join(TaskSpec, TaskSpec.id == TaskExecution.task_id)
         .join(PlanVersion, PlanVersion.id == TaskSpec.plan_version_id)
         .where(TaskExecution.domain == domain, TaskExecution.state == "BLOCKED",
                TaskExecution.reason.contains("NEEDS_DECISION")))
    if project_ids is not None:
        q = q.where(TaskExecution.project_id.in_(project_ids))
    for te, spec, version in session.execute(q):
        items.append(_item(
            kind="TASK_BLOCKED", title=f"{spec.task_key}: {spec.title}",
            target={"type": "task", "id": spec.id, "task_key": spec.task_key, "hash": spec.spec_hash},
            why=te.reason or "Task BLOCKED menunggu keputusan.",
            requested="Putuskan: perpanjang batas/attempt dalam policy, ubah kontrak (versi rencana baru), atau batalkan.",
            link=f"/dag?plan={version.plan_id}&task={spec.task_key}", created_at=te.updated_at,
            project_id=te.project_id, domain=te.domain, priority=1,
        ))

    # Worker runs in REVIEW that explicitly need a human.
    q = select(WorkerRun).where(
        WorkerRun.domain == domain, WorkerRun.execution_status == "REVIEW",
        or_(WorkerRun.block_reason.contains("NEEDS_HUMAN"), WorkerRun.block_reason.contains("NEEDS_DECISION"),
            WorkerRun.review_verdict.in_(["BLOCKED", "NOT-PROVEN"])),
    )
    if project_ids is not None:
        q = q.where(WorkerRun.project_id.in_(project_ids))
    for run in session.scalars(q):
        items.append(_item(
            kind="RUN_REVIEW", title=f"Run {run.id[:8]} ({run.role_key or run.run_type}) menunggu keputusan",
            target={"type": "worker_run", "id": run.id, "task_id": run.task_id, "verdict": run.review_verdict},
            why=run.block_reason or f"Verdict review {run.review_verdict}; bukti belum cukup untuk penerimaan.",
            requested="Tinjau bukti dan temuan, lalu minta repair, minta bukti tambahan, atau hentikan.",
            link=f"/runs/{run.id}", created_at=run.updated_at, project_id=run.project_id, domain=run.domain,
            priority=2, extra={"mode": run.mode},
        ))

    # ACTIVE decisions expiring soon (global decisions without a domain appear in both domains).
    horizon = utcnow() + EXPIRING_WITHIN
    q = select(Decision).where(Decision.status == "ACTIVE", Decision.expires_at.is_not(None),
                               or_(Decision.domain == domain, Decision.domain.is_(None)))
    if project_ids is not None:
        q = q.where(or_(Decision.project_id.in_(project_ids), Decision.project_id.is_(None)))
    now = utcnow()
    for d in session.scalars(q):
        exp = ensure_aware(d.expires_at)
        if exp is None or exp <= now or exp > horizon:
            continue
        items.append(_item(
            kind="DECISION_EXPIRING", title=f"Keputusan {d.kind} akan kedaluwarsa",
            target={"type": "decision", "id": d.id, "target_type": d.target_type, "target_id": d.target_id,
                    "hash": d.target_hash},
            why=f"Berlaku sampai {exp.isoformat()}; setelah itu operasi yang bergantung padanya ditolak (fail closed).",
            requested="Perpanjang dengan keputusan baru (scope eksplisit) atau biarkan kedaluwarsa.",
            link=f"/decisions?decision={d.id}", created_at=d.created_at, project_id=d.project_id, domain=d.domain,
            priority=2, extra={"expires_at": exp.isoformat()},
        ))

    # Owner delegations recorded from an ADR (ADR-0003 seed exception) that have no machine expiry: the owner
    # sees them as pending so the bounded delegation is ended deliberately (§09), never forgotten.
    q = select(Decision).where(Decision.status == "ACTIVE", Decision.actor_kind == "HUMAN_RECORDED",
                               Decision.expires_at.is_(None), or_(Decision.domain == domain, Decision.domain.is_(None)))
    if project_ids is not None:
        q = q.where(or_(Decision.project_id.in_(project_ids), Decision.project_id.is_(None)))
    for d in session.scalars(q):
        until = str((d.details or {}).get("expires_when") or "pemilik mencabutnya")
        items.append(_item(
            kind="DELEGATION_ACTIVE", title=f"Delegasi {d.actor_user_id} masih aktif",
            target={"type": "decision", "id": d.id, "target_type": d.target_type, "target_id": d.target_id,
                    "hash": d.target_hash},
            why=f"Pengecualian {d.kind} ({', '.join(d.allowed_operations or [])}) tidak memiliki tanggal kedaluwarsa; "
                f"berlaku sampai: {until}. Akhirnya tidak ditegakkan mesin.",
            requested="Cabut delegasi bila syarat berakhirnya sudah tercapai (mis. seed mencapai M4), atau biarkan "
                      "aktif secara sadar.",
            link=f"/decisions?decision={d.id}", created_at=d.created_at, project_id=d.project_id, domain=d.domain,
            priority=3, extra={"expires_when": until},
        ))

    # Open decision requests (exception/risk/budget/...).
    for r in decision_requests.open_requests(session, domain=domain):
        if project_ids is not None and r.project_id is not None and r.project_id not in project_ids:
            continue
        items.append(_item(
            kind="DECISION_REQUEST", title=r.title,
            target={"type": "decision_request", "id": r.id, "request_kind": r.kind, "target_type": r.target_type,
                    "target_id": r.target_id, "hash": r.target_hash},
            why=r.reason or "Permintaan keputusan terbuka.",
            requested=f"Catat keputusan {r.kind} dengan rationale dan scope eksplisit, atau tolak permintaan.",
            link=f"/decisions?request={r.id}", created_at=r.created_at, project_id=r.project_id, domain=r.domain,
            priority=2, extra={"requested_by": r.requested_by, "requested_by_kind": r.requested_by_kind},
        ))

    items.sort(key=lambda i: (i["priority"], i["created_at"] or ""))
    return items


def counts(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i["kind"]] = out.get(i["kind"], 0) + 1
    return out
