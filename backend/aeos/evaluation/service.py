"""Model/configuration evaluation and leaderboard (§43, §44, ADR-0081).

* Unit of evaluation = full AIConfiguration (model/version + provider route + harness + prompt +
  tools + policy + context params); results with the same model name on another route are never
  merged — grouping is by configuration hash.
* Security/authority/evidence gates are ELIGIBILITY filters: a failing (or unproven) configuration
  is excluded, never out-scored by speed or price.
* Failed/timeout/quota-interrupted samples are never dropped; they count in every denominator.
* The leaderboard is computed only from recorded EvalRuns, per role, with sample size, variance,
  Wilson intervals and ties. No runs -> every candidate is BELUM DIUJI.
* Results never change routes. A scorecard can only open a ChangeProposal (decision request).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
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
from aeos.models.research import EvalRun, EvalSuite
from aeos.models.routing import ModelRoute

NOT_TESTED = "BELUM_DIUJI"
SMALL_SAMPLE = 10  # informational only: "Kecilnya sampel ... membatasi kesimpulan" (§40); not a universal threshold
CANDIDATE_POOLS = ("WORKER", "ARENA", "COMPARATOR", "LOCAL", "WATCHLIST")


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
    }


def _stored_hash(s: EvalSuite) -> str:
    return content_hash({"suite_key": s.suite_key, "title": s.title, "role_key": s.role_key,
                         "task_class": s.task_class, "description": s.description, "cases": s.cases,
                         "acceptance": s.acceptance, "isolation": s.isolation, "protocol": D.PROTOCOL,
                         "version": s.version})


def list_suites(session: Session) -> list[dict[str, Any]]:
    return [suite_view(s) for s in session.scalars(select(EvalSuite).order_by(EvalSuite.suite_key))]


def run_view(r: EvalRun) -> dict[str, Any]:
    return {
        "id": r.id, "suite_key": r.suite_key, "role_key": r.role_key, "topology": r.topology, "status": r.status,
        "configuration": r.configuration, "configuration_hash": r.configuration_hash, "route_key": r.route_key,
        "repetitions": r.repetitions, "sample_size": r.sample_size, "metrics": r.metrics or {},
        "failures": r.failures or [], "gate_results": r.gate_results or {}, "limits": r.limits or {},
        "planned_by": r.planned_by, "result_source": r.result_source, "mode": r.mode, "notes": r.notes,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "eligibility": eligibility(r.gate_results or {}) if r.status == "COMPLETED" else None,
    }


def _scope(q: Any, *, domain: str | None, scoped: bool) -> Any:
    """Viewer scoping: platform rows plus rows planned in the viewer's own domain (never the other one)."""
    if not scoped:
        return q
    if domain is None:
        return q.where(EvalRun.submitted_domain.is_(None))
    return q.where(or_(EvalRun.submitted_domain.is_(None), EvalRun.submitted_domain == domain))


def list_runs(session: Session, *, suite_key: str | None = None, role: str | None = None, domain: str | None = None,
              scoped: bool = False) -> list[dict[str, Any]]:
    q = _scope(select(EvalRun).order_by(EvalRun.created_at.desc()), domain=domain, scoped=scoped)
    if suite_key:
        q = q.where(EvalRun.suite_key == suite_key)
    if role:
        q = q.where(EvalRun.role_key == role)
    return [run_view(r) for r in session.scalars(q)]


# --------------------------------------------------------------------------- planning a run


def normalise_configuration(cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    missing = [f for f in D.AI_CONFIGURATION_FIELDS if f not in cfg or cfg[f] in (None, "")]
    if missing:
        raise ValidationFailed("AIConfiguration is incomplete; state UNKNOWN explicitly where a value is not known",
                               details={"missing": missing})
    out: dict[str, Any] = {}
    unknown: list[str] = []
    for f in D.AI_CONFIGURATION_FIELDS:
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


def plan_run(session: Session, *, suite_key: str, configuration: dict[str, Any], role_key: str | None,
             topology: str | None, repetitions: int, limits: dict[str, Any], actor_user_id: str,
             notes: str = "", domain: str | None = None) -> EvalRun:
    suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == suite_key))
    if suite is None:
        raise NotFound("eval suite not found")
    if topology is not None and topology not in {e["key"] for e in D.EXPERIMENTS}:
        raise ValidationFailed("topology must be one of A, B, C, D")
    if not 1 <= repetitions <= 50:
        raise ValidationFailed("repetitions must be between 1 and 50")
    required_limits = ("max_attempts", "max_minutes", "max_cost_usd")
    missing = [k for k in required_limits if k not in limits]
    if missing:
        raise ValidationFailed("declare budget/attempt/time limits before an evaluation (§44)",
                               details={"missing": missing})
    for k in required_limits:
        v = limits[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or (isinstance(v, float) and math.isnan(v)):
            raise ValidationFailed(f"{k} must be a number >= 0")
    if limits["max_cost_usd"] > 0:
        # Paid evaluation spend needs the same owner BUDGET decision as any worker spend; the plan
        # only records the cap — reservations are still enforced by aeos.cost at execution time.
        limits = {**limits, "budget_note": "Biaya > $0 memerlukan keputusan BUDGET; admission tetap lewat aeos.cost"}
    cfg, unknown = normalise_configuration(configuration)
    role = role_key or suite.role_key
    run = EvalRun(
        suite_key=suite_key, configuration=cfg, configuration_hash=content_hash(cfg), topology=topology,
        status="PLANNED", sample_size=0, metrics={}, failures=[], gate_results={}, role_key=role,
        route_key=cfg["provider_route"] if cfg["provider_route"] != "UNKNOWN" else None, repetitions=repetitions,
        samples=[], limits=redact(limits), planned_by=actor_user_id, notes=redact_text(notes or "")[:2000], mode="LIVE",
        submitted_domain=domain,
    )
    session.add(run)
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.plan", target_type="eval_run",
                 target_id=run.id, domain=domain, details={"suite_key": suite_key, "configuration_hash": run.configuration_hash,
                                            "unknown_fields": unknown, "topology": topology})
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


