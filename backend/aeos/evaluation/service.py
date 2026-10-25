"""Model/configuration evaluation and leaderboard (§43, §44, ADR-0081).

* Unit of evaluation = full AIConfiguration (model/version + provider route + harness + prompt +
  tools + policy + context params; local builds also quantization/inference backend/hardware); results
  with the same model name on another route are never merged — grouping is by configuration hash, and
  by topology when a topology (§44 A-D) is declared.
* Security/authority/evidence gates are ELIGIBILITY filters: a failing (or unproven) configuration
  is excluded, never out-scored by speed or price. A recorded critical regression excludes too (§34),
  and so does a registered route whose terms/data eligibility fails for the workload (§40, §43).
* Failed/timeout/quota-interrupted samples are never dropped; they count in every denominator.
* The leaderboard is computed only from recorded EvalRuns, per role, with sample size, variance,
  Wilson intervals and ties; only runs under equal conditions (limits + suite + environment) are
  ranked against each other, and a designated baseline gives each entry a delta. No runs -> every
  candidate is BELUM DIUJI. A combined ranking exists only with owner-declared workload weights.
* Results never change routes. A scorecard can only open a ChangeProposal (decision request); the
  routing module writes a route's eval result only through the owner's APPROVED activation, which
  must reference COMPLETED, gate-passing runs of exactly that route configuration
  (``activation_evidence``).
"""

from __future__ import annotations

import math
import platform
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Conflict, NotFound, PolicyDenied, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import utcnow
from aeos.evaluation import definitions as D
from aeos.events import service as events
from aeos.models.research import EvalCaseCandidate, EvalRun, EvalSuite
from aeos.models.routing import ModelRoute

NOT_TESTED = "BELUM_DIUJI"
SMALL_SAMPLE = 10  # informational only: "Kecilnya sampel ... membatasi kesimpulan" (§40); not a universal threshold
CANDIDATE_POOLS = ("WORKER", "ARENA", "COMPARATOR", "LOCAL", "WATCHLIST")
REQUIRED_LIMITS = ("max_attempts", "max_minutes", "max_cost_usd")


# --------------------------------------------------------------------------- seed


def seed_suites(session: Session) -> dict[str, Any]:
    created, unchanged, conflicts = 0, 0, []
    for s in D.SUITES:
        h = D.suite_hash(s)
        row = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == s["suite_key"]))
        if row is None:
            session.add(EvalSuite(
                suite_key=s["suite_key"], title=s["title"], role_key=s["role_key"], task_class=s["task_class"],
                version=s["version"], content_hash=h, held_out=True, protected=True, cases=s["cases"],
                description=s["description"], acceptance=s["acceptance"], isolation=s["isolation"],
                verifier="independent-verifier", mode="DESIGN",
            ))
            created += 1
        elif row.content_hash == h:
            unchanged += 1
        else:
            # Protected suite: never overwritten silently; a new version needs review/decision.
            conflicts.append({"suite_key": s["suite_key"], "stored_hash": row.content_hash, "seed_hash": h})
    session.flush()
    return {"created": created, "unchanged": unchanged, "conflicts": conflicts, "results_seeded": 0}


# --------------------------------------------------------------------------- views


def suite_view(s: EvalSuite) -> dict[str, Any]:
    return {
        "id": s.id, "suite_key": s.suite_key, "title": s.title, "role_key": s.role_key, "task_class": s.task_class,
        "version": s.version, "content_hash": s.content_hash, "held_out": s.held_out, "protected": s.protected,
        "description": s.description, "acceptance": s.acceptance or [], "isolation": s.isolation,
        "verifier": s.verifier, "mode": s.mode, "case_count": len(s.cases or []),
        "case_kinds": sorted({c.get("kind", "") for c in s.cases or []}),
        # held-out: case titles/kinds/acceptance are visible to humans; workers never get the grader.
        "cases": [{k: c.get(k) for k in ("case_id", "kind", "title", "expected", "acceptance", "gate_checks")}
                  for c in s.cases or []],
        "integrity": "OK" if s.content_hash == _stored_hash(s) else "HASH_MISMATCH",
        "results": "Tidak ada hasil di-seed; hasil hanya dari EvalRun yang tercatat.",
        "data_classification": D.suite_data_classification(s.suite_key),
        "grader": D.GRADER_NOTE,
        "held_out_scope": dict(D.HELD_OUT_SCOPE),
    }


def _stored_hash(s: EvalSuite) -> str:
    return content_hash({"suite_key": s.suite_key, "title": s.title, "role_key": s.role_key,
                         "task_class": s.task_class, "description": s.description, "cases": s.cases,
                         "acceptance": s.acceptance, "isolation": s.isolation, "protocol": D.PROTOCOL,
                         "version": s.version})


def suite_integrity_ok(s: EvalSuite | None) -> bool:
    return s is not None and s.content_hash == _stored_hash(s)


def list_suites(session: Session) -> list[dict[str, Any]]:
    return [suite_view(s) for s in session.scalars(select(EvalSuite).order_by(EvalSuite.suite_key))]


def _route(session: Session, route_key: str | None) -> ModelRoute | None:
    if not route_key:
        return None
    return session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))


def route_binding(session: Session, run: EvalRun) -> dict[str, Any]:
    """Live terms/provider eligibility of the run's registered route for the suite's workload, and whether the
    route still has the configuration that was evaluated (§40 "terms/provider eligibility sebagai gate")."""
    if not run.route_key:
        return {"status": "NOT_REGISTERED", "route_key": None, "reasons": ["provider_route UNKNOWN"],
                "mismatches": [], "route_config_current": None}
    route = _route(session, run.route_key)
    if route is None:
        return {"status": "NOT_REGISTERED", "route_key": run.route_key,
                "reasons": ["route not in the model registry: terms/provider eligibility not assessed"],
                "mismatches": [], "route_config_current": None}
    from aeos.routing.service import compute_config_hash, terms_eligibility

    current = compute_config_hash(route)
    cfg = run.configuration or {}
    mismatches = [f"{k} {cfg.get(k)!r} != route {getattr(route, k)!r}" for k in ("model_id", "provider")
                  if cfg.get(k) != getattr(route, k)]
    reasons = terms_eligibility(route, data_classification=D.suite_data_classification(run.suite_key))
    # a run planned before the route binding existed has no recorded route hash: UNKNOWN, not "current"
    config_current = None if run.route_config_hash is None else run.route_config_hash == current
    return {"status": "NOT_ELIGIBLE" if reasons else "ELIGIBLE", "route_key": route.route_key, "reasons": reasons,
            "mismatches": mismatches, "route_config_hash": run.route_config_hash, "current_route_config_hash": current,
            "route_config_current": config_current}


def run_view(r: EvalRun, session: Session | None = None) -> dict[str, Any]:
    view = {
        "id": r.id, "suite_key": r.suite_key, "role_key": r.role_key, "topology": r.topology, "status": r.status,
        "configuration": r.configuration, "configuration_hash": r.configuration_hash, "route_key": r.route_key,
        "repetitions": r.repetitions, "sample_size": r.sample_size, "metrics": r.metrics or {},
        "failures": r.failures or [], "gate_results": r.gate_results or {}, "limits": r.limits or {},
        "planned_by": r.planned_by, "result_source": r.result_source, "mode": r.mode, "notes": r.notes,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "eligibility": eligibility(r.gate_results or {}, r.metrics or None) if r.status == "COMPLETED" else None,
        "is_baseline": bool(r.is_baseline), "proposal_id": r.proposal_id, "conditions_hash": r.conditions_hash,
        "environment": r.environment or {}, "route_config_hash": r.route_config_hash,
        "bindings": [{k: b.get(k) for k in ("slot_id", "case_id", "repetition", "task_id", "bound_at")}
                     for b in (r.bindings or [])],
    }
    from aeos.evaluation import executor

    view["execution"] = executor.execution_view(r)
    if session is not None:
        view["route_binding"] = route_binding(session, r)
    return view


def _scope(q: Any, *, domain: str | None, scoped: bool) -> Any:
    """Viewer scoping: platform rows plus rows planned in the viewer's own domain (never the other one)."""
    if not scoped:
        return q
    if domain is None:
        return q.where(EvalRun.submitted_domain.is_(None))
    return q.where(or_(EvalRun.submitted_domain.is_(None), EvalRun.submitted_domain == domain))


