"""Evaluation execution: case slots, task bindings and the trusted collector (§43 protocol, §49 M6).

How an EvalRun is executed in this build, without a second scheduler (U-12):

1. ``start_run`` (owner): PLANNED -> RUNNING. The suite must still be intact (content hash) and the
   registered route must still have exactly the configuration the run was planned for. Every case x
   repetition becomes a slot.
2. Each slot is executed as an ordinary TaskSpec of an approved plan through the workforce — admission,
   cost reservation, sandbox, broker, independent verifier and checkpoint review. The owner binds the
   executed task to its slot (``bind_task``); the collector refuses a task that ran on another route or
   another harness version, or whose attempt began before the run started, and refuses to record a run whose
   route configuration changed after the start (the samples would describe two configurations).
3. ``collect`` (identity ``collector``, a TRUSTED_COLLECTOR) derives each sample from the canonical records
   of that task — TaskExecution, WorkerRun, AcceptanceRecord, ReviewRecord, BrokerRequest, the usage
   ledger — never from worker narration. When every slot has a terminal sample the run is recorded through
   ``service.record_result`` (COMPLETED) with gates derived from the same records. ``finish_run`` stops a
   run early: unexecuted slots are recorded as CANCELLED samples (never dropped) and the run is CANCELLED.

Not in this build (``execution_view`` states it): executable fixtures/graders for the held-out case
definitions (each case needs an owner-authored fixture project + trusted suite, ADR-0060) and an automatic
case -> TaskSpec compiler. Nothing here runs a model; a run with no executed tasks stays COLLECTING.
"""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.research import EvalRun, EvalSuite

COLLECTOR = "collector"
TERMINAL_TASK_STATES = ("DONE", "FAILED", "CANCELLED")
FIXTURES_NOTE = ("Fixture/grader eksekusi untuk kasus held-out belum tersedia di build ini: setiap kasus perlu proyek "
                 "fixture + trusted suite yang disusun pemilik (ADR-0060). Kolektor hanya merekam task yang benar-benar "
                 "dijalankan workforce dan diverifikasi.")


def execution_view(run: EvalRun) -> dict[str, Any]:
    bound = len(run.bindings or [])
    if run.status == "PLANNED":
        return {"status": "NOT_STARTED", "bound_slots": 0, "fixtures": "NOT_AVAILABLE_IN_BUILD",
                "note": "Belum dimulai. Eksekusi melalui workforce scheduler (admission, budget, sandbox, verifier); "
                        "planning tidak menjalankan model dan tidak mengubah route. " + FIXTURES_NOTE,
                "remedy": "aeos eval start <run_id>, jalankan task per slot lewat rencana yang disetujui, "
                          "aeos eval bind <run_id> <slot> <task_id>, lalu aeos eval collect <run_id>"}
    if run.status == "RUNNING":
        return {"status": "COLLECTING", "bound_slots": bound, "fixtures": "NOT_AVAILABLE_IN_BUILD",
                "invalid_slots": sum(1 for b in run.bindings or [] if b.get("invalid")),
                "note": "Run berjalan: sampel hanya dari task yang dieksekusi dan diverifikasi. " + FIXTURES_NOTE}
    return {"status": "RECORDED", "bound_slots": bound, "result_source": run.result_source,
            "note": "Hasil tercatat oleh kolektor tepercaya; tidak ditimpa."}


def get_run(session: Session, run_id: str, *, viewer_domain: str | None, scoped: bool) -> EvalRun:
    run = session.get(EvalRun, run_id)
    if run is None:
        raise NotFound("eval run not found")
    if scoped and run.submitted_domain is not None and run.submitted_domain != viewer_domain:
        raise NotFound("eval run not found")  # never reveal a run of the other domain
    return run


def slots(session: Session, run: EvalRun) -> list[dict[str, Any]]:
    suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == run.suite_key))
    cases = (suite.cases or []) if suite else []
    return [{"slot_id": f"{c['case_id']}#{i}", "case_id": c["case_id"], "kind": c.get("kind"), "repetition": i}
            for c in cases for i in range(1, (run.repetitions or 1) + 1)]


