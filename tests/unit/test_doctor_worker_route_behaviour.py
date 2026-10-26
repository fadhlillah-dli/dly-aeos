"""W6-021 §08 Worker "model tool/schema behaviour": the Doctor reads a real worker route's behaviour from trusted
records only - the §43 reliability measure ``tool_schema_failures`` of a COMPLETED evaluation by a trusted collector
on exactly the route's current configuration, plus independently verified runs. UNKNOWN until such runs exist,
STALE when the route configuration or the engine changed, FAILED only above the OWNER's threshold (POLICY
decision); without that decision a measured failure is reported, never judged."""

from __future__ import annotations

import pytest

from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.runner import make_context, run_check
from aeos.models.audit import Event
from aeos.models.decisions import Decision
from aeos.models.research import EvalRun
from aeos.models.workforce import WorkerRun
from tests.unit.test_inventory_fakes import base_runner
from tests.unit.test_workforce_support import make_route

CID = "worker.route_behaviour"


@pytest.fixture(autouse=True)
def engine(monkeypatch):
    from aeos.worker_runtime import opencode

    state = {"status": "INSTALLED", "version": "1.19.3", "binary": "opencode"}
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, refresh=False: dict(state))
    return state


def _check(settings, db):
    load_all()
    return run_check(REGISTRY[CID], make_context(settings, db, runner=base_runner()))


def _eval(db, route, *, failures, n=4, config_hash=None, source="collector", mode="LIVE", harness="1.19.3",
          domain=None, status="COMPLETED"):
    e = EvalRun(suite_key="worker-implementation", configuration={"harness_version": harness},
                configuration_hash="sha256:cfg", status=status, sample_size=n, route_key=route.route_key,
                route_config_hash=config_hash or route.config_hash, result_source=source, mode=mode,
                submitted_domain=domain, metrics={"reliability": {"tool_schema_failures": failures}})
    db.add(e)
    db.commit()
    return e


def _threshold(db, rate):
    db.add(Decision(kind="POLICY", actor_user_id="owner", actor_kind="HUMAN", target_type="worker_route_behaviour",
                    target_id="tool_schema_failures", domain="PERSONAL", rationale="owner threshold for routes",
                    details={"max_failure_rate": rate}))
    db.commit()


def test_without_worker_routes_or_runs_the_behaviour_is_not_proven(settings, db):
    assert _check(settings, db).status == "UNKNOWN" and _check(settings, db).error_code == "NO_WORKER_ROUTE"
    route = make_route(db, key="worker.a")
    db.commit()
    res = _check(settings, db)
    assert res.status == "UNKNOWN" and res.error_code == "ROUTE_BEHAVIOUR_NOT_PROVEN"
    assert res.observed_evidence["routes"][0]["route_key"] == route.route_key
    # MOCK, untrusted and OFFICE-submitted evaluations never prove the platform route
    _eval(db, route, failures=0, mode="MOCK")
    _eval(db, route, failures=0, source="worker")
    _eval(db, route, failures=0, domain="OFFICE")
    _eval(db, route, failures=0, status="RUNNING")
    assert _check(settings, db).status == "UNKNOWN"


def test_zero_failures_on_the_current_configuration_is_verified(settings, db):
    route = make_route(db, key="worker.a")
    db.commit()
    ev = _eval(db, route, failures=0, n=6)
    res = _check(settings, db)
    assert res.status == "VERIFIED", res.summary
    v = res.observed_evidence["routes"][0]
    assert v["eval_run_id"] == ev.id and v["tool_schema_failures"] == 0 and v["sample_size"] == 6


def test_evidence_for_another_route_configuration_or_engine_is_stale(settings, db, engine):
    route = make_route(db, key="worker.a")
    db.commit()
    _eval(db, route, failures=0, config_hash="sha256:older-configuration")
    res = _check(settings, db)
    assert res.status == "STALE" and res.error_code == "ROUTE_BEHAVIOUR_STALE"
    _eval(db, route, failures=0)
    assert _check(settings, db).status == "VERIFIED"
    engine["version"] = "1.20.0"  # the engine was upgraded after the evaluation
    res = _check(settings, db)
    assert res.status == "STALE" and "1.20.0" in res.summary


def test_measured_failures_are_reported_until_the_owner_sets_a_threshold(settings, db):
    route = make_route(db, key="worker.a")
    db.commit()
    _eval(db, route, failures=2, n=4)
    res = _check(settings, db)
    assert res.status == "UNKNOWN" and res.error_code == "ROUTE_BEHAVIOUR_THRESHOLD_UNDECIDED"
    assert res.observed_evidence["threshold"]["status"] == "UNDECIDED"
    assert res.observed_evidence["routes"][0]["failure_rate"] == 0.5
    _threshold(db, 0.25)
    res = _check(settings, db)
    assert res.status == "FAILED" and res.error_code == "ROUTE_TOOL_SCHEMA_FAILURES"
    assert res.observed_evidence["threshold"]["max_failure_rate"] == 0.25


def test_failures_within_the_owner_threshold_pass(settings, db):
    route = make_route(db, key="worker.a")
    db.commit()
    _eval(db, route, failures=1, n=10)
    _threshold(db, 0.2)
    assert _check(settings, db).status == "VERIFIED"


def test_unrecorded_measure_is_unknown_not_zero(settings, db):
    route = make_route(db, key="worker.a")
    db.commit()
    _eval(db, route, failures=None)
    res = _check(settings, db)
    assert res.status == "UNKNOWN" and "tidak mencatat" in res.summary


def test_verified_runs_and_runner_observed_tool_errors_are_surfaced(settings, db):
    route = make_route(db, key="worker.a")
    run = WorkerRun(project_id="p", domain="PERSONAL", model_route_key=route.route_key, verifier_status="PASS",
                    engine_version="1.19.3")
    other = WorkerRun(project_id="p", domain="OFFICE", model_route_key=route.route_key, verifier_status="PASS")
    db.add_all([run, other])
    db.flush()
    db.add(Event(source="runner", kind="run.action", run_id=run.id, payload={"kind": "tool_error"}))
    db.add(Event(source="worker", kind="run.action", run_id=run.id, payload={"kind": "tool_error"}))  # not trusted
    db.commit()
    v = _check(settings, db).observed_evidence["routes"][0]
    assert v["verified_runs"] == 1 and v["tool_errors"] == 1