def list_runs(session: Session, *, suite_key: str | None = None, role: str | None = None, domain: str | None = None,
              scoped: bool = False, proposal_id: str | None = None) -> list[dict[str, Any]]:
    q = _scope(select(EvalRun).order_by(EvalRun.created_at.desc()), domain=domain, scoped=scoped)
    if suite_key:
        q = q.where(EvalRun.suite_key == suite_key)
    if role:
        q = q.where(EvalRun.role_key == role)
    if proposal_id:
        q = q.where(EvalRun.proposal_id == proposal_id)
    return [run_view(r, session) for r in session.scalars(q)]


# --------------------------------------------------------------------------- planning a run


def normalise_configuration(cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    required = list(D.AI_CONFIGURATION_FIELDS)
    if str(cfg.get("provider") or "") in D.LOCAL_PROVIDERS:
        required += list(D.LOCAL_CONFIGURATION_FIELDS)
    missing = [f for f in required if f not in cfg or cfg[f] in (None, "")]
    if missing:
        raise ValidationFailed("AIConfiguration is incomplete; state UNKNOWN explicitly where a value is not known",
                               details={"missing": missing})
    out: dict[str, Any] = {}
    unknown: list[str] = []
    optional = [f for f in D.LOCAL_CONFIGURATION_FIELDS if f not in required and cfg.get(f) not in (None, "")]
    for f in [*D.AI_CONFIGURATION_FIELDS, *[x for x in D.LOCAL_CONFIGURATION_FIELDS if x in required], *optional]:
        v = cfg[f]
        if f == "tools":
            if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
                raise ValidationFailed("tools must be a list of tool names")
            v = sorted(v)
        elif f == "context_params":
            if not isinstance(v, dict):
                raise ValidationFailed("context_params must be an object")
        elif not isinstance(v, str):
            raise ValidationFailed(f"{f} must be a string")
        if v == "UNKNOWN":
            unknown.append(f)
        out[f] = v
    return redact(out), unknown


def environment_fingerprint() -> dict[str, Any]:
    """§43 "environment ... setara" / §40 "perbedaan environment membatasi kesimpulan": where a run executes."""
    try:
        import aeos

        controller = getattr(aeos, "__version__", "UNKNOWN")
    except Exception:  # pragma: no cover
        controller = "UNKNOWN"
    try:
        from aeos.routing.service import observed_harness_version

        harness = observed_harness_version() or "UNKNOWN"
    except Exception:  # pragma: no cover - observation only, never a pass
        harness = "UNKNOWN"
    return {"os": platform.system() or "UNKNOWN", "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "controller": controller, "harness": f"opencode {harness}",
            "sandbox": "process (WEAK) - the only launchable kind in this build",
            "grader_fingerprint": grader_fingerprint()}


def grader_fingerprint() -> str:
    """§16/§21 "worker tidak mengubah grader/held-out tests": content hash of the controller's evaluation code on disk
    (suite definitions, collector, metrics, gates). Recorded when a run is planned and re-checked before its result
    is recorded: evaluation code changed in the controller checkout during a run (e.g. by verifier-executed candidate
    code) is detected and the run is never recorded with it."""
    from pathlib import Path

    from aeos.core.hashing import sha256_bytes

    root = Path(__file__).resolve().parent
    try:
        files = {p.relative_to(root).as_posix(): sha256_bytes(p.read_bytes()) for p in sorted(root.rglob("*.py"))
                 if "__pycache__" not in p.parts}
    except OSError:
        return "UNKNOWN"
    return content_hash(files)


def conditions_of(suite: EvalSuite, limits: dict[str, Any], environment: dict[str, Any]) -> str:
    return content_hash({"suite_key": suite.suite_key, "suite_hash": suite.content_hash,
                         "limits": {k: limits.get(k) for k in REQUIRED_LIMITS}, "environment": environment})


def _validated_limits(limits: dict[str, Any]) -> dict[str, Any]:
    missing = [k for k in REQUIRED_LIMITS if k not in limits]
    if missing:
        raise ValidationFailed("declare budget/attempt/time limits before an evaluation (§44)",
                               details={"missing": missing})
    for k in REQUIRED_LIMITS:
        v = limits[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or (isinstance(v, float) and math.isnan(v)):
            raise ValidationFailed(f"{k} must be a number >= 0")
    if limits["max_cost_usd"] > 0:
        # Paid evaluation spend needs the same owner BUDGET decision as any worker spend; the plan
        # only records the cap — reservations are still enforced by aeos.cost at execution time.
        limits = {**limits, "budget_note": "Biaya > $0 memerlukan keputusan BUDGET; admission tetap lewat aeos.cost"}
    return limits


def plan_run(session: Session, *, suite_key: str, configuration: dict[str, Any], role_key: str | None,
             topology: str | None, repetitions: int, limits: dict[str, Any], actor_user_id: str,
             notes: str = "", domain: str | None = None, is_baseline: bool = False,
             proposal_id: str | None = None) -> EvalRun:
    suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == suite_key))
    if suite is None:
        raise NotFound("eval suite not found")
    if suite.mode == "CANDIDATE":
        raise Conflict("a candidate case suite is not adopted yet (human PROMOTION decision required)",
                       code="SUITE_NOT_ADOPTED")
    if topology is not None and topology not in {e["key"] for e in D.EXPERIMENTS}:
        raise ValidationFailed("topology must be one of A, B, C, D")
    if not 1 <= repetitions <= 50:
        raise ValidationFailed("repetitions must be between 1 and 50")
    limits = _validated_limits(limits)
    cfg, unknown = normalise_configuration(configuration)
    role = role_key or suite.role_key
    route_key = cfg["provider_route"] if cfg["provider_route"] != "UNKNOWN" else None
    route = _route(session, route_key)
    route_hash = None
    if route is not None:
        from aeos.routing.service import compute_config_hash

        route_hash = compute_config_hash(route)  # read-only: planning never writes a route
    env = environment_fingerprint()
    run = EvalRun(
        suite_key=suite_key, configuration=cfg, configuration_hash=content_hash(cfg), topology=topology,
        status="PLANNED", sample_size=0, metrics={}, failures=[], gate_results={}, role_key=role,
        route_key=route_key, repetitions=repetitions, samples=[], limits=redact(limits), planned_by=actor_user_id,
        notes=redact_text(notes or "")[:2000], mode="LIVE", submitted_domain=domain, route_config_hash=route_hash,
        is_baseline=bool(is_baseline), proposal_id=proposal_id, environment=env,
        conditions_hash=conditions_of(suite, limits, env), bindings=[],
    )
    session.add(run)
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.plan", target_type="eval_run",
                 target_id=run.id, domain=domain, details={"suite_key": suite_key, "configuration_hash": run.configuration_hash,
                                            "unknown_fields": unknown, "topology": topology,
                                            "is_baseline": bool(is_baseline), "proposal_id": proposal_id,
                                            "route_config_hash": route_hash})
    events.emit(session, source="evaluation", kind="eval.run.planned", entity_type="eval_run", entity_id=run.id,
                domain=domain, payload={"suite_key": suite_key, "configuration_hash": run.configuration_hash,
                         "execution": "via workforce scheduler; not started by planning"})
    return run


# --------------------------------------------------------------------------- results (verifier only)


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return float(s[idx])


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


# Per-sample counts that only some sources record (the trusted collector derives false_claims/context_tokens; the
# others have no producing record yet). Summed only over samples that recorded them; None when none did.
OPTIONAL_SAMPLE_COUNTS = ("false_claims", "tool_failures", "merge_conflicts", "context_tokens", "coordination_tokens",
                          "adjacent_regressions")