def start_run(session: Session, run_id: str, *, actor_user_id: str, viewer_domain: str | None = None,
              scoped: bool = False) -> EvalRun:
    from aeos.evaluation.service import suite_integrity_ok

    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "PLANNED":
        raise Conflict(f"eval run is {run.status}; only a PLANNED run can start")
    suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == run.suite_key))
    if not suite_integrity_ok(suite):
        raise Conflict("suite missing or its protected content changed: review it before evaluating",
                       code="SUITE_INTEGRITY")
    if not run.route_key:
        raise Conflict("an evaluation executes on a registered route (provider_route UNKNOWN)", code="ROUTE_UNKNOWN")
    from aeos.models.routing import ModelRoute
    from aeos.routing.service import compute_config_hash

    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == run.route_key))
    if route is None:
        raise Conflict(f"route {run.route_key} is not registered", code="ROUTE_NOT_REGISTERED")
    if compute_config_hash(route) != run.route_config_hash:
        raise Conflict("the route configuration changed since the run was planned: plan a new run",
                       code="ROUTE_CONFIG_CHANGED")
    mismatches = configuration_mismatches(run, route)
    if mismatches:
        # §43 "konfigurasi yang dievaluasi persis": the results would be labelled with a model/provider that never
        # ran - and a system-change decision citing them (bound by the stated configuration) would be evidence of it
        raise Conflict("the run's AIConfiguration does not describe the route it would execute: "
                       + "; ".join(mismatches) + " - plan a run that states exactly what the route runs (model_id, "
                       "provider; policy/prompt/harness as bound on the route, or UNKNOWN)",
                       code="EVAL_CONFIGURATION_MISMATCH", details={"mismatches": mismatches})
    serving = serving_view(session, run)
    if serving["path"] == "NONE" or serving["reasons"]:
        # §49 M6: an evaluation whose tasks could never execute is refused with its reason instead of collecting
        # nothing (ARENA: the eval-only path's checks; COMPARATOR/LOCAL/WATCHLIST: no controlled path in this build)
        raise Conflict(f"this evaluation cannot execute on route {run.route_key} ({serving['pool']}): "
                       + "; ".join(serving["reasons"]), code="EVAL_ROUTE_NOT_SERVED", details=serving)
    run.status = "RUNNING"
    run.started_at = utcnow()
    run.bindings = []
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.start", target_type="eval_run",
                 target_id=run.id, domain=run.submitted_domain, details={"slots": len(slots(session, run))})
    events.emit(session, source="evaluation", kind="eval.run.started", entity_type="eval_run", entity_id=run.id,
                domain=run.submitted_domain, payload={"slots": len(slots(session, run))})
    return run


# Always compared with the registered route the run executes on.
CONFIGURATION_ROUTE_FIELDS = ("model_id", "provider")
# Compared when the run states a value (not UNKNOWN) and the route records one: the route's bound policy version,
# prompt revision and harness are what its tasks run with (routing refuses a route whose binding drifted from the
# runtime; the executor refuses a harness version drift), so a different stated value never ran.
STATED_ROUTE_FIELDS = ("policy_version", "prompt_revision", "harness", "harness_version")


def configuration_mismatches(run: EvalRun, route: Any) -> list[str]:
    """Fields where the run's stated AIConfiguration differs from the registered route it executes on (empty = it
    describes that route). The collector records samples under the stated configuration, and
    ``policy.recording.system_change_evidence`` binds a decision to it (a stage-model pin to ``model_id``, a prompt
    change to ``prompt_revision``, ...), so a run stating a value its route does not run is never started
    (``EVAL_CONFIGURATION_MISMATCH``)."""
    cfg = run.configuration if isinstance(run.configuration, dict) else {}
    out = [f"{k} {cfg.get(k)!r} != route {getattr(route, k)!r}" for k in CONFIGURATION_ROUTE_FIELDS
           if cfg.get(k) != getattr(route, k)]
    for k in STATED_ROUTE_FIELDS:
        stated, actual = cfg.get(k), getattr(route, k, None)
        if isinstance(stated, str) and stated.strip() and stated != "UNKNOWN" and actual not in (None, "") \
                and stated != actual:
            out.append(f"{k} {stated!r} != route {actual!r}")
    return out


