"""§44/§47 topology experiments: a case slot that holds an ORDERED SET of TaskSpecs.

Topologies B (specialist on demand), C (bounded parallel) and D (company-role workflow) solve one case with more than
one task; A solves it with one implementer slice at a time. To compare them on the same suite (§44 "Bandingkan pola
pada feature/task suite yang sama"; §47 "Uji jumlah/topology agent per kelas tugas"), a slot binds every TaskSpec the
case was executed with, in order (``bind_additional_task``), and the trusted collector aggregates ONE sample per slot
from the canonical records of all of them (``derive_slot_sample``):

* outcome: ACCEPTED only when every task of the ordered set (and therefore the final one) was accepted; otherwise
  the status of the first task that was not - a partial result is never an accepted case;
* tokens, cost, repairs, reviews, interventions, permission/scope/security/regression counts - every numeric measure
  the per-task collector produces, including ones added later: summed over all tasks; a measure that any task left
  unknown (None or absent) stays unknown, never a zero; a yes/no measure is yes when any task says yes;
* latency: wall clock from the first attempt's start to the last attempt's finish (the case's real duration, so a
  parallel topology is not credited with the sum of its parts); queue wait: the longest any task waited;
* ``parallel_overlap_s``: how long implementation attempts of different tasks ran at the same time (WorkerRun
  timestamps) - the measured coordination shape; coordination tokens stay UNKNOWN (no trusted producer yet).

A slot with one task is collected exactly as before (``executor.derive_sample``). Nothing here runs a model.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.research import EvalRun

MAX_TASKS_PER_SLOT = 8
# Keys of the per-task sample with their own slot rule (identity, outcome, time); every other numeric key is summed
# with _sum_or_unknown and every other yes/no key is OR-ed, so a measure the collector gains later is aggregated
# without being silently dropped or turned from UNKNOWN into zero.
SPECIAL_KEYS = frozenset({"sample_id", "case_id", "repetition", "task_id", "status", "collector", "first_pass",
                          "latency_s", "queue_wait_s", "quota_delay_s", "unknown_fields", "quality"})


def slot_task_ids(binding: dict[str, Any] | None) -> list[str]:
    """The ordered TaskSpec ids of one slot binding (a single-task binding has only ``task_id``)."""
    if not binding:
        return []
    ids = binding.get("task_ids")
    if isinstance(ids, list) and ids:
        return [str(t) for t in ids if t]
    return [str(binding["task_id"])] if binding.get("task_id") else []


def bind_additional_task(session: Session, run_id: str, *, slot_id: str, task_id: str, actor_user_id: str,
                         viewer_domain: str | None = None, scoped: bool = False) -> EvalRun:
    """Append one more executed (or to-be-executed) TaskSpec to an already bound slot of a RUNNING run (ordered)."""
    from aeos.evaluation import executor
    from aeos.models.planning import TaskSpec

    run = executor.get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "RUNNING":
        raise Conflict(f"eval run is {run.status}; start it before binding tasks")
    bindings = [dict(b) for b in run.bindings or []]
    current = next((b for b in bindings if b.get("slot_id") == slot_id), None)
    if current is None:
        raise ValidationFailed(f"slot {slot_id} has no first task yet; bind it first (without append)")
    task = session.get(TaskSpec, task_id)
    if task is None or task.domain != (run.submitted_domain or "PERSONAL"):
        raise NotFound("task not found")  # an OFFICE task never feeds a PERSONAL/platform evaluation
    ids = slot_task_ids(current)
    if any(task_id in slot_task_ids(b) for b in bindings):
        raise Conflict("this task already fills a slot of the run", code="TASK_ALREADY_BOUND")
    other = executor._task_bound_elsewhere(session, task_id, run.id)
    if other:
        raise Conflict(f"this task already fills a slot of eval run {other}", code="TASK_ALREADY_BOUND")
    if len(ids) >= MAX_TASKS_PER_SLOT:
        raise ValidationFailed(f"a slot holds at most {MAX_TASKS_PER_SLOT} tasks")
    current["task_ids"] = [*ids, task_id]
    current.pop("invalid", None)
    run.bindings = [current if b.get("slot_id") == slot_id else b for b in bindings]
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.bind", target_type="eval_run",
                 target_id=run.id, domain=run.submitted_domain,
                 details={"slot_id": slot_id, "task_id": task_id, "append": True, "position": len(ids) + 1,
                          "bound_at": utcnow().isoformat()})
    return run


def _impl_windows(session: Session, task_ids: list[str]) -> list[tuple[str, Any, Any]]:
    from aeos.models.workforce import WorkerRun

    rows = session.scalars(select(WorkerRun).where(WorkerRun.task_id.in_(task_ids)))
    out = []
    for r in rows:
        if (r.run_type or "IMPLEMENTATION") != "IMPLEMENTATION":
            continue
        a, b = ensure_aware(r.started_at), ensure_aware(r.finished_at)
        if a is not None and b is not None and b >= a:  # type: ignore[operator]
            out.append((str(r.task_id), a, b))
    return out


def _overlap_seconds(windows: list[tuple[str, Any, Any]]) -> float:
    total = 0.0
    for i, (ta, a0, a1) in enumerate(windows):
        for tb, b0, b1 in windows[i + 1:]:
            if ta == tb:
                continue
            start, end = max(a0, b0), min(a1, b1)
            if end > start:
                total += (end - start).total_seconds()
    return round(total, 3)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sum_or_unknown(parts: list[dict[str, Any]], key: str) -> Any:
    vals = [p.get(key) for p in parts]
    nums: list[int | float] = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(nums) != len(vals):
        return None
    total = sum(nums)
    return float(total) if any(isinstance(v, float) for v in nums) else total


def aggregate(session: Session, binding: dict[str, Any], task_ids: list[str],
              parts: list[dict[str, Any]]) -> dict[str, Any]:
    """One slot sample from the per-task samples (see module docstring for every rule)."""
    first_bad = next((p for p in parts if p.get("status") != "ACCEPTED"), None)
    status = "ACCEPTED" if first_bad is None else first_bad.get("status")
    windows = _impl_windows(session, task_ids)
    latency = round((max(w[2] for w in windows) - min(w[1] for w in windows)).total_seconds(), 3) if windows else None
    queues: list[Any] = [p.get("queue_wait_s") for p in parts]
    sample: dict[str, Any] = {
        "sample_id": binding["slot_id"], "case_id": binding["case_id"], "repetition": binding["repetition"],
        "task_id": task_ids[-1], "task_ids": list(task_ids), "task_count": len(task_ids), "status": status,
        "collector": parts[0].get("collector"),
        "first_pass": all(bool(p.get("first_pass")) for p in parts),
        "latency_s": latency,
        "queue_wait_s": None if any(q is None for q in queues) else max(queues),
        "parallel_overlap_s": _overlap_seconds(windows) if windows else None,
        "quota_delay_s": None,
        "tasks": [{"task_id": t, "status": p.get("status"), "latency_s": p.get("latency_s"),
                   "repair_count": p.get("repair_count")} for t, p in zip(task_ids, parts, strict=True)],
    }
    keys: list[str] = []
    for p in parts:
        keys += [k for k in p if k not in SPECIAL_KEYS and k not in keys]
    unknown = {u for p in parts for u in (p.get("unknown_fields") or [])}
    for key in keys:
        vals = [p.get(key) for p in parts]
        known = [v for v in vals if v is not None]
        if known and all(isinstance(v, bool) for v in known):
            sample[key] = any(bool(v) for v in known)  # e.g. provider_unavailable: any task hit it
        elif all(_is_num(v) for v in known):
            sample[key] = _sum_or_unknown(parts, key)  # None (or absent) in any task -> UNKNOWN for the slot
            if sample[key] is None:
                unknown.add(key)
        # anything else (text/structures) is per task only and stays in the per-task records
    sample["unknown_fields"] = sorted(unknown)
    if binding.get("quality"):
        sample["quality"] = dict(binding["quality"])
    return sample


def derive_slot_sample(session: Session, run: EvalRun, binding: dict[str, Any]
                       ) -> tuple[str, dict[str, Any] | None, str]:
    """(PENDING|READY|INVALID, sample, reason) for one slot - the per-task rules of ``executor.derive_sample`` apply
    to every task of the ordered set; the slot is READY only when all of them are."""
    from aeos.evaluation import executor

    ids = slot_task_ids(binding)
    if len(ids) <= 1:
        return executor.derive_sample(session, run, binding)
    parts: list[dict[str, Any]] = []
    for i, tid in enumerate(ids, start=1):
        state, sample, why = executor.derive_sample(session, run, {**binding, "task_id": tid})
        if state != "READY" or sample is None:
            return state, None, f"task {i}/{len(ids)} ({tid}): {why}"
        parts.append(sample)
    return "READY", aggregate(session, binding, ids, parts), ""