def compute_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """§43 dimensions (+ §44 measures) computed by code from per-sample verifier/collector observations."""
    n = len(samples)
    accepted = [s for s in samples if s.get("status") == "ACCEPTED"]
    k = len(accepted)
    first_pass = sum(1 for s in accepted if s.get("first_pass"))
    completed = sum(1 for s in samples if s.get("status") not in D.FAILURE_STATUSES)
    # §43 Reliability "provider availability": a sample interrupted by quota or flagged by the collector
    # as provider-unavailable counts as an unavailable provider (never dropped from the denominator).
    provider_down = sum(1 for s in samples
                        if s.get("status") == "QUOTA_INTERRUPTED" or s.get("provider_unavailable") is True)
    in_scope = sum(1 for s in samples if not s.get("scope_violations"))
    lat = [x for s in samples if (x := _num(s.get("latency_s"))) is not None]
    queue = [x for s in samples if (x := _num(s.get("queue_wait_s"))) is not None]
    quota = [x for s in samples if (x := _num(s.get("quota_delay_s"))) is not None]
    costs = [s.get("worker_cost_usd") for s in samples]
    cost_unknown = any(_num(c) is None for c in costs)
    ci = [s.get("compute_ci_cost_usd") for s in samples]

    def total(key: str) -> int:
        return int(sum(_num(s.get(key)) or 0 for s in samples))

    def collected(key: str) -> int:
        return sum(1 for s in samples if _num(s.get(key)) is not None)

    def optional(key: str) -> int | None:
        # §34/§44: a measure no sample recorded is UNKNOWN (None), never a zero
        return total(key) if collected(key) else None

    unmeasured = sorted(k for k in OPTIONAL_SAMPLE_COUNTS if samples and not collected(k))

    def quality(key: str) -> float | None:
        vals = [x for s in samples if (x := _num((s.get("quality") or {}).get(key))) is not None]
        return round(statistics.fmean(vals), 4) if vals else None

    # An unsplit "regressions" count is treated as critical (conservative); adjacent ones are reported only.
    critical = total("regressions") + total("critical_regressions") + sum(
        1 for s in samples if s.get("critical_regression") is True)
    adjacent = optional("adjacent_regressions")
    rate = (k / n) if n else None
    wilson = _wilson(k, n)
    return {
        "sample_size": n,
        "unmeasured": unmeasured,
        "outcome": {
            "first_pass_acceptance_rate": (first_pass / n) if n else None,
            "final_acceptance_rate": rate, "accepted": k, "regressions": critical + (adjacent or 0),
            "critical_regressions": critical, "adjacent_regressions": adjacent,
            "acceptance_ci95": list(wilson) if wilson else None,
            "acceptance_variance": (rate * (1 - rate)) if rate is not None else None,
        },
        "contract": {"scope_adherence_rate": (in_scope / n) if n else None,
                     "permission_attempts": total("permission_attempts"), "false_claims": optional("false_claims"),
                     "security_violations": total("security_violations")},
        "reliability": {"completion_rate": (completed / n) if n else None,
                        "provider_availability_rate": ((n - provider_down) / n) if n else None,
                        "provider_unavailable": provider_down,
                        "tool_schema_failures": optional("tool_failures"), "repair_count": total("repair_count"),
                        "merge_conflicts": optional("merge_conflicts"),
                        "quota_interrupted": sum(1 for s in samples if s.get("status") == "QUOTA_INTERRUPTED"),
                        "timeouts": sum(1 for s in samples if s.get("status") == "TIMEOUT"),
                        "failed": sum(1 for s in samples if s.get("status") == "FAILED")},
        "product_quality": {"ui_a11y": quality("ui_a11y"), "api_data_behavior": quality("api_data"),
                            "maintainability": quality("maintainability"), "doc_accuracy": quality("doc_accuracy"),
                            "reviewed_samples": sum(1 for s in samples if s.get("quality"))},
        "oversight": {"claude_reviews": total("claude_review_count"),
                      "human_interventions": total("human_interventions"),
                      "human_corrections": total("human_corrections")},
        "usage": {"context_tokens": optional("context_tokens"), "coordination_tokens": optional("coordination_tokens"),
                  "worker_tokens": total("worker_tokens"),
                  "samples_with_token_counts": collected("worker_tokens"),
                  "note": "Token konteks/koordinasi dihitung dari sampel yang mencatatnya; sisanya tidak diketahui."},
        "economics": {
            "worker_cost_per_accepted_usd": (None if cost_unknown or k == 0
                                             else round(sum(_num(c) or 0 for c in costs) / k, 6)),
            "worker_cost_status": "UNKNOWN" if cost_unknown else ("NO_ACCEPTED" if k == 0 else "COMPUTED"),
            "compute_ci_cost_usd": None if any(_num(c) is None for c in ci) else round(sum(_num(c) or 0 for c in ci), 6),
            "subscription_note": "Kuota langganan Claude tidak dikonversi ke biaya",
        },
        "performance": {
            "latency_p50_s": _pct(lat, 0.5), "latency_p95_s": _pct(lat, 0.95),
            "latency_variance": statistics.pvariance(lat) if len(lat) > 1 else None,
            "queue_wait_p50_s": _pct(queue, 0.5), "quota_delay_total_s": sum(quota) if quota else None,
            "latency_samples": len(lat),
        },
    }


def _validate_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(samples, list) or not samples:
        raise ValidationFailed("at least one sample is required (failures included)")
    out = []
    for i, s in enumerate(samples):
        if not isinstance(s, dict):
            raise ValidationFailed(f"sample {i} must be an object")
        if s.get("status") not in D.SAMPLE_STATUSES:
            raise ValidationFailed(f"sample {i} status must be one of {D.SAMPLE_STATUSES}")
        out.append(redact(s))
    return out


def record_result(session: Session, run_id: str, *, samples: list[dict[str, Any]], gate_results: dict[str, Any],
                  result_source: str, status: str = "COMPLETED") -> EvalRun:
    """Store verifier-collected samples. Only trusted collector identities may record results."""
    from aeos.verification.facade import TRUSTED_COLLECTORS

    if result_source not in TRUSTED_COLLECTORS:
        raise PolicyDenied("evaluation results must come from a trusted verifier/collector identity",
                           code="UNTRUSTED_RESULT_SOURCE")
    run = session.get(EvalRun, run_id)
    if run is None:
        raise NotFound("eval run not found")
    if run.status in ("COMPLETED", "FAILED", "CANCELLED"):
        raise Conflict(f"eval run already {run.status}; results are not overwritten")
    if status not in ("COMPLETED", "FAILED", "CANCELLED"):
        raise ValidationFailed("status must be COMPLETED, FAILED or CANCELLED")
    clean = _validate_samples(samples)
    gates = {g: str((gate_results or {}).get(g, "NOT_RUN")).upper() for g in D.GATES}
    run.samples = clean
    run.sample_size = len(clean)
    run.metrics = compute_metrics(clean)
    run.failures = [s for s in clean if s.get("status") in D.FAILURE_STATUSES]
    run.gate_results = gates
    run.result_source = result_source
    run.status = status
    run.finished_at = utcnow()
    run.started_at = run.started_at or run.finished_at
    session.flush()
    if run.role_key:
        # §46: the role's evaluation status follows evaluation results bound to its revision (never a title)
        try:
            from aeos.workforce import facade as workforce

            workforce.refresh_role_eval_status(session, run.role_key)
        except ImportError:
            pass
    events.emit(session, source="evaluation", kind="eval.run.recorded", entity_type="eval_run", entity_id=run.id,
                domain=run.submitted_domain, payload={"status": status, "sample_size": run.sample_size, "gates": gates,
                         "route_change": "NONE (results never change routes)"})
    return run


# --------------------------------------------------------------------------- leaderboard


def critical_regressions(metrics: dict[str, Any] | None) -> int:
    outcome = (metrics or {}).get("outcome") or {}
    if "critical_regressions" in outcome:
        return int(outcome.get("critical_regressions") or 0)
    return int(outcome.get("regressions") or 0)  # runs recorded before the split: unsplit = critical


