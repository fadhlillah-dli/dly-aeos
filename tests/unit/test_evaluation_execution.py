"""Evaluation execution and the honest leaderboard (§28, §34, §40, §43, §44, §49 M6).

* the trusted collector derives samples only from canonical task records (TaskExecution, WorkerRun,
  AcceptanceRecord, ReviewRecord, BrokerRequest, usage ledger) of tasks bound to case slots, refuses tasks
  that ran on another route/harness, never drops unexecuted slots and derives the gates from the records;
* leaderboard: critical regression excludes, topology results are not pooled, only equal conditions are
  ranked, a baseline yields deltas, mixed environments are flagged, a registered route failing the terms/data
  gate is excluded, combined ranking only with declared weights;
* failures become CANDIDATE cases adopted only by a human PROMOTION decision; blind qualitative review;
  baseline-vs-candidate comparison for a proposal; the held-out evaluation code is a protected path.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from aeos.core.errors import Conflict, NotFound, PolicyDenied, ValidationFailed
from aeos.core.ids import new_id
from aeos.core.timeutil import utcnow
from aeos.evaluation import definitions as D
from aeos.evaluation import executor, service
from aeos.models.broker_ext import BrokerRequest
from aeos.models.cost import UsageLedgerEntry
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact, ReviewRecord
from aeos.models.research import ChangeProposal, EvalCaseCandidate, EvalRun, EvalSuite
from aeos.models.routing import ModelRoute
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.routing import seed as route_seed
from tests.unit.test_routing_fakes import install_fakes
from tests.unit.test_workforce_support import make_plan, make_project

LIMITS = {"max_attempts": 2, "max_minutes": 30, "max_cost_usd": 0}
ROUTE = "opencode-zen/deepseek-v4.1-flash-free"
SUITE = "eval.small-bugfix"  # 5 cases


@pytest.fixture
def ev(db, monkeypatch):
    install_fakes(monkeypatch, cost=False)
    route_seed.run(db)
    service.seed_suites(db)
    db.commit()
    return db


def cfg(model="opencode/deepseek-v4.1-flash-free", route=ROUTE, provider="opencode-zen", **kw):
    base = {"model_id": model, "model_version": "UNKNOWN", "provider": provider, "provider_route": route,
            "harness": "opencode", "harness_version": "1.19.3", "prompt_revision": "wp-1", "tools": ["fs.read"],
            "policy_version": "pol-1", "context_params": {}}
    return base | kw


def plan(db, config=None, **kw):
    kw.setdefault("repetitions", 1)
    return service.plan_run(db, suite_key=kw.pop("suite", SUITE), configuration=config or cfg(), role_key=None,
                            topology=kw.pop("topology", None), limits=kw.pop("limits", LIMITS), actor_user_id="owner",
                            **kw)


def record(db, config, *, accepted, total, gates=None, samples=None, **kw):
    r = plan(db, config, **kw)
    rows = samples or [{"sample_id": f"s{i}", "status": "ACCEPTED" if i < accepted else "REJECTED",
                        "first_pass": i < accepted, "worker_cost_usd": 0.0} for i in range(total)]
    service.record_result(db, r.id, samples=rows, result_source="verifier",
                          gate_results=gates or {"security": "PASS", "authority": "PASS", "evidence": "PASS"})
    db.flush()
    return r


def suite_of(board, key=SUITE):
    return next(s for s in board["suites"] if s["suite_key"] == key)


# ------------------------------------------------------------------------------------------- the collector


def _executed(db, spec, *, state, route=ROUTE, attempts=1, accepted=None, quota=False, engine="1.19.3",
              denied=0, quarantined=False, claude_reviews=1, cost=0.01):
    """Canonical records the workforce/verifier would leave behind for one executed task."""
    now = utcnow()
    te = TaskExecution(task_id=spec.id, project_id=spec.project_id, domain=spec.domain, state=state,
                       attempts_used=attempts, created_at=now - timedelta(minutes=30))
    db.add(te)
    runs = []
    for i in range(attempts):
        run = WorkerRun(task_id=spec.id, project_id=spec.project_id, domain=spec.domain, attempt=i + 1,
                        model_route_key=route, engine="opencode", engine_version=engine,
                        execution_status="DONE" if state == "DONE" and i == attempts - 1 else "FAILED",
                        started_at=now - timedelta(minutes=20 - i), finished_at=now - timedelta(minutes=10 - i),
                        quota_status="RATE_LIMITED" if quota else None, quarantined=quarantined,
                        quarantine_reasons=["protected path"] if quarantined else [])
        db.add(run)
        db.flush()
        runs.append(run)
        db.add(UsageLedgerEntry(run_id=run.id, task_id=spec.id, provider="opencode-zen", category="WORKER_API",
                                source="broker", cost_usd=cost, input_tokens=100, output_tokens=50, route_key=route))
        for _ in range(denied):
            db.add(BrokerRequest(grant_id="g", run_id=run.id, project_id=spec.project_id, domain=spec.domain,
                                 route_key=route, outcome="DENIED"))
        db.add(BrokerRequest(grant_id="g", run_id=run.id, project_id=spec.project_id, domain=spec.domain,
                             route_key=route, outcome="OK", prompt_tokens=100, completion_tokens=50))
    if accepted is not None:
        db.add(AcceptanceRecord(task_id=spec.id, run_id=runs[-1].id, project_id=spec.project_id,
                                artifact_hash="sha256:a", acceptance_status=accepted,
                                reasons=[] if accepted == "ACCEPTED" else ["verifier status is FAIL (PASS required)"]))
    for _ in range(claude_reviews):
        db.add(ReviewRecord(target_type="run", target_id=runs[-1].id, task_id=spec.id, reviewer_kind="CLAUDE",
                            verdict="PASS"))
    db.flush()
    return te


def _running(db, n_tasks=5, *, domain="PERSONAL"):
    run = plan(db)
    executor.start_run(db, run.id, actor_user_id="owner")
    project = make_project(db, domain=domain)
    _, _, specs = make_plan(db, project, [{"task_key": f"T{i}"} for i in range(n_tasks)])
    return run, specs


def test_collector_records_samples_only_from_executed_task_records(ev):
    db = ev
    run, specs = _running(db)
    slots = executor.slots(db, run)
    assert len(slots) == 5 and run.status == "RUNNING" and run.route_config_hash
    for s, spec in zip(slots, specs, strict=True):
        executor.bind_task(db, run.id, slot_id=s["slot_id"], task_id=spec.id, actor_user_id="owner")
    out = executor.collect(db, run.id)
    assert out["recorded"] is False and len(out["pending"]) == 5  # nothing executed: nothing recorded
    _executed(db, specs[0], state="DONE", accepted="ACCEPTED")
    _executed(db, specs[1], state="DONE", accepted="ACCEPTED")
    _executed(db, specs[2], state="DONE", accepted="ACCEPTED", attempts=2)
    _executed(db, specs[3], state="FAILED", quota=True)
    out = executor.collect(db, run.id)
    assert out["recorded"] is False and [p["task_id"] for p in out["pending"]] == [specs[4].id]
    _executed(db, specs[4], state="FAILED", accepted="REJECTED", attempts=2)
    out = executor.collect(db, run.id)
    assert out["recorded"] is True and run.status == "COMPLETED" and run.result_source == "collector"
    by = {s["task_id"]: s for s in run.samples}
    assert [by[s.id]["status"] for s in specs] == ["ACCEPTED", "ACCEPTED", "ACCEPTED", "QUOTA_INTERRUPTED", "REJECTED"]
    assert by[specs[0].id]["first_pass"] is True and by[specs[2].id]["first_pass"] is False
    assert by[specs[2].id]["repair_count"] == 1 and by[specs[0].id]["latency_s"] == pytest.approx(600, abs=2)
    assert by[specs[0].id]["worker_tokens"] == 150 and by[specs[0].id]["claude_review_count"] == 1
    m = run.metrics
    assert m["sample_size"] == 5 and m["outcome"]["accepted"] == 3 and m["reliability"]["quota_interrupted"] == 1
    assert m["oversight"]["claude_reviews"] == 5 and m["economics"]["worker_cost_status"] == "COMPUTED"
    assert run.gate_results == {"security": "PASS", "authority": "PASS", "evidence": "PASS"}
    excluded = suite_of(service.leaderboard(db, role="feature-implementer"))["excluded"][0]
    assert excluded["eligibility"]["status"] == "EXCLUDED_ROUTE_TERMS"  # seeded route: terms never reviewed by owner
    assert "pricing/terms never checked" in excluded["eligibility"]["route_reasons"]
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE))
    route.terms_checked_at = route.pricing_checked_at = utcnow()  # the owner's terms review (not in the identity)
    entry = suite_of(service.leaderboard(db, role="feature-implementer"))["ranking"][0]
    assert entry["run_ids"] == [run.id] and entry["route_binding"]["route_config_current"] is True


def test_collector_refuses_tasks_run_on_another_route_or_harness(ev):
    db = ev
    run, specs = _running(db, 2)
    s1, s2 = executor.slots(db, run)[:2]
    executor.bind_task(db, run.id, slot_id=s1["slot_id"], task_id=specs[0].id, actor_user_id="owner")
    executor.bind_task(db, run.id, slot_id=s2["slot_id"], task_id=specs[1].id, actor_user_id="owner")
    _executed(db, specs[0], state="DONE", accepted="ACCEPTED", route="opencode-zen/glm-5.3-flash-free")
    _executed(db, specs[1], state="DONE", accepted="ACCEPTED", engine="1.20.0")
    out = executor.collect(db, run.id)
    reasons = {i["slot_id"]: i["reason"] for i in out["invalid"]}
    assert "other routes" in reasons[s1["slot_id"]] and "harness" in reasons[s2["slot_id"]]
    assert out["recorded"] is False and run.status == "RUNNING"
    with pytest.raises(Conflict):  # a valid binding is never silently replaced ...
        executor.bind_task(db, run.id, slot_id=executor.slots(db, run)[2]["slot_id"], task_id=specs[0].id,
                           actor_user_id="owner")
    _, _, more = make_plan(db, make_project(db), [{"task_key": "R1"}])
    executor.bind_task(db, run.id, slot_id=s1["slot_id"], task_id=more[0].id, actor_user_id="owner")  # ... an invalid one is


def test_collector_refuses_earlier_attempts_and_a_route_changed_during_the_run(ev):
    """Samples describe exactly the configuration the run started on: an attempt made before the start, or a
    route configuration that changed (even back again) during the run, never becomes test evidence."""
    db = ev
    run, specs = _running(db, 5)
    for s, spec in zip(executor.slots(db, run), specs, strict=True):
        executor.bind_task(db, run.id, slot_id=s["slot_id"], task_id=spec.id, actor_user_id="owner")
        _executed(db, spec, state="DONE", accepted="ACCEPTED")
    earlier = db.scalar(select(WorkerRun).where(WorkerRun.task_id == specs[0].id))
    earlier.created_at = run.started_at - timedelta(minutes=5)  # executed before the evaluation started
    out = executor.collect(db, run.id)
    assert out["recorded"] is False and "before this evaluation started" in out["invalid"][0]["reason"]
    earlier.created_at = utcnow()
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE))
    original = route.context_window
    route.context_window = 64_000  # e.g. a catalog observation: another configuration
    with pytest.raises(Conflict) as ei:
        executor.collect(db, run.id)
    assert ei.value.code == "ROUTE_CONFIG_CHANGED" and run.status == "RUNNING"
    route.context_window = original  # changed back: same hash, but the run spanned two configurations
    route.config_changed_at = utcnow() + timedelta(seconds=1)
    with pytest.raises(Conflict) as ei:
        executor.collect(db, run.id)
    assert "during the run" in ei.value.message
    executor.finish_run(db, run.id, actor_user_id="owner", reason="route changed during the evaluation")
    assert run.status == "CANCELLED"  # stopped, never COMPLETED evidence


def test_unexecuted_slots_are_recorded_as_cancelled_never_dropped(ev):
    db = ev
    run, specs = _running(db, 1)
    executor.bind_task(db, run.id, slot_id=executor.slots(db, run)[0]["slot_id"], task_id=specs[0].id,
                       actor_user_id="owner")
    _executed(db, specs[0], state="DONE", accepted="ACCEPTED")
    executor.finish_run(db, run.id, actor_user_id="owner", reason="fixtures for the other cases are not authored")
    assert run.status == "CANCELLED" and run.sample_size == 5
    assert [s["status"] for s in run.samples].count("CANCELLED") == 4 and len(run.failures) == 4
    assert suite_of(service.leaderboard(db, role="feature-implementer"))["ranking"] == []  # cancelled: not ranked


def test_gates_are_derived_from_the_records(ev):
    db = ev
    for kw, gate in (({"denied": 1}, "authority"), ({"quarantined": True}, "security")):
        run, specs = _running(db, 5)
        for s, spec in zip(executor.slots(db, run), specs, strict=True):
            executor.bind_task(db, run.id, slot_id=s["slot_id"], task_id=spec.id, actor_user_id="owner")
            _executed(db, spec, state="DONE", accepted="ACCEPTED", **(kw if spec is specs[0] else {}))
        executor.collect(db, run.id)
        assert run.gate_results[gate] == "FAIL", gate
        assert service.run_view(run)["eligibility"]["status"] == "EXCLUDED_GATE_FAILED"


def test_start_and_bind_guards(ev):
    db = ev
    run = plan(db)
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE))
    route.context_window = 99_000  # the route changed after planning: results would describe another config
    with pytest.raises(Conflict) as ei:
        executor.start_run(db, run.id, actor_user_id="owner")
    assert ei.value.code == "ROUTE_CONFIG_CHANGED"
    unknown = plan(db, cfg(route="UNKNOWN"))
    with pytest.raises(Conflict) as ei:
        executor.start_run(db, unknown.id, actor_user_id="owner")
    assert ei.value.code == "ROUTE_UNKNOWN"
    route.context_window = None
    suite = db.scalar(select(EvalSuite).where(EvalSuite.suite_key == SUITE))
    suite.content_hash = "sha256:tampered"
    with pytest.raises(Conflict) as ei:
        executor.start_run(db, run.id, actor_user_id="owner")
    assert ei.value.code == "SUITE_INTEGRITY"
    suite.content_hash = service._stored_hash(suite)
    executor.start_run(db, run.id, actor_user_id="owner")
    _, _, office = make_plan(db, make_project(db, domain="OFFICE"), [{"task_key": "O1"}])
    with pytest.raises(NotFound):  # an Office task never feeds a platform/Personal evaluation
        executor.bind_task(db, run.id, slot_id=executor.slots(db, run)[0]["slot_id"], task_id=office[0].id,
                           actor_user_id="owner")
    with pytest.raises(ValidationFailed):
        executor.bind_task(db, run.id, slot_id="nope#1", task_id=office[0].id, actor_user_id="owner")
    assert service.run_view(run)["execution"]["status"] == "COLLECTING"
    assert service.run_view(plan(db))["execution"]["fixtures"] == "NOT_AVAILABLE_IN_BUILD"


# ------------------------------------------------------------------------------------------- leaderboard


def test_regression_excludes_even_with_higher_acceptance(ev):
    db = ev
    record(db, cfg(model="fast", route="r-fast"), accepted=0, total=0,
           samples=[{"status": "ACCEPTED"} for _ in range(9)] + [{"status": "ACCEPTED", "critical_regressions": 1}])
    record(db, cfg(model="steady", route="r-steady"), accepted=6, total=10)
    s = suite_of(service.leaderboard(db, role="feature-implementer"))
    assert [e["configuration"]["model_id"] for e in s["ranking"]] == ["steady"]
    assert s["excluded"][0]["eligibility"]["status"] == "EXCLUDED_REGRESSION"
    adjacent = record(db, cfg(model="adj", route="r-adj"), accepted=0, total=0,
                      samples=[{"status": "ACCEPTED", "adjacent_regressions": 1}])
    assert service.run_view(adjacent)["eligibility"]["eligible"] is True  # adjacent regressions are reported only
    assert adjacent.metrics["outcome"]["adjacent_regressions"] == 1


def test_topology_results_are_not_pooled_and_compared_on_a_fixed_configuration(ev):
    db = ev
    same = cfg(model="m", route="r-m")
    record(db, same, accepted=2, total=10, topology="A")
    record(db, same, accepted=9, total=10, topology="C")
    record(db, cfg(model="other", route="r-o"), accepted=5, total=10, topology="B")
    s = suite_of(service.leaderboard(db, role="feature-implementer"))
    mine = [e for e in s["ranking"] if e["configuration"]["model_id"] == "m"]
    assert sorted(e["topology"] for e in mine) == ["A", "C"] and all(e["sample_size"] == 10 for e in mine)
    h = mine[0]["configuration_hash"]
    comp = service.topology_comparison(db, suite_key=SUITE, configuration_hash=h)
    assert comp["status"] == "COMPARED" and [t["topology"] for t in comp["topologies"]] == ["A", "C"]
    assert comp["excluded_other_configurations"] and h not in comp["excluded_other_configurations"]
    best = min(comp["topologies"], key=lambda t: t["rank"])
    assert best["topology"] == "C"


def test_only_runs_under_equal_conditions_are_ranked_together(ev):
    db = ev
    record(db, cfg(model="a", route="ra"), accepted=6, total=10)
    record(db, cfg(model="b", route="rb"), accepted=7, total=10)
    record(db, cfg(model="c", route="rc"), accepted=10, total=10, limits={**LIMITS, "max_attempts": 5})  # more repair budget
    s = suite_of(service.leaderboard(db, role="feature-implementer"))
    assert {e["configuration"]["model_id"] for e in s["ranking"]} == {"a", "b"}
    assert [e["configuration"]["model_id"] for e in s["not_comparable"]] == ["c"]
    assert "NOT_COMPARABLE" in s["not_comparable"][0]["comparability"]


def test_baseline_gives_each_entry_a_delta(ev):
    db = ev
    record(db, cfg(model="base", route="rbase"), accepted=5, total=50, is_baseline=True)
    record(db, cfg(model="cand", route="rcand"), accepted=48, total=50)
    s = suite_of(service.leaderboard(db, role="feature-implementer"))
    by = {e["configuration"]["model_id"]: e for e in s["ranking"]}
    assert s["baseline"] == by["base"]["entry_key"] and by["base"]["delta_vs_baseline"] is None
    assert by["cand"]["delta_vs_baseline"]["acceptance_rate"] == pytest.approx(0.86)
    assert by["cand"]["delta_vs_baseline"]["conclusion"] == "BETTER"
    no_base = suite_of(service.leaderboard(db, role="feature-implementer"), "eval.feature-fe-api")
    assert no_base["baseline"] is None and no_base["baseline_note"]


def test_mixed_environments_are_flagged(ev):
    db = ev
    r1 = record(db, cfg(model="m", route="rm"), accepted=5, total=10)
    r2 = record(db, cfg(model="m", route="rm"), accepted=6, total=10)
    assert r1.environment["os"] and r1.conditions_hash == r2.conditions_hash
    r2.environment = {**r2.environment, "controller": "9.9.9"}
    entry = suite_of(service.leaderboard(db, role="feature-implementer"))["ranking"][0]
    assert entry["environment_mixed"] is True and entry["runs"] == 2


def test_registered_route_failing_the_terms_gate_is_excluded(ev):
    db = ev
    gemini = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "gemini/gemini-3.8-flash-free-tier"))
    record(db, cfg(model=gemini.model_id, route=gemini.route_key, provider="gemini"), accepted=10, total=10)
    record(db, cfg(model="unregistered", route="r-unregistered"), accepted=6, total=10)
    s = suite_of(service.leaderboard(db, role="feature-implementer"))
    assert [e["configuration"]["model_id"] for e in s["ranking"]] == ["unregistered"]
    assert s["ranking"][0]["route_binding"]["status"] == "NOT_REGISTERED"  # flagged, cannot be activated anyway
    ex = s["excluded"][0]
    assert ex["eligibility"]["status"] == "EXCLUDED_ROUTE_TERMS"
    assert any("INTERNAL not allowed" in x for x in ex["eligibility"]["route_reasons"])


def test_combined_ranking_only_with_declared_weights(ev):
    db = ev
    for model, a, b in (("x", 9, 2), ("y", 5, 9)):
        record(db, cfg(model=model, route=f"r-{model}"), accepted=a, total=10)
        record(db, cfg(model=model, route=f"r-{model}"), accepted=b, total=10, suite="eval.feature-fe-api")
    record(db, cfg(model="z", route="r-z"), accepted=10, total=10)  # no result on the second weighted suite
    board = service.leaderboard(db, role="feature-implementer")
    assert board["combined"] is None and "bobot" in board["combined_note"]
    with pytest.raises(ValidationFailed):
        service.declare_weights(db, role="feature-implementer", weights={"eval.docs-only-change": 1.0},
                                actor_user_id="owner", rationale="docs suite is another role")
    service.declare_weights(db, role="feature-implementer", weights={SUITE: 3.0, "eval.feature-fe-api": 1.0},
                            actor_user_id="owner", rationale="bugfixes are three times as frequent here")
    db.flush()
    combined = service.leaderboard(db, role="feature-implementer")["combined"]
    assert combined["weights"] == {SUITE: 3.0, "eval.feature-fe-api": 1.0} and combined["source"].startswith("decision:")
    labels = [e["label"].split("@")[0] for e in combined["ranking"]]
    assert labels[0] == "x" and combined["ranking"][0]["score"] == pytest.approx((3 * 0.9 + 0.2) / 4)
    assert [i["label"].split("@")[0] for i in combined["incomplete"]] == ["z"]
    assert all(len(e["interval"]) == 2 and "tie" in e for e in combined["ranking"])


def test_combined_ranking_never_ranks_a_suite_result_without_its_interval():
    """A weighted suite result needs its rate AND its 95% interval; otherwise the configuration is incomplete
    (never ranked on a missing bound, never a crash)."""
    def entry(key, rate, ci):
        return {"entry_key": key, "label": key, "configuration_hash": f"h-{key}", "topology": None,
                "final_acceptance_rate": rate, "acceptance_ci95": ci, "sample_size": 10}

    per_suite = [{"suite_key": "a", "ranking": [entry("x", 0.9, [0.6, 0.98]), entry("y", 0.5, None),
                                                entry("z", None, None)]},
                 {"suite_key": "b", "ranking": [entry("x", 0.2, [0.05, 0.5]), entry("y", 0.9, [0.6, 0.98])]}]
    out = service.combined_ranking(per_suite, {"a": 3.0, "b": 1.0}, source="decision:d")
    assert [e["entry_key"] for e in out["ranking"]] == ["x"]
    assert out["ranking"][0]["score"] == pytest.approx((3 * 0.9 + 0.2) / 4)
    assert out["ranking"][0]["sample_size"] == 20
    assert out["incomplete"] == [{"entry_key": "y", "label": "y", "missing_suites": ["a"]},
                                 {"entry_key": "z", "label": "z", "missing_suites": ["a", "b"]}]


def test_metric_dimensions_are_computed_from_sample_values():
    """§43 Contract / Product quality / Oversight and the §44 measures, from per-sample values."""
    m = service.compute_metrics([
        {"status": "ACCEPTED", "scope_violations": 0, "permission_attempts": 0, "false_claims": 0,
         "quality": {"ui_a11y": 0.8, "maintainability": 0.6}, "claude_review_count": 2, "human_interventions": 1,
         "context_tokens": 1000, "coordination_tokens": 200, "worker_tokens": 5000, "merge_conflicts": 1,
         "critical_regressions": 0, "adjacent_regressions": 2, "security_violations": 0},
        {"status": "REJECTED", "scope_violations": 1, "permission_attempts": 2, "false_claims": 1,
         "quality": {"ui_a11y": 0.4}, "claude_review_count": 1, "human_corrections": 1, "worker_tokens": 3000},
    ])
    assert m["contract"] == {"scope_adherence_rate": 0.5, "permission_attempts": 2, "false_claims": 1,
                             "security_violations": 0}
    assert m["product_quality"]["ui_a11y"] == pytest.approx(0.6) and m["product_quality"]["maintainability"] == 0.6
    assert m["product_quality"]["api_data_behavior"] is None and m["product_quality"]["reviewed_samples"] == 2
    assert m["oversight"] == {"claude_reviews": 3, "human_interventions": 1, "human_corrections": 1}
    assert m["usage"]["context_tokens"] == 1000 and m["usage"]["coordination_tokens"] == 200
    assert m["usage"]["worker_tokens"] == 8000 and m["usage"]["samples_with_token_counts"] == 2
    assert m["reliability"]["merge_conflicts"] == 1
    assert m["outcome"]["adjacent_regressions"] == 2 and m["outcome"]["critical_regressions"] == 0
    assert {"usage"} == {d["key"] for d in D.SECTION_44_METRICS}


def test_local_build_parameters_are_part_of_the_configuration(ev):
    db = ev
    local = cfg(model="Qwen/Qwen3.8-Flash-Next", route="local/test-build", provider="local")
    with pytest.raises(ValidationFailed) as ei:
        plan(db, local)
    assert set(ei.value.details["missing"]) == {"quantization", "inference_backend", "hardware"}
    q4 = plan(db, local | {"quantization": "Q4_K_M", "inference_backend": "llama.cpp", "hardware": "RTX 4070"})
    fp16 = plan(db, local | {"quantization": "FP16", "inference_backend": "vllm", "hardware": "RTX 4070"})
    assert q4.configuration_hash != fp16.configuration_hash  # a quantized build never inherits another's results
    service.record_result(db, fp16.id, samples=[{"status": "ACCEPTED"}], result_source="verifier",
                          gate_results={"security": "PASS", "authority": "PASS", "evidence": "PASS"})
    s = suite_of(service.leaderboard(db, role="feature-implementer"))
    assert [e["configuration"]["quantization"] for e in s["ranking"]] == ["FP16"]


# ------------------------------------------------------------------------------------------- §28 / §43 / §40


def test_observed_failure_becomes_a_candidate_case_adopted_only_by_a_human_promotion(ev):
    db = ev
    project = make_project(db)
    run = WorkerRun(task_id="t1", project_id=project.id, domain="PERSONAL", execution_status="FAILED",
                    outcome_code="SCOPE_VIOLATION", role_key="feature-implementer")
    ok = WorkerRun(task_id="t2", project_id=project.id, domain="PERSONAL", execution_status="DONE")
    untrusted = EvidenceArtifact(kind="TEST_REPORT", origin="worker", collector_identity="worker:x", trusted=False,
                                 content_hash="sha256:w", claim="all tests pass")
    db.add_all([run, ok, untrusted])
    db.flush()
    P = "PERSONAL"
    with pytest.raises(Conflict):
        service.propose_case_from_failure(db, source_type="worker_run", source_id=ok.id, actor_user_id="owner",
                                          title="not a failure", expected="nothing to reproduce here", viewer_domain=P)
    with pytest.raises(PolicyDenied):
        service.propose_case_from_failure(db, source_type="evidence", source_id=untrusted.id, actor_user_id="owner",
                                          title="worker claim", expected="a worker claim is not a failure record")
    with pytest.raises(NotFound):  # domain-first: a PERSONAL failure is never read from a session without a domain
        service.propose_case_from_failure(db, source_type="worker_run", source_id=run.id, actor_user_id="owner",
                                          title="Write outside allowed paths",
                                          expected="Changes stay inside the TaskSpec write set; violation reported")
    cand = service.propose_case_from_failure(db, source_type="worker_run", source_id=run.id, actor_user_id="owner",
                                             title="Write outside allowed paths", related_suite_key=SUITE,
                                             expected="Changes stay inside the TaskSpec write set; violation reported",
                                             viewer_domain=P)
    assert cand.status == "CANDIDATE" and cand.case["source"]["id"] == run.id and cand.role_key == "feature-implementer"
    with pytest.raises(Conflict):
        service.propose_case_from_failure(db, source_type="worker_run", source_id=run.id, actor_user_id="owner",
                                          title="duplicate", expected="the same failure is proposed only once",
                                          viewer_domain=P)
    assert not db.scalar(select(EvalSuite).where(EvalSuite.task_class == "regression"))  # unused until adopted
    with pytest.raises(Conflict):
        service.adopt_case(db, cand.id, actor_user_id="owner", expected_case_hash="sha256:other",
                           rationale="reviewed the regression case", viewer_domain=P)
    for viewer in (None, "OFFICE"):  # a PERSONAL candidate is invisible outside PERSONAL
        with pytest.raises(NotFound):
            service.adopt_case(db, cand.id, actor_user_id="owner", expected_case_hash=cand.case_hash,
                               rationale="reviewed the regression case", viewer_domain=viewer)
        with pytest.raises(NotFound):
            service.reject_case(db, cand.id, actor_user_id="owner", reason="not relevant", viewer_domain=viewer)
    suite = service.adopt_case(db, cand.id, actor_user_id="owner", expected_case_hash=cand.case_hash,
                               rationale="reviewed the regression case", viewer_domain=P)
    assert suite.protected and suite.held_out and service.suite_view(suite)["integrity"] == "OK"
    assert cand.status == "ADOPTED" and cand.decision_id and cand.adopted_suite_key == suite.suite_key
    office = WorkerRun(task_id="t3", project_id=project.id, domain="OFFICE", execution_status="DONE")
    db.add(office)
    db.flush()
    with pytest.raises(NotFound):  # not "NOT_A_FAILURE": the state of an Office run is not revealed to PERSONAL
        service.propose_case_from_failure(db, source_type="worker_run", source_id=office.id, actor_user_id="owner",
                                          title="Office failure", expected="never promoted into platform suites",
                                          viewer_domain=P)
    office.execution_status = "FAILED"
    oc = service.propose_case_from_failure(db, source_type="worker_run", source_id=office.id, actor_user_id="owner",
                                           title="Office failure", expected="never promoted into platform suites",
                                           viewer_domain="OFFICE")
    with pytest.raises(PolicyDenied):
        service.adopt_case(db, oc.id, actor_user_id="owner", expected_case_hash=oc.case_hash,
                           rationale="trying to promote office data", viewer_domain="OFFICE")
    assert db.get(EvalCaseCandidate, oc.id).status == "CANDIDATE"


def test_blind_review_scores_feed_product_quality(ev):
    db = ev
    run, specs = _running(db, 5)
    for s, spec in zip(executor.slots(db, run), specs, strict=True):
        executor.bind_task(db, run.id, slot_id=s["slot_id"], task_id=spec.id, actor_user_id="owner")
        _executed(db, spec, state="DONE", accepted="ACCEPTED")
    items = service.blind_review_items(db, suite_key=SUITE, scoped=False)["items"]
    assert len(items) == 5
    text = repr(items)
    assert run.id not in text and ROUTE not in text and run.configuration_hash not in text  # anonymised
    assert all(i["item_id"] not in repr(service.run_view(run)) for i in items)  # the blind id is never shown
    with pytest.raises(ValidationFailed):
        service.record_blind_review(db, suite_key=SUITE, item_id=items[0]["item_id"], scores={"ui_a11y": 3},
                                    reviewer_user_id="owner", scoped=False)
    service.record_blind_review(db, suite_key=SUITE, item_id=items[0]["item_id"], reviewer_user_id="owner",
                                scores={"maintainability": 0.75}, scoped=False)
    with pytest.raises(Conflict):
        service.record_blind_review(db, suite_key=SUITE, item_id=items[0]["item_id"], reviewer_user_id="owner",
                                    scores={"maintainability": 0.1}, scoped=False)
    executor.collect(db, run.id)
    assert run.metrics["product_quality"]["maintainability"] == 0.75
    assert run.metrics["product_quality"]["reviewed_samples"] == 1


def test_controlled_comparison_for_a_proposal_uses_cost_per_accepted_outcome(ev):
    """§40: baseline vs candidate on the same suite and limits; a token-saving candidate with fewer accepted
    outcomes is worse on cost per accepted outcome ("output lebih pendek bukan bukti biaya lebih rendah")."""
    db = ev
    p = ChangeProposal(kind="UPGRADE", title="context reduction tool", submitted_by="owner", status="TRIAL_APPROVED",
                       domain="PERSONAL")
    db.add(p)
    db.flush()
    base, cand = cfg(model="base", route="r-base"), cfg(model="base", route="r-base", context_params={"rtk": True})
    with pytest.raises(ValidationFailed):
        service.plan_comparison(db, proposal_id=p.id, suite_keys=[SUITE], baseline=base, candidate=base, limits=LIMITS,
                                repetitions=1, actor_user_id="owner", domain="PERSONAL")
    for viewer in (None, "OFFICE"):  # domain-first: a PERSONAL proposal is prepared only from PERSONAL
        with pytest.raises(NotFound):
            service.plan_comparison(db, proposal_id=p.id, suite_keys=[SUITE], baseline=base, candidate=cand,
                                    limits=LIMITS, repetitions=1, actor_user_id="owner", domain=viewer)
    runs = service.plan_comparison(db, proposal_id=p.id, suite_keys=[SUITE], baseline=base, candidate=cand,
                                   limits=LIMITS, repetitions=1, actor_user_id="owner", domain="PERSONAL")
    assert [r.is_baseline for r in runs] == [True, False] and {r.proposal_id for r in runs} == {p.id}
    assert service.comparison(db, proposal_id=p.id)["status"] == "IN_PROGRESS"
    gates = {"security": "PASS", "authority": "PASS", "evidence": "PASS"}
    service.record_result(db, runs[0].id, result_source="verifier", gate_results=gates, samples=[
        {"status": "ACCEPTED" if i < 8 else "REJECTED", "worker_tokens": 10_000, "worker_cost_usd": 0.10}
        for i in range(10)])
    service.record_result(db, runs[1].id, result_source="verifier", gate_results=gates, samples=[
        {"status": "ACCEPTED" if i < 5 else "REJECTED", "worker_tokens": 6_000, "worker_cost_usd": 0.08}
        for i in range(10)])
    comp = service.comparison(db, proposal_id=p.id)
    side = comp["suites"][0]
    b, c = side["baseline"]["result"]["metrics"], side["candidate"]["result"]["metrics"]
    assert c["usage"]["worker_tokens"] < b["usage"]["worker_tokens"]  # fewer tokens ...
    assert c["economics"]["worker_cost_per_accepted_usd"] > b["economics"]["worker_cost_per_accepted_usd"]  # ... costlier
    assert comp["status"] == "RECORDED" and side["equal_conditions"] and side["verdict"] in ("WORSE", "NOT_DISTINGUISHABLE")
    p.status = "REJECTED"
    with pytest.raises(Conflict):
        service.plan_comparison(db, proposal_id=p.id, suite_keys=[SUITE], baseline=base, candidate=cand,
                                limits=LIMITS, repetitions=1, actor_user_id="owner", domain="PERSONAL")


def test_activation_evidence_contract(ev):
    db = ev
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE))
    good = record(db, cfg(), accepted=8, total=10)
    other_model = record(db, cfg(model="not-this-model"), accepted=8, total=10)
    from aeos.routing.service import compute_config_hash

    out = service.activation_evidence(db, route_key=ROUTE, route_config_hash=compute_config_hash(route),
                                      run_ids=[good.id, other_model.id, new_id()])
    assert out["ok"] is False and len(out["problems"]) == 2
    assert any("does not describe this route" in p for p in out["problems"])
    ok = service.activation_evidence(db, route_key=ROUTE, route_config_hash=compute_config_hash(route), run_ids=[good.id])
    assert ok["ok"] and ok["roles"]["feature-implementer"]["run_ids"] == [good.id]


def test_activation_evidence_needs_an_accepted_outcome_and_never_an_office_run(ev):
    """U-07 "lolos evaluasi": gate-passing runs without a single accepted outcome did not pass; an evaluation
    planned in OFFICE never flows into the (PERSONAL-only) route activation and its shared eval results."""
    db = ev
    from aeos.routing.service import compute_config_hash

    h = compute_config_hash(db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE)))
    none_accepted = record(db, cfg(), accepted=0, total=10)
    office = record(db, cfg(), accepted=8, total=10, domain="OFFICE")
    out = service.activation_evidence(db, route_key=ROUTE, route_config_hash=h, run_ids=[none_accepted.id])
    assert out["ok"] is False and "NO_ACCEPTED_OUTCOME" in out["problems"][0]
    out = service.activation_evidence(db, route_key=ROUTE, route_config_hash=h, run_ids=[office.id])
    assert out["ok"] is False and "never evidence for a PERSONAL route activation" in out["problems"][0]
    good = record(db, cfg(), accepted=1, total=10)
    assert service.activation_evidence(db, route_key=ROUTE, route_config_hash=h, run_ids=[good.id])["ok"] is True


def test_runs_of_another_route_configuration_are_never_pooled(ev):
    """The same declared AIConfiguration on a route whose configuration changed is another configuration (§23)."""
    db = ev
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE))
    route.terms_checked_at = route.pricing_checked_at = utcnow()  # owner terms review: the terms gate passes
    old = record(db, cfg(), accepted=2, total=10)
    route.context_window = 64_000  # the route changed; the declared configuration did not
    new = record(db, cfg(), accepted=9, total=10)
    assert old.configuration_hash == new.configuration_hash and old.route_config_hash != new.route_config_hash
    s = suite_of(service.leaderboard(db, role="feature-implementer"))
    assert [e["run_ids"] for e in s["ranking"]] == [[new.id]] and s["ranking"][0]["sample_size"] == 10
    assert [e["run_ids"] for e in s["excluded"]] == [[old.id]]
    assert s["excluded"][0]["eligibility"]["status"] == "EXCLUDED_STALE_CONFIGURATION"


def test_held_out_evaluation_code_is_a_protected_path():
    """§16/§21: workers may not change graders/held-out tests - a patch touching them is quarantined."""
    from aeos.planning import dag
    from aeos.verification import scope as vscope
    from aeos.worker_runtime import scope as wscope

    path = "backend/aeos/evaluation/definitions.py"
    res = wscope.check_scope([wscope.ChangedPath(path, "M")], allowed=["backend/aeos/**"], forbidden=[],
                             protected=wscope.PLATFORM_PROTECTED_PATHS)
    assert [h["rule"] for h in res.protected_hits] == ["protected-path"]  # quarantined for special review
    other = wscope.check_scope([wscope.ChangedPath("backend/aeos/feature/x.py", "M")], allowed=["backend/aeos/**"],
                               forbidden=[], protected=wscope.PLATFORM_PROTECTED_PATHS)
    assert other.protected_hits == []
    assert "backend/aeos/evaluation/**" in vscope.PLATFORM_PROTECTED_PATHS
    assert "backend/aeos/evaluation/**" in dag.protected_paths() and "backend/aeos/evaluation/**" in dag.FALLBACK_PROTECTED_PATHS
    assert D.GRADER_NOTE and service.suite_view(EvalSuite(suite_key="x", title="x", cases=[]))["grader"] == D.GRADER_NOTE


def test_run_rows_survive_without_new_fields():
    """Runs recorded before the C5 columns existed (no conditions/environment) still render."""
    r = EvalRun(suite_key=SUITE, configuration={}, configuration_hash="sha256:x", status="PLANNED", bindings=None)
    assert service.run_view(r)["bindings"] == [] and executor.execution_view(r)["status"] == "NOT_STARTED"
