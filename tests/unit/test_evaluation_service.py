"""Evaluation & leaderboard (§43, §44; §50 rows "Kandidat lebih cepat/murah tetapi gagal authority/
security gate -> Tidak promoted", "Model alias/provider route berubah -> konfigurasi baru",
"Model/prompt/tool/policy baru -> Controlled eval, version, decision")."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aeos.core.errors import Conflict, NotFound, PolicyDenied, ValidationFailed
from aeos.evaluation import definitions as D
from aeos.evaluation import facade, service
from aeos.models.research import EvalRun, EvalSuite
from aeos.models.routing import ModelRoute

LIMITS = {"max_attempts": 2, "max_minutes": 30, "max_cost_usd": 0}


def cfg(model="deepseek-v4.1-flash", route="opencode-zen/deepseek-flash-free", **kw):
    base = {"model_id": model, "model_version": "UNKNOWN", "provider": "opencode-zen", "provider_route": route,
            "harness": "opencode", "harness_version": "1.4.0", "prompt_revision": "p1", "tools": ["edit", "bash"],
            "policy_version": "pol-1", "context_params": {"max_tokens": 64000}}
    return base | kw


def _seed(db):
    out = service.seed_suites(db)
    db.commit()
    return out


def _samples(accepted: int, total: int, **extra):
    rows = []
    for i in range(total):
        rows.append({"sample_id": f"s{i}", "case_id": "bugfix-normal-1",
                     "status": "ACCEPTED" if i < accepted else "REJECTED", "first_pass": i < accepted,
                     "latency_s": 60 + i, "worker_cost_usd": 0.0, "compute_ci_cost_usd": 0.0} | extra)
    return rows


def _run(db, config, *, accepted, total, gates=None, suite="eval.small-bugfix", samples=None, topology=None):
    r = service.plan_run(db, suite_key=suite, configuration=config, role_key=None, topology=topology, repetitions=3,
                         limits=LIMITS, actor_user_id="owner")
    db.commit()
    facade.record_result(db, r.id, samples=samples or _samples(accepted, total),
                         gate_results=gates or {"security": "PASS", "authority": "PASS", "evidence": "PASS"},
                         result_source="verifier")
    db.commit()
    return r


def test_seed_creates_protected_held_out_suites_without_results(db):
    out = _seed(db)
    assert out["created"] == 8 and out["results_seeded"] == 0
    classes = {s.task_class for s in db.scalars(select(EvalSuite))}
    assert classes == {"small-bugfix", "feature-fe-api", "shared-schema-auth-change", "independent-readonly-reviews",
                       "docs-only-change", "provider-quota-interruption", "self-build-controller-change",
                       "crash-recovery"}
    for s in db.scalars(select(EvalSuite)):
        assert s.held_out and s.protected and s.mode == "DESIGN" and s.content_hash
        assert s.cases and all(c["acceptance"] for c in s.cases) and s.acceptance
    assert db.scalar(select(func.count()).select_from(EvalRun)) == 0
    assert all(v["integrity"] == "OK" for v in service.list_suites(db))
    again = _seed(db)
    assert again["created"] == 0 and again["unchanged"] == 8


def test_seed_never_overwrites_a_changed_protected_suite(db):
    _seed(db)
    s = db.scalar(select(EvalSuite).where(EvalSuite.suite_key == "eval.small-bugfix"))
    s.content_hash = "sha256:tampered"
    db.commit()
    out = _seed(db)
    assert out["conflicts"] and out["conflicts"][0]["suite_key"] == "eval.small-bugfix"
    assert db.get(EvalSuite, s.id).content_hash == "sha256:tampered"
    assert next(v for v in service.list_suites(db) if v["suite_key"] == "eval.small-bugfix")["integrity"] == "HASH_MISMATCH"


def test_experiments_share_identical_gates():
    assert [e["key"] for e in D.EXPERIMENTS] == ["A", "B", "C", "D"]
    assert len({e["gates_hash"] for e in D.EXPERIMENTS}) == 1
    assert all(e["fixed_configuration_required"] for e in D.EXPERIMENTS)
    assert D.EXPERIMENTS[3]["status"] == "EXPERIMENT_ONLY"
    assert "jumlah agent" in D.NOT_MEASURED


def test_plan_run_requires_full_configuration_and_limits(db):
    _seed(db)
    bad = cfg()
    bad.pop("harness_version")
    with pytest.raises(ValidationFailed) as e:
        service.plan_run(db, suite_key="eval.small-bugfix", configuration=bad, role_key=None, topology=None,
                         repetitions=1, limits=LIMITS, actor_user_id="o")
    assert "harness_version" in e.value.details["missing"]
    with pytest.raises(ValidationFailed):
        service.plan_run(db, suite_key="eval.small-bugfix", configuration=cfg(), role_key=None, topology=None,
                         repetitions=1, limits={"max_attempts": 1}, actor_user_id="o")
    with pytest.raises(ValidationFailed):
        service.plan_run(db, suite_key="eval.small-bugfix", configuration=cfg(), role_key=None, topology="Z",
                         repetitions=1, limits=LIMITS, actor_user_id="o")
    with pytest.raises(NotFound):
        service.plan_run(db, suite_key="nope", configuration=cfg(), role_key=None, topology=None, repetitions=1,
                         limits=LIMITS, actor_user_id="o")
    r = service.plan_run(db, suite_key="eval.small-bugfix", configuration=cfg(), role_key=None, topology="A",
                         repetitions=3, limits=LIMITS, actor_user_id="o")
    db.commit()
    assert r.status == "PLANNED" and r.role_key == "feature-implementer" and r.sample_size == 0
    swapped = cfg(tools=["bash", "edit"])
    r2 = service.plan_run(db, suite_key="eval.small-bugfix", configuration=swapped, role_key=None, topology=None,
                          repetitions=1, limits=LIMITS, actor_user_id="o")
    assert r2.configuration_hash == r.configuration_hash  # tool order is not a different configuration


def test_results_only_from_trusted_collector_and_never_overwritten(db):
    _seed(db)
    r = service.plan_run(db, suite_key="eval.small-bugfix", configuration=cfg(), role_key=None, topology=None,
                         repetitions=1, limits=LIMITS, actor_user_id="o")
    db.commit()
    with pytest.raises(PolicyDenied):
        facade.record_result(db, r.id, samples=_samples(1, 1), gate_results={}, result_source="worker")
    facade.record_result(db, r.id, samples=_samples(1, 1), gate_results={}, result_source="verifier")
    db.commit()
    with pytest.raises(Conflict):
        facade.record_result(db, r.id, samples=_samples(1, 1), gate_results={}, result_source="verifier")
    with pytest.raises(ValidationFailed):
        r3 = service.plan_run(db, suite_key="eval.small-bugfix", configuration=cfg(), role_key=None, topology=None,
                              repetitions=1, limits=LIMITS, actor_user_id="o")
        facade.record_result(db, r3.id, samples=[{"status": "BOGUS"}], gate_results={}, result_source="verifier")


def test_leaderboard_empty_every_candidate_belum_diuji(db):
    _seed(db)
    db.add(ModelRoute(route_key="free-glm", pool="WORKER", provider="opencode-zen", model_id="glm-5.3-flash",
                      display_name="GLM-5.3 Flash Free"))
    db.commit()
    board = service.leaderboard(db, role="feature-implementer")
    assert board["status"] == "BELUM_DIUJI"
    assert board["message"] == "Semua kandidat BELUM DIUJI dalam platform ini"
    assert all(s["ranking"] == [] and s["status"] == "BELUM_DIUJI" for s in board["suites"])
    assert board["candidates_not_tested"] and all(c["status"] == "BELUM_DIUJI" for c in board["candidates_not_tested"])
    assert board["combined"] is None and "bobot" in board["combined_note"]
    assert board["route_change"].startswith("NONE")


def test_gate_failure_is_excluded_not_outscored(db):
    _seed(db)
    _run(db, cfg(model="fast-cheap", route="r-fast"), accepted=10, total=10,
         gates={"security": "FAIL", "authority": "PASS", "evidence": "PASS"})
    _run(db, cfg(model="unproven", route="r-unproven"), accepted=9, total=10,
         gates={"security": "PASS", "authority": "PASS"})  # evidence gate NOT_RUN
    _run(db, cfg(model="steady", route="r-steady"), accepted=6, total=10)
    suite = next(s for s in service.leaderboard(db, role="feature-implementer")["suites"]
                 if s["suite_key"] == "eval.small-bugfix")
    assert [e["configuration"]["model_id"] for e in suite["ranking"]] == ["steady"]
    excluded = {e["configuration"]["model_id"]: e["eligibility"]["status"] for e in suite["excluded"]}
    assert excluded == {"fast-cheap": "EXCLUDED_GATE_FAILED", "unproven": "EXCLUDED_GATE_NOT_PROVEN"}


def test_ties_sample_size_and_variance_reported(db):
    _seed(db)
    _run(db, cfg(model="a", route="ra"), accepted=6, total=10)
    _run(db, cfg(model="b", route="rb"), accepted=7, total=10)
    _run(db, cfg(model="c", route="rc"), accepted=50, total=50)  # Wilson lower bound 0.93 > b's upper 0.89
    suite = next(s for s in service.leaderboard(db, role="feature-implementer")["suites"]
                 if s["suite_key"] == "eval.small-bugfix")
    by = {e["configuration"]["model_id"]: e for e in suite["ranking"]}
    assert by["c"]["rank"] == 1 and not by["c"]["tie"]
    assert by["a"]["rank"] == by["b"]["rank"] == 2 and by["a"]["tie"] and by["b"]["tie"]
    assert by["a"]["sample_size"] == 10 and by["a"]["small_sample"] is False
    assert by["a"]["acceptance_variance"] == pytest.approx(0.24)
    assert by["a"]["acceptance_ci95"][0] < 0.6 < by["a"]["acceptance_ci95"][1]


def test_failures_timeouts_quota_interruptions_never_dropped(db):
    _seed(db)
    samples = _samples(2, 2) + [{"status": "TIMEOUT"}, {"status": "QUOTA_INTERRUPTED", "quota_delay_s": 120},
                                {"status": "FAILED", "worker_cost_usd": None}]
    r = _run(db, cfg(), accepted=0, total=0, samples=samples)
    run = db.get(EvalRun, r.id)
    assert run.sample_size == 5 and len(run.failures) == 3
    m = run.metrics
    assert m["outcome"]["final_acceptance_rate"] == pytest.approx(0.4)
    assert m["reliability"]["quota_interrupted"] == 1 and m["reliability"]["timeouts"] == 1
    assert m["economics"]["worker_cost_status"] == "UNKNOWN"
    assert m["economics"]["worker_cost_per_accepted_usd"] is None
    assert set(m) >= {"outcome", "contract", "reliability", "product_quality", "oversight", "economics", "performance"}
    assert m["performance"]["latency_p50_s"] is not None and m["performance"]["latency_samples"] == 2


def test_same_model_on_different_routes_not_merged(db):
    _seed(db)
    _run(db, cfg(model="deepseek-flash", route="opencode-zen/free"), accepted=5, total=10)
    _run(db, cfg(model="deepseek-flash", route="openrouter/free"), accepted=8, total=10)
    suite = next(s for s in service.leaderboard(db)["suites"] if s["suite_key"] == "eval.small-bugfix")
    assert len(suite["ranking"]) == 2
    assert {e["configuration"]["provider_route"] for e in suite["ranking"]} == {"opencode-zen/free", "openrouter/free"}


def test_repeated_runs_of_same_configuration_pool_samples(db):
    _seed(db)
    _run(db, cfg(), accepted=3, total=5)
    _run(db, cfg(), accepted=4, total=5)
    suite = next(s for s in service.leaderboard(db)["suites"] if s["suite_key"] == "eval.small-bugfix")
    assert len(suite["ranking"]) == 1 and suite["ranking"][0]["sample_size"] == 10 and suite["ranking"][0]["runs"] == 2
    assert suite["ranking"][0]["small_sample"] is False


def test_results_never_change_routes(db):
    _seed(db)
    db.add(ModelRoute(route_key="r-steady", pool="WORKER", provider="x", model_id="steady", status="CANDIDATE"))
    db.commit()
    _run(db, cfg(model="steady", route="r-steady"), accepted=10, total=10)
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "r-steady"))
    assert route.status == "CANDIDATE" and route.eval_results == {}
    snap = service.scorecard_snapshot(db, role="feature-implementer")
    assert snap["scorecard_hash"].startswith("sha256:") and snap["status"] == "HAS_RESULTS"