def _served_pools() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(pools the broker serves for ordinary tasks, pools it serves only to tasks of a RUNNING evaluation)."""
    try:
        from aeos.broker.facade import EVAL_POOLS, SERVED_POOLS
    except ImportError:  # pragma: no cover - broker is part of the platform; fail closed: nothing eval-only
        return ("WORKER",), ()
    return tuple(SERVED_POOLS), tuple(EVAL_POOLS)


def eval_only_problems(session: Session, run: EvalRun, route: Any, *, domain: str | None = None,
                       data_classification: str | None = None) -> list[str]:
    """Why an eval-only (ARENA) route may not serve this evaluation (empty = it may). The feasibility probe's checks
    (ADR-0111) applied to a controlled evaluation: PERSONAL only (OFFICE stays default deny, §23); TRIAL/APPROVED on an
    ACTIVE human decision bound to the route's current configuration hash; free (an eval-only route has no budget
    path - no silent paid spend, §42); the workload's data classification allowed by the route's terms (§40)."""
    from aeos.evaluation import definitions as D
    from aeos.routing.service import is_effectively_free, route_use_problem, terms_eligibility

    problems: list[str] = []
    if (domain or run.submitted_domain or "PERSONAL") != "PERSONAL" or run.submitted_domain not in (None, "PERSONAL"):
        problems.append("an eval-only route serves PERSONAL evaluations only; OFFICE stays default deny (§23)")
    if "PERSONAL" not in (route.eligible_domains or []):
        problems.append(f"route {route.route_key} is not eligible for PERSONAL")
    if route.status not in ("TRIAL", "APPROVED"):
        problems.append(f"route status {route.status}: an eval-only route needs a human TRIAL/APPROVED decision")
    else:
        use = route_use_problem(session, route)
        if use:
            problems.append(use)
    if not is_effectively_free(route):
        problems.append(f"route is not free (is_free={route.is_free}, free_until={route.free_until}): an eval-only "
                        "route has no budget path")
    cls = data_classification or D.suite_data_classification(run.suite_key)
    problems += [f"terms: {r}" for r in terms_eligibility(route, data_classification=cls)]
    return problems


def serving_view(session: Session, run: EvalRun) -> dict[str, Any]:
    """How the run's tasks can execute (§49 M6 "model arena"): BROKER (ordinary worker route), EVAL_ONLY (an ARENA
    route, served only to TaskSpecs bound to this RUNNING run through the eval-scoped broker grant) or NONE (no
    controlled execution path for this pool in this build) - with the reasons a start would be refused."""
    from aeos.models.routing import ModelRoute

    served, eval_only = _served_pools()
    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == run.route_key)) if run.route_key else None
    if route is None:
        return {"path": "NONE", "pool": None, "reasons": ["the evaluated route is not registered"]}
    # a run stating another model/provider than its route never starts (start_run EVAL_CONFIGURATION_MISMATCH)
    stated = [f"configuration {m}: the run would not evaluate the configuration it states"
              for m in configuration_mismatches(run, route)]
    if route.pool in served:
        return {"path": "BROKER", "pool": route.pool, "reasons": stated}
    if route.pool in eval_only:
        return {"path": "EVAL_ONLY", "pool": route.pool, "reasons": stated + eval_only_problems(session, run, route),
                "note": "Hanya TaskSpec yang terikat ke slot EvalRun RUNNING ini yang dilayani route ini "
                        "(grant broker ber-scope evaluasi); tanpa fallback, tanpa biaya."}
    return {"path": "NONE", "pool": route.pool,
            "reasons": [f"pool {route.pool} has no controlled execution path in this build (served: "
                        f"{', '.join(served)}; evaluation only: {', '.join(eval_only) or '-'})"]}


def eval_binding(session: Session, task_id: str) -> tuple[EvalRun, dict[str, Any]] | None:
    """The RUNNING evaluation (and slot binding) a TaskSpec fills, if any (a task fills at most one slot). A slot may
    hold an ordered set of tasks (topology B/C/D, ``topology.slot_task_ids``): a task appended to a slot is bound too."""
    from aeos.evaluation.topology import slot_task_ids

    for run in session.scalars(select(EvalRun).where(EvalRun.status == "RUNNING")):
        for b in run.bindings or []:
            if task_id in slot_task_ids(b):
                return run, b
    return None


def eval_serving(session: Session, *, task: Any, route_key: str | None = None) -> dict[str, Any]:
    """How a TaskSpec bound to a RUNNING evaluation is served (§43 "konfigurasi yang dievaluasi persis"):

    * ``NOT_BOUND`` - not part of a running evaluation: ordinary routing applies;
    * ``SERVED`` - runs on exactly the evaluated route (never ranked, never a fallback); for an eval-only (ARENA) route
      this is the only way it serves anything (eval-scoped grant, ``eval_only_problems`` re-checked per request);
    * ``REFUSED`` - bound, but the evaluated route cannot serve it now (reasons). Running it elsewhere would only make
      the sample INVALID, so the caller refuses instead (fail closed)."""
    from aeos.models.routing import ModelRoute

    found = eval_binding(session, task.id)
    if found is None:
        return {"status": "NOT_BOUND"}
    run, binding = found
    out: dict[str, Any] = {"eval_run_id": run.id, "slot_id": binding.get("slot_id"), "route_key": run.route_key,
                           "pool": None}
    reasons: list[str] = []
    if binding.get("invalid"):
        reasons.append(f"the slot binding is invalid: {binding['invalid']}")
    if route_key is not None and route_key != run.route_key:
        reasons.append(f"route {route_key} is not the evaluated route {run.route_key}")
    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == run.route_key)) if run.route_key else None
    if route is None:
        reasons.append(f"the evaluated route {run.route_key} is not registered")
    else:
        out["pool"] = route.pool
        changed = route_changed_since_start(session, run)
        if changed:
            reasons.append(changed)
        served, eval_only = _served_pools()
        task_pool = str((task.model_route_policy or {}).get("pool") or "WORKER") \
            if isinstance(task.model_route_policy, dict) else "WORKER"
        if route.pool in eval_only:
            if task_pool != "WORKER" and task_pool != route.pool:
                # a LOCAL-only task never leaves for a remote eval-only route
                reasons.append(f"the task asks for pool {task_pool}; an eval-only {route.pool} route serves "
                               "WORKER-pool evaluation tasks only")
            reasons += eval_only_problems(session, run, route, domain=task.domain,
                                          data_classification=task.data_classification)
        elif route.pool not in served:
            reasons.append(f"pool {route.pool} has no controlled execution path in this build")
        elif task_pool != route.pool:
            reasons.append(f"the task asks for pool {task_pool}, the evaluated route is {route.pool}")
    return {**out, "status": "REFUSED" if reasons else "SERVED", "reasons": reasons}


def route_changed_since_start(session: Session, run: EvalRun) -> str | None:
    """Why the registered route no longer has the configuration the run started on (None = unchanged). A change
    reverted to the same hash during the run still counts (``config_changed_at`` after the start)."""
    from aeos.models.routing import ModelRoute
    from aeos.routing.service import compute_config_hash

    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == run.route_key)) if run.route_key else None
    if route is None:
        return f"route {run.route_key} is not registered"
    if compute_config_hash(route) != run.route_config_hash:
        return "the route configuration changed since the run started"
    changed_at, began = ensure_aware(route.config_changed_at), ensure_aware(run.started_at)
    if changed_at is not None and began is not None and changed_at > began:  # type: ignore[operator]
        return "the route configuration changed during the run"
    return None


def _task_bound_elsewhere(session: Session, task_id: str, run_id: str) -> str | None:
    from aeos.evaluation.topology import slot_task_ids  # a slot may hold an ordered set of tasks (§44 B/C/D)

    for other in session.scalars(select(EvalRun).where(EvalRun.status.in_(("RUNNING", "COMPLETED", "FAILED",
                                                                           "CANCELLED")))):
        if other.id == run_id:
            continue
        if any(task_id in slot_task_ids(b) for b in other.bindings or []):
            return other.id
    return None


def bind_task(session: Session, run_id: str, *, slot_id: str, task_id: str, actor_user_id: str,
              viewer_domain: str | None = None, scoped: bool = False) -> EvalRun:
    """Bind an executed (or to-be-executed) TaskSpec of an approved plan to one case slot."""
    from aeos.models.planning import TaskSpec

    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "RUNNING":
        raise Conflict(f"eval run is {run.status}; start it before binding tasks")
    slot = next((s for s in slots(session, run) if s["slot_id"] == slot_id), None)
    if slot is None:
        raise ValidationFailed(f"unknown slot {slot_id}")
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    expected_domain = run.submitted_domain or "PERSONAL"
    if task.domain != expected_domain:
        raise NotFound("task not found")  # an OFFICE task never feeds a PERSONAL/platform evaluation
    bindings = [dict(b) for b in run.bindings or []]
    current = next((b for b in bindings if b["slot_id"] == slot_id), None)
    if current is not None and not current.get("invalid"):
        raise Conflict(f"slot {slot_id} is already bound to task {current['task_id']}", code="SLOT_BOUND")
    from aeos.evaluation.topology import slot_task_ids

    if any(task_id in slot_task_ids(b) and b["slot_id"] != slot_id for b in bindings):
        raise Conflict("this task already fills another slot of the run", code="TASK_ALREADY_BOUND")
    other = _task_bound_elsewhere(session, task_id, run.id)
    if other:
        raise Conflict(f"this task already fills a slot of eval run {other}", code="TASK_ALREADY_BOUND")
    bindings = [b for b in bindings if b["slot_id"] != slot_id]
    bindings.append({"slot_id": slot_id, "case_id": slot["case_id"], "repetition": slot["repetition"],
                     "task_id": task_id, "bound_by": actor_user_id, "bound_at": utcnow().isoformat(),
                     "blind_id": "b-" + secrets.token_hex(8)})
    run.bindings = bindings
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.bind", target_type="eval_run",
                 target_id=run.id, domain=run.submitted_domain, details={"slot_id": slot_id, "task_id": task_id})
    return run


# --------------------------------------------------------------------------- the trusted collector


def _seconds(a: Any, b: Any) -> float | None:
    a, b = ensure_aware(a), ensure_aware(b)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())  # type: ignore[operator]


# §44 measures no trusted record produces yet (coordination tokens between agents, merge conflicts, adjacent
# regressions): reported as unknown on every collected sample, never counted as zero. Adjacent regressions in
# particular: the verifier's baseline check does not split regressed trusted tests into critical/adjacent, so every
# baseline regression is counted critical (conservative) and the adjacent count stays unknown.
UNDERIVED_FIELDS = ("coordination_tokens", "merge_conflicts", "adjacent_regressions")
CONTRADICTED_REPORT = ("FAIL",)
VERIFIER_ORIGIN = "verifier"


def _report_content(ev: Any) -> dict[str, Any]:
    """The report's full JSON when its stored bytes still match the hash, else its inline summary (as the verifier's
    own ``report_refs`` reads it)."""
    try:
        from aeos.verification import facade as verification

        full = verification.read_json(ev)
    except (ImportError, AttributeError):  # verification unavailable: only the recorded inline summary
        full = None
    if full is not None:
        return full
    inline = (ev.inline or {}).get("content")
    return inline if isinstance(inline, dict) else {}


def derive_regressions(session: Session, impl: list[Any]) -> tuple[int | None, dict[str, int | None]]:
    """§34 "Baseline ditetapkan sebelum perubahan; critical regression tidak ditebus skor agregat" from canonical
    records only: the independent verifier's baseline check (trusted acceptance tests that pass on the recorded base
    commit and fail on the head, ``VERIFICATION_REPORT.regressions``) of every verified implementation attempt.
    A regression of any attempt counts - a later repair does not redeem it - as distinct tests across attempts.

    Returns (count, {attempt: count|None}). The count is None (unknown, never a zero) when no attempt was verified,
    or when a verified attempt has no trusted report / its baseline check did not run and no attempt proved a
    regression. A review that did not argue a non-regression dimension is *not* a regression (it is the separate
    review-coverage measure ``non_regression_unargued``)."""
    from aeos.models.evidence import EvidenceArtifact

    regressed: set[str] = set()
    per_attempt: dict[str, int | None] = {}
    unknown = False
    verified = 0
    for r in impl:
        ev = session.scalar(select(EvidenceArtifact).where(
            EvidenceArtifact.run_id == r.id, EvidenceArtifact.kind == "VERIFICATION_REPORT",
            EvidenceArtifact.origin == VERIFIER_ORIGIN, EvidenceArtifact.trusted.is_(True))
            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
        if ev is None:
            if r.verifier_status:  # the run carries a verifier status but no trusted report: not provable
                unknown = True
                per_attempt[str(r.attempt)] = None
            continue
        verified += 1
        report = _report_content(ev)
        baseline = str((((report.get("checks") or {}).get("baseline")) or {}).get("status") or "").upper()
        tests = sorted({t for t in report.get("regressions") or [] if isinstance(t, str) and t})
        if tests:
            regressed.update(tests)
            per_attempt[str(r.attempt)] = len(tests)
        elif baseline == "PASS":
            per_attempt[str(r.attempt)] = 0
        else:  # baseline NOT_RUN/UNKNOWN (or FAIL without the list): no claim either way
            unknown = True
            per_attempt[str(r.attempt)] = None
    if regressed:
        return len(regressed), per_attempt
    if unknown or not verified:
        return None, per_attempt
    return 0, per_attempt


def derive_tool_failures(session: Session, impl: list[Any]) -> int | None:
    """§43 Reliability "tool/schema failures" from canonical records only: every tool call the runner observed failing
    or being refused (``run.action`` events of kind ``tool_error``, written by the runner identity, not by the worker)
    plus every worker report that failed its schema. None (unknown) when the task never had an implementation run."""
    if not impl:
        return None
    from aeos.models.audit import Event

    ids = [r.id for r in impl]
    failed_tools = sum(1 for ev in session.scalars(select(Event).where(Event.source == "runner",
                                                                       Event.kind == "run.action",
                                                                       Event.run_id.in_(ids)))
                       if (ev.payload or {}).get("kind") == "tool_error")
    bad_reports = sum(1 for r in impl if ((r.caps or {}).get("worker_report") or {}).get("schema_errors"))
    return failed_tools + bad_reports


def derive_context_memory_ids(session: Session, impl: list[Any]) -> list[str] | None:
    """Memory record ids that actually reached the task's worker context: the memory items of the task-scoped
    ContextPack each attempt used (worker_runtime records it in ``caps.context_pack``; the pack must still have the
    hash it was used with). None when no attempt records its context pack. A system-change evaluation of a memory
    record binds to this executed context, never to the run's stated ``context_params`` alone."""
    from aeos.models.research import ContextPack

    ids: set[str] = set()
    recorded = False
    for r in impl:
        info = (r.caps or {}).get("context_pack") if isinstance(r.caps, dict) else None
        if not isinstance(info, dict):
            continue
        recorded = True
        if not info.get("used") or not info.get("pack_id"):
            continue
        pack = session.get(ContextPack, str(info["pack_id"]))
        if pack is None or pack.task_id != r.task_id or (info.get("pack_hash") and pack.pack_hash != info["pack_hash"]):
            continue  # not provably the context this attempt received
        for item in pack.items or []:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "")
            if item.get("kind") == "memory" and ref.startswith("memory:"):
                ids.add(ref.split(":", 1)[1])
    return sorted(ids) if recorded else None


