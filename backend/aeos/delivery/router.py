"""Delivery & CI + Governance handoff API (§31, §32). There is no merge or deploy endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, Blocked, NotFound
from aeos.core.timeutil import utcnow
from aeos.delivery import bitbucket, gates, governance, pr_bundle, provider, sync
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
        if exc.code in pr_bundle.PERSISTED_FAILURE_CODES:
            # Keep the acceptance invalidation (STALE) and the broker outbox/audit rows of a FAILED or
            # UNKNOWN push/PR: they are what reconciliation reads before any retry (§14) —
            # audit_denied rolls back otherwise.
            db.commit()
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
    # U-02..U-04: PERSONAL code lives on GitHub, OFFICE code on Bitbucket (ADR-0111).
    office = domain == Domain.OFFICE
    host = "Bitbucket" if office else "GitHub"
    ids = visible_project_ids(db, principal, domain)
    q = select(RepositoryReference).where(RepositoryReference.project_id.in_(ids),
                                          RepositoryReference.provider.in_([host.upper(), host.lower()]))
    if repo:
        (bitbucket.validate_repo if office else provider.validate_repo)(repo)
        q = q.where(RepositoryReference.full_name == repo)
    rows = list(db.scalars(q))
    if not rows:
        raise NotFound(f"no registered {host} repository in this domain" + (f" named {repo}" if repo else ""))
    rows.sort(key=lambda r: 0 if r.role == "platform" else 1)
    ref = rows[0]
    project = db.get(Project, ref.project_id)
    if project is None:
        raise NotFound("project not found")
    return ref, project


def _capabilities(db: Session, ref: RepositoryReference, project: Project, branch: str | None) -> Any:
    if project.domain == "OFFICE":
        return bitbucket.capabilities_for(db, project, ref.full_name, branch or ref.default_branch)
    token, credential = provider.github_token(project.domain)
    caps = provider.check_capabilities(ref.full_name, branch or ref.default_branch, token=token, credential=credential)
    # enforcement_tested is VERIFIED only through recorded provider evidence bound to this repo/branch/ruleset
    return provider.apply_enforcement_evidence(db, caps)


@router.get("/api/delivery/provider-capabilities")
def provider_capabilities(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, repo: str | None = None,
                          branch: str | None = Query(None, max_length=200)) -> dict[str, Any]:
    ref, project = _repo_for(db, principal, domain, repo)
    caps = _capabilities(db, ref, project, branch)
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
    caps = _capabilities(db, ref, project, body.branch)
    ev = provider.record_capability_evidence(db, project, caps)
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="delivery.provider.capabilities",
                 result="SUCCESS", target_type="repository", target_id=ref.full_name, project_id=project.id,
                 domain=project.domain, details={"evidence_id": ev.id, "merge_gate": caps.merge_gate,
                                                 "branch": caps.branch, "access": caps.credential})
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
    # checked_at = last provider sync (null = never synced); generated_at = when this list was read
    return {"items": [gates.pr_view(r) for r in rows], "source": "delivery_pull_requests (provider observations)",
            "checked_at": latest_sync.isoformat() if latest_sync else None,
            "generated_at": utcnow().isoformat(), "mode": "LIVE", "merge_authority": "HUMAN_ONLY"}


@router.post("/api/delivery/prs/{pr_id}/sync")
def sync_pr(pr_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    pr = db.get(PullRequestRecord, pr_id)
    if pr is None or pr.project_id not in visible_project_ids(db, principal, domain):
        raise NotFound("pull request record not found")
    result = sync.sync_pull_request(db, pr.id, actor=principal.user.id)
    db.commit()
    return result


class ObserveDeploymentIn(BaseModel):
    project_id: str = Field(max_length=36)
    environment: str = Field(pattern="^(staging|production)$")
    sha: str = Field(min_length=7, max_length=64)


@router.post("/api/delivery/targets/observe")
def observe_target(body: ObserveDeploymentIn, principal: AdminPrincipal, domain: ActiveDomain,
                   db: DB) -> dict[str, Any]:
    """Read-only observation of an owner-registered deployment target (G6/G8). AEOS never deploys."""
    from aeos.delivery import deployment

    project = load_project(db, principal, body.project_id, domain=domain)
    try:
        result = deployment.observe(db, project.id, body.environment, body.sha, actor=principal.user.id)
    except AeosError as exc:
        audit_denied(db, principal, "delivery.target.observe", exc, target_type="project", target_id=project.id,
                     project_id=project.id, domain=project.domain)
        raise
    db.commit()
    return result


def _load_target(db: Session, principal: Principal, domain: Domain, integration_id: str) -> Any:
    from aeos.delivery import deployment
    from aeos.models.portfolio import ProjectIntegration

    integ = db.get(ProjectIntegration, integration_id)
    if integ is None or integ.provider != deployment.PROVIDER:
        raise NotFound("deployment target not found")
    load_project(db, principal, integ.project_id, domain=domain)
    return integ


@router.get("/api/delivery/targets")
def list_targets(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                 project_id: str = Query(max_length=36)) -> dict[str, Any]:
    """Registered observe-only deployment targets of a project with their check settings and the status of the
    newest observation for the current configuration (§31 G6/G8)."""
    from aeos.delivery import deployment
    from aeos.models.portfolio import ProjectIntegration

    project = load_project(db, principal, project_id, domain=domain)
    rows = db.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == project.id,
                                                       ProjectIntegration.provider == deployment.PROVIDER)
                      .order_by(ProjectIntegration.purpose, ProjectIntegration.created_at.desc()))
    items = [deployment.target_view(db, r) for r in rows]
    observed = [i["latest_observation"].get("observed_at") for i in items if i["latest_observation"].get("observed_at")]
    return {"items": items, "source": "project_integrations (deployment-target) + evidence_artifacts",
            "checked_at": max(observed) if observed else None, "generated_at": utcnow().isoformat(), "mode": "LIVE",
            "config_fields": {"health_path": "absolute path, e.g. /health", "smoke_checks": deployment.CHECK_KINDS,
                              "sli_checks": "name, path, json_key, max/min (owner thresholds; none = NO_TARGET)",
                              "rollback_on": deployment.ROLLBACK_CONDITIONS,
                              "require_artifact_digest": "true/false"}}


class TargetChecksIn(BaseModel):
    # an unknown key (e.g. a typo such as "smoke_check") is refused, never silently ignored
    model_config = ConfigDict(extra="forbid")

    # absolute path on the target itself, e.g. /health or /api/health ("" = back to the default /health)
    health_path: str | None = Field(default=None, max_length=201)
    smoke_checks: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    sli_checks: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    rollback_on: list[str] | None = Field(default=None, max_length=10)
    require_artifact_digest: bool | None = None


@router.put("/api/delivery/targets/{integration_id}/checks")
def put_target_checks(integration_id: str, body: TargetChecksIn, principal: AdminPrincipal, domain: ActiveDomain,
                      db: DB) -> dict[str, Any]:
    """Owner configuration of a target's health path, smoke/critical-flow GET checks, product SLI checks (owner
    thresholds), rollback criteria and the artifact-digest requirement. AEOS still only observes the target."""
    from aeos.delivery import deployment

    integ = _load_target(db, principal, domain, integration_id)
    try:
        view = deployment.set_target_checks(db, integ, body.model_dump(exclude_none=True), actor=principal.user.id)
    except AeosError as exc:
        audit_denied(db, principal, "delivery.target.checks", exc, target_type="integration", target_id=integration_id,
                     project_id=integ.project_id, domain=integ.domain)
        raise
    db.commit()
    return view


class ArtifactsRecordIn(BaseModel):
    project_id: str = Field(max_length=36)
    sha: str = Field(min_length=40, max_length=64)
    repo: str | None = Field(default=None, max_length=140)


@router.post("/api/delivery/artifacts/record")
def record_artifacts(body: ArtifactsRecordIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Read-only GETs of the provider CI artifacts (digests + attestation presence) of one commit, recorded as
    provider-ci ARTIFACT_DIGEST evidence (§31 build once). PERSONAL/GitHub only."""
    project = load_project(db, principal, body.project_id, domain=domain)
    if project.domain == "OFFICE":
        raise Blocked("Bitbucket artifact digests are not supported (OFFICE delivery is disabled by default)",
                      code="NOT_SUPPORTED")
    ref = project_github_repo(db, project, body.repo)
    token, _cred = provider.github_token(project.domain)
    result = provider.fetch_artifact_digests(ref.full_name, body.sha, token=token)
    ev = provider.record_artifact_evidence(db, project, result)
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="delivery.artifacts.record",
                 result="SUCCESS" if ev is not None else "FAILED", target_type="repository", target_id=ref.full_name,
                 project_id=project.id, domain=project.domain,
                 details={"sha": body.sha, "status": result.status, "evidence_id": ev.id if ev else None,
                          "error": result.error})
    db.commit()
    return {**result.to_dict(), "eligible_digests": result.eligible_digests, "evidence_id": ev.id if ev else None}


