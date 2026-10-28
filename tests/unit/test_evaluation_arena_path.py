"""§49 M6 "Arena model" (W6-141/W6-142): ARENA routes could only be feasibility-probed, and an ARENA EvalRun could be
planned and started although no task on it could ever execute (the broker serves the WORKER pool only).

Controlled evaluation path (wave 7): a TaskSpec bound to a slot of a RUNNING EvalRun runs on exactly the evaluated
route (never ranked, never a fallback). An eval-only (ARENA) route serves nothing else: routing admits it only for
such a task, the workforce re-checks it, the broker issues an eval-scoped MODEL_PROXY grant and re-checks the scope on
every request with the feasibility probe's rules (PERSONAL, TRIAL/APPROVED on a hash-bound decision, free,
terms-eligible). An evaluation whose route has no controlled execution path is refused at start with its reason."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeos.core.errors import Blocked, Conflict, Forbidden
from aeos.core.timeutil import utcnow
from aeos.evaluation import executor, service
from aeos.models.decisions import Decision
from aeos.models.routing import CapabilityGrant, ModelRoute
from aeos.models.workforce import WorkerRun
from aeos.routing import service as routing_service
from tests.unit.test_workforce_support import make_plan, make_project

ARENA = "arena.free-x"
LIMITS = {"max_attempts": 1, "max_minutes": 30, "max_cost_usd": 0}
UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
KEY = "sk-or-v1-" + "a" * 48


def _route(db, key=ARENA, *, pool="ARENA", status="TRIAL", is_free="YES", classes=("PUBLIC", "INTERNAL"),
           suffix=":free"):
    rt = routing_service.runtime_context()
    r = ModelRoute(route_key=key, pool=pool, provider="openrouter", model_id=f"vendor/{key}{suffix}", status=status,
                   eligible_domains=["PERSONAL"], is_free=is_free, allowed_data_classes=list(classes),
                   supports_tools="YES", availability="AVAILABLE", identity_status="VERIFIED", mode_params={},
                   data_terms={"may_use_inputs_for_training": False, "trial_only_no_confidential": False},
                   pricing_checked_at=utcnow(), terms_checked_at=utcnow(), policy_version=rt["policy_version"],
                   prompt_revision=rt.get("prompt_revision"), context_strategy=rt.get("context_strategy"),
                   harness="opencode", harness_version=rt.get("harness_version"))
    db.add(r)
    db.flush()
    r.config_hash = routing_service.compute_config_hash(r)
    if status in ("TRIAL", "APPROVED"):
        db.add(Decision(kind="TRIAL" if status == "TRIAL" else "ROUTE_ACTIVATION", actor_user_id="owner",
                        actor_kind="HUMAN", target_type="model_route", target_id=key, target_hash=r.config_hash,
                        domain="PERSONAL", rationale="arena trial reviewed", allowed_operations=["route.use"],
                        expires_at=utcnow() + timedelta(days=7)))
    db.flush()
    return r


def _cfg(route: ModelRoute) -> dict:
    return {"model_id": route.model_id, "model_version": "UNKNOWN", "provider": route.provider,
            "provider_route": route.route_key, "harness": "opencode", "harness_version": "UNKNOWN",
            "prompt_revision": "UNKNOWN", "tools": ["fs.read"], "policy_version": "UNKNOWN", "context_params": {}}


def _plan(db, route, **kw):
    return service.plan_run(db, suite_key="eval.small-bugfix", configuration=_cfg(route), role_key=None,
                            topology=None, repetitions=1, limits=LIMITS, actor_user_id="owner", **kw)


@pytest.fixture
def arena(db, worker_keys):
    service.seed_suites(db)
    route = _route(db)
    db.commit()
    return route


def _bound_task(db, run, *, pool=None):
    project = make_project(db)
    if pool:  # TaskSpecs are append-only: the route policy is part of the spec when it is created
        from aeos.core.hashing import content_hash
        from aeos.models.planning import TaskSpec

        _, pv, _ = make_plan(db, project, [])
        specs = [TaskSpec(plan_version_id=pv.id, plan_version_hash=pv.manifest_hash, project_id=project.id,
                          domain=project.domain, task_key=k, title=k, goal="g", allowed_paths=[f"src/{k}/**"],
                          model_route_policy={"pool": pool}, spec_hash=content_hash({"k": k, "pool": pool}))
                 for k in ("L1", "L2")]
        db.add_all(specs)
        db.flush()
    else:
        _, _, specs = make_plan(db, project, [{"task_key": "A1"}, {"task_key": "A2"}])
    executor.bind_task(db, run.id, slot_id=executor.slots(db, run)[0]["slot_id"], task_id=specs[0].id,
                       actor_user_id="owner")
    return specs[0], specs[1]


def test_an_evaluation_without_a_controlled_execution_path_is_refused_at_start(arena, db):
    paid = _route(db, "arena.paid", is_free="NO")
    comparator = _route(db, "comparator.x", pool="COMPARATOR")
    local = _route(db, "local.x", pool="LOCAL")
    candidate = _route(db, "arena.candidate", status="CANDIDATE")
    for route, why in ((paid, "not free"), (comparator, "no controlled execution path"),
                       (local, "no controlled execution path"), (candidate, "TRIAL/APPROVED")):
        run = _plan(db, route)
        assert service.run_view(run, db)["execution"]["serving"]["path"] in ("EVAL_ONLY", "NONE")
        with pytest.raises(Conflict) as exc:
            executor.start_run(db, run.id, actor_user_id="owner")
        assert exc.value.code == "EVAL_ROUTE_NOT_SERVED" and why in exc.value.message, route.route_key
        assert run.status == "PLANNED"
    office = _plan(db, arena, domain="OFFICE")
    with pytest.raises(Conflict) as exc:
        executor.start_run(db, office.id, actor_user_id="owner")
    assert "OFFICE stays default deny" in exc.value.message
    public_only = _route(db, "arena.public", classes=("PUBLIC",))
    with pytest.raises(Conflict) as exc:  # the held-out suite is an INTERNAL workload: never sent to it
        executor.start_run(db, _plan(db, public_only).id, actor_user_id="owner")
    assert "INTERNAL not allowed" in exc.value.message
    ok = _plan(db, arena)
    serving = service.run_view(ok, db)["execution"]["serving"]
    assert serving["path"] == "EVAL_ONLY" and serving["reasons"] == []
    executor.start_run(db, ok.id, actor_user_id="owner")
    assert ok.status == "RUNNING"


def test_only_a_task_bound_to_the_running_evaluation_is_routed_to_the_arena_route(arena, db):
    from aeos.routing import facade as routing
    from aeos.workforce import integrations as ext
    from aeos.workforce.scheduler import agent_configuration_problems

    run = _plan(db, arena)
    executor.start_run(db, run.id, actor_user_id="owner")
    bound, unbound = _bound_task(db, run)
    decision = routing.select_route(db, task=bound)
    assert decision.route is not None and decision.route.route_key == ARENA
    assert decision.eval_run_id == run.id and decision.eligible == [ARENA] and "no fallback" in decision.reason
    route, reasons = ext.select_route(db, bound)  # defence in depth re-checks the evaluation binding itself
    assert route is not None and route.route_key == ARENA and reasons == []
    agent = type("Agent", (), {"agent_key": "opencode-implementer", "tools": ["read", "edit", "write", "list",
                                                                            "glob", "grep", "patch"],
                               "route_policy": {"pool": "WORKER"}, "limits": {}})()
    assert not [c for c, _ in agent_configuration_problems(agent, bound, route, eval_route=True)
                if c == "AGENT_ROUTE_POLICY"]
    assert [c for c, _ in agent_configuration_problems(agent, bound, route) if c == "AGENT_ROUTE_POLICY"]
    with pytest.raises(Blocked) as exc:  # an ordinary task never reaches the ARENA pool
        routing.select_route(db, task=unbound)
    assert exc.value.code == "NO_ELIGIBLE_ROUTE"
    with pytest.raises(Blocked) as exc:  # the evaluated configuration is never left for a fallback
        routing.fallback_route(db, task=bound, failed_route_key=ARENA)
    assert exc.value.code == "EVAL_NO_FALLBACK"
    # the route's decision no longer matches (e.g. the catalog shows a price): refused, never run elsewhere
    arena.is_free = "NO"
    db.flush()
    with pytest.raises(Blocked) as exc:
        routing.select_route(db, task=bound)
    assert exc.value.code == "EVAL_ROUTE_NOT_SERVED"
    arena.is_free = "YES"
    db.flush()
    assert routing.select_route(db, task=bound).eval_run_id == run.id
    executor.finish_run(db, run.id, actor_user_id="owner", reason="arena evaluation stopped")
    with pytest.raises(Blocked):  # no longer RUNNING: the binding serves nothing
        routing.select_route(db, task=bound)


def test_a_bound_task_runs_on_the_evaluated_worker_route_not_the_top_ranked_one(arena, db):
    """W6-142: with several eligible WORKER routes, ranking picked the top one, so a bound task of an evaluation of
    another route always produced an INVALID sample ("ran on other routes")."""
    from aeos.routing import facade as routing

    # not ':free' models: an OpenRouter ':free' WORKER route serves PUBLIC tasks only (decision C); these tasks are INTERNAL
    _route(db, "worker.a", pool="WORKER", suffix="")
    second = _route(db, "worker.b", pool="WORKER", suffix="")
    run = _plan(db, second)
    assert service.run_view(run, db)["execution"]["serving"]["path"] == "BROKER"
    executor.start_run(db, run.id, actor_user_id="owner")
    bound, unbound = _bound_task(db, run)
    assert routing.select_route(db, task=unbound).route.route_key == "worker.a"  # ordinary ranking unchanged
    decision = routing.select_route(db, task=bound)
    assert decision.route.route_key == "worker.b" and decision.eval_run_id == run.id


def test_a_local_only_task_never_leaves_for_a_remote_arena_route(arena, db):
    from aeos.routing import facade as routing

    run = _plan(db, arena)
    executor.start_run(db, run.id, actor_user_id="owner")
    bound, _ = _bound_task(db, run, pool="LOCAL")
    out = executor.eval_serving(db, task=bound)
    assert out["status"] == "REFUSED" and any("WORKER-pool evaluation tasks only" in r for r in out["reasons"])
    with pytest.raises(Blocked) as exc:
        routing.select_route(db, task=bound)
    assert exc.value.code == "EVAL_ROUTE_NOT_SERVED"


def test_the_broker_issues_an_eval_scoped_grant_only_for_a_bound_task(arena, db):
    from aeos.broker import facade as broker

    run = _plan(db, arena)
    executor.start_run(db, run.id, actor_user_id="owner")
    bound, unbound = _bound_task(db, run)
    ok_run = WorkerRun(task_id=bound.id, project_id=bound.project_id, domain="PERSONAL", execution_status="RUNNING",
                       model_route_key=ARENA)
    other = WorkerRun(task_id=unbound.id, project_id=unbound.project_id, domain="PERSONAL",
                      execution_status="RUNNING", model_route_key=ARENA)
    db.add_all([ok_run, other])
    db.flush()
    _, grant = broker.issue_grant(db, run_id=ok_run.id, project_id=bound.project_id, domain="PERSONAL",
                                  kind="MODEL_PROXY", ttl_seconds=600, max_requests=3,
                                  scope={"models": {"arena-x": ARENA},
                                         "eval": {"eval_run_id": "forged", "route_key": "x"}})
    assert grant.scope["eval"] == {"eval_run_id": run.id, "slot_id": executor.slots(db, run)[0]["slot_id"],
                                   "route_key": ARENA, "task_id": bound.id}
    with pytest.raises(Forbidden) as exc:
        broker.issue_grant(db, run_id=other.id, project_id=unbound.project_id, domain="PERSONAL", kind="MODEL_PROXY",
                           ttl_seconds=600, scope={"models": {"arena-x": ARENA}})
    assert exc.value.code == "EVAL_SCOPE_REQUIRED"
    assert db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == other.id)) is None


@pytest.fixture
def arena_proxy(settings, db, secrets_memory):
    from aeos.broker import facade as broker
    from tests.conftest import make_app

    secrets_memory.put("workers/openrouter", KEY, actor="test")
    service.seed_suites(db)
    route = _route(db)
    run = _plan(db, route)
    executor.start_run(db, run.id, actor_user_id="owner")
    bound, _ = _bound_task(db, run)
    wr = WorkerRun(task_id=bound.id, project_id=bound.project_id, domain="PERSONAL", execution_status="RUNNING",
                   model_route_key=ARENA)
    db.add(wr)
    db.flush()
    raw, grant = broker.issue_grant(db, run_id=wr.id, project_id=bound.project_id, domain="PERSONAL",
                                    kind="MODEL_PROXY", scope={"models": {"arena-x": ARENA}}, ttl_seconds=600,
                                    max_requests=10)
    db.commit()
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        yield {"client": c, "token": raw, "run": run, "route": route}


def _call(env):
    return env["client"].post("/broker/v1/chat/completions",
                              json={"model": "arena-x", "messages": [{"role": "user", "content": "x"}]},
                              headers={"Authorization": f"Bearer {env['token']}"})


def _ok(model):
    return {"id": "x", "object": "chat.completion", "model": model, "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}


def test_the_proxy_serves_the_arena_route_only_while_the_evaluation_scope_holds(arena_proxy, db):
    env = arena_proxy
    with respx.mock(assert_all_mocked=True) as mock:
        upstream = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok(env["route"].model_id)))
        r = _call(env)
        assert r.status_code == 200, r.text
        assert upstream.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
        route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ARENA))
        route.is_free, route.pricing = "NO", {"prompt": "0.000001"}  # no budget path for an eval-only route
        db.commit()
        r = _call(env)
        assert r.status_code == 403 and r.json()["error"]["code"] == "EVAL_SCOPE_INVALID", r.text
        route.is_free, route.pricing = "YES", {}
        db.commit()
        assert _call(env).status_code == 200
        executor.finish_run(db, env["run"].id, actor_user_id="owner", reason="arena evaluation stopped")
        db.commit()
        r = _call(env)  # the evaluation is no longer RUNNING: the next request is refused
        assert r.status_code == 403 and r.json()["error"]["code"] == "EVAL_SCOPE_INVALID"
        assert len(upstream.calls) == 2, "nothing was forwarded once the evaluation scope stopped holding"