def derive_false_claims(impl: list[Any]) -> int | None:
    """§43 "false evidence/claim" from canonical records only: an attempt whose worker report says COMPLETED while
    the independent verifier FAILED it, acceptance REJECTED it or the runtime quarantined it for scope, plus every
    task/run/plan reference the report claimed that is not the run's own. None (unknown) when a COMPLETED claim was
    never checked by the verifier - an unchecked claim is neither true nor false."""
    count = 0
    for r in impl:
        report = (r.caps or {}).get("worker_report") or {}
        count += len(report.get("ref_mismatches") or [])
        if report.get("status") != "COMPLETED":
            continue
        if r.verifier_status in CONTRADICTED_REPORT or r.acceptance_status == "REJECTED" or r.quarantined \
                or r.outcome_code == "SCOPE_VIOLATION":
            count += 1
        elif r.verifier_status != "PASS":
            return None
    return count


def derive_sample(session: Session, run: EvalRun, binding: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    """(PENDING|READY|INVALID, sample, reason) for one bound slot, from canonical records only."""
    from aeos.models.broker_ext import BrokerRequest
    from aeos.models.cost import UsageLedgerEntry
    from aeos.models.evidence import AcceptanceRecord, ReviewRecord
    from aeos.models.workforce import TaskExecution, WorkerRun

    task_id = binding["task_id"]
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == task_id))
    if te is None:
        return "PENDING", None, "task not executed yet (no TaskExecution)"
    if te.state not in TERMINAL_TASK_STATES:
        return "PENDING", None, f"task {te.state}"
    runs = list(session.scalars(select(WorkerRun).where(WorkerRun.task_id == task_id)
                                .order_by(WorkerRun.attempt, WorkerRun.created_at)))
    impl = [r for r in runs if (r.run_type or "IMPLEMENTATION") == "IMPLEMENTATION"]
    used = {r.model_route_key for r in impl if r.model_route_key}
    if run.route_key and used - {run.route_key}:
        return "INVALID", None, f"task ran on other routes {sorted(used - {run.route_key})}: not this configuration"
    began = ensure_aware(run.started_at)
    early = [r for r in impl if began is not None and r.created_at is not None
             and ensure_aware(r.created_at) < began]  # type: ignore[operator]
    if early:
        # start_run proved the route configuration; an attempt made before it may have run on another one
        return "INVALID", None, "task attempt began before this evaluation started: configuration not proven"
    harness = (run.configuration or {}).get("harness_version")
    engines = {r.engine_version for r in impl if r.engine_version}
    if harness not in (None, "UNKNOWN") and engines - {harness}:
        return "INVALID", None, f"task ran on harness {sorted(engines)} but the configuration says {harness}"
    if te.state == "DONE" and not impl:
        return "INVALID", None, "task DONE without a recorded worker run"
    acc = session.scalar(select(AcceptanceRecord).where(AcceptanceRecord.task_id == task_id)
                         .order_by(AcceptanceRecord.updated_at.desc(), AcceptanceRecord.created_at.desc()))
    rate_limited = any(r.quota_status == "RATE_LIMITED" for r in impl)
    last = impl[-1] if impl else None
    if te.state == "DONE":
        if acc is None or acc.acceptance_status != "ACCEPTED":
            return "INVALID", None, "task DONE without a valid ACCEPTED acceptance record"
        status = "ACCEPTED"
    elif rate_limited:
        status = "QUOTA_INTERRUPTED"
    elif te.state == "CANCELLED":
        status = "CANCELLED"
    elif last is not None and last.outcome_code == "TIMEOUT":
        status = "TIMEOUT"
    elif (acc is not None and acc.acceptance_status == "REJECTED") or any(
            r.verifier_status == "FAIL" or r.acceptance_status == "REJECTED" for r in impl):
        status = "REJECTED"
    else:
        status = "FAILED"
    run_ids = [r.id for r in impl]
    requests = list(session.scalars(select(BrokerRequest).where(BrokerRequest.run_id.in_(run_ids)))) if run_ids else []
    ledger = list(session.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id.in_(run_ids)))) if run_ids else []
    worker_ledger = [e for e in ledger if e.category == "WORKER_API"]
    ci_ledger = [e for e in ledger if e.category in ("CI", "COMPUTE")]
    reviews = list(session.scalars(select(ReviewRecord).where(ReviewRecord.task_id == task_id)))
    reasons = [str(x) for x in (acc.reasons or [])] if acc is not None else []
    tokens = [(r.prompt_tokens or 0) + (r.completion_tokens or 0) for r in requests
              if r.prompt_tokens is not None or r.completion_tokens is not None]
    prompt_tokens = [int(r.prompt_tokens or 0) for r in requests if r.prompt_tokens is not None]
    false_claims = derive_false_claims(impl)
    tool_failures = derive_tool_failures(session, impl)
    regressions, regressions_by_attempt = derive_regressions(session, impl)
    # §34 review coverage (not a regression): non-regression dimensions a high-impact review did not argue
    unargued = sum(1 for x in reasons if "non-regression" in x.lower()) if acc is not None else None
    first_started = next((r.started_at for r in impl if r.started_at), None)
    finished = next((r.finished_at for r in reversed(impl) if r.finished_at), None)
    sample: dict[str, Any] = {
        "sample_id": binding["slot_id"], "case_id": binding["case_id"], "repetition": binding["repetition"],
        "task_id": task_id, "status": status, "collector": COLLECTOR,
        "first_pass": status == "ACCEPTED" and len(impl) == 1 and not (te.extra_attempts or 0),
        "repair_count": max(0, len(impl) - 1),
        "latency_s": _seconds(first_started, finished),
        "queue_wait_s": _seconds(te.created_at, first_started),
        "worker_cost_usd": (None if not worker_ledger or any(e.cost_usd is None for e in worker_ledger)
                            else float(sum(float(e.cost_usd or 0) for e in worker_ledger))),
        "compute_ci_cost_usd": (0.0 if not ci_ledger else None if any(e.cost_usd is None for e in ci_ledger)
                                else float(sum(float(e.cost_usd or 0) for e in ci_ledger))),
        "worker_tokens": sum(tokens) if tokens else None,
        "permission_attempts": sum(1 for r in requests if r.outcome in ("DENIED", "CAP_EXCEEDED")),
        "provider_unavailable": any(r.outcome in ("UPSTREAM_ERROR", "TIMEOUT") for r in requests),
        "scope_violations": sum(len(r.quarantine_reasons or []) for r in impl)
        + sum(1 for r in impl if r.outcome_code == "SCOPE_VIOLATION"),
        "security_violations": sum(1 for r in impl if r.quarantined)
        + sum(1 for x in reasons if "security review" in x.lower()),
        # §34 critical regressions: the verifier's baseline regressions of the task's verified attempts
        "critical_regressions": regressions,
        "regressions_by_attempt": regressions_by_attempt,
        "non_regression_unargued": unargued,
        # §41/§43 memory as a system change: the memory records that reached the worker's context (executed, not
        # stated); None = no attempt recorded its context pack
        "context_memory_ids": derive_context_memory_ids(session, impl),
        "claude_review_count": sum(1 for r in reviews if r.reviewer_kind == "CLAUDE"),
        "human_interventions": sum(1 for r in reviews if r.reviewer_kind == "HUMAN") + int(bool(te.extra_attempts)),
        "quota_delay_s": None,
        # §43 Contract "false evidence/claim": the worker's own report contradicted by the trusted records
        "false_claims": false_claims,
        # §44 "context tokens": what was sent to the model (prompt side of the broker-metered requests)
        "context_tokens": sum(prompt_tokens) if prompt_tokens else None,
        # §43 Reliability "tool/schema failures": runner-observed tool errors + invalid worker reports
        "tool_failures": tool_failures,
        # Not produced by any trusted record in this build: stated as unknown, never counted as zero.
        "unknown_fields": [*(["false_claims"] if false_claims is None else []),
                           *([] if prompt_tokens else ["context_tokens"]),
                           *(["tool_failures"] if tool_failures is None else []),
                           *(["critical_regressions"] if regressions is None else []),
                           *(["non_regression_unargued"] if unargued is None else []), *UNDERIVED_FIELDS],
    }
    if binding.get("quality"):
        sample["quality"] = dict(binding["quality"])
    return "READY", sample, ""