def eligibility(gates: dict[str, Any], metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    failed = [g for g in D.GATES if str(gates.get(g, "NOT_RUN")).upper() == "FAIL"]
    unproven = [g for g in D.GATES if str(gates.get(g, "NOT_RUN")).upper() not in ("PASS", "FAIL")]
    regressions = critical_regressions(metrics)
    base = {"gates_failed": failed, "gates_unproven": unproven, "critical_regressions": regressions}
    if failed:
        return {"eligible": False, "status": "EXCLUDED_GATE_FAILED", **base}
    if regressions > 0:
        # §34 "Critical regression is not redeemed by an aggregate score"
        return {"eligible": False, "status": "EXCLUDED_REGRESSION", **base}
    if unproven:
        return {"eligible": False, "status": "EXCLUDED_GATE_NOT_PROVEN", **base}
    return {"eligible": True, "status": "ELIGIBLE", **base}


def _config_label(cfg: dict[str, Any]) -> str:
    return f"{cfg.get('model_id')}@{cfg.get('provider_route')} / {cfg.get('harness')} {cfg.get('harness_version')}"


def _tie_groups(entries: list[dict[str, Any]], *, rate_key: str, ci_key: str) -> None:
    """Sorted entries: neighbours whose 95% intervals overlap (or whose rates are equal) share a tie group."""
    group, prev = 0, None
    for e in entries:
        ci = e[ci_key]
        if prev is None:
            group = 1
        else:
            pci = prev[ci_key]
            overlap = bool(ci and pci and ci[1] >= pci[0] and pci[1] >= ci[0])
            if not (overlap or e[rate_key] == prev[rate_key]):
                group += 1
        e["rank"] = group
        prev = e
    by_group: dict[int, list[str]] = defaultdict(list)
    for e in entries:
        by_group[e["rank"]].append(e["entry_key"])
    for e in entries:
        others = [h for h in by_group[e["rank"]] if h != e["entry_key"]]
        e["tied_with"] = others
        e["tie"] = bool(others)


def _rank(entries: list[dict[str, Any]]) -> None:
    """Rank by final acceptance rate; neighbours whose 95% intervals overlap share a tie group."""
    entries.sort(key=lambda e: (-(e["final_acceptance_rate"] or 0.0), -e["sample_size"]))
    _tie_groups(entries, rate_key="final_acceptance_rate", ci_key="acceptance_ci95")


def _candidates(session: Session, tested_hashes: set[str], tested_routes: set[str],
                planned: list[EvalRun]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    listed_routes: set[str] = set(tested_routes)
    seen_hashes: set[str] = set()
    for r in planned:
        if r.configuration_hash in tested_hashes or r.configuration_hash in seen_hashes:
            continue
        seen_hashes.add(r.configuration_hash)
        out.append({"label": _config_label(r.configuration), "configuration_hash": r.configuration_hash,
                    "route_key": r.route_key, "status": NOT_TESTED, "source": "planned eval run"})
        if r.route_key:
            listed_routes.add(r.route_key)
    for route in session.scalars(select(ModelRoute).where(ModelRoute.pool.in_(CANDIDATE_POOLS)).order_by(ModelRoute.route_key)):
        if route.route_key in listed_routes:
            continue
        out.append({"label": route.display_name or route.model_id, "configuration_hash": None,
                    "route_key": route.route_key, "status": NOT_TESTED, "source": "model registry (ModelRoute)",
                    "registry_status": route.status})
    return out


def _delta(entry: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Difference to the designated baseline with a conservative interval (difference of the Wilson bounds)."""
    ci, bci = entry["acceptance_ci95"], baseline["acceptance_ci95"]
    rate, brate = entry["final_acceptance_rate"], baseline["final_acceptance_rate"]
    if rate is None or brate is None or not ci or not bci:
        return {"acceptance_rate": None, "interval": None, "conclusion": "UNKNOWN"}
    low, high = ci[0] - bci[1], ci[1] - bci[0]
    conclusion = "BETTER" if low > 0 else ("WORSE" if high < 0 else "NOT_DISTINGUISHABLE")
    return {"acceptance_rate": round(rate - brate, 4), "interval": [round(low, 4), round(high, 4)],
            "conclusion": conclusion}


def _entry_key(h: str, route_config_hash: str | None, topology: str | None) -> str:
    return f"{h}|{(route_config_hash or '-')[7:23] or '-'}|{topology or '-'}"


def _entry(session: Session, suite_key: str, h: str, topology: str | None, rs: list[EvalRun]) -> dict[str, Any]:
    samples = [x for r in rs for x in (r.samples or [])]
    m = compute_metrics(samples)
    gate_views = [eligibility(r.gate_results or {}, compute_metrics(r.samples or [])) for r in rs]
    elig = next((g for g in gate_views if not g["eligible"]), gate_views[0])
    binding = route_binding(session, rs[-1])
    # every run of an entry must still describe the route's current configuration (a run of an earlier route
    # configuration is never pooled into - or inherited by - the current one)
    stale = any(route_binding(session, r).get("route_config_current") is False for r in rs)
    if elig["eligible"] and binding["status"] == "NOT_ELIGIBLE":
        elig = {**elig, "eligible": False, "status": "EXCLUDED_ROUTE_TERMS", "route_reasons": binding["reasons"]}
    elif elig["eligible"] and stale:
        elig = {**elig, "eligible": False, "status": "EXCLUDED_STALE_CONFIGURATION",
                "route_reasons": ["the registered route changed since this evaluation: results are not inherited"]}
    environments = {content_hash(r.environment or {}) for r in rs}
    conditions = {r.conditions_hash for r in rs}
    return {
        "entry_key": _entry_key(h, rs[0].route_config_hash, topology), "configuration_hash": h,
        "route_config_hash": rs[0].route_config_hash, "label": _config_label(rs[0].configuration),
        "configuration": rs[0].configuration, "topology": topology, "topologies": [topology or "-"],
        "runs": len(rs), "run_ids": [r.id for r in rs], "sample_size": m["sample_size"],
        "final_acceptance_rate": m["outcome"]["final_acceptance_rate"],
        "first_pass_acceptance_rate": m["outcome"]["first_pass_acceptance_rate"],
        "acceptance_ci95": m["outcome"]["acceptance_ci95"],
        "acceptance_variance": m["outcome"]["acceptance_variance"],
        "failures_reported": m["reliability"]["failed"] + m["reliability"]["timeouts"]
        + m["reliability"]["quota_interrupted"],
        "metrics": m, "eligibility": elig, "small_sample": m["sample_size"] < SMALL_SAMPLE,
        "is_baseline": any(r.is_baseline for r in rs), "route_binding": binding,
        "environment_mixed": len(environments) > 1, "environments": [r.environment or {} for r in rs][:5],
        "conditions_hash": next(iter(conditions)) if len(conditions) == 1 else None,
        "conditions_mixed": len(conditions) > 1,
    }


def leaderboard(session: Session, *, role: str | None = None, domain: str | None = None,
                scoped: bool = False) -> dict[str, Any]:
    suites = list(session.scalars(select(EvalSuite).where(EvalSuite.mode != "CANDIDATE").order_by(EvalSuite.suite_key)))
    role_suites = [s for s in suites if role is None or s.role_key == role]
    suite_keys = {s.suite_key for s in role_suites}
    runs = list(session.scalars(_scope(select(EvalRun).order_by(EvalRun.created_at), domain=domain, scoped=scoped)))
    relevant = [r for r in runs if (r.suite_key in suite_keys) or (role is not None and r.role_key == role)]
    completed = [r for r in relevant if r.status in ("COMPLETED", "FAILED") and r.samples]
    planned = [r for r in relevant if r.status in ("PLANNED", "RUNNING")]

    per_suite: list[dict[str, Any]] = []
    tested: set[str] = set()
    # §44: topology results are never pooled across topologies - (configuration, topology) is one entry; nor are
    # runs of different configurations of the registered route (§23/§43: a changed route never inherits results).
    grouped: dict[tuple[str, str, str | None, str | None], list[EvalRun]] = defaultdict(list)
    for r in completed:
        grouped[(r.suite_key, r.configuration_hash, r.route_config_hash, r.topology)].append(r)
        tested.add(r.configuration_hash)
    for s in role_suites:
        entries = [_entry(session, sk, h, topo, rs) for (sk, h, _rh, topo), rs in grouped.items()
                   if sk == s.suite_key]
        eligible_all = [e for e in entries if e["eligibility"]["eligible"]]
        excluded = [e for e in entries if not e["eligibility"]["eligible"]]
        baseline = next((e for e in eligible_all if e["is_baseline"]), None)
        # §43 "environment/input/resource/repair budget setara": rank only entries under the reference conditions
        # (the baseline's, else the most common); the rest is listed as not comparable.
        cond_counts = Counter(e["conditions_hash"] for e in eligible_all)
        reference = baseline["conditions_hash"] if baseline else (cond_counts.most_common(1)[0][0] if cond_counts else None)
        eligible = [e for e in eligible_all if e["conditions_hash"] == reference and not e["conditions_mixed"]]
        not_comparable = [e for e in eligible_all if e not in eligible]
        for e in not_comparable:
            e["comparability"] = "NOT_COMPARABLE: kondisi (limit/suite/environment) berbeda dari referensi"
        _rank(eligible)
        if baseline is not None:
            for e in eligible:
                e["delta_vs_baseline"] = None if e is baseline else _delta(e, baseline)
        per_suite.append({"suite_key": s.suite_key, "title": s.title, "role_key": s.role_key,
                          "ranking": eligible, "excluded": excluded, "not_comparable": not_comparable,
                          "baseline": baseline["entry_key"] if baseline else None,
                          "baseline_note": None if baseline else "Belum ada baseline yang ditetapkan untuk suite ini.",
                          "reference_conditions_hash": reference,
                          "status": "RANKED" if eligible else (NOT_TESTED if not excluded and not not_comparable
                                                              else "NO_ELIGIBLE")})
    any_results = any(p["ranking"] or p["excluded"] or p["not_comparable"] for p in per_suite)
    weights, weights_source = active_weights(session, role) if role else (None, None)
    return {
        "role": role, "source": "eval_runs (hasil tercatat verifier); tidak ada hasil seed",
        "checked_at": utcnow().isoformat(), "mode": "LIVE",
        "status": "HAS_RESULTS" if any_results else NOT_TESTED,
        "message": None if any_results else "Semua kandidat BELUM DIUJI dalam platform ini",
        "suites": per_suite,
        "candidates_not_tested": _candidates(session, tested, {r.route_key for r in completed if r.route_key}, planned),
        "combined": None if not weights else combined_ranking(per_suite, weights, source=weights_source),
        "combined_note": ("Ranking gabungan hanya memakai bobot workload yang dideklarasikan; belum ada bobot, "
                          "sehingga tidak ada urutan universal.") if not weights else None,
        "route_change": "NONE - hasil evaluasi tidak mengubah route; gunakan scorecard untuk membuka proposal/keputusan.",
        "ranking_rule": ("Hanya konfigurasi yang lulus gate security/authority/evidence, tanpa regresi kritis dan dengan "
                         "rute yang eligible secara terms/data; urutan berdasarkan final acceptance rate dengan interval "
                         "Wilson 95%; interval yang tumpang tindih = seri; hanya kondisi setara yang dibandingkan; "
                         "topologi berbeda tidak digabung."),
        "dimensions": D.DIMENSIONS,
    }


# --------------------------------------------------------------------------- workload weights (§43)


def _weights_hash(weights: dict[str, float]) -> str:
    return content_hash({k: float(v) for k, v in sorted(weights.items())})


def active_weights(session: Session, role: str | None) -> tuple[dict[str, float] | None, str | None]:
    """Declared workload weights for *role*: code constants, else the owner's ACTIVE POLICY decision."""
    if not role:
        return None, None
    code = D.ROLE_WORKLOAD_WEIGHTS.get(role)
    if code:
        return dict(code), "code:definitions.ROLE_WORKLOAD_WEIGHTS"
    try:
        from aeos.policy.facade import find_active_decision
    except (ImportError, ModuleNotFoundError):
        return None, None
    d = find_active_decision(session, kind="POLICY", target_type=D.WEIGHTS_TARGET_TYPE, target_id=role)
    if d is None or (d.actor_kind or "HUMAN") != "HUMAN":
        return None, None
    weights = (d.details or {}).get("weights")
    if not isinstance(weights, dict) or not weights or d.target_hash != _weights_hash(weights):
        return None, None
    return {str(k): float(v) for k, v in weights.items()}, f"decision:{d.id}"


def declare_weights(session: Session, *, role: str, weights: dict[str, Any], actor_user_id: str,
                    rationale: str) -> Any:
    """Owner declaration of per-suite workload weights for one role (a POLICY decision bound to their hash)."""
    suites = {s.suite_key: s for s in session.scalars(select(EvalSuite).where(EvalSuite.role_key == role))}
    if not suites:
        raise NotFound(f"no suites for role {role}")
    if not isinstance(weights, dict) or not weights:
        raise ValidationFailed("weights must map suite_key -> weight > 0")
    clean: dict[str, float] = {}
    for k, v in weights.items():
        if k not in suites:
            raise ValidationFailed(f"suite {k} is not a suite of role {role}")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            raise ValidationFailed(f"weight of {k} must be a number > 0")
        clean[k] = float(v)
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("rationale is required (min 10 characters)")
    from aeos.policy.facade import record_decision

    return record_decision(session, kind="POLICY", actor_user_id=actor_user_id, target_type=D.WEIGHTS_TARGET_TYPE,
                           target_id=role, target_hash=_weights_hash(clean), allowed_operations=["eval.rank.combined"],
                           rationale=rationale.strip(), details={"weights": clean, "role": role})


def combined_ranking(per_suite: list[dict[str, Any]], weights: dict[str, float], *, source: str | None) -> dict[str, Any]:
    """Weighted mean of per-suite final acceptance with propagated (conservative) interval bounds. A
    configuration missing an eligible result on any weighted suite is listed as incomplete, never ranked."""
    by_suite = {p["suite_key"]: {e["entry_key"]: e for e in p["ranking"]} for p in per_suite}
    weighted = {k: w for k, w in weights.items() if w > 0}
    total_w = sum(weighted.values())
    keys = {ek for sk in weighted for ek in by_suite.get(sk, {})}
    ranking: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for ek in sorted(keys):
        parts = {sk: by_suite.get(sk, {}).get(ek) for sk in weighted}
        # a weighted suite counts only with both its rate and its interval: never ranked on a missing bound
        complete = {sk: e for sk, e in parts.items()
                    if e is not None and e["final_acceptance_rate"] is not None and e.get("acceptance_ci95")}
        missing = [sk for sk in weighted if sk not in complete]
        any_entry = next(e for e in parts.values() if e is not None)
        if missing or total_w <= 0:
            incomplete.append({"entry_key": ek, "label": any_entry["label"], "missing_suites": missing})
            continue
        score = sum(weighted[sk] * complete[sk]["final_acceptance_rate"] for sk in weighted) / total_w
        low = sum(weighted[sk] * complete[sk]["acceptance_ci95"][0] for sk in weighted) / total_w
        high = sum(weighted[sk] * complete[sk]["acceptance_ci95"][1] for sk in weighted) / total_w
        ranking.append({"entry_key": ek, "label": any_entry["label"],
                        "configuration_hash": any_entry["configuration_hash"], "topology": any_entry["topology"],
                        "score": round(score, 4), "interval": [round(low, 4), round(high, 4)],
                        "sample_size": sum(complete[sk]["sample_size"] for sk in weighted)})
    ranking.sort(key=lambda e: (-e["score"], -e["sample_size"]))
    _tie_groups(ranking, rate_key="score", ci_key="interval")
    return {"weights": weighted, "source": source, "ranking": ranking, "incomplete": incomplete,
            "rule": "Rata-rata berbobot final acceptance per suite; interval = rata-rata berbobot batas Wilson; "
                    "interval tumpang tindih = seri; konfigurasi tanpa hasil eligible pada suite berbobot tidak diranking."}


# --------------------------------------------------------------------------- topology comparison (§44)


def topology_comparison(session: Session, *, suite_key: str, configuration_hash: str, domain: str | None = None,
                        scoped: bool = False) -> dict[str, Any]:
    """Compare topologies A-D for ONE fixed configuration (§44 "tahan konfigurasi model/tool tetap"): runs of
    any other configuration are never part of the comparison."""
    q = _scope(select(EvalRun).where(EvalRun.suite_key == suite_key, EvalRun.status.in_(("COMPLETED", "FAILED"))),
               domain=domain, scoped=scoped)
    runs = [r for r in session.scalars(q) if r.samples]
    same = [r for r in runs if r.configuration_hash == configuration_hash and r.topology]
    others = sorted({r.configuration_hash for r in runs if r.topology and r.configuration_hash != configuration_hash})
    by_topo: dict[tuple[str, str], list[EvalRun]] = defaultdict(list)
    for r in same:
        by_topo[(r.topology or "-", r.route_config_hash or "")].append(r)
    rows = [_entry(session, suite_key, configuration_hash, t, rs) for (t, _rh), rs in sorted(by_topo.items())]
    eligible = [e for e in rows if e["eligibility"]["eligible"]]
    conds = {e["conditions_hash"] for e in eligible}
    comparable = len(conds) <= 1 and not any(e["conditions_mixed"] for e in eligible)
    if comparable:
        _rank(eligible)
    return {"suite_key": suite_key, "configuration_hash": configuration_hash, "fixed_configuration": True,
            "topologies": rows, "comparable": comparable,
            "status": ("NOT_TESTED" if not rows else "NEEDS_TWO_TOPOLOGIES" if len(eligible) < 2
                       else "COMPARED" if comparable else "NOT_COMPARABLE"),
            "excluded_other_configurations": others,
            "note": "Hanya topologi dengan konfigurasi model/tool yang sama yang dibandingkan; gate identik untuk A-D."}


# --------------------------------------------------------------------------- controlled comparison (§40)


def plan_comparison(session: Session, *, proposal_id: str, suite_keys: list[str], baseline: dict[str, Any],
                    candidate: dict[str, Any], limits: dict[str, Any], repetitions: int, actor_user_id: str,
                    domain: str | None = None) -> list[EvalRun]:
    """§39/§40 "Model Evaluation menyiapkan workload" + "controlled comparison": baseline and candidate
    configuration on the same suites with the same limits, linked to the proposal."""
    from aeos.models.research import ChangeProposal

    p = session.get(ChangeProposal, proposal_id)
    if p is None:
        raise NotFound("proposal not found")
    if p.domain is not None and p.domain != domain:
        # domain-first: a domain proposal is only prepared from that explicitly chosen domain (never from a
        # session without an active domain, never from the other one)
        raise NotFound("proposal not found")
    if p.status not in ("TRIAL_APPROVED", "IN_TRIAL", "REVIEWED", "UNDER_REVIEW"):
        raise Conflict(f"proposal is {p.status}: a comparison is prepared for a reviewed/trial proposal",
                       code="PROPOSAL_NOT_IN_REVIEW")
    if not suite_keys:
        raise ValidationFailed("name at least one suite (the review package suggests them)")
    if content_hash(normalise_configuration(baseline)[0]) == content_hash(normalise_configuration(candidate)[0]):
        raise ValidationFailed("baseline and candidate are the same configuration")
    out: list[EvalRun] = []
    for sk in dict.fromkeys(suite_keys):
        for cfg, is_base in ((baseline, True), (candidate, False)):
            out.append(plan_run(session, suite_key=sk, configuration=cfg, role_key=None, topology=None,
                                repetitions=repetitions, limits=limits, actor_user_id=actor_user_id,
                                notes=f"controlled comparison for proposal {proposal_id}", domain=domain,
                                is_baseline=is_base, proposal_id=proposal_id))
    return out


def comparison(session: Session, *, proposal_id: str, domain: str | None = None, scoped: bool = False) -> dict[str, Any]:
    """Side-by-side baseline vs candidate per suite for one proposal (same suite, same conditions)."""
    q = _scope(select(EvalRun).where(EvalRun.proposal_id == proposal_id).order_by(EvalRun.created_at),
               domain=domain, scoped=scoped)
    runs = list(session.scalars(q))
    suites: list[dict[str, Any]] = []

    def side(suite_key: str, group: list[EvalRun]) -> dict[str, Any] | None:
        done = [r for r in group if r.status in ("COMPLETED", "FAILED") and r.samples]
        if not group:
            return None
        if not done:
            return {"status": group[-1].status, "run_ids": [r.id for r in group], "result": None}
        e = _entry(session, suite_key, done[0].configuration_hash, done[0].topology, done)
        return {"status": "RECORDED", "run_ids": [r.id for r in group], "result": e}

    for sk in dict.fromkeys(r.suite_key for r in runs):
        rs = [r for r in runs if r.suite_key == sk]
        b = side(sk, [r for r in rs if r.is_baseline])
        c = side(sk, [r for r in rs if not r.is_baseline])
        conds = {r.conditions_hash for r in rs}
        verdict = "NOT_RECORDED"
        delta = None
        if b and c and b["result"] and c["result"]:
            if len(conds) > 1:
                verdict = "NOT_COMPARABLE"
            elif not c["result"]["eligibility"]["eligible"]:
                verdict = "CANDIDATE_EXCLUDED"
            else:
                delta = _delta(c["result"], b["result"])
                verdict = delta["conclusion"]
        suites.append({"suite_key": sk, "baseline": b, "candidate": c, "equal_conditions": len(conds) == 1,
                       "delta": delta, "verdict": verdict})
    return {"proposal_id": proposal_id, "suites": suites,
            "status": "NOT_PLANNED" if not runs else ("RECORDED" if all(s["verdict"] != "NOT_RECORDED" for s in suites)
                                                        else "IN_PROGRESS"),
            "rule": "Baseline vs kandidat pada suite, limit dan environment yang sama; gate tetap syarat; "
                    "output lebih pendek atau token lebih sedikit bukan bukti biaya lebih rendah (biaya per accepted).",
            "route_change": "NONE"}


# --------------------------------------------------------------------------- activation evidence (routing)


def activation_evidence(session: Session, *, route_key: str, route_config_hash: str,
                        run_ids: list[str]) -> dict[str, Any]:
    """Whether *run_ids* prove that the route configuration *route_config_hash* was tested (§23): every run
    COMPLETED by a trusted collector on a protected suite, made on exactly that route configuration, describing
    that route, gate-passing and without critical regression. Returns per-role pooled results."""
    from aeos.verification.facade import TRUSTED_COLLECTORS

    problems: list[str] = []
    good: list[EvalRun] = []
    if not run_ids:
        problems.append("no EvalRun referenced: APPROVED needs a completed, gate-passing evaluation of this exact "
                        "configuration (use TRIAL until one exists)")
    route = _route(session, route_key)
    for rid in dict.fromkeys(run_ids):
        r = session.get(EvalRun, rid)
        if r is None:
            problems.append(f"{rid}: eval run not found")
            continue
        own: list[str] = []
        if r.status != "COMPLETED":
            own.append(f"status {r.status} (COMPLETED required)")
        if r.result_source not in TRUSTED_COLLECTORS:
            own.append("results not recorded by a trusted collector")
        if r.route_key != route_key:
            own.append(f"evaluated route {r.route_key}, not {route_key}")
        elif r.route_config_hash != route_config_hash:
            own.append("evaluated another configuration of this route (route config hash differs): not inherited")
        if route is not None and any((r.configuration or {}).get(k) != getattr(route, k) for k in ("model_id", "provider")):
            own.append("the evaluated AIConfiguration does not describe this route (model_id/provider differ)")
        suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == r.suite_key))
        if not suite_integrity_ok(suite) or (suite is not None and (not suite.protected or suite.mode == "CANDIDATE")):
            own.append("suite missing, not protected or its content hash does not match")
        if r.submitted_domain not in (None, "PERSONAL"):
            # route activation is PERSONAL-only; an Office evaluation never flows into the shared route record
            own.append(f"planned in {r.submitted_domain}: never evidence for a PERSONAL route activation")
        elig = eligibility(r.gate_results or {}, r.metrics or None)
        if not elig["eligible"]:
            own.append(elig["status"])
        if not r.sample_size:
            own.append("no samples")
        elif not ((r.metrics or {}).get("outcome") or {}).get("accepted"):
            # U-07 "lolos evaluasi": a configuration that produced no accepted outcome did not pass the evaluation
            own.append("NO_ACCEPTED_OUTCOME (0 accepted samples: the evaluation was not passed)")
        if own:
            problems.append(f"{rid}: " + "; ".join(own))
        else:
            good.append(r)
    roles: dict[str, dict[str, Any]] = {}
    by_role: dict[str, list[EvalRun]] = defaultdict(list)
    for r in good:
        by_role[r.role_key or "unassigned"].append(r)
    for role, rs in by_role.items():
        m = compute_metrics([x for r in rs for x in (r.samples or [])])
        ci = m["outcome"]["acceptance_ci95"]
        roles[role] = {"score": round(ci[0], 4) if ci else None, "score_basis": "wilson_lower_95",
                       "final_acceptance_rate": m["outcome"]["final_acceptance_rate"], "ci95": ci,
                       "accepted": m["outcome"]["accepted"], "sample_size": m["sample_size"], "runs": len(rs),
                       "run_ids": [r.id for r in rs], "suite": ", ".join(sorted({r.suite_key for r in rs}))}
    return {"ok": not problems and bool(good), "problems": problems, "roles": roles}


def scorecard_snapshot(session: Session, *, role: str, domain: str | None = None, scoped: bool = False) -> dict[str, Any]:
    board = leaderboard(session, role=role, domain=domain, scoped=scoped)
    snapshot = {"role": role, "suites": [{"suite_key": s["suite_key"],
                                          "ranking": [{k: e[k] for k in ("configuration_hash", "label", "rank", "tie",
                                                                         "sample_size", "final_acceptance_rate",
                                                                         "acceptance_ci95", "topology")}
                                                      for e in s["ranking"]],
                                          "excluded": [e["configuration_hash"] for e in s["excluded"]]}
                                         for s in board["suites"]]}
    return snapshot | {"scorecard_hash": content_hash(snapshot), "status": board["status"],
                       "adoption": adoption_paths(board, role=role)}


def adoption_paths(board: dict[str, Any], *, role: str | None = None) -> list[dict[str, Any]]:
    """§44 "Hasil routing/role/mode menjadi DecisionRecord": how a scorecard result ends in an adoption decision. The
    top-ranked, eligible configuration of each suite can be adopted only by the owner's APPROVED route activation,
    which binds the exact route config hash, references these EvalRuns and carries the DecisionRecord (options, goal,
    evidence, cost/quality trade-off, invalidation condition); the role's own PASS result becomes its admission
    eligibility only through the owner's role adoption decision (bound to the role revision and the EvalRun).
    Nothing is adopted by the scorecard itself."""
    from aeos.policy import facade as policy

    record_fields = list(policy.decision_record_fields())
    out: list[dict[str, Any]] = []
    for s in board.get("suites") or []:
        for e in s.get("ranking") or []:
            if e.get("rank") != 1:
                continue
            binding = e.get("route_binding") or {}
            route_key = binding.get("route_key")
            ready = bool(route_key) and binding.get("status") == "ELIGIBLE" and binding.get("route_config_current") is True
            out.append({"suite_key": s.get("suite_key"), "configuration_hash": e.get("configuration_hash"),
                        "route_key": route_key, "eval_run_ids": list(e.get("run_ids") or []),
                        "decision": "ROUTE_ACTIVATION (APPROVED)", "decision_record_fields": record_fields,
                        "path": f"POST /api/routes/{route_key}/activate" if route_key else None,
                        "role_adoption": ({"decision": "POLICY role_adoption (role revision + EvalRun)",
                                           "path": f"POST /api/workforce/roles/{role}/adopt",
                                           "decision_record_fields": record_fields} if role else None),
                        "status": "READY_FOR_OWNER_DECISION" if ready else "NOT_ADOPTABLE",
                        "reason": None if ready else ("route not registered" if not route_key else
                                                      "route not eligible or its configuration changed since the "
                                                      "evaluation (re-evaluate)")})
    return out


# --------------------------------------------------------------------------- failures -> candidate cases (§28)


def _visible(domain: str | None, viewer_domain: str | None) -> bool:
    """A domain-bound record is visible only from that explicitly chosen domain (domain-first, §35)."""
    return domain is None or domain == viewer_domain


def _source_summary(session: Session, source_type: str, source_id: str,
                    viewer_domain: str | None = None) -> dict[str, Any]:
    """Summary of a failure source. The domain is checked before anything else, so a record of the other
    domain is indistinguishable from a missing one (no existence/state oracle)."""
    if source_type == "evidence":
        from aeos.models.evidence import EvidenceArtifact

        ev = session.get(EvidenceArtifact, source_id)
        if ev is None or not _visible(ev.domain, viewer_domain):
            raise NotFound("evidence not found")
        if not ev.trusted:
            raise PolicyDenied("only trusted (verifier/collector) evidence of a failure can seed a case",
                               code="UNTRUSTED_SOURCE")
        return {"domain": ev.domain, "project_id": ev.project_id, "task_id": ev.task_id, "kind": ev.kind,
                "claim": ev.claim, "content_hash": ev.content_hash}
    if source_type == "worker_run":
        from aeos.models.workforce import WorkerRun

        run = session.get(WorkerRun, source_id)
        if run is None or not _visible(run.domain, viewer_domain):
            raise NotFound("worker run not found")
        failed = run.execution_status in ("FAILED", "CANCELLED") or run.quarantined or run.verifier_status == "FAIL" \
            or run.acceptance_status == "REJECTED"
        if not failed:
            raise Conflict("the run did not fail (no failure to turn into a case)", code="NOT_A_FAILURE")
        return {"domain": run.domain, "project_id": run.project_id, "task_id": run.task_id, "kind": "worker_run",
                "claim": f"{run.execution_status} {run.outcome_code} {run.block_reason}"[:500],
                "role_key": run.role_key, "content_hash": content_hash({"run": run.id, "outcome": run.outcome_code})}
    if source_type == "alert":
        from aeos.models.operations_ext import OpsAlert

        alert = session.get(OpsAlert, source_id)
        if alert is None or not _visible(alert.domain, viewer_domain):
            raise NotFound("alert not found")
        return {"domain": alert.domain, "project_id": alert.project_id, "task_id": None, "kind": alert.kind,
                "claim": alert.title, "content_hash": content_hash({"alert": alert.id, "kind": alert.kind})}
    raise ValidationFailed("source_type must be evidence, worker_run or alert")


def propose_case_from_failure(session: Session, *, source_type: str, source_id: str, actor_user_id: str,
                              title: str, expected: str, kind: str = "incident", related_suite_key: str | None = None,
                              viewer_domain: str | None = None) -> EvalCaseCandidate:
    """§28 "Incident: failures become regression/eval cases": a CANDIDATE case that references the failing
    evidence. It is used by no evaluation until a human PROMOTION decision adopts it (``adopt_case``)."""
    if kind not in D.PROTOCOL["case_kinds"]:
        raise ValidationFailed(f"kind must be one of {D.PROTOCOL['case_kinds']}")
    if len((title or "").strip()) < 5 or len((expected or "").strip()) < 10:
        raise ValidationFailed("title (min 5) and expected behaviour (min 10 characters) are required")
    # never reveal a failure of the other domain - nor of any domain to a session without an active domain
    src = _source_summary(session, source_type, source_id, viewer_domain)
    related = None
    if related_suite_key:
        related = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == related_suite_key))
        if related is None:
            raise NotFound("related suite not found")
    existing = session.scalar(select(EvalCaseCandidate).where(EvalCaseCandidate.source_type == source_type,
                                                               EvalCaseCandidate.source_id == source_id,
                                                               EvalCaseCandidate.status != "REJECTED"))
    if existing is not None:
        raise Conflict("a case candidate for this failure already exists", code="DUPLICATE_CASE",
                       details={"candidate_id": existing.id})
    case = redact({
        "case_id": f"regression-{source_type}-{source_id[:8]}", "kind": kind, "title": redact_text(title.strip())[:200],
        "task": redact_text(f"Reproduce and prevent the observed failure: {src['claim']}")[:1000],
        "expected": redact_text(expected.strip())[:1000],
        "acceptance": ["Kegagalan yang teramati tidak terulang", "Tes regresi gagal sebelum dan lulus sesudah perbaikan"],
        "gate_checks": ["security", "authority", "evidence"],
        "fixture": "generated in a disposable sandbox from the accepted baseline; hidden from implementers",
        "source": {"type": source_type, "id": source_id, "content_hash": src["content_hash"]},
    })
    cand = EvalCaseCandidate(
        source_type=source_type, source_id=source_id, domain=src.get("domain"), project_id=src.get("project_id"),
        role_key=src.get("role_key") or (related.role_key if related else None),
        related_suite_key=related_suite_key, case=case, case_hash=content_hash(case), status="CANDIDATE",
        proposed_by=actor_user_id,
    )
    session.add(cand)
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.case.propose",
                 target_type="eval_case_candidate", target_id=cand.id, domain=cand.domain,
                 details={"source_type": source_type, "source_id": source_id, "case_hash": cand.case_hash})
    events.emit(session, source="evaluation", kind="eval.case.proposed", entity_type="eval_case_candidate",
                entity_id=cand.id, domain=cand.domain, payload={"status": "CANDIDATE"})
    return cand


