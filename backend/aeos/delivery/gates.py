"""Delivery gates G0-G8 per task/PR (§31), computed by code from records and evidence.

| Gate | Source in DLY AEOS |
|---|---|
| G0 Input/policy | the task's PlanVersion is the version bound by an ACTIVE approval (planning facade), hash + domain match; data classification >= project floor; non-empty safe allowed_paths; the PR repository is a registered project repository |
| G1 Static | provider CI check-runs (lint/type/secret-scan jobs) for the head commit; verifier ruff/secret scan support |
| G2 Controlled tests | independent verifier report (trusted suite) for the head commit |
| G3 Product tests | provider CI frontend job for the head commit (NOT_APPLICABLE when no frontend change and no job) |
| G4 Review | completed Claude checkpoint review bound to the verified artifact **and** independent evidence (G2) |
| G5 Integration | retest on the merged state: provider CI on the merge commit, after a human merged; the merge brought no protected path beyond the verified change |
| G6 Staging | NOT_APPLICABLE until the owner registers a staging ``deployment-target``; then trusted runtime evidence for the commit |
| G7 Human release | merge performed by a human account recorded as authorised release approver (POLICY ``release_approvers``), observed from GitHub ``merged_by`` / Bitbucket ``closed_by`` (never by AEOS) |
| G8 Post-release | NOT_APPLICABLE until a production ``deployment-target`` is registered; then a runtime observation of the merge commit |

Statuses: PASS | FAIL | PENDING | BLOCKED | NOT_PROVEN | STALE | NOT_APPLICABLE | UNKNOWN. Nothing that is
UNKNOWN/NOT_PROVEN/PENDING is ever reported as PASS. ``ready_for_human_merge`` is informational: the
merge itself stays a human action.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import NotFound
from aeos.core.timeutil import utcnow
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import EvidenceArtifact
from aeos.models.planning import PlanVersion, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.verification import acceptance as acceptance_mod
from aeos.verification import collector
from aeos.verification.integration import call
from aeos.verification.review import (
    NON_REGRESSION_SCOPE,
    PASS_VERDICTS,
    SPECIALIST_KINDS,
    latest_review,
    required_reviews,
    security_review_required,
)
from aeos.verification.verifier import current_config_hash, latest_report, related_evidence, report_view

GATE_NAMES: tuple[tuple[str, str], ...] = (
    ("G0", "Input / policy"),
    ("G1", "Static"),
    ("G2", "Controlled tests"),
    ("G3", "Product tests"),
    ("G4", "Review"),
    ("G5", "Integration"),
    ("G6", "Staging"),
    ("G7", "Human release"),
    ("G8", "Post-release"),
)
BOT_TYPES = frozenset({"Bot", "App", "Mannequin"})


@dataclass
class GateResult:
    gate: str
    name: str
    status: str
    summary: str
    source: str
    evidence_refs: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _gate(gate: str, status: str, summary: str, source: str, refs: list[str | None] | None = None,
          reasons: list[str] | None = None) -> GateResult:
    name = dict(GATE_NAMES)[gate]
    return GateResult(gate, name, status, summary, source, [r for r in (refs or []) if r], reasons or [])


def latest_ci_evidence(session: Session, project_id: str, sha: str | None) -> EvidenceArtifact | None:
    if not sha:
        return None
    return session.scalar(
        select(EvidenceArtifact)
        .where(EvidenceArtifact.project_id == project_id, EvidenceArtifact.kind == "CI_STATUS",
               EvidenceArtifact.origin == "provider-ci", EvidenceArtifact.trusted.is_(True),
               EvidenceArtifact.subject_type == "commit", EvidenceArtifact.subject_id == sha)
        .order_by(EvidenceArtifact.created_at.desc()).limit(1)
    )


def ci_gate_status(ev: EvidenceArtifact | None, gate: str) -> tuple[str, list[str]]:
    from aeos.delivery.provider import CheckRunView, summarize_runs

    if ev is None:
        return "NO_EVIDENCE", []
    data = collector.read_json(ev) or {}
    runs = [CheckRunView(name=str(r.get("name") or ""), status=str(r.get("status") or "unknown"),
                         conclusion=r.get("conclusion"), app=r.get("app"), url=r.get("url"),
                         started_at=r.get("started_at"), completed_at=r.get("completed_at"),
                         gates=[str(g) for g in (r.get("gates") or [])])
            for r in data.get("runs") or [] if isinstance(r, dict)]
    selected = [r for r in runs if gate in r.gates]
    status = summarize_runs(selected)
    if data.get("truncated") and status in ("PASS", "NO_CHECKS"):
        status = "NOT_PROVEN"  # the provider reported more check-runs than were read
    return status, [r.name for r in selected]


def latest_pr(session: Session, task_id: str) -> PullRequestRecord | None:
    return session.scalar(select(PullRequestRecord).where(PullRequestRecord.task_id == task_id)
                          .order_by(PullRequestRecord.created_at.desc()).limit(1))


def _relevant_run(session: Session, task: TaskSpec, acc: Any) -> WorkerRun | None:
    if acc is not None:
        run = session.get(WorkerRun, acc.run_id)
        if run is not None:
            return run
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    if te is not None and te.accepted_run_id:
        run = session.get(WorkerRun, te.accepted_run_id)
        if run is not None:
            return run
    return session.scalar(select(WorkerRun).where(WorkerRun.task_id == task.id)
                          .order_by(WorkerRun.attempt.desc(), WorkerRun.created_at.desc()).limit(1))


CLASS_ORDER = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")


def _g0_target_problems(session: Session, task: TaskSpec, project: Project,
                        pr: PullRequestRecord | None) -> list[str]:
    """§31 G0 "target, classification and scope correct" (beyond the plan binding)."""
    from aeos.portfolio import facade as portfolio
    from aeos.verification.scope import unsafe_path_reason

    problems: list[str] = []
    t_cls, p_cls = task.data_classification or "INTERNAL", project.data_classification or "INTERNAL"
    if t_cls not in CLASS_ORDER:
        problems.append(f"klasifikasi data task tidak dikenal: {t_cls}")
    elif p_cls in CLASS_ORDER and CLASS_ORDER.index(t_cls) < CLASS_ORDER.index(p_cls):
        problems.append(f"klasifikasi data task {t_cls} lebih rendah dari lantai proyek {p_cls}")
    allowed = list(task.allowed_paths or [])
    if not allowed:
        problems.append("TaskSpec tidak menyatakan allowed_paths (scope kosong)")
    for pattern in allowed:
        why = unsafe_path_reason(str(pattern).strip().replace("\\", "/").rstrip("/") or "")
        if why:
            problems.append(f"allowed_path {pattern!r} tidak aman: {why}")
    if pr is not None:
        repos = {r.full_name.lower() for r in portfolio.repositories(session, project.id)}
        if (pr.repo_full_name or "").lower() not in repos:
            problems.append(f"repository PR {pr.repo_full_name} bukan repository terdaftar proyek ini")
    return problems


def _g0(session: Session, task: TaskSpec, project: Project, pr: PullRequestRecord | None = None) -> GateResult:
    from aeos.planning import facade as planning

    pv = session.get(PlanVersion, task.plan_version_id)
    if pv is None:
        return _gate("G0", "FAIL", "TaskSpec merujuk PlanVersion yang tidak ada", "plan_versions")
    if task.domain != project.domain or pv.domain != project.domain:
        return _gate("G0", "FAIL", "Domain task/rencana tidak sama dengan domain proyek", "task_specs")
    res = call(planning.get_approved_version, session, pv.plan_id)
    if not res.ok:
        return _gate("G0", "UNKNOWN", f"Pengikatan persetujuan rencana tidak dapat diperiksa ({res.label()})",
                     "planning facade")
    approved = res.result
    if approved is None:
        return _gate("G0", "BLOCKED", "Belum ada persetujuan manusia yang aktif untuk rencana ini", "decisions")
    if approved.id != task.plan_version_id:
        return _gate("G0", "FAIL", "Task berasal dari PlanVersion yang bukan versi yang disetujui", "decisions",
                     reasons=[f"approved={approved.id}", f"task={task.plan_version_id}"])
    if approved.manifest_hash != task.plan_version_hash:
        return _gate("G0", "FAIL", "Hash PlanVersion pada TaskSpec tidak sama dengan versi yang disetujui", "decisions")
    problems = _g0_target_problems(session, task, project, pr)
    if problems:
        return _gate("G0", "FAIL", "Target, klasifikasi data atau scope task tidak sesuai", "task_specs + repositories",
                     reasons=problems)
    return _gate("G0", "PASS", f"PlanVersion v{approved.version} disetujui; hash, domain, klasifikasi, scope dan "
                               "target cocok", "decisions", refs=list(approved.decision_refs or []))


RELEASE_APPROVERS_TARGET = "release_approvers"


def release_approvers(session: Session, project: Project) -> tuple[list[str], str | None]:
    """Provider identities the owner authorised to merge/release this project (§31 G7 "authorised
    owner/approver"): GitHub logins (PERSONAL) or Bitbucket Atlassian account ids (OFFICE).

    Recorded as a human POLICY decision ``target_type=release_approvers, target_id=<project id>`` with
    ``auth_scope.github_logins`` / ``auth_scope.bitbucket_accounts`` (``POST /api/decisions``). Missing -> no
    authorised approver is known."""
    from aeos.policy import facade as policy

    res = call(policy.find_active_decision, session, kind="POLICY", target_type=RELEASE_APPROVERS_TARGET,
               target_id=project.id)
    d = res.result if res.ok else None
    # human-only AUTH (AGENTS.md #3): exactly a HUMAN decision of this project's own domain — never an
    # owner-recorded/agent row, never a decision recorded under the other domain for this project id.
    if d is None or d.actor_kind != "HUMAN" or (d.domain or "") != project.domain:
        return [], None
    key = "bitbucket_accounts" if project.domain == "OFFICE" else "github_logins"
    logins = (d.auth_scope or {}).get(key) or []
    return sorted({str(x).strip().lower() for x in logins if str(x).strip()}), d.id


def _deployment_target(session: Session, project: Project, purpose: str) -> Any:
    from aeos.delivery.deployment import target_for

    return target_for(session, project, purpose)


RUNTIME_KINDS = ("SMOKE_TEST", "RUNTIME_OBSERVATION", "PROBE", "HEALTH")


def runtime_observation(session: Session, project: Project, shas: list[str | None],
                        environment: str) -> tuple[EvidenceArtifact | None, str]:
    """Latest trusted runtime observation of *environment* (staging|production) for one of *shas*."""
    wanted = [s for s in shas if s]
    if not wanted:
        return None, "NO_EVIDENCE"
    rows = session.scalars(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project.id, EvidenceArtifact.kind.in_(RUNTIME_KINDS),
        EvidenceArtifact.trusted.is_(True), EvidenceArtifact.subject_type == "commit",
        EvidenceArtifact.subject_id.in_(wanted)).order_by(EvidenceArtifact.created_at.desc()))
    for ev in rows:
        if (ev.environment or {}).get("deployment_environment") != environment or ev.mode != "LIVE":
            continue
        data = collector.read_json(ev) or collector.evidence_summary(ev)
        status = str(data.get("status") or "UNKNOWN").upper()
        return ev, status if status in ("PASS", "FAIL") else "NOT_PROVEN"
    return None, "NO_EVIDENCE"


def _merge_delta(pr: PullRequestRecord, accepted_changed: list[str], project: Project) -> tuple[str, list[str]]:
    """G5 "policy files protected" on the merged state: protected paths the merge commit brought in that the
    accepted (verified) change did not contain."""
    from aeos.verification import scope

    delta = (pr.details or {}).get("merge_delta") or {}
    if not delta or delta.get("error"):
        return "UNKNOWN", []
    protected = list(scope.GENERIC_PROTECTED_PATHS)
    if project.is_platform:
        protected += [p for p in scope.PLATFORM_PROTECTED_PATHS if p not in protected]
    verified = set(accepted_changed)
    hits = sorted(f for f in delta.get("files") or []
                  if f not in verified and scope.first_match(str(f), protected, case_insensitive=True))
    if hits:
        return "FAIL", hits
    return ("UNKNOWN", []) if delta.get("truncated") else ("PASS", [])


def governance_status(session: Session, task: TaskSpec, acc: Any) -> dict[str, Any]:
    """§32 latest GOVERNANCE_HANDOFF of the task and whether it binds the accepted artifact."""
    from aeos.delivery import governance

    adapter = governance.configured_adapter()
    ev = session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.task_id == task.id,
                                                       EvidenceArtifact.kind == "GOVERNANCE_HANDOFF")
                        .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    data = (collector.read_json(ev) or {}) if ev is not None else {}
    output = data.get("output") or {}
    bound = bool(ev is not None and acc is not None and (ev.environment or {}).get("artifact_hash") == acc.artifact_hash)
    configured = adapter.name != governance.NullGovernanceHarness.name
    result = output.get("result") if ev is not None else None
    if ev is None:
        status = "NOT_RUN"
    elif not bound:
        status = "STALE"
    else:
        status = str(result or "UNKNOWN")
    ok = status in ("HANDED_OFF", "ACCEPTED") or (not configured and status in ("NOT_CONFIGURED", "NOT_RUN"))
    return {"adapter": adapter.name, "adapter_configured": configured, "contract_version": governance.CONTRACT_VERSION,
            "status": status, "result": result, "evidence_id": ev.id if ev is not None else None,
            "bound_to_accepted_artifact": bound, "allows_promotion": ok,
            "required_downstream_checks": output.get("required_downstream_checks") or [],
            "note": ("Belum ada governance harness downstream; status NOT_CONFIGURED ditampilkan apa adanya (§32)"
                     if not configured else "")}


def _changed_files(session: Session, report: EvidenceArtifact | None) -> list[str]:
    scope_ev = related_evidence(session, report, "SCOPE_CHECK")
    return list((collector.read_json(scope_ev) or {}).get("changed_files") or [])


def compute_gates(session: Session, task_id: str) -> dict[str, Any]:
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    project = session.get(Project, task.project_id)
    if project is None:
        raise NotFound("project not found")
    acc = acceptance_mod.latest_acceptance(session, task.id)
    acc_view = acceptance_mod.acceptance_view(session, acc) if acc else None
    run = _relevant_run(session, task, acc)
    report = latest_report(session, run.id) if run else None
    view = report_view(report)
    review = latest_review(session, run.id) if run else None
    pr = latest_pr(session, task.id)
    head = (pr.observed_head_sha or pr.accepted_head_sha) if pr else (
        acc.head_commit if acc else (view or {}).get("head_commit") or (run.head_commit if run else None))
    ci_head = latest_ci_evidence(session, project.id, head)
    gates: list[GateResult] = [_g0(session, task, project, pr)]

    # G1 static — provider CI is the source; verifier static checks support (and can fail) it.
    ci1, names1 = ci_gate_status(ci_head, "G1")
    v_static_fail = view is not None and (view.get("ruff_status") == "FAIL" or view.get("leak_scan_status") == "FAIL"
                                          or view.get("scope_status") == "FAIL")
    refs1 = [ci_head.id if ci_head else None, view["evidence_id"] if view else None]
    if ci1 == "FAIL" or v_static_fail:
        gates.append(_gate("G1", "FAIL", "Pemeriksaan statis gagal (CI provider atau verifier)", "provider CI + verifier",
                           refs1, reasons=names1))
    elif ci1 == "PASS":
        gates.append(_gate("G1", "PASS", f"CI statis lulus untuk {head[:12] if head else '?'}: {', '.join(names1)}",
                           "provider CI", refs1))
    elif ci1 == "PENDING":
        gates.append(_gate("G1", "PENDING", "CI provider masih berjalan", "provider CI", refs1))
    else:
        support = f" (verifier ruff: {view.get('ruff_status')})" if view else ""
        gates.append(_gate("G1", "NOT_PROVEN", "Belum ada hasil CI provider untuk commit ini" + support,
                           "provider CI", refs1))

    # G2 controlled tests — only the independent verifier counts (worker claims never do).
    if view is None:
        gates.append(_gate("G2", "NOT_PROVEN", "Belum ada verifikasi independen; klaim worker tidak dihitung",
                           "verifier"))
    else:
        vs = view["status"]
        stale = bool(run and run.head_commit and view.get("head_commit") and run.head_commit != view["head_commit"])
        stale = stale or bool(head and view.get("head_commit") and head != view["head_commit"])
        # §08/§50: a changed grader/lint/verifier/TaskSpec configuration makes the old result stale too
        current_cfg = current_config_hash(task)
        config_stale = vs == "PASS" and (current_cfg is None or view.get("config_hash") != current_cfg)
        mapping = {"PASS": "PASS", "FAIL": "FAIL", "QUARANTINED": "BLOCKED", "NOT_PROVEN": "NOT_PROVEN",
                   "BLOCKED": "BLOCKED"}
        status = "STALE" if (stale or config_stale) else mapping.get(vs, "UNKNOWN")
        summary = (f"Suite terpercaya: {view.get('tests_passed')} lulus, {view.get('tests_failed')} gagal, "
                   f"{view.get('tests_skipped')} dilewati" if view.get("tests_passed") is not None
                   else str(view.get("summary") or vs))
        if stale:
            summary = "Verifikasi tidak lagi mencakup head terbaru — perlu verifikasi ulang"
        elif config_stale:
            summary = ("Konfigurasi verifier/grader berubah atau tidak diketahui sejak verifikasi — "
                       "perlu verifikasi ulang")
        gates.append(_gate("G2", status, summary, "independent verifier (trusted suite)", [view["evidence_id"]],
                           reasons=[str(view.get("summary") or "")]))

    # G3 product tests — provider CI frontend job.
    ci3, names3 = ci_gate_status(ci_head, "G3")
    changed = _changed_files(session, report)
    touches_frontend = any(p.startswith("frontend/") for p in changed)
    if ci3 in ("PASS", "FAIL", "PENDING"):
        gates.append(_gate("G3", ci3, f"Job produk CI: {', '.join(names3)}", "provider CI",
                           [ci_head.id if ci_head else None]))
    elif ci3 == "NO_CHECKS" or ci3 == "NO_EVIDENCE":
        if not touches_frontend and ci3 == "NO_CHECKS":
            gates.append(_gate("G3", "NOT_APPLICABLE", "Tidak ada perubahan frontend dan tidak ada job produk",
                               "provider CI", [ci_head.id if ci_head else None]))
        else:
            gates.append(_gate("G3", "NOT_PROVEN", "Belum ada hasil job produk (frontend) dari CI provider",
                               "provider CI"))
    else:
        gates.append(_gate("G3", "NOT_PROVEN", f"Hasil job produk {ci3}", "provider CI",
                           [ci_head.id if ci_head else None]))

    # G4 review — Claude review + independent evidence; reviewer verdict alone never passes.
    g2 = gates[2].status
    if review is None:
        gates.append(_gate("G4", "PENDING", "Review checkpoint Claude belum diminta", "review_records"))
    elif review.status == "WAITING_QUOTA":
        gates.append(_gate("G4", "BLOCKED", "WAITING_QUOTA — review menunggu kuota; gate tidak dibuka",
                           "review_records", [review.id]))
    elif review.status == "NEEDS_DECISION":
        gates.append(_gate("G4", "BLOCKED", "Menunggu review manual melalui Claude Code resmi", "review_records",
                           [review.id], reasons=[review.status_reason]))
    elif review.status != "COMPLETED":
        gates.append(_gate("G4", "NOT_PROVEN", f"Review {review.status}: {review.status_reason}", "review_records",
                           [review.id]))
    elif review.mode == "MOCK":
        gates.append(_gate("G4", "NOT_PROVEN", "Review MOCK tidak dihitung", "review_records", [review.id]))
    elif review.verdict in PASS_VERDICTS:
        bound = view is not None and review.target_hash == view.get("artifact_hash")
        required = required_reviews(session, task, view, report)
        sec_needed = bool(required)
        specialist_recs = {k: latest_review(session, run.id, reviewer=k) if run else None for k in required}

        def _spec_ok(rec: Any) -> bool:
            return (rec is not None and rec.status == "COMPLETED" and rec.mode != "MOCK"
                    and rec.verdict in PASS_VERDICTS and view is not None
                    and rec.target_hash == view.get("artifact_hash"))

        failing = {k: r for k, r in specialist_recs.items() if not _spec_ok(r)}
        sec_ok = not failing
        static_ok = bool(view and view.get("docs_only")) and acceptance_mod.static_allowed(task)
        nr_gaps = [k for k, _q in NON_REGRESSION_SCOPE
                   if (review.non_regression or {}).get(k, {}).get("status") not in ("COVERED", "NOT_APPLICABLE")]
        high_impact = bool(view and view.get("impact") == "HIGH")
        if review.verdict == "PASS-STATIC" and not static_ok:
            gates.append(_gate("G4", "NOT_PROVEN", "PASS-STATIC hanya cukup untuk perubahan dokumen yang diizinkan "
                                                   "TaskSpec", "review_records", [review.id]))
        elif high_impact and nr_gaps:
            gates.append(_gate("G4", "NOT_PROVEN", "Perubahan berdampak tinggi: non-regression belum seluruhnya "
                                                   "dibuktikan (§31, §34)", "review_records", [review.id],
                               reasons=[f"non-regression '{k}' belum COVERED/NOT_APPLICABLE" for k in nr_gaps]))
        elif g2 == "PASS" and bound and sec_needed and not sec_ok:
            recs = [r for r in failing.values() if r is not None]
            status = "BLOCKED" if any(r.status != "COMPLETED" for r in recs) else (
                "FAIL" if any(r.verdict == "FAIL" for r in recs) else "PENDING")
            labels = {"SECURITY": "keamanan", "A11Y": "aksesibilitas/UX", "PRIVACY": "privasi/data", "SRE": "SRE/operasi"}
            names = ", ".join(labels.get(k, k) for k in failing)
            gates.append(_gate("G4", status, f"Review spesialis terpisah diperlukan (§15, §17) dan belum lulus: {names}",
                               "review_records", [review.id, *[r.id for r in recs]],
                               reasons=[f"{k}: {required[k]}" for k in failing]))
        elif g2 == "PASS" and bound:
            label = f"{review.verdict} + bukti independen (G2)" + (
                f" + review spesialis ({', '.join(required)})" if sec_needed else "")
            gates.append(_gate("G4", "PASS", label, "review_records + verifier",
                               [review.id, view["evidence_id"] if view else None,
                                *[r.id for r in specialist_recs.values() if r is not None]]))
        else:
            gates.append(_gate("G4", "NOT_PROVEN", f"{review.verdict} tanpa bukti independen yang terikat artefak sama",
                               "review_records", [review.id]))
    elif review.verdict == "FAIL":
        gates.append(_gate("G4", "FAIL", "Reviewer menemukan masalah; repair diperlukan", "review_records", [review.id]))
    elif review.verdict == "BLOCKED":
        gates.append(_gate("G4", "BLOCKED", review.status_reason or "Keputusan owner diperlukan", "review_records",
                           [review.id]))
    else:
        gates.append(_gate("G4", "NOT_PROVEN", f"Verdict {review.verdict}", "review_records", [review.id]))

    # G5 integration — retest on merged state after a human merge.
    if pr is None or not pr.merged:
        gates.append(_gate("G5", "PENDING", "Belum di-merge; retest pada merged state dilakukan setelah merge manusia",
                           "delivery_pull_requests"))
    else:
        ci_merge = latest_ci_evidence(session, project.id, pr.merge_commit_sha)
        identity_ok = bool(pr.accepted_head_sha and pr.observed_head_sha == pr.accepted_head_sha)
        delta_status, protected_hits = _merge_delta(pr, changed, project)
        if not identity_ok:
            gates.append(_gate("G5", "FAIL", "Head yang di-merge berbeda dari head yang diterima", "delivery_pull_requests",
                               [pr.id]))
        elif delta_status == "FAIL":
            gates.append(_gate("G5", "FAIL", "Merge commit membawa perubahan path terlindungi (policy/pipeline/verifier) "
                                             "di luar perubahan yang diverifikasi", "provider commit files",
                               [pr.id], reasons=protected_hits[:20]))
        elif ci_merge is None:
            gates.append(_gate("G5", "NOT_PROVEN", "Belum ada retest (CI) pada merge commit", "provider CI", [pr.id]))
        elif delta_status != "PASS":
            gates.append(_gate("G5", "NOT_PROVEN", "Daftar file merge commit belum teramati; perlindungan path "
                                                   "terlindungi pada merged state belum terbukti (sync PR)",
                               "provider commit files", [pr.id, ci_merge.id]))
        else:
            data = collector.read_json(ci_merge) or {}
            overall = str(data.get("overall") or "UNKNOWN")
            status = {"PASS": "PASS", "FAIL": "FAIL", "PENDING": "PENDING"}.get(overall, "NOT_PROVEN")
            gates.append(_gate("G5", status, f"CI pada merge commit {(pr.merge_commit_sha or '')[:12]}: {overall}; "
                                             "tidak ada path terlindungi baru", "provider CI", [ci_merge.id]))

    # G6 staging — only once the owner registered a staging target; then trusted runtime evidence decides.
    staging = _deployment_target(session, project, "staging")
    if staging is None:
        gates.append(_gate("G6", "NOT_APPLICABLE", "Belum ada target staging terdaftar untuk proyek ini", "project"))
    else:
        ev6, st6 = runtime_observation(session, project, [pr.merge_commit_sha if pr else None, head], "staging")
        summary6 = {"PASS": "Observasi runtime staging lulus", "FAIL": "Observasi runtime staging gagal"}.get(
            st6, f"Target staging terdaftar ({staging.root_id}); belum ada observasi runtime untuk commit ini")
        gates.append(_gate("G6", st6 if st6 in ("PASS", "FAIL") else "NOT_PROVEN", summary6,
                           "deployment-target + runtime evidence", [staging.id, ev6.id if ev6 else None]))

    # G7 human release — merge observed from the provider; AEOS has no merge capability.
    approvers, approver_decision = release_approvers(session, project)
    merged_src = "Bitbucket closed_by" if project.domain == "OFFICE" else "GitHub merged_by"
    if pr is None:
        gates.append(_gate("G7", "PENDING", "Belum ada PR", "delivery_pull_requests"))
    elif not pr.merged:
        gates.append(_gate("G7", "PENDING", "Menunggu merge oleh manusia (AEOS tidak melakukan merge)",
                           "delivery_pull_requests", [pr.id]))
    elif (pr.merged_by_type in BOT_TYPES) or (pr.merged_by_login or "").endswith("[bot]"):
        gates.append(_gate("G7", "FAIL", f"Merge dilakukan oleh akun non-manusia: {pr.merged_by_login}",
                           merged_src, [pr.id]))
    elif pr.merged_by_type == "User" and not approvers:
        gates.append(_gate("G7", "NOT_PROVEN", f"Merge oleh manusia teramati ({pr.merged_by_login}), tetapi approver "
                                               "rilis berwenang belum dicatat (keputusan POLICY release_approvers)",
                           merged_src, [pr.id]))
    elif pr.merged_by_type == "User" and (pr.merged_by_login or "").lower() in approvers:
        gates.append(_gate("G7", "PASS", f"Merge oleh approver berwenang: {pr.merged_by_login}", merged_src,
                           [pr.id, approver_decision],
                           reasons=["merged_by adalah observasi provider; AEOS tidak memiliki operasi merge"]))
    elif pr.merged_by_type == "User":
        gates.append(_gate("G7", "FAIL", f"Merge oleh akun yang tidak berwenang untuk rilis: {pr.merged_by_login}",
                           merged_src, [pr.id, approver_decision],
                           reasons=[f"approver berwenang: {', '.join(approvers)}"]))
    else:
        gates.append(_gate("G7", "UNKNOWN", "Pelaku merge tidak diketahui", merged_src, [pr.id]))

    # G8 post-release — only with a registered production target and a runtime observation after the merge.
    production = _deployment_target(session, project, "production")
    if production is None:
        gates.append(_gate("G8", "NOT_APPLICABLE", "Belum ada target deployment; tidak ada observasi pasca-rilis",
                           "project"))
    elif pr is None or not pr.merged:
        gates.append(_gate("G8", "PENDING", "Rilis produksi adalah tindakan manusia setelah merge", "project",
                           [production.id]))
    else:
        ev8, st8 = runtime_observation(session, project, [pr.merge_commit_sha], "production")
        summary8 = {"PASS": "Observasi runtime produksi pasca-rilis lulus",
                    "FAIL": "Observasi runtime produksi gagal — insiden/rollback oleh manusia"}.get(
            st8, f"Target produksi terdaftar ({production.root_id}); belum ada observasi pasca-rilis")
        gates.append(_gate("G8", st8 if st8 in ("PASS", "FAIL") else "NOT_PROVEN", summary8,
                           "deployment-target + runtime evidence", [production.id, ev8.id if ev8 else None]))

    by = {g.gate: g.status for g in gates}
    gov = governance_status(session, task, acc)
    ready = (by["G0"] == "PASS" and by["G2"] == "PASS" and by["G4"] == "PASS" and by["G1"] == "PASS"
             and by["G3"] in ("PASS", "NOT_APPLICABLE") and bool(acc_view and acc_view["valid"])
             and gov["allows_promotion"])
    sec_review = latest_review(session, run.id, reviewer="SECURITY") if run else None
    separate = separate_status(run, view, review, acc_view, pr)
    separate["governance"] = gov
    separate["delivery_provider"] = delivery_provider_status(project, session)
    separate["release_approvers"] = {"logins": approvers, "decision_id": approver_decision}
    separate["security_reviewer"] = {
        "required": security_review_required(task, view),
        "verdict": sec_review.verdict if sec_review else None,
        "status": sec_review.status if sec_review else None,
        "review_id": sec_review.id if sec_review else None,
    }
    needed = required_reviews(session, task, view, report)
    separate["specialist_reviews"] = {}
    for kind in SPECIALIST_KINDS:
        rec_k = latest_review(session, run.id, reviewer=kind) if run else None
        separate["specialist_reviews"][kind] = {
            "required": kind in needed, "reason": needed.get(kind), "verdict": rec_k.verdict if rec_k else None,
            "status": rec_k.status if rec_k else None, "review_id": rec_k.id if rec_k else None}
    separate["verifier"]["isolation"] = (view or {}).get("verifier_isolation")
    return {
        "task_id": task.id,
        "task_key": task.task_key,
        "title": task.title,
        "project_id": project.id,
        "domain": project.domain,
        "run_id": run.id if run else None,
        "head_commit": head,
        "gates": [asdict(g) for g in gates],
        "separate_status": separate,
        "acceptance": acc_view,
        "pull_request": pr_view(pr) if pr else None,
        "ready_for_human_merge": ready,
        "merge_authority": "HUMAN_ONLY",
        "checked_at": utcnow().isoformat(),
        "source": "task_specs, evidence_artifacts, review_records, acceptance_records, delivery_pull_requests",
        "mode": "LIVE" if (run is None or run.mode == "LIVE") else run.mode,
    }


OFFICE_DELIVERY_NOTE = (
    "Kode OFFICE berada di Bitbucket Cloud: push aeos/* (sha-pinned) dan PR lewat broker tepercaya, CI dari Bitbucket "
    "Pipelines/commit status. Nonaktif secara default (OFFICE default deny, §23) sampai ada otorisasi perusahaan "
    "tercatat yang mencantumkan bitbucket (deployment CLOUD), integrasi bitbucket/code enabled dan repository "
    "terverifikasi; Bitbucket Data Center NOT_SUPPORTED (ADR-0111).")


def delivery_provider_status(project: Project, session: Session | None = None) -> dict[str, Any]:
    """Which code/CI provider delivery uses for the project's domain — and, for OFFICE, whether the Bitbucket
    Cloud adapter may run and why not (DISABLED by default, NOT_SUPPORTED for Data Center, BLOCKED)."""
    if project.domain == "OFFICE":
        from aeos.delivery.bitbucket import DISABLED_REMEDY, LIVE_PROOF, office_delivery_readiness

        if session is None:
            return {"provider": "bitbucket", "ci": "bitbucket-pipelines", "status": "DISABLED",
                    "reason": OFFICE_DELIVERY_NOTE, "remedy": DISABLED_REMEDY, "reasons": [], "live_proof": LIVE_PROOF}
        r = office_delivery_readiness(session, project)
        reason = OFFICE_DELIVERY_NOTE if r.status != "AVAILABLE" else "Adapter Bitbucket Cloud siap; " + LIVE_PROOF
        return {"provider": "bitbucket", "ci": "bitbucket-pipelines", "status": r.status,
                "reason": reason + ("" if not r.reasons else " Alasan: " + "; ".join(r.reasons)), "remedy": r.remedy,
                "reasons": r.reasons, "deployment": r.deployment, "repo": r.repo, "live_proof": r.live_proof}
    return {"provider": "github", "ci": "github-actions", "status": "AVAILABLE", "reason": "", "remedy": ""}


def separate_status(run: WorkerRun | None, view: dict[str, Any] | None, review: Any, acc_view: dict[str, Any] | None,
                    pr: PullRequestRecord | None) -> dict[str, Any]:
    """§35: worker-reported / verifier / reviewer / accepted / merged are separate fields."""
    return {
        "worker_reported": {"status": run.worker_reported_status if run else None,
                            "exit_code": run.exit_code if run else None, "trust": "UNTRUSTED_CLAIM"},
        "verifier": {"status": view["status"] if view else None, "evidence_id": view["evidence_id"] if view else None},
        "reviewer": {"verdict": review.verdict if review else None, "status": review.status if review else None,
                     "review_id": review.id if review else None},
        "accepted": {"status": acc_view["acceptance_status"] if acc_view else None,
                     "valid": acc_view["valid"] if acc_view else False,
                     "acceptance_id": acc_view["id"] if acc_view else None},
        "merged": {"status": (pr.state if pr else None), "merged_by": pr.merged_by_login if pr else None,
                   "merged_by_type": pr.merged_by_type if pr else None},
    }


def pr_view(pr: PullRequestRecord) -> dict[str, Any]:
    return {
        "id": pr.id, "project_id": pr.project_id, "domain": pr.domain, "task_id": pr.task_id, "run_id": pr.run_id,
        "acceptance_id": pr.acceptance_id, "provider": pr.provider, "repo": pr.repo_full_name, "number": pr.number,
        "url": pr.url, "head_ref": pr.head_ref, "base_ref": pr.base_ref, "accepted_head_sha": pr.accepted_head_sha,
        "initial_base_sha": pr.initial_base_sha, "observed_head_sha": pr.observed_head_sha,
        "observed_base_sha": pr.observed_base_sha, "bundle_hash": pr.bundle_hash,
        "bundle_evidence_id": pr.bundle_evidence_id, "state": pr.state, "merged": pr.merged,
        "merged_by_login": pr.merged_by_login, "merged_by_type": pr.merged_by_type,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None, "merge_commit_sha": pr.merge_commit_sha,
        "last_synced_at": pr.last_synced_at.isoformat() if pr.last_synced_at else None,
        "sync_status": pr.sync_status, "sync_error": pr.sync_error, "created_at": pr.created_at.isoformat(),
        "merge_authority": "HUMAN_ONLY",
    }