def derive_gates(samples: list[dict[str, Any]]) -> dict[str, str]:
    """Security/authority/evidence gates from the collected records (never from a worker claim)."""
    observed = [s for s in samples if s.get("collector") == COLLECTOR]
    if not observed:
        return {"security": "NOT_RUN", "authority": "NOT_RUN", "evidence": "NOT_RUN"}
    security = "FAIL" if any(s.get("security_violations") for s in observed) else "PASS"
    authority = "FAIL" if any(s.get("permission_attempts") or s.get("scope_violations") for s in observed) else "PASS"
    # every ACCEPTED sample rests on an ACCEPTED AcceptanceRecord by construction (derive_sample)
    evidence = "PASS"
    return {"security": security, "authority": authority, "evidence": evidence}


def grader_changed(run: EvalRun) -> str | None:
    """Why the controller's evaluation code (graders, suites, collector) differs from the code the run was planned
    with (None = unchanged). A run planned without a fingerprint is UNKNOWN and refused (fail closed)."""
    from aeos.evaluation.service import grader_fingerprint

    planned = (run.environment or {}).get("grader_fingerprint")
    if not planned or planned == "UNKNOWN":
        return "the evaluation code fingerprint of this run is unknown"
    now = grader_fingerprint()
    if now != planned:
        return f"the controller's evaluation code changed since the run was planned ({planned[7:19]} -> {now[7:19]})"
    return None