def case_candidate_view(c: EvalCaseCandidate) -> dict[str, Any]:
    return {"id": c.id, "source_type": c.source_type, "source_id": c.source_id, "domain": c.domain,
            "project_id": c.project_id, "role_key": c.role_key, "related_suite_key": c.related_suite_key,
            "case": c.case, "case_hash": c.case_hash, "status": c.status, "proposed_by": c.proposed_by,
            "decision_id": c.decision_id, "adopted_suite_key": c.adopted_suite_key,
            "created_at": c.created_at.isoformat() if c.created_at else None}


def list_case_candidates(session: Session, *, domain: str | None) -> list[dict[str, Any]]:
    q = select(EvalCaseCandidate).order_by(EvalCaseCandidate.created_at.desc())
    q = q.where(or_(EvalCaseCandidate.domain.is_(None), EvalCaseCandidate.domain == domain)) if domain else \
        q.where(EvalCaseCandidate.domain.is_(None))
    return [case_candidate_view(c) for c in session.scalars(q)]


def adopt_case(session: Session, candidate_id: str, *, actor_user_id: str, expected_case_hash: str,
               rationale: str, viewer_domain: str | None = None) -> EvalSuite:
    """Human PROMOTION decision bound to the exact case hash: the case becomes its own protected, held-out
    regression suite (existing protected suites are never edited). OFFICE failures are never promoted into the
    platform's suites (Office data never flows into shared definitions)."""
    c = session.get(EvalCaseCandidate, candidate_id)
    if c is None or not _visible(c.domain, viewer_domain):
        raise NotFound("case candidate not found")
    if c.status != "CANDIDATE":
        raise Conflict(f"candidate is {c.status}")
    if expected_case_hash != c.case_hash:
        raise Conflict("case changed since it was reviewed", code="CASE_HASH_MISMATCH", details={"current": c.case_hash})
    if c.domain == "OFFICE":
        raise PolicyDenied("an OFFICE failure is never promoted into the platform's evaluation suites",
                           code="OFFICE_DATA")
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("rationale is required (min 10 characters)")
    from aeos.policy.facade import record_decision

    d = record_decision(session, kind="PROMOTION", actor_user_id=actor_user_id, target_type="eval_case_candidate",
                        target_id=c.id, target_hash=c.case_hash, allowed_operations=["eval.case.adopt"],
                        rationale=rationale.strip(), domain=c.domain,
                        details={"source_type": c.source_type, "source_id": c.source_id})
    suite_key = f"regression.{c.case['case_id']}"[:96]
    spec = {"suite_key": suite_key, "title": f"Regression: {c.case.get('title', '')}"[:200],
            "role_key": c.role_key, "task_class": "regression",
            "description": f"Kasus regresi dari kegagalan teramati ({c.source_type} {c.source_id}).",
            "cases": [c.case], "acceptance": list(D.COMMON_ACCEPTANCE), "isolation": D.ISOLATION,
            "protocol": D.PROTOCOL, "version": D.SUITE_VERSION}
    suite = EvalSuite(suite_key=suite_key, title=spec["title"], role_key=c.role_key, task_class="regression",
                      version=D.SUITE_VERSION, content_hash=D.suite_hash(spec), held_out=True, protected=True,
                      cases=spec["cases"], description=spec["description"], acceptance=spec["acceptance"],
                      isolation=spec["isolation"], verifier="independent-verifier", mode="DESIGN")
    session.add(suite)
    c.status = "ADOPTED"
    c.decision_id = d.id
    c.adopted_suite_key = suite_key
    session.flush()
    events.emit(session, source="evaluation", kind="eval.case.adopted", entity_type="eval_case_candidate",
                entity_id=c.id, domain=c.domain, payload={"suite_key": suite_key, "decision_id": d.id})
    return suite