class EnforcementIn(BaseModel):
    repo: str | None = Field(default=None, max_length=140)
    branch: str | None = Field(default=None, max_length=200)
    time_period: str = Field(default="week", pattern="^(hour|day|week|month)$")
    statement: str | None = Field(default=None, max_length=2000)


@router.post("/api/delivery/enforcement/observe")
def observe_enforcement(body: EnforcementIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Read the provider's ruleset evaluation history (GET only) for the branch; a refused change by an active
    ruleset is recorded as PROVIDER_ENFORCEMENT_TEST evidence (§31, M4 'direct bypass tests')."""
    ref, project = _repo_for(db, principal, domain, body.repo)
    if project.domain == "OFFICE":
        raise Blocked("Bitbucket rule history is not supported", code="NOT_SUPPORTED")
    token, _cred = provider.github_token(project.domain)
    obs = provider.fetch_enforcement_observation(ref.full_name, body.branch or ref.default_branch or "main",
                                                 token=token, time_period=body.time_period)
    ev = provider.record_enforcement_evidence(db, project, obs)
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="delivery.enforcement.observe",
                 result="SUCCESS" if obs.error is None else "FAILED", target_type="repository", target_id=ref.full_name,
                 project_id=project.id, domain=project.domain,
                 details={"branch": obs.branch, "status": obs.status, "refusals": len(obs.refusals),
                          "evidence_id": ev.id if ev else None, "error": obs.error})
    db.commit()
    return {**obs.to_dict(), "evidence_id": ev.id if ev else None}


@router.post("/api/delivery/enforcement/attest")
def attest_enforcement(body: EnforcementIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Owner fallback: record the owner's own statement of a refused bypass attempt as an untrusted HUMAN claim
    (CLAIMED, never VERIFIED)."""
    ref, project = _repo_for(db, principal, domain, body.repo)
    ev = provider.record_owner_attestation(db, project, repo=ref.full_name,
                                           branch=body.branch or ref.default_branch or "main",
                                           statement=body.statement or "", actor_user_id=principal.user.id)
    db.commit()
    return {"evidence_id": ev.id, "trusted": ev.trusted, "evidence_level": ev.evidence_level,
            "label": "HUMAN_RECORDED claim (not a provider observation)", "enforcement_tested": "CLAIMED",
            "source": "owner attestation", "checked_at": ev.created_at.isoformat() if ev.created_at else None}


def project_github_repo(db: Session, project: Project, repo: str | None) -> RepositoryReference:
    """The project's own registered GitHub repository (the named one, or the only/first one)."""
    rows = [r for r in db.scalars(select(RepositoryReference).where(RepositoryReference.project_id == project.id))
            if (r.provider or "").upper() == "GITHUB"]
    if repo:
        provider.validate_repo(repo)
        rows = [r for r in rows if r.full_name == repo]
    if not rows:
        raise NotFound("no registered GitHub repository for this project" + (f" named {repo}" if repo else ""))
    return sorted(rows, key=lambda r: (0 if r.role == "platform" else 1, r.full_name))[0]


class HandoffIn(BaseModel):
    artifact_hash: str | None = Field(default=None, max_length=80)


@router.post("/api/delivery/governance/{task_id}/handoff")
def governance_handoff(task_id: str, body: HandoffIn, principal: AdminPrincipal, domain: ActiveDomain,
                       db: DB) -> dict[str, Any]:
    task = _load_task(db, principal, domain, task_id)
    try:
        result = governance.handoff(db, task.id, actor=principal.user.id, artifact_hash=body.artifact_hash)
    except AeosError as exc:
        audit_denied(db, principal, "delivery.governance.handoff", exc, target_type="task", target_id=task.id,
                     project_id=task.project_id, domain=task.domain)
        raise
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
