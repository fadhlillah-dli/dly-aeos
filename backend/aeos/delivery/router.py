"""Delivery & CI + Governance handoff API (§31, §32). There is no merge or deploy endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, NotFound
from aeos.core.timeutil import utcnow
from aeos.delivery import gates, governance, pr_bundle, provider, sync
from aeos.identity.service import Principal
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import EvidenceArtifact
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project, RepositoryReference
from aeos.verification import collector
from aeos.verification.router import audit_denied, visible_project_ids

router = APIRouter(tags=["delivery"])


def _load_task(db: Session, principal: Principal, domain: Domain, task_id: str) -> TaskSpec:
    task = db.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    load_project(db, principal, task.project_id, domain=domain)
    return task


class PreparePRIn(BaseModel):
    base_branch: str | None = Field(default=None, max_length=200)


@router.post("/api/tasks/{task_id}/prepare-pr")
def prepare_pr(task_id: str, body: PreparePRIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    task = _load_task(db, principal, domain, task_id)
    try:
        result = pr_bundle.prepare_pr(db, task.id, actor_user_id=principal.user.id, base_branch=body.base_branch)
    except AeosError as exc:
        audit_denied(db, principal, "delivery.pr.prepare", exc, target_type="task", target_id=task.id,
                     project_id=task.project_id, domain=task.domain)
        raise
    db.commit()
    return result


@router.get("/api/delivery/bundle")
def preview_bundle(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, task_id: str) -> dict[str, Any]:
    task = _load_task(db, principal, domain, task_id)
    bundle = pr_bundle.build_pr_bundle(db, task.id)
    return {"task_id": task.id, "bundle_hash": bundle.bundle_hash, "markdown": bundle.markdown,
            "checked_at": utcnow().isoformat(), "source": "acceptance + evidence ledger", "mode": "LIVE"}


@router.get("/api/delivery/gates")
def delivery_gates(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, task_id: str) -> dict[str, Any]:
    task = _load_task(db, principal, domain, task_id)
    return gates.compute_gates(db, task.id)


def _repo_for(db: Session, principal: Principal, domain: Domain, repo: str | None) -> tuple[RepositoryReference, Project]:
    ids = visible_project_ids(db, principal, domain)
    q = select(RepositoryReference).where(RepositoryReference.project_id.in_(ids),
                                          RepositoryReference.provider.in_(["GITHUB", "github"]))
    if repo:
        provider.validate_repo(repo)
        q = q.where(RepositoryReference.full_name == repo)
    rows = list(db.scalars(q))
    if not rows:
        raise NotFound("no registered GitHub repository in this domain" + (f" named {repo}" if repo else ""))
    rows.sort(key=lambda r: 0 if r.role == "platform" else 1)
    ref = rows[0]
    project = db.get(Project, ref.project_id)
    if project is None:
        raise NotFound("project not found")
    return ref, project


@router.get("/api/delivery/provider-capabilities")
def provider_capabilities(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, repo: str | None = None,
                          branch: str | None = Query(None, max_length=200)) -> dict[str, Any]:
    ref, project = _repo_for(db, principal, domain, repo)
    token, credential = provider.github_token(project.domain)
    caps = provider.check_capabilities(ref.full_name, branch or ref.default_branch, token=token, credential=credential)
    out = caps.to_dict()
    out["checked_at"] = caps.checked_at
    out["project_id"] = project.id
    return out


class CapabilityRecordIn(BaseModel):
    repo: str | None = None
    branch: str | None = Field(default=None, max_length=200)


@router.post("/api/delivery/provider-capabilities/record")
def record_capabilities(body: CapabilityRecordIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    ref, project = _repo_for(db, principal, domain, body.repo)
    token, credential = provider.github_token(project.domain)
    caps = provider.check_capabilities(ref.full_name, body.branch or ref.default_branch, token=token,
                                       credential=credential)
    ev = provider.record_capability_evidence(db, project, caps)
    db.commit()
    out = caps.to_dict()
    out["evidence_id"] = ev.id
    return out


@router.get("/api/delivery/prs")
def list_prs(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, task_id: str | None = None,
             limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    ids = visible_project_ids(db, principal, domain)
    q = select(PullRequestRecord).where(PullRequestRecord.project_id.in_(ids))
    if task_id:
        q = q.where(PullRequestRecord.task_id == task_id)
    rows = db.scalars(q.order_by(PullRequestRecord.created_at.desc()).limit(limit)).all()
    latest_sync = max((r.last_synced_at for r in rows if r.last_synced_at), default=None)
    return {"items": [gates.pr_view(r) for r in rows], "source": "delivery_pull_requests (provider observations)",
            "checked_at": latest_sync.isoformat() if latest_sync else None, "mode": "LIVE",
            "merge_authority": "HUMAN_ONLY"}


@router.post("/api/delivery/prs/{pr_id}/sync")
def sync_pr(pr_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    pr = db.get(PullRequestRecord, pr_id)
    if pr is None or pr.project_id not in visible_project_ids(db, principal, domain):
        raise NotFound("pull request record not found")
    result = sync.sync_pull_request(db, pr.id, actor=principal.user.id)
    db.commit()
    return result


class HandoffIn(BaseModel):
    artifact_hash: str | None = Field(default=None, max_length=80)


@router.post("/api/delivery/governance/{task_id}/handoff")
def governance_handoff(task_id: str, body: HandoffIn, principal: AdminPrincipal, domain: ActiveDomain,
                       db: DB) -> dict[str, Any]:
    task = _load_task(db, principal, domain, task_id)
    result = governance.handoff(db, task.id, actor=principal.user.id, artifact_hash=body.artifact_hash)
    db.commit()
    return result


@router.get("/api/delivery/governance")
def governance_status(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, task_id: str) -> dict[str, Any]:
    task = _load_task(db, principal, domain, task_id)
    ev = db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.task_id == task.id,
                                                  EvidenceArtifact.kind == "GOVERNANCE_HANDOFF")
                   .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    content = collector.read_json(ev) if ev else None
    return {"task_id": task.id, "adapter": governance.configured_adapter().name,
            "contract_version": governance.CONTRACT_VERSION, "latest": content,
            "evidence_id": ev.id if ev else None, "checked_at": ev.created_at.isoformat() if ev else None,
            "source": "evidence_artifacts (GOVERNANCE_HANDOFF)", "mode": "LIVE"}