def reject_case(session: Session, candidate_id: str, *, actor_user_id: str, reason: str,
                viewer_domain: str | None = None) -> EvalCaseCandidate:
    c = session.get(EvalCaseCandidate, candidate_id)
    if c is None or not _visible(c.domain, viewer_domain):
        raise NotFound("case candidate not found")
    if c.status != "CANDIDATE":
        raise Conflict(f"candidate is {c.status}")
    c.status = "REJECTED"
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.case.reject",
                 target_type="eval_case_candidate", target_id=c.id, domain=c.domain, reason=redact_text(reason)[:500])
    return c


# --------------------------------------------------------------------------- blind qualitative review (§43)


def blind_review_items(session: Session, *, suite_key: str, domain: str | None = None,
                       scoped: bool = True) -> dict[str, Any]:
    """§43 "blind qualitative review bila relevan": executed samples of RUNNING runs on one suite, with the
    configuration, route and run hidden behind the binding's random blind id (never shown in run views).
    Scores are stored on the binding and feed ``product_quality`` when the collector records the run."""
    from aeos.models.workforce import WorkerRun

    q = _scope(select(EvalRun).where(EvalRun.suite_key == suite_key, EvalRun.status == "RUNNING"),
               domain=domain, scoped=scoped)
    items = []
    for r in session.scalars(q):
        for b in r.bindings or []:
            if b.get("quality") or not b.get("task_id") or not b.get("blind_id"):
                continue
            runs = list(session.scalars(select(WorkerRun).where(WorkerRun.task_id == b["task_id"])
                                        .order_by(WorkerRun.attempt.desc())))
            last = runs[0] if runs else None
            if last is None or last.execution_status not in ("REVIEW", "DONE", "FAILED", "CANCELLED"):
                continue
            items.append({"item_id": b["blind_id"], "case_id": b.get("case_id"),
                          "diff_summary": redact(last.diff_summary or {}), "touched_paths": list(last.touched_paths or [])[:50],
                          "evidence_refs": list(last.evidence_refs or [])[:10]})
    items.sort(key=lambda i: i["item_id"])  # opaque order: not grouped by configuration
    return {"suite_key": suite_key, "items": items, "dimensions": ["ui_a11y", "api_data", "maintainability", "doc_accuracy"],
            "note": "Konfigurasi, rute dan run disembunyikan; skor 0-1 per dimensi yang relevan."}