def compute_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """§43 dimensions computed by code from per-sample verifier observations."""
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

    def quality(key: str) -> float | None:
        vals = [x for s in samples if (x := _num((s.get("quality") or {}).get(key))) is not None]
        return round(statistics.fmean(vals), 4) if vals else None

    rate = (k / n) if n else None
    wilson = _wilson(k, n)
    return {
        "sample_size": n,
        "outcome": {
            "first_pass_acceptance_rate": (first_pass / n) if n else None,
            "final_acceptance_rate": rate, "accepted": k, "regressions": total("regressions"),
            "acceptance_ci95": list(wilson) if wilson else None,
            "acceptance_variance": (rate * (1 - rate)) if rate is not None else None,
        },
        "contract": {"scope_adherence_rate": (in_scope / n) if n else None,
                     "permission_attempts": total("permission_attempts"), "false_claims": total("false_claims")},
        "reliability": {"completion_rate": (completed / n) if n else None,
                        "provider_availability_rate": ((n - provider_down) / n) if n else None,
                        "provider_unavailable": provider_down,
                        "tool_schema_failures": total("tool_failures"), "repair_count": total("repair_count"),
                        "quota_interrupted": sum(1 for s in samples if s.get("status") == "QUOTA_INTERRUPTED"),
                        "timeouts": sum(1 for s in samples if s.get("status") == "TIMEOUT"),
                        "failed": sum(1 for s in samples if s.get("status") == "FAILED")},
        "product_quality": {"ui_a11y": quality("ui_a11y"), "api_data_behavior": quality("api_data"),
                            "maintainability": quality("maintainability"), "doc_accuracy": quality("doc_accuracy")},
        "oversight": {"claude_reviews": total("claude_review_count"),
                      "human_interventions": total("human_interventions"),
                      "human_corrections": total("human_corrections")},
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


def eligibility(gates: dict[str, Any]) -> dict[str, Any]:
    failed = [g for g in D.GATES if str(gates.get(g, "NOT_RUN")).upper() == "FAIL"]
    unproven = [g for g in D.GATES if str(gates.get(g, "NOT_RUN")).upper() not in ("PASS", "FAIL")]
    if failed:
        return {"eligible": False, "status": "EXCLUDED_GATE_FAILED", "gates_failed": failed, "gates_unproven": unproven}
    if unproven:
        return {"eligible": False, "status": "EXCLUDED_GATE_NOT_PROVEN", "gates_failed": [], "gates_unproven": unproven}
    return {"eligible": True, "status": "ELIGIBLE", "gates_failed": [], "gates_unproven": []}


def _config_label(cfg: dict[str, Any]) -> str:
    return f"{cfg.get('model_id')}@{cfg.get('provider_route')} / {cfg.get('harness')} {cfg.get('harness_version')}"


def _rank(entries: list[dict[str, Any]]) -> None:
    """Rank by final acceptance rate; neighbours whose 95% intervals overlap share a tie group."""
    entries.sort(key=lambda e: (-(e["final_acceptance_rate"] or 0.0), -e["sample_size"]))
    group, prev = 0, None
    for e in entries:
        ci = e["acceptance_ci95"]
        if prev is None:
            group = 1
        else:
            pci = prev["acceptance_ci95"]
            overlap = bool(ci and pci and ci[1] >= pci[0] and pci[1] >= ci[0])
            equal = e["final_acceptance_rate"] == prev["final_acceptance_rate"]
            if not (overlap or equal):
                group += 1
        e["rank"] = group
        prev = e
    by_group: dict[int, list[str]] = defaultdict(list)
    for e in entries:
        by_group[e["rank"]].append(e["configuration_hash"])
    for e in entries:
        others = [h for h in by_group[e["rank"]] if h != e["configuration_hash"]]
        e["tied_with"] = others
        e["tie"] = bool(others)


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


def leaderboard(session: Session, *, role: str | None = None, domain: str | None = None,
                scoped: bool = False) -> dict[str, Any]:
    suites = list(session.scalars(select(EvalSuite).order_by(EvalSuite.suite_key)))
    role_suites = [s for s in suites if role is None or s.role_key == role]
    suite_keys = {s.suite_key for s in role_suites}
    runs = list(session.scalars(_scope(select(EvalRun).order_by(EvalRun.created_at), domain=domain, scoped=scoped)))
    relevant = [r for r in runs if (r.suite_key in suite_keys) or (role is not None and r.role_key == role)]
    completed = [r for r in relevant if r.status in ("COMPLETED", "FAILED") and r.samples]
    planned = [r for r in relevant if r.status in ("PLANNED", "RUNNING")]

    per_suite: list[dict[str, Any]] = []
    tested: set[str] = set()
    grouped: dict[tuple[str, str], list[EvalRun]] = defaultdict(list)
    for r in completed:
        grouped[(r.suite_key, r.configuration_hash)].append(r)
        tested.add(r.configuration_hash)
    for s in role_suites:
        eligible: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for (sk, h), rs in grouped.items():
            if sk != s.suite_key:
                continue
            samples = [x for r in rs for x in (r.samples or [])]
            m = compute_metrics(samples)
            gate_views = [eligibility(r.gate_results or {}) for r in rs]
            elig = next((g for g in gate_views if not g["eligible"]), gate_views[0])
            entry = {
                "configuration_hash": h, "label": _config_label(rs[0].configuration),
                "configuration": rs[0].configuration, "topologies": sorted({r.topology or "-" for r in rs}),
                "runs": len(rs), "sample_size": m["sample_size"],
                "final_acceptance_rate": m["outcome"]["final_acceptance_rate"],
                "first_pass_acceptance_rate": m["outcome"]["first_pass_acceptance_rate"],
                "acceptance_ci95": m["outcome"]["acceptance_ci95"],
                "acceptance_variance": m["outcome"]["acceptance_variance"],
                "failures_reported": m["reliability"]["failed"] + m["reliability"]["timeouts"]
                + m["reliability"]["quota_interrupted"],
                "metrics": m, "eligibility": elig, "small_sample": m["sample_size"] < SMALL_SAMPLE,
            }
            (eligible if elig["eligible"] else excluded).append(entry)
        _rank(eligible)
        per_suite.append({"suite_key": s.suite_key, "title": s.title, "role_key": s.role_key,
                          "ranking": eligible, "excluded": excluded,
                          "status": "RANKED" if eligible else (NOT_TESTED if not excluded else "NO_ELIGIBLE")})
    any_results = any(p["ranking"] or p["excluded"] for p in per_suite)
    weights = D.ROLE_WORKLOAD_WEIGHTS.get(role or "")
    return {
        "role": role, "source": "eval_runs (hasil tercatat verifier); tidak ada hasil seed",
        "checked_at": utcnow().isoformat(), "mode": "LIVE",
        "status": "HAS_RESULTS" if any_results else NOT_TESTED,
        "message": None if any_results else "Semua kandidat BELUM DIUJI dalam platform ini",
        "suites": per_suite,
        "candidates_not_tested": _candidates(session, tested, {r.route_key for r in completed if r.route_key}, planned),
        "combined": None if not weights else {"weights": weights},
        "combined_note": ("Ranking gabungan hanya memakai bobot workload yang dideklarasikan; belum ada bobot, "
                          "sehingga tidak ada urutan universal.") if not weights else None,
        "route_change": "NONE - hasil evaluasi tidak mengubah route; gunakan scorecard untuk membuka proposal/keputusan.",
        "ranking_rule": ("Hanya konfigurasi yang lulus gate security/authority/evidence; urutan berdasarkan final "
                         "acceptance rate dengan interval Wilson 95%; interval yang tumpang tindih = seri."),
        "dimensions": D.DIMENSIONS,
    }


def scorecard_snapshot(session: Session, *, role: str, domain: str | None = None, scoped: bool = False) -> dict[str, Any]:
    board = leaderboard(session, role=role, domain=domain, scoped=scoped)
    snapshot = {"role": role, "suites": [{"suite_key": s["suite_key"],
                                          "ranking": [{k: e[k] for k in ("configuration_hash", "label", "rank", "tie",
                                                                         "sample_size", "final_acceptance_rate",
                                                                         "acceptance_ci95")} for e in s["ranking"]],
                                          "excluded": [e["configuration_hash"] for e in s["excluded"]]}
                                         for s in board["suites"]]}
    return snapshot | {"scorecard_hash": content_hash(snapshot), "status": board["status"]}
