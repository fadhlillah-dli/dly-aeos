"""PR evidence bundle and PR preparation (§31, §34, §35).

The bundle states what is verified, by whom and for which exact artifact, and — just as visibly —
what is UNKNOWN / NOT-PROVEN. PR preparation requires an ACCEPTED, still-valid acceptance and the
``CAN_PREPARE_PR`` capability, passes the external-write interlock, and then uses only the trusted
broker (``push_task_branch`` for ``aeos/*`` branches + ``create_pull_request``). There is no merge or
deploy call anywhere in delivery: merge to main is a human action (U-09).

OFFICE (ADR-0111): the same flow on Bitbucket Cloud. It runs only when ``office_delivery_readiness`` is
AVAILABLE (company authorisation listing ``bitbucket``, enabled ``bitbucket/code`` integration, Cloud, verified
repository) and ``CAN_USE_OFFICE_INTEGRATIONS`` is READY; the interlock provider is ``bitbucket``. Data Center
is refused with ``BITBUCKET_DC_NOT_SUPPORTED``; the disabled default with ``OFFICE_DELIVERY_NOT_AVAILABLE``.

Owner rule (AGENTS.md invariant 11): no AI appears on the code host. The bundle renders neutral roles
("supervisor review", "worker run", "verifier") and never a model, route, agent or reviewer tool name - those stay in
the bundle data / DLY AEOS records. The PR title and body are checked before the push (``AI_ATTRIBUTION_IN_TEXT``:
refused, never stripped) and the broker checks every pushed commit against the code host's configured identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.attribution import assert_no_ai_attribution
from aeos.core.enums import Capability
from aeos.core.errors import Blocked, Conflict, NotFound, PolicyDenied
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact_text
from aeos.events import service as events
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import EvidenceArtifact, ReviewRecord
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.verification import acceptance as acceptance_mod
from aeos.verification import collector
from aeos.verification.integration import call
from aeos.verification.procs import GitError, GitReader
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
    from aeos.delivery.governance import change_deltas

    migrations = change_deltas(changed)["migrations"]
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
    gov = gates["separate_status"]["governance"]
    if gov["status"] not in ("HANDED_OFF", "ACCEPTED"):
        unknowns.append(f"Governance handoff (§32): {gov['status']} via {gov['adapter']}"
                        + (f" — {gov['note']}" if gov.get("note") else ""))
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
                     "scope": view.get("scope_status"), "leak_scan": view.get("leak_scan_status"),
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
        "governance": {"adapter": gov["adapter"], "contract_version": gov["contract_version"], "status": gov["status"],
                       "result": gov["result"], "evidence_id": gov["evidence_id"],
                       "bound_to_accepted_artifact": gov["bound_to_accepted_artifact"], "is_human_approval": False},
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


# System-generated gate/unknown wording names the supervisor tool for the dashboard; on the code host the bundle
# uses the neutral role (owner rule, AGENTS.md invariant 11). Owner-written text is never rewritten: the outbound
# guard refuses it instead.
_SUPERVISOR_TOOL = re.compile(r"(?<![\w.])Claude(?:\s+Code)?(?:\s+resmi)?(?![\w])", re.I)


def _neutral(v: Any) -> str:
    return _SUPERVISOR_TOOL.sub("pengawas", _cell(v))


def _markdown(d: dict[str, Any], bundle_hash: str) -> str:
    t, a, v, r = d["task"], d["artifact"], d["verifier"], d["review"]
    lines = [
        f"## DLY AEOS evidence bundle — {t['title']}",
        "",
        f"> Prepared by DLY AEOS delivery. Bundle hash `{bundle_hash}`. This bundle is evidence, not an approval.",
        "",
        "### Task & plan",
        "| Field | Value |", "|---|---|",
        f"| Task | `{t['task_key']}` (`{t['id']}`) |",
        f"| TaskSpec hash | `{_cell(t['spec_hash'])}` |",
        f"| PlanVersion | `{t['plan_version_id']}` |",
        f"| PlanVersion hash | `{_cell(t['plan_version_hash'])}` |",
        f"| Project / domain | `{d['project']['key']}` / {d['project']['domain']} |",
        f"| Worker run / attempt | `{d['run']['id']}` / {_cell(d['run']['attempt'])} |",
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
        f"- Status: **{_cell(v['status'])}** — independent verifier",
        f"- Trusted acceptance suite: {_cell(v['trusted_suite'])} ({_cell(v['tests_passed'])} passed, "
        f"{_cell(v['tests_failed'])} failed, {_cell(v['tests_skipped'])} skipped)",
        f"- Candidate's own tests (lower trust): {_cell(v['candidate_tests'])} · ruff (trusted config): "
        f"{_cell(v['ruff'])} · scope: {_cell(v['scope'])} · secret scan: {_cell(v['leak_scan'])}",
        "",
        "| Evidence | Kind | Level | Trusted | Content hash |", "|---|---|---|---|---|",
        *[f"| `{e['id']}` | {e['kind']} | {e['level']} | {'yes' if e['trusted'] else 'no'} | `{e['content_hash']}` |"
          for e in v["evidence"]],
        "",
        # neutral role only: the reviewer tool and model stay in the review record (owner rule, invariant 11)
        "### Supervisor review (read-only)",
        f"- Verdict: **{_cell(r['verdict'])}** (requested: {_cell(r['verdict_requested'])}) — supervisor review "
        f"`{_cell(r['id'])}`",
        *([f"- [{_cell(f['severity'])}] {_cell(f['title'])}" for f in r["findings"]] or ["- No findings recorded."]),
        "",
        "### Acceptance",
        f"- `{d['acceptance']['id']}`: **{d['acceptance']['status']}**, decided by `{_cell(d['acceptance']['decided_by'])}` "
        "(deterministic rule: verifier PASS + review PASS-CONTROLLED or better). Not a human approval.",
        "",
        "### Delivery gates (§31)",
        "| Gate | Status | Summary |", "|---|---|---|",
        *[f"| {g['gate']} {g['name']} | {g['status']} | {_neutral(g['summary'])} |" for g in d["gates"]],
        "",
        "### Governance handoff (§32)",
        f"- Adapter `{_cell(d['governance']['adapter'])}` (contract {_cell(d['governance']['contract_version'])}): "
        f"**{_cell(d['governance']['status'])}** — evidence `{_cell(d['governance']['evidence_id'])}`. "
        "Not a human approval.",
        "",
        "### Unknowns / NOT-PROVEN",
        *([f"- {_neutral(u)}" for u in d["unknowns"]] or ["- None recorded."]),
        "",
        "### Rollback",
        *[f"- {n}" for n in d["rollback"]],
        "",
        "### Merge authority",
        HUMAN_ONLY_STATEMENT,
        "",
    ]
    return "\n".join(lines)


CODE_HOST = {"PERSONAL": "GITHUB", "OFFICE": "BITBUCKET"}  # U-02..U-04: the provider follows the domain
WRITE_PROVIDER = {"PERSONAL": "github-repo", "OFFICE": "bitbucket"}  # interlock provider of delivery writes


def _repository(session: Session, project: Project) -> str:
    from aeos.portfolio import facade as portfolio

    host = CODE_HOST.get(project.domain, "GITHUB")
    repos = [r for r in portfolio.repositories(session, project.id) if r.provider.upper() == host]
    repos.sort(key=lambda r: 0 if r.role == "platform" else 1)
    if not repos:
        raise Blocked(f"no {host.title()} repository is registered for this project", code="NO_REPOSITORY")
    return repos[0].full_name


def _office_ready(session: Session, project: Project) -> Any:
    """OFFICE delivery runs only through Bitbucket Cloud behind the company authorisation (ADR-0111)."""
    from aeos.delivery.bitbucket import office_delivery_readiness

    ready = office_delivery_readiness(session, project)
    if ready.status != "AVAILABLE":
        from aeos.delivery.gates import OFFICE_DELIVERY_NOTE

        code = "BITBUCKET_DC_NOT_SUPPORTED" if ready.status == "NOT_SUPPORTED" else "OFFICE_DELIVERY_NOT_AVAILABLE"
        raise Blocked(f"OFFICE delivery (Bitbucket Cloud) {ready.status}: " + "; ".join(ready.reasons) + " - "
                      + OFFICE_DELIVERY_NOTE, code=code,
                      details={"status": ready.status, "reasons": ready.reasons, "remedy": ready.remedy,
                               "deployment": ready.deployment})
    return ready


def prepare_pr(session: Session, task_id: str, *, actor_user_id: str, base_branch: str | None = None) -> dict[str, Any]:
    """Prepare the PR for an accepted task through the trusted broker (caller commits)."""
    from aeos.broker import facade as broker
    from aeos.delivery import governance
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
    office = project.domain == "OFFICE"
    office_ready = _office_ready(session, project) if office else None
    acc = _require_valid_acceptance(session, task)
    # §14 "REVIEW --> DONE: penerimaan valid": the scheduler must have accepted exactly this run. An
    # ACCEPTED record whose mark_accepted was refused (task BLOCKED/READY, another attempt active,
    # reopened) is not a deliverable result.
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    if te is None or te.state != "DONE" or te.accepted_run_id != acc.run_id:
        raise Blocked("PR preparation requires the task DONE on the accepted run (scheduler acceptance)",
                      code="TASK_NOT_DONE", details={"task_state": te.state if te else None,
                                                     "accepted_run_id": te.accepted_run_id if te else None})
    run = session.get(WorkerRun, acc.run_id)
    if run is None or not run.branch or not run.branch.startswith(TASK_BRANCH_PREFIX):
        raise Blocked("accepted run has no aeos/* task branch", code="BRANCH_NOT_TASK_BRANCH")
    if not run.workspace_ref:
        raise Blocked("accepted run has no candidate workspace", code="NO_CANDIDATE")
    scope_problems = capability_scope_problems(task, run)
    if scope_problems:
        raise PolicyDenied("delivery exceeds the TaskSpec capability scope: " + "; ".join(scope_problems),
                           code="CAPABILITY_SCOPE_DENIED", details={"problems": scope_problems})
    # The broker pushes the branch tip: it must still be exactly the accepted head (read-only git plumbing).
    try:
        local_head = GitReader(Path(run.workspace_ref)).branch_head(run.branch)
    except GitError as exc:
        raise Blocked(f"task branch head could not be read from the candidate workspace: {exc}",
                      code="BRANCH_UNRESOLVED") from exc
    if local_head != acc.head_commit:
        acceptance_mod.invalidate(session, task_id=task.id, reason="task branch moved after acceptance",
                                  actor="delivery", head_commit=local_head)
        raise Conflict("the task branch no longer points at the accepted head; acceptance is now STALE",
                       code="HEAD_MISMATCH", details={"accepted": acc.head_commit, "branch_head": local_head})
    existing = session.scalar(select(PullRequestRecord).where(PullRequestRecord.acceptance_id == acc.id,
                                                              PullRequestRecord.state.in_(["PREPARED", "OPEN"])))
    if existing is not None:
        raise Conflict("a PR is already prepared for this acceptance", code="PR_EXISTS",
                       details={"pr_id": existing.id, "url": existing.url})

    # OFFICE: the OFFICE integration gate (company authorisation, §23) — CAN_PREPARE_PR is a PERSONAL capability.
    capability = Capability.CAN_USE_OFFICE_INTEGRATIONS if office else Capability.CAN_PREPARE_PR
    cap = call(doctor.require_capability, session, capability, domain=project.domain, project_id=project.id)
    if not cap.ok:
        raise Blocked(f"{capability.value} not ready ({cap.label()})", code="CAPABILITY_NOT_READY")

    repo = office_ready.repo if office_ready is not None and office_ready.repo else _repository(session, project)
    default_branch = next((r.default_branch for r in portfolio.repositories(session, project.id)
                           if r.full_name == repo), "main")
    base = base_branch or default_branch or "main"
    if base.startswith(TASK_BRANCH_PREFIX) or base == run.branch:
        raise Blocked("base branch must be the integration branch, not a task branch", code="BASE_INVALID")

    notes: list[str] = []
    # Capabilities first (the interlock checks the real, unexpired capability of this step), revoked in
    # the finally below whatever happens (§25, §26).
    grant_expires = _issue_delivery_grants(session, broker, run, repo=repo, base=base)
    try:
        # The interlock binds the write to the ACTIVE human APPROVAL of the task's exact plan version
        # (its allowed_operations must include create_pr and its targets the repository) — §26.
        intent = policy.WriteIntent(
            actor_user_id=actor_user_id, project_id=project.id, domain=project.domain,
            provider=WRITE_PROVIDER.get(project.domain, "github-repo"),
            operation="create_pr", target={"repo": repo, "head": run.branch, "base": base},
            approved_plan_version_id=task.plan_version_id, approved_hash=task.plan_version_hash,
            decision_id=approval_decision_id(session, task, "create_pr"), capability_expires_at=grant_expires,
            extra={"acceptance_id": acc.id, "artifact_hash": acc.artifact_hash, "head_commit": acc.head_commit},
        )
        lock = call(policy.check_external_write, session, intent)
        if not lock.ok:
            raise Blocked(f"external-write interlock {lock.label()}", code="INTERLOCK_UNAVAILABLE")
        if not lock.result.allowed:
            raise PolicyDenied("external-write interlock refused the PR", code="INTERLOCK_DENIED",
                               details={"reasons": list(lock.result.reasons)})

        # §01/§03/§32 order: governance runs after review/acceptance and before CI/PR. The handoff is
        # recorded as evidence for the exact accepted artifact; a REJECTED/BLOCKED result stops delivery,
        # NOT_CONFIGURED (no downstream harness yet) is carried visibly in the bundle — never a pass.
        handed = governance.handoff(session, task.id, actor=actor_user_id, artifact_hash=acc.artifact_hash)
        gov_result = str((handed.get("output") or {}).get("result") or "UNKNOWN")
        if gov_result in ("REJECTED", "BLOCKED"):
            raise Blocked(f"governance handoff {gov_result}: "
                          + "; ".join((handed.get("output") or {}).get("reasons") or [])[:400],
                          code="GOVERNANCE_REJECTED",
                          details={"evidence_id": handed.get("evidence_id"), "result": gov_result})
        notes.append(f"governance handoff {gov_result} (evidence {handed.get('evidence_id')})")

        bundle = build_pr_bundle(session, task.id)
        bundle_ev = collector.collect_evidence(
            session, kind="PR_BUNDLE", origin="collector", collector_identity="aeos-delivery-bundler",
            claim=f"PR evidence bundle for task {task.task_key} at {(acc.head_commit or '')[:12]}",
            content=bundle.markdown, evidence_level="E1", subject_type="task", subject_id=task.id,
            project_id=project.id, domain=project.domain, run_id=run.id, task_id=task.id,
            environment={"commit": acc.head_commit, "artifact_hash": acc.artifact_hash,
                         "bundle_hash": bundle.bundle_hash},
        )
        # Owner rule (AGENTS.md invariant 11): the PR text is checked BEFORE the branch leaves the machine - a
        # title/body with AI attribution (e.g. from the task title or a review finding) is refused, never stripped.
        title = f"[AEOS] {task.title}"[:240]
        host = "Bitbucket" if office else "GitHub"
        assert_no_ai_attribution(title, f"{host} pull request title")
        assert_no_ai_attribution(bundle.markdown, f"{host} pull request body (PR evidence bundle)")
        pushed = _push_branch(session, broker, run, repo=repo, accepted_head=acc.head_commit or "", notes=notes)
        pushed_sha = pushed.get("head_sha")
        if not pushed_sha:
            notes.append("broker did not report the pushed head; identity is NOT-PROVEN until the PR is synced "
                         "(aeos delivery sync-pr)")
        if pushed_sha and acc.head_commit and pushed_sha != acc.head_commit:
            acceptance_mod.invalidate(session, task_id=task.id, reason="pushed branch head differs from accepted head",
                                      actor="delivery", head_commit=pushed_sha)
            raise Conflict("pushed branch head differs from the accepted head; acceptance is now STALE",
                           code="HEAD_MISMATCH")
        res = _create_pr(session, broker, run, repo=repo, base=base, title=title, body=bundle.markdown, notes=notes)
    finally:
        # The delivery capabilities are bound to this one step; nothing may use them afterwards.
        call(broker.revoke_grants, session, run_id=run.id, reason="delivery step finished")
    number = res.get("number")
    pr = PullRequestRecord(
        project_id=project.id, domain=project.domain, task_id=task.id, run_id=run.id, acceptance_id=acc.id,
        provider=CODE_HOST.get(project.domain, "GITHUB"),
        repo_full_name=repo, number=int(number) if str(number or "").isdigit() else None,
        url=res.get("url") or res.get("html_url"),
        head_ref=run.branch, base_ref=base, accepted_head_sha=acc.head_commit,
        initial_base_sha=res.get("base_sha"), observed_head_sha=res.get("head_sha") or pushed_sha,
        observed_base_sha=res.get("base_sha"), bundle_hash=bundle.bundle_hash, bundle_evidence_id=bundle_ev.id,
        state="OPEN", created_by=actor_user_id, details={"broker_push": _safe(pushed), "broker_pr": _safe(res)},
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
            "merge_authority": "HUMAN_ONLY", "notes": notes}


DELIVERY_GRANT_TTL = 900
DELIVERY_GRANT_REQUESTS = 3  # push + PR + one reconciled retry
# Owner rule (AGENTS.md invariant 11): the broker's commit gate and the outbound text guard refuse with these codes.
OWNER_RULE_CODES = frozenset({"COMMIT_IDENTITY_MISSING", "COMMIT_IDENTITY_MISMATCH", "AI_ATTRIBUTION_IN_COMMIT",
                              "COMMIT_RANGE_UNBOUNDED", "AI_ATTRIBUTION_IN_TEXT"})
# Broker failures whose outbox/audit rows must survive the refusal (the router commits before raising).
PERSISTED_FAILURE_CODES = frozenset({"HEAD_MISMATCH", "BROKER_PUSH_FAILED", "BROKER_PUSH_UNKNOWN",
                                     "BROKER_PR_FAILED", "BROKER_PR_UNKNOWN", "GOVERNANCE_REJECTED",
                                     *OWNER_RULE_CODES})


def _owner_rule_refusal(rejected: Any, what: str) -> None:
    """A broker refusal of the owner rule keeps its own code (commit identity / AI attribution) instead of the
    generic broker failure, so the owner sees why and what to fix."""
    detail = str(getattr(rejected, "detail", "") or "")
    code = detail.split(":", 1)[0].strip()
    if getattr(rejected, "status", "") == "REJECTED" and code in OWNER_RULE_CODES:
        raise PolicyDenied(f"trusted broker refused the {what}: {detail}"[:1500], code=code)


def approval_decision_id(session: Session, task: TaskSpec, operation: str) -> str | None:
    """The ACTIVE human APPROVAL bound to the task's exact plan version + hash that allows *operation*
    (the interlock re-checks everything; None makes it refuse with DECISION_MISSING)."""
    try:
        from aeos.policy import facade as policy

        d = policy.find_active_decision(session, kind="APPROVAL", target_type="plan_version",
                                        target_id=task.plan_version_id, target_hash=task.plan_version_hash,
                                        operation=operation)
    except ImportError:
        return None
    return d.id if d is not None else None


DELIVERY_GRANT_OPERATIONS = ("push_branch", "create_pr")


def capability_scope_problems(task: TaskSpec, run: WorkerRun) -> list[str]:
    """§21 TaskSpec "capability scope": the delivery grants never exceed what the TaskSpec declares —
    the task branch must carry the declared branch prefix, and when the TaskSpec lists operations, the
    delivery operations must be among them (an empty list means none were scoped)."""
    scope = task.capability_scope if isinstance(task.capability_scope, dict) else {}
    problems: list[str] = []
    prefix = str(scope.get("branch_prefix") or "")
    if prefix and not (run.branch or "").startswith(prefix):
        problems.append(f"task branch {run.branch!r} is outside the TaskSpec branch prefix {prefix!r}")
    ops = [str(o) for o in scope.get("operations") or [] if str(o).strip()]
    missing = [o for o in DELIVERY_GRANT_OPERATIONS if ops and o not in ops]
    if missing:
        problems.append("TaskSpec capability scope does not list " + ", ".join(missing))
    return problems


def _issue_delivery_grants(session: Session, broker: Any, run: WorkerRun, *, repo: str, base: str) -> Any:
    """GIT_BRANCH + PR_CREATE capabilities for exactly this run/repo/branch/base, short-lived (§25, §31).
    The broker refuses any push/PR without them (ADR-0053); delivery never holds a credential.
    Returns the earliest expiry (what the interlock checks)."""
    expires: list[Any] = []
    for kind, scope in (("GIT_BRANCH", {"repo": repo, "branch": run.branch}),
                        ("PR_CREATE", {"repo": repo, "head": run.branch, "base": base})):
        issued = call(broker.issue_grant, session, run_id=run.id, project_id=run.project_id, domain=run.domain,
                      kind=kind, scope=scope, ttl_seconds=DELIVERY_GRANT_TTL, max_requests=DELIVERY_GRANT_REQUESTS)
        if not issued.ok:
            call(broker.revoke_grants, session, run_id=run.id, reason="delivery grant issuance failed")
            raise Blocked(f"broker capability {kind} {issued.label()}", code="BROKER_UNAVAILABLE")
        _raw, grant = issued.result
        expires.append(grant.expires_at)
    return min(expires)


def _open_side_effects(session: Session, run_id: str, kind: str) -> list[Any]:
    from aeos.models.broker_ext import BrokerSideEffect

    return list(session.scalars(select(BrokerSideEffect).where(
        BrokerSideEffect.run_id == run_id, BrokerSideEffect.kind == kind,
        BrokerSideEffect.status.in_(["PENDING", "UNKNOWN"]))))


def _reconcile_open(session: Session, broker: Any, run: WorkerRun, kind: str, notes: list[str]) -> list[dict[str, Any]]:
    """§14 "Crash recovery membaca ... publication records, dan status eksternal sebelum retry": every
    PENDING/UNKNOWN push/PR of this run is reconciled (read-only) before anything is retried."""
    out: list[dict[str, Any]] = []
    for se in _open_side_effects(session, run.id, kind):
        rec = call(broker.reconcile_side_effect, session, side_effect_id=se.id, actor="delivery")
        result = dict(rec.result or {}) if rec.ok else {"id": se.id, "status": "UNKNOWN", "detail": rec.label()}
        notes.append(f"reconciled {kind} {se.id}: {result.get('status')}")
        out.append(result)
    return out


def _push_branch(session: Session, broker: Any, run: WorkerRun, *, repo: str, accepted_head: str,
                 notes: list[str]) -> dict[str, Any]:
    """Sha-pinned push of the accepted head; FAILED/UNKNOWN results are refusals, never success."""
    from aeos.models.broker_ext import BrokerSideEffect

    for rec in _reconcile_open(session, broker, run, "PUSH_BRANCH", notes):
        if rec.get("status") == "UNKNOWN":
            raise Blocked("an earlier push of this branch is still UNKNOWN after reconciliation; nothing is "
                          "retried until its outcome is known", code="BROKER_PUSH_UNKNOWN",
                          details={"side_effect_id": rec.get("id")})
    landed = session.scalar(select(BrokerSideEffect).where(
        BrokerSideEffect.run_id == run.id, BrokerSideEffect.kind == "PUSH_BRANCH",
        BrokerSideEffect.status == "SUCCEEDED", BrokerSideEffect.commit_sha == accepted_head))
    if landed is not None:
        notes.append("accepted head already on the remote (outbox SUCCEEDED); push not repeated")
        return {"status": "PUSHED", "ok": True, "head_sha": landed.commit_sha, "side_effect_id": landed.id,
                "reused": True}
    pushed = call(broker.push_task_branch, session, run_id=run.id, worktree=run.workspace_ref, branch=run.branch,
                  repo_full_name=repo, expected_head_sha=accepted_head or None)
    if not pushed.ok:
        _owner_rule_refusal(pushed, "branch push")
        raise Blocked(f"trusted broker push {pushed.label()}", code="BROKER_PUSH_FAILED")
    res = dict(pushed.result or {})
    status = str(res.get("status") or "UNKNOWN")
    if status == "UNKNOWN":
        rec = call(broker.reconcile_side_effect, session, side_effect_id=res.get("side_effect_id") or "",
                   actor="delivery")
        reconciled = dict(rec.result or {}) if rec.ok else {}
        notes.append(f"push outcome UNKNOWN; reconciled: {reconciled.get('status', rec.label())}")
        if reconciled.get("status") != "SUCCEEDED":
            raise Blocked("push outcome is UNKNOWN (it may have landed); reconcile before any retry",
                          code="BROKER_PUSH_UNKNOWN", details={"side_effect_id": res.get("side_effect_id")})
        res.update(status="PUSHED", ok=True)
    elif status != "PUSHED" or not res.get("ok"):
        raise Blocked(f"trusted broker push {status}: {res.get('error', '')}"[:300], code="BROKER_PUSH_FAILED",
                      details={"side_effect_id": res.get("side_effect_id"), "status": status})
    return res


def _create_pr(session: Session, broker: Any, run: WorkerRun, *, repo: str, base: str, title: str, body: str,
               notes: list[str]) -> dict[str, Any]:
    for rec in _reconcile_open(session, broker, run, "CREATE_PR", notes):
        if rec.get("status") == "UNKNOWN":
            raise Blocked("an earlier PR creation is still UNKNOWN after reconciliation; not retried",
                          code="BROKER_PR_UNKNOWN", details={"side_effect_id": rec.get("id")})
    created = call(broker.create_pull_request, session, run_id=run.id, repo_full_name=repo, head=run.branch,
                   base=base, title=title, body=body)
    if not created.ok:
        _owner_rule_refusal(created, "pull request")
        raise Blocked(f"trusted broker PR creation {created.label()}", code="BROKER_PR_FAILED")
    res = dict(created.result or {})
    status = str(res.get("status") or "UNKNOWN")
    if status == "UNKNOWN":
        rec = call(broker.reconcile_side_effect, session, side_effect_id=res.get("side_effect_id") or "",
                   actor="delivery")
        reconciled = dict(rec.result or {}) if rec.ok else {}
        if reconciled.get("status") != "SUCCEEDED":
            raise Blocked("PR creation outcome is UNKNOWN; reconcile before any retry", code="BROKER_PR_UNKNOWN",
                          details={"side_effect_id": res.get("side_effect_id")})
        from aeos.models.broker_ext import BrokerSideEffect

        se = session.get(BrokerSideEffect, res.get("side_effect_id"))
        res.update(status="CREATED", ok=True, number=se.external_id if se else None,
                   url=se.external_url if se else None)
    elif status not in ("CREATED", "EXISTS"):
        raise Blocked(f"trusted broker PR creation {status}: {res.get('error', '')}"[:300], code="BROKER_PR_FAILED",
                      details={"side_effect_id": res.get("side_effect_id"), "status": status})
    return res


def _safe(value: Any) -> dict[str, Any]:
    from aeos.core.redaction import redact

    return redact(dict(value)) if isinstance(value, dict) else {}