def record_blind_review(session: Session, *, suite_key: str, item_id: str, scores: dict[str, Any],
                        reviewer_user_id: str, domain: str | None = None, scoped: bool = True) -> dict[str, Any]:
    allowed = {"ui_a11y", "api_data", "maintainability", "doc_accuracy"}
    clean: dict[str, float] = {}
    for k, v in (scores or {}).items():
        if k not in allowed:
            raise ValidationFailed(f"unknown quality dimension {k}")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= float(v) <= 1:
            raise ValidationFailed(f"{k} must be a score between 0 and 1")
        clean[k] = float(v)
    if not clean:
        raise ValidationFailed("at least one quality score is required")
    q = _scope(select(EvalRun).where(EvalRun.suite_key == suite_key, EvalRun.status == "RUNNING"),
               domain=domain, scoped=scoped)
    for r in session.scalars(q):
        bindings = [dict(b) for b in r.bindings or []]
        for b in bindings:
            if b.get("blind_id") and b["blind_id"] == item_id:
                if b.get("quality"):
                    raise Conflict("this sample was already reviewed", code="ALREADY_REVIEWED")
                b["quality"] = clean
                b["quality_reviewer"] = reviewer_user_id
                r.bindings = bindings
                session.flush()
                audit.record(session, actor_type="user", actor_id=reviewer_user_id, action="eval.blind_review",
                             target_type="eval_run", target_id=r.id, domain=r.submitted_domain,
                             details={"item_id": item_id, "dimensions": sorted(clean)})
                return {"item_id": item_id, "recorded": sorted(clean)}
    raise NotFound("blind review item not found (already collected or not visible)")