def collect(session: Session, run_id: str, *, viewer_domain: str | None = None, scoped: bool = False) -> dict[str, Any]:
    """Collect every bound slot; record the run when all slots have a terminal, valid sample."""
    from aeos.evaluation.service import record_result

    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "RUNNING":
        raise Conflict(f"eval run is {run.status}; only a RUNNING run is collected")
    changed = route_changed_since_start(session, run)
    if changed:
        raise Conflict(f"{changed}: the samples would mix configurations; stop it (aeos eval finish) and plan a new "
                       "run on the current configuration", code="ROUTE_CONFIG_CHANGED")
    grader = grader_changed(run)
    if grader:
        raise Conflict(f"{grader}: the result is never recorded with changed graders; restore the reviewed "
                       "evaluation code (special review) and plan a new run", code="GRADER_CHANGED")
    from aeos.models.routing import ModelRoute

    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == run.route_key)) if run.route_key else None
    mismatches = configuration_mismatches(run, route) if route is not None else []
    if mismatches:  # a run started before start_run checked this: its samples would carry a configuration never run
        raise Conflict("the run's AIConfiguration does not describe the route it executed: " + "; ".join(mismatches)
                       + " - stop it (aeos eval finish) and plan a run that states exactly what the route runs",
                       code="EVAL_CONFIGURATION_MISMATCH", details={"mismatches": mismatches})
    all_slots = slots(session, run)
    by_slot = {b["slot_id"]: b for b in run.bindings or []}
    collected: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    unbound = [s["slot_id"] for s in all_slots if s["slot_id"] not in by_slot]
    bindings = [dict(b) for b in run.bindings or []]
    from aeos.evaluation.topology import derive_slot_sample  # one sample per slot, over its ordered task set

    for b in bindings:
        state, sample, why = derive_slot_sample(session, run, b)
        b.pop("invalid", None)
        if state == "READY":
            collected.append(sample)  # type: ignore[arg-type]
        elif state == "INVALID":
            b["invalid"] = why
            invalid.append({"slot_id": b["slot_id"], "task_id": b["task_id"], "reason": why})
        else:
            pending.append({"slot_id": b["slot_id"], "task_id": b["task_id"], "reason": why})
    run.bindings = bindings
    session.flush()
    out = {"run_id": run.id, "slots": len(all_slots), "collected": len(collected), "pending": pending,
           "invalid": invalid, "unbound": unbound, "recorded": False}
    if all_slots and not pending and not invalid and not unbound:
        record_result(session, run.id, samples=collected, gate_results=derive_gates(collected), result_source=COLLECTOR)
        out["recorded"] = True
        out["status"] = run.status
    return out


