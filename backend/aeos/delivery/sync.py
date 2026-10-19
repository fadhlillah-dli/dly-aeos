"""PR observation and acceptance invalidation hooks (§14, §31, §50 "PR baru mengubah head atau base").

``sync_pull_request`` reads the provider (GET only): PR state, head/base SHA, ``merged_by``, merge
commit, and CI check-runs for the head and — after a human merge — the merge commit (G5 retest on the
merged state). New commits on the PR (head changed) or a moved base make the bound acceptance STALE.
``on_branch_updated`` is the hook a poller/webhook calls when a task branch receives commits.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import NotFound
from aeos.core.timeutil import utcnow
from aeos.delivery import provider
from aeos.delivery.gates import pr_view
from aeos.events import service as events
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.portfolio import Project
from aeos.verification import acceptance as acceptance_mod
from aeos.verification.integration import call


def observe_pr_commits(session: Session, pr: PullRequestRecord, *, head_sha: str | None, base_sha: str | None,
                       actor: str) -> list[str]:
    """Record observed head/base; invalidate acceptance when they moved. Returns invalidation notes."""
    notes: list[str] = []
    if base_sha and not pr.initial_base_sha:
        pr.initial_base_sha = base_sha
    pr.observed_head_sha = head_sha or pr.observed_head_sha
    pr.observed_base_sha = base_sha or pr.observed_base_sha
    if head_sha and pr.accepted_head_sha and head_sha != pr.accepted_head_sha:
        changed = acceptance_mod.invalidate(session, task_id=pr.task_id, reason="PR head changed (new commits)",
                                            actor=actor, head_commit=head_sha)
        if changed:
            notes.append(f"acceptance STALE: head {pr.accepted_head_sha[:12]} -> {head_sha[:12]}")
    if base_sha and pr.initial_base_sha and base_sha != pr.initial_base_sha:
        changed = acceptance_mod.invalidate(
            session, task_id=pr.task_id, actor=actor,
            reason=f"PR base moved {pr.initial_base_sha[:12]} -> {base_sha[:12]}; retest on the new base required")
        if changed:
            notes.append("acceptance STALE: base moved")
    return notes


def on_branch_updated(session: Session, *, repo: str, branch: str, head_sha: str, base_sha: str | None = None,
                      actor: str = "delivery") -> int:
    """Hook for new commits on a task branch: every open PR record for it is re-observed."""
    count = 0
    for pr in session.scalars(select(PullRequestRecord).where(
            PullRequestRecord.repo_full_name == repo, PullRequestRecord.head_ref == branch,
            PullRequestRecord.state.in_(["PREPARED", "OPEN"]))):
        if observe_pr_commits(session, pr, head_sha=head_sha, base_sha=base_sha, actor=actor):
            count += 1
    session.flush()
    return count


def sync_pull_request(session: Session, pr_id: str, *, actor: str) -> dict[str, Any]:
    """Read the provider state of one PR (caller commits)."""
    from aeos.workforce import facade as workforce

    pr = session.get(PullRequestRecord, pr_id)
    if pr is None:
        raise NotFound("pull request record not found")
    project = session.get(Project, pr.project_id)
    if project is None:
        raise NotFound("project not found")
    token, credential = provider.github_token(project.domain)
    notes: list[str] = []
    if pr.number is None:
        pr.sync_status, pr.sync_error = "FAILED", "PR number unknown (broker did not report it)"
        return {"pull_request": pr_view(pr), "notes": notes}
    data = provider.fetch_pull(pr.repo_full_name, pr.number, token=token)
    pr.last_synced_at = utcnow()
    if "error" in data:
        pr.sync_status = "FAILED"
        pr.sync_error = f"{data['error']} (credential {credential})"[:1000]
        session.flush()
        return {"pull_request": pr_view(pr), "notes": [pr.sync_error]}
    pr.sync_status, pr.sync_error = "SYNCED", ""
    pr.url = data.get("url") or pr.url
    pr.merged = bool(data.get("merged"))
    pr.state = "MERGED" if pr.merged else str(data.get("state") or "UNKNOWN").upper()
    pr.merged_by_login = data.get("merged_by_login")
    pr.merged_by_type = data.get("merged_by_type")
    pr.merged_at = provider.parse_time(data.get("merged_at"))
    pr.merge_commit_sha = data.get("merge_commit_sha") if pr.merged else None
    notes += observe_pr_commits(session, pr, head_sha=data.get("head_sha"), base_sha=data.get("base_sha"), actor=actor)

    if pr.merged and pr.merge_commit_sha:
        # G5 on the merged state: which files the merge commit brought in (relative to its first parent),
        # so protected policy/pipeline/verifier paths outside the accepted change are detected.
        delta = provider.fetch_commit_files(pr.repo_full_name, pr.merge_commit_sha, token=token)
        pr.details = {**(pr.details or {}), "merge_delta": delta}
        if delta.get("error"):
            notes.append(f"merge-commit file delta UNKNOWN: {delta['error']}")
    ci_ids: list[str] = []
    gate_map = provider.pipeline_reference(session, project)
    for sha in filter(None, [pr.observed_head_sha, pr.merge_commit_sha]):
        ci = provider.fetch_check_runs(pr.repo_full_name, sha, token=token, gate_map=gate_map)
        ev = provider.record_ci_evidence(session, project, ci, task_id=pr.task_id, run_id=pr.run_id)
        if ev is not None:
            ci_ids.append(ev.id)
        else:
            notes.append(f"CI for {sha[:12]} UNKNOWN: {ci.error}")
    upd = call(workforce.update_run, session, pr.run_id, actor="delivery", merged_status=pr.state)
    if not upd.ok:
        notes.append(f"run merged_status update {upd.label()}")
    non_human = pr.merged and ((pr.merged_by_type or "") != "User" or (pr.merged_by_login or "").endswith("[bot]"))
    audit.record(session, actor_type="user", actor_id=actor, action="delivery.pr.sync",
                 result="SUCCESS", target_type="pull_request", target_id=pr.id, project_id=pr.project_id,
                 domain=pr.domain, reason="merged by a non-human account" if non_human else "",
                 details={"state": pr.state, "merged_by": pr.merged_by_login, "head": pr.observed_head_sha,
                          "ci_evidence": ci_ids, "notes": notes})
    events.emit(session, source="delivery", kind="delivery.pr.synced", entity_type="pull_request", entity_id=pr.id,
                entity_version=pr.observed_head_sha, project_id=pr.project_id, domain=pr.domain, run_id=pr.run_id,
                payload={"state": pr.state, "merged_by": pr.merged_by_login, "non_human_merge": non_human,
                         "task_id": pr.task_id})
    session.flush()
    return {"pull_request": pr_view(pr), "ci_evidence_ids": ci_ids, "notes": notes}
