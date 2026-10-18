"""PR evidence bundle and PR preparation (§31, §34, §35).

The bundle states what is verified, by whom and for which exact artifact, and — just as visibly —
what is UNKNOWN / NOT-PROVEN. PR preparation requires an ACCEPTED, still-valid acceptance and the
``CAN_PREPARE_PR`` capability, passes the external-write interlock, and then uses only the trusted
broker (``push_task_branch`` for ``aeos/*`` branches + ``create_pull_request``). There is no merge or
deploy call anywhere in delivery: merge to main is a human action (U-09).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Capability
from aeos.core.errors import Blocked, Conflict, NotFound, PolicyDenied
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact_text
from aeos.events import service as events
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import EvidenceArtifact, ReviewRecord
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun
from aeos.verification import acceptance as acceptance_mod
from aeos.verification import collector
from aeos.verification.integration import call
from aeos.verification.verifier import related_evidence, report_view

TASK_BRANCH_PREFIX = "aeos/"
HUMAN_ONLY_STATEMENT = (
    "**Merge authority: human only.** DLY AEOS prepared this pull request through its trusted broker and has no "
    "merge or deploy capability. Merging into the default branch and any release/production step are decisions of "
    "the human owner (U-09, §31). Evidence below is not an approval.\n\n"
    "_Merge ke branch utama dan rilis adalah keputusan manusia; DLY AEOS tidak melakukan merge._"
)


@dataclass
class PRBundle:
    markdown: str
    bundle_hash: str
    data: dict[str, Any]


def _require_valid_acceptance(session: Session, task: TaskSpec) -> Any:
    acc = acceptance_mod.latest_acceptance(session, task.id)
    if acc is None or acc.acceptance_status != acceptance_mod.ACCEPTED:
        raise Blocked("PR preparation requires an ACCEPTED acceptance record (verifier PASS + review PASS)",
                      code="ACCEPTANCE_REQUIRED",
                      details={"acceptance_status": acc.acceptance_status if acc else None})
    view = acceptance_mod.acceptance_view(session, acc)
    if not view["valid"]:
        raise Blocked("acceptance is no longer valid: " + "; ".join(view["staleness"] or [acc.mode]),
                      code="ACCEPTANCE_STALE", details={"staleness": view["staleness"]})
    return acc


def _rollback_notes(changed: list[str]) -> list[str]:
    migrations = [p for p in changed if "/migrations/" in p or p.endswith(".sql")]
    notes = ["Rollback kode: human reverts the merge commit (e.g. `git revert -m 1 <merge-sha>`) through a new PR."]
    if migrations:
        notes.append("Schema/data migration changed (" + ", ".join(migrations[:10]) + "): reverting code does NOT "
                     "revert data — a forward fix or a tested restore is required (§31).")
    else:
        notes.append("No migration files changed in this diff.")
    if any(p.startswith(("policy/", ".github/workflows/", "backend/aeos/verification/", "backend/aeos/broker/"))
           for p in changed):
        notes.append("Protected control-plane paths changed: promotion needs special review; the running controller "
                     "is not modified by this PR (§09).")
    return notes


def build_pr_bundle(session: Session, task_id: str) -> PRBundle:
    from aeos.delivery.gates import compute_gates

    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    project = session.get(Project, task.project_id)
    if project is None:
        raise NotFound("project not found")
    acc = _require_valid_acceptance(session, task)
    run = session.get(WorkerRun, acc.run_id)
    report = session.get(EvidenceArtifact, acc.verification_evidence_id) if acc.verification_evidence_id else None
    view = report_view(report) or {}
    full = (collector.read_json(report) or collector.evidence_summary(report)) if report else {}
    review = session.get(ReviewRecord, acc.review_record_id) if acc.review_record_id else None
    scope_ev = related_evidence(session, report, "SCOPE_CHECK")
    changed = list((collector.read_json(scope_ev) or {}).get("changed_files") or [])
    gates = compute_gates(session, task.id)
    evidence_rows = []
    for ev_id in acc.verifier_evidence_refs or []:
        ev = session.get(EvidenceArtifact, ev_id)
        if ev is not None:
            evidence_rows.append({"id": ev.id, "kind": ev.kind, "level": ev.evidence_level, "trusted": ev.trusted,
                                  "content_hash": ev.content_hash, "claim": ev.claim[:160]})
    unknowns: list[str] = []
    unknowns += [str(u) for u in (review.unknowns if review else [])]
    unknowns += [str(n) for n in (full.get("not_proven") or [])]
    unknowns += [str(r) for r in (full.get("residual_risks") or [])]
    for g in gates["gates"]:
        if g["status"] not in ("PASS",):
            unknowns.append(f"{g['gate']} {g['name']}: {g['status']} — {g['summary']}")
    cap_ev = session.scalar(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project.id, EvidenceArtifact.kind == "PROVIDER_CAPABILITY")
        .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    if cap_ev is not None:
        caps = collector.read_json(cap_ev) or {}
        unknowns += [str(r) for r in caps.get("residual_risks") or []]
    else:
        unknowns.append("Provider merge-gate capability not checked yet (aeos delivery capabilities)")
    unknowns = list(dict.fromkeys(u for u in unknowns if u))

    data = {
        "task": {"id": task.id, "task_key": task.task_key, "title": task.title, "spec_hash": task.spec_hash,
                 "plan_version_id": task.plan_version_id, "plan_version_hash": task.plan_version_hash},
        "project": {"id": project.id, "key": project.project_key, "domain": project.domain},
        "run": {"id": run.id if run else acc.run_id, "attempt": run.attempt if run else None,
                "branch": run.branch if run else None},
        "artifact": {"base_commit": acc.base_commit, "head_commit": acc.head_commit, "tree": view.get("tree_hash"),
                     "artifact_hash": acc.artifact_hash, "config_hash": acc.config_hash,
                     "suite_hash": view.get("suite_hash")},
        "verifier": {"status": view.get("status"), "identity": view.get("verifier"),
                     "trusted_suite": view.get("trusted_suite_status"), "tests_passed": view.get("tests_passed"),
                     "tests_failed": view.get("tests_failed"), "tests_skipped": view.get("tests_skipped"),
                     "candidate_tests": view.get("candidate_tests_status"), "ruff": view.get("ruff_status"),
                     "scope": view.get("scope_status"), "secret_scan": view.get("secret_scan_status"),
                     "evidence": evidence_rows},
        "review": {"id": review.id if review else None, "verdict": review.verdict if review else None,
                   "verdict_requested": review.verdict_requested if review else None,
                   "reviewer": review.reviewer_identity if review else None,
                   "model_resolved": review.model_resolved if review else None,
                   "findings": [{"severity": f.get("severity"), "title": f.get("title")}
                                for f in (review.findings if review else [])][:30]},
        "acceptance": {"id": acc.id, "status": acc.acceptance_status, "decided_by": acc.decided_by,
                       "is_human_approval": False},
        "gates": [{"gate": g["gate"], "name": g["name"], "status": g["status"], "summary": g["summary"]}
                  for g in gates["gates"]],
        "unknowns": unknowns,
        "rollback": _rollback_notes(changed),
        "changed_files": changed[:200],
        "merge_authority": "HUMAN_ONLY",
    }
    bundle_hash = content_hash(data)
    md = _markdown(data, bundle_hash)
    return PRBundle(markdown=redact_text(md), bundle_hash=bundle_hash, data=data)


def _cell(v: Any) -> str:
    s = "—" if v is None or v == "" else str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def _markdown(d: dict[str, Any], bundle_hash: str) -> str:
    t, a, v, r = d["task"], d["artifact"], d["verifier"], d["review"]
    lines = [
        f"## DLY AEOS evidence bundle — {t['title']}",
        "",
        f"> Generated by DLY AEOS delivery. Bundle hash `{bundle_hash}`. This bundle is evidence, not an approval.",
        "",
        "### Task & plan",
        "| Field | Value |", "|---|---|",
        f"| Task | `{t['task_key']}` (`{t['id']}`) |",
        f"| TaskSpec hash | `{_cell(t['spec_hash'])}` |",
        f"| PlanVersion | `{t['plan_version_id']}` |",
        f"| PlanVersion hash | `{_cell(t['plan_version_hash'])}` |",
        f"| Project / domain | `{d['project']['key']}` / {d['project']['domain']} |",
        f"| Run / attempt | `{d['run']['id']}` / {_cell(d['run']['attempt'])} |",
        "",
        "### Artifact identity",
        "| Field | Value |", "|---|---|",
        f"| Base commit | `{_cell(a['base_commit'])}` |",
        f"| Head commit | `{_cell(a['head_commit'])}` |",
        f"| Tree | `{_cell(a['tree'])}` |",
        f"| Artifact hash (tree + diff) | `{_cell(a['artifact_hash'])}` |",
        f"| Config hash (grader/lint/verifier) | `{_cell(a['config_hash'])}` |",
        f"| Trusted suite hash | `{_cell(a['suite_hash'])}` |",
        "",
        "### Independent verifier (trusted suite from the controller, not from the candidate)",
        f"- Status: **{_cell(v['status'])}** — verifier `{_cell(v['identity'])}`",
        f"- Trusted acceptance suite: {_cell(v['trusted_suite'])} ({_cell(v['tests_passed'])} passed, "
        f"{_cell(v['tests_failed'])} failed, {_cell(v['tests_skipped'])} skipped)",
        f"- Candidate's own tests (lower trust): {_cell(v['candidate_tests'])} · ruff (trusted config): "
        f"{_cell(v['ruff'])} · scope: {_cell(v['scope'])} · secret scan: {_cell(v['secret_scan'])}",
        "",
        "| Evidence | Kind | Level | Trusted | Content hash |", "|---|---|---|---|---|",
        *[f"| `{e['id']}` | {e['kind']} | {e['level']} | {'yes' if e['trusted'] else 'no'} | `{e['content_hash']}` |"
          for e in v["evidence"]],
        "",
        "### Checkpoint review (Claude, read-only)",
        f"- Verdict: **{_cell(r['verdict'])}** (requested: {_cell(r['verdict_requested'])}) — "
        f"{_cell(r['reviewer'])}, model {_cell(r['model_resolved'])}",
        *([f"- [{_cell(f['severity'])}] {_cell(f['title'])}" for f in r["findings"]] or ["- No findings recorded."]),
        "",
        "### Acceptance",
        f"- `{d['acceptance']['id']}`: **{d['acceptance']['status']}**, decided by `{_cell(d['acceptance']['decided_by'])}` "
        "(deterministic rule: verifier PASS + review PASS-CONTROLLED or better). Not a human approval.",
        "",
        "### Delivery gates (§31)",
        "| Gate | Status | Summary |", "|---|---|---|",
        *[f"| {g['gate']} {g['name']} | {g['status']} | {_cell(g['summary'])} |" for g in d["gates"]],
        "",
        "### Unknowns / NOT-PROVEN",
        *([f"- {_cell(u)}" for u in d["unknowns"]] or ["- None recorded."]),
        "",
        "### Rollback",
        *[f"- {n}" for n in d["rollback"]],
        "",
        "### Merge authority",
        HUMAN_ONLY_STATEMENT,
        "",
    ]
    return "\n".join(lines)


def _repository(session: Session, project: Project) -> str:
    from aeos.portfolio import facade as portfolio

    repos = [r for r in portfolio.repositories(session, project.id) if r.provider.upper() == "GITHUB"]
    repos.sort(key=lambda r: 0 if r.role == "platform" else 1)
    if not repos:
        raise Blocked("no GitHub repository is registered for this project", code="NO_REPOSITORY")
    return repos[0].full_name


def prepare_pr(session: Session, task_id: str, *, actor_user_id: str, base_branch: str | None = None) -> dict[str, Any]:
    """Prepare the PR for an accepted task through the trusted broker (caller commits)."""
    from aeos.broker import facade as broker
    from aeos.delivery.gates import pr_view
    from aeos.doctor import facade as doctor
    from aeos.policy import facade as policy
    from aeos.portfolio import facade as portfolio

    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    project = session.get(Project, task.project_id)
    if project is None:
        raise NotFound("project not found")
    acc = _require_valid_acceptance(session, task)
    run = session.get(WorkerRun, acc.run_id)
    if run is None or not run.branch or not run.branch.startswith(TASK_BRANCH_PREFIX):
        raise Blocked("accepted run has no aeos/* task branch", code="BRANCH_NOT_TASK_BRANCH")
    if not run.workspace_ref:
        raise Blocked("accepted run has no candidate workspace", code="NO_CANDIDATE")
    existing = session.scalar(select(PullRequestRecord).where(PullRequestRecord.acceptance_id == acc.id,
                                                              PullRequestRecord.state.in_(["PREPARED", "OPEN"])))
    if existing is not None:
        raise Conflict("a PR is already prepared for this acceptance", code="PR_EXISTS",
                       details={"pr_id": existing.id, "url": existing.url})

    cap = call(doctor.require_capability, session, Capability.CAN_PREPARE_PR, domain=project.domain,
               project_id=project.id)
    if not cap.ok:
        raise Blocked(f"CAN_PREPARE_PR not ready ({cap.label()})", code="CAPABILITY_NOT_READY")

    repo = _repository(session, project)
    default_branch = next((r.default_branch for r in portfolio.repositories(session, project.id)
                           if r.full_name == repo), "main")
    base = base_branch or default_branch or "main"
    if base.startswith(TASK_BRANCH_PREFIX) or base == run.branch:
        raise Blocked("base branch must be the integration branch, not a task branch", code="BASE_INVALID")

    intent = policy.WriteIntent(
        actor_user_id=actor_user_id, project_id=project.id, domain=project.domain, provider="github-repo",
        operation="create_pr", target={"repo": repo, "head": run.branch, "base": base},
        approved_plan_version_id=task.plan_version_id, approved_hash=task.plan_version_hash, decision_id=None,
        extra={"acceptance_id": acc.id, "artifact_hash": acc.artifact_hash, "head_commit": acc.head_commit},
    )
    lock = call(policy.check_external_write, session, intent)
    if not lock.ok:
        raise Blocked(f"external-write interlock {lock.label()}", code="INTERLOCK_UNAVAILABLE")
    if not lock.result.allowed:
        raise PolicyDenied("external-write interlock refused the PR", code="INTERLOCK_DENIED",
                           details={"reasons": list(lock.result.reasons)})

    bundle = build_pr_bundle(session, task.id)
    bundle_ev = collector.collect_evidence(
        session, kind="PR_BUNDLE", origin="collector", collector_identity="aeos-delivery-bundler",
        claim=f"PR evidence bundle for task {task.task_key} at {(acc.head_commit or '')[:12]}",
        content=bundle.markdown, evidence_level="E1", subject_type="task", subject_id=task.id,
        project_id=project.id, domain=project.domain, run_id=run.id, task_id=task.id,
        environment={"commit": acc.head_commit, "artifact_hash": acc.artifact_hash, "bundle_hash": bundle.bundle_hash},
    )

    pushed = call(broker.push_task_branch, session, run_id=run.id, worktree=run.workspace_ref, branch=run.branch,
                  repo_full_name=repo)
    if not pushed.ok:
        raise Blocked(f"trusted broker push {pushed.label()}", code="BROKER_PUSH_FAILED")
    pushed_sha = (pushed.result or {}).get("head_sha") or (pushed.result or {}).get("sha")
    if pushed_sha and acc.head_commit and pushed_sha != acc.head_commit:
        acceptance_mod.invalidate(session, task_id=task.id, reason="pushed branch head differs from accepted head",
                                  actor="delivery", head_commit=pushed_sha)
        raise Conflict("pushed branch head differs from the accepted head; acceptance is now STALE",
                       code="HEAD_MISMATCH")
    created = call(broker.create_pull_request, session, run_id=run.id, repo_full_name=repo, head=run.branch,
                   base=base, title=f"[AEOS] {task.title}"[:240], body=bundle.markdown)
    if not created.ok:
        raise Blocked(f"trusted broker PR creation {created.label()}", code="BROKER_PR_FAILED")
    res = dict(created.result or {})
    pr = PullRequestRecord(
        project_id=project.id, domain=project.domain, task_id=task.id, run_id=run.id, acceptance_id=acc.id,
        repo_full_name=repo, number=res.get("number"), url=res.get("url") or res.get("html_url"),
        head_ref=run.branch, base_ref=base, accepted_head_sha=acc.head_commit,
        initial_base_sha=res.get("base_sha"), observed_head_sha=res.get("head_sha") or pushed_sha,
        observed_base_sha=res.get("base_sha"), bundle_hash=bundle.bundle_hash, bundle_evidence_id=bundle_ev.id,
        state="OPEN", created_by=actor_user_id, details={"broker_push": _safe(pushed.result), "broker_pr": _safe(res)},
    )
    session.add(pr)
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="delivery.pr.prepare", result="SUCCESS",
                 target_type="task", target_id=task.id, project_id=project.id, domain=project.domain,
                 details={"pr_id": pr.id, "repo": repo, "number": pr.number, "bundle_hash": bundle.bundle_hash,
                          "acceptance_id": acc.id})
    events.emit(session, source="delivery", kind="delivery.pr.prepared", entity_type="pull_request", entity_id=pr.id,
                entity_version=acc.head_commit, project_id=project.id, domain=project.domain, run_id=run.id,
                payload={"task_id": task.id, "url": pr.url, "number": pr.number})
    return {"pull_request": pr_view(pr), "bundle_hash": bundle.bundle_hash, "bundle_evidence_id": bundle_ev.id,
            "merge_authority": "HUMAN_ONLY"}


def _safe(value: Any) -> dict[str, Any]:
    from aeos.core.redaction import redact

    return redact(dict(value)) if isinstance(value, dict) else {}