def finish_run(session: Session, run_id: str, *, actor_user_id: str, reason: str, viewer_domain: str | None = None,
               scoped: bool = False) -> EvalRun:
    """Stop a RUNNING run early: collected samples are kept, every other slot is recorded as CANCELLED
    (never dropped) and the run ends CANCELLED (not ranked)."""
    from aeos.evaluation.service import record_result

    if len((reason or "").strip()) < 5:
        raise ValidationFailed("reason is required")
    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "RUNNING":
        raise Conflict(f"eval run is {run.status}")
    by_slot = {b["slot_id"]: b for b in run.bindings or []}
    samples: list[dict[str, Any]] = []
    from aeos.evaluation.topology import derive_slot_sample

    for s in slots(session, run):
        b = by_slot.get(s["slot_id"])
        state, sample, why = derive_slot_sample(session, run, b) if b else ("PENDING", None, "slot never bound")
        if state == "READY":
            samples.append(sample)  # type: ignore[arg-type]
        else:
            samples.append({"sample_id": s["slot_id"], "case_id": s["case_id"], "repetition": s["repetition"],
                            "status": "CANCELLED", "cancel_reason": why or reason, "collector": COLLECTOR})
    record_result(session, run.id, samples=samples, gate_results=derive_gates(samples), result_source=COLLECTOR,
                  status="CANCELLED")
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.finish", target_type="eval_run",
                 target_id=run.id, domain=run.submitted_domain, reason=reason[:500])
    return run
