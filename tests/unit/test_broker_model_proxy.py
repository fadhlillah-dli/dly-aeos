"""OpenAI-compatible model proxy (§25, §26, §42, §50 'Free quota habis / 429', 'PR code mencoba
membaca production secret')."""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeos.api.routers import MODULE_ROUTERS
from aeos.broker import facade as broker
from aeos.core.timeutil import utcnow
from aeos.models.broker_ext import BrokerRequest
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun
from tests.conftest import ADMIN_PW, create_admin, make_app
from tests.unit.test_workforce_support import FakeModules, make_project, make_route

# The real routers (and the services they import at module level) load before FakeModules replaces
# implementation modules in sys.modules, so this file also runs on its own (not only after other tests).
for _mod in MODULE_ROUTERS:
    importlib.import_module(_mod)

UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
KEY = "sk-or-v1-" + "k" * 48


@pytest.fixture
def env(settings, db, secrets_memory, monkeypatch):
    fk = FakeModules().install(monkeypatch)
    secrets_memory.put("workers/openrouter", KEY, actor="test")
    p = make_project(db)
    make_route(db, key="worker.free-a", model_id="vendor/model-a:free")
    make_route(db, key="worker.free-b", model_id="vendor/model-b:free")
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="RUNNING", model_route_key="worker.free-a")
    db.add(run)
    db.flush()
    raw, grant = broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="MODEL_PROXY",
                                    scope={"models": {"model-a": "worker.free-a"}, "per_request_output_cap": 256},
                                    ttl_seconds=600, max_requests=5, max_tokens=10_000, max_cost_usd=0.0)
    db.commit()
    app = make_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield {"client": c, "token": raw, "grant": grant, "run": run, "db": db, "fakes": fk}


def _post(env, body=None, token=None, **kw):
    body = body or {"model": "model-a", "messages": [{"role": "user", "content": "hi"}]}
    return env["client"].post("/broker/v1/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {token or env['token']}"}, **kw)


def _ok_json(model="vendor/model-a:free", usage=None):
    return {"id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 5}}


def test_rejects_missing_and_unknown_and_cookie_auth(env, db):
    c = env["client"]
    r = c.post("/broker/v1/chat/completions", json={"model": "model-a", "messages": []})
    assert r.status_code == 401 and r.json()["error"]["code"] == "CAPABILITY_TOKEN_MISSING"
    assert _post(env, token="aeoscap_nope").status_code == 401
    create_admin(db)
    login = c.post("/api/auth/login", json={"username": "owner", "password": ADMIN_PW})
    assert login.status_code == 200
    r = _post(env)  # session cookie now attached by the client
    assert r.status_code == 401 and r.json()["error"]["code"] == "COOKIE_AUTH_REJECTED"


def test_expired_and_revoked_tokens_rejected(env, db):
    g = db.get(CapabilityGrant, env["grant"].id)
    g.expires_at = utcnow()
    db.commit()
    r = _post(env)
    assert r.status_code == 401 and r.json()["error"]["code"] == "CAPABILITY_TOKEN_EXPIRED"


def test_revoked_token_rejected(env, db):
    broker.revoke_grants(db, run_id=env["run"].id, reason="cancel")
    db.commit()
    r = _post(env)
    assert r.status_code == 401 and r.json()["error"]["code"] == "CAPABILITY_TOKEN_REVOKED"


def test_unknown_model_rejected_and_never_forwarded(env):
    with respx.mock(assert_all_mocked=True) as mock:
        r = _post(env, body={"model": "model-b", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 404 and r.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
        assert len(mock.calls) == 0


def test_forwards_exact_route_model_strips_fallback_fields_records_usage(env, db):
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json()))
        r = _post(env, body={"model": "model-a", "messages": [{"role": "user", "content": "x"}], "max_tokens": 99999,
                             "models": ["openai/gpt-5"], "route": "fallback", "provider": {"order": ["x"]}, "n": 4})
        assert r.status_code == 200, r.text
        sent = json.loads(route.calls[0].request.content)
        assert sent["model"] == "vendor/model-a:free"
        assert "models" not in sent and "route" not in sent and "provider" not in sent and sent["n"] == 1
        assert sent["max_tokens"] == 256  # clamped to the per-request cap
        assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in r.text and r.json()["model"] == "model-a"
    db.expire_all()
    g = db.get(CapabilityGrant, env["grant"].id)
    assert g.used_requests == 1 and g.used_tokens == 15
    ledger = db.scalar(select(UsageLedgerEntry))
    assert ledger.input_tokens == 10 and ledger.output_tokens == 5 and ledger.cost_usd is None  # UNKNOWN
    assert ledger.source == "broker" and ledger.category == "WORKER_API"
    req = db.scalar(select(BrokerRequest).where(BrokerRequest.outcome == "OK"))
    assert req.model_resolved == "vendor/model-a:free"
    run = db.get(WorkerRun, env["run"].id)
    assert run.usage_summary["cost_unknown_requests"] == 1


def test_reported_cost_recorded(env, db):
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json(
            usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0})))
        assert _post(env).status_code == 200
    db.expire_all()
    assert float(db.scalar(select(UsageLedgerEntry)).cost_usd) == 0.0


def test_upstream_429_returns_429_records_quota_and_never_falls_back(env, db):
    """§50: free quota exhausted -> backoff/queue, NOT a paid or other-model fallback."""
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(UPSTREAM).mock(return_value=httpx.Response(
            429, headers={"retry-after": "42"}, json={"error": {"message": f"rate limited for key {KEY}"}}))
        r = _post(env)
        assert r.status_code == 429 and r.headers["retry-after"] == "42"
        assert r.json()["error"]["code"] == "UPSTREAM_RATE_LIMITED"
        assert route.call_count == 1 and len(mock.calls) == 1  # no second upstream / model tried
    assert KEY not in r.text
    db.expire_all()
    q = db.scalar(select(QuotaState).where(QuotaState.scope == "worker.free-a"))
    assert q.state == "EXHAUSTED" and q.source == "observed-429"
    run = db.get(WorkerRun, env["run"].id)
    assert run.quota_status == "RATE_LIMITED" and run.current_action.startswith("WAITING_QUOTA")
    assert db.scalar(select(BrokerRequest).where(BrokerRequest.outcome == "RATE_LIMITED")) is not None
    assert db.scalar(select(UsageLedgerEntry)) is None


def test_request_and_token_caps(env, db):
    g = db.get(CapabilityGrant, env["grant"].id)
    g.max_requests = 1
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json()))
        assert _post(env).status_code == 200
        r = _post(env)
        assert r.status_code == 403 and r.json()["error"]["code"] == "CAP_EXCEEDED"
        assert len(mock.calls) == 1
    g = db.get(CapabilityGrant, env["grant"].id)
    db.refresh(g)
    g.max_requests = 100
    g.used_tokens = g.max_tokens
    db.commit()
    r = _post(env)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CAP_EXCEEDED"


def test_cost_cap_on_free_route(env, db):
    g = db.get(CapabilityGrant, env["grant"].id)
    g.used_cost_usd = 0.01  # a "free" route started charging -> stop
    db.commit()
    r = _post(env)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CAP_EXCEEDED"


def test_stream_passthrough_and_usage(env, db):
    chunks = [
        {"id": "c", "choices": [{"index": 0, "delta": {"content": "he"}}], "model": "vendor/model-a:free"},
        {"id": "c", "choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]},
        {"id": "c", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(UPSTREAM).mock(return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()))
        r = _post(env, body={"model": "model-a", "stream": True, "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200
        assert r.text == body
        sent = json.loads(route.calls[0].request.content)
        assert sent["stream"] is True and sent["stream_options"]["include_usage"] is True
    db.expire_all()
    e = db.scalar(select(UsageLedgerEntry))
    assert e.input_tokens == 7 and e.output_tokens == 3


def test_upstream_errors_redacted(env):
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(400, json={"error": f"bad request, key={KEY}"}))
        r = _post(env)
        assert r.status_code == 400 and KEY not in r.text
        mock.post(UPSTREAM).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))
        r = _post(env)
        assert r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_AUTH_FAILED"
        mock.post(UPSTREAM).mock(side_effect=httpx.ConnectTimeout("t"))
        r = _post(env)
        assert r.status_code == 504


def test_missing_credential_and_disabled_route(env, db, secrets_memory):
    secrets_memory._data.clear()
    r = _post(env)
    assert r.status_code == 503 and r.json()["error"]["code"] == "UPSTREAM_CREDENTIAL_MISSING"
    from aeos.models.routing import ModelRoute

    db.scalar(select(ModelRoute).where(ModelRoute.route_key == "worker.free-a")).status = "DISABLED"
    db.commit()
    r = _post(env)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ROUTE_NOT_APPROVED"


def test_claude_native_route_never_proxied(env, db):
    """§12: no Claude API — a claude-native route is never reachable through the worker proxy."""
    from aeos.models.routing import ModelRoute

    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "worker.free-a"))
    route.provider = "claude-native"
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        r = _post(env)
        assert r.status_code == 403 and len(mock.calls) == 0


def test_models_endpoint_lists_scope_only(env):
    r = env["client"].get("/broker/v1/models", headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["model-a"]
    assert env["client"].get("/broker/v1/models").status_code == 401


# ------------------------------------------------------------------ §23 per-request route authorisation


@pytest.fixture
def real_routing(settings, db, secrets_memory, monkeypatch):
    """Like ``env`` but with the REAL routing module, so the per-request approval rule itself is tested."""
    fk = FakeModules().install(monkeypatch, skip=("aeos.routing.service",))
    secrets_memory.put("workers/openrouter", KEY, actor="test")
    p = make_project(db)
    route = make_route(db, key="worker.free-a", model_id="vendor/model-a:free")
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="RUNNING", model_route_key="worker.free-a")
    db.add(run)
    db.flush()
    raw, grant = broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="MODEL_PROXY",
                                    scope={"models": {"model-a": "worker.free-a"}}, ttl_seconds=3600, max_requests=20)
    db.commit()
    app = make_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield {"client": c, "token": raw, "grant": grant, "run": run, "db": db, "fakes": fk, "route": route}


def test_inflight_grant_stops_when_the_route_approval_no_longer_holds(real_routing, db):
    """§23: an admitted run's grant (TTL up to hours) is checked against the route's CURRENT approval on
    every request: a configuration change (here: the catalog now shows a price), an expired TRIAL or a
    revoked approval refuses the next request — the upstream is never called."""
    from aeos.models.decisions import Decision
    from aeos.models.routing import ModelRoute

    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json()))
        assert _post(real_routing).status_code == 200
        route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "worker.free-a"))
        route.is_free, route.pricing = "NO", {"prompt": "0.000002", "completion": "0.000008"}
        db.commit()
        r = _post(real_routing)
        assert r.status_code == 403 and r.json()["error"]["code"] == "ROUTE_APPROVAL_INVALID", r.text
        assert len(mock.calls) == 1, "nothing was forwarded after the approval stopped matching"
    # back to the approved configuration, but the human decision is revoked
    route.is_free, route.pricing = "YES", {}
    d = db.scalar(select(Decision).where(Decision.target_id == "worker.free-a", Decision.kind == "ROUTE_ACTIVATION"))
    d.status = "INVALIDATED"
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        r = _post(real_routing)
        assert r.status_code == 403 and r.json()["error"]["code"] == "ROUTE_APPROVAL_INVALID"
        assert len(mock.calls) == 0


def test_expired_trial_decision_refuses_the_next_request(real_routing, db):
    from datetime import timedelta

    from aeos.models.decisions import Decision
    from aeos.models.routing import ModelRoute

    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "worker.free-a"))
    route.status = "TRIAL"
    db.add(Decision(kind="TRIAL", actor_user_id="owner", actor_kind="HUMAN", target_type="model_route",
                    target_id="worker.free-a", target_hash=route.config_hash, domain="PERSONAL", rationale="trial",
                    expires_at=utcnow() + timedelta(hours=1)))  # expiry is forced explicitly below
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json()))
        assert _post(real_routing).status_code == 200
        d = db.scalar(select(Decision).where(Decision.kind == "TRIAL"))
        d.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        r = _post(real_routing)
        assert r.status_code == 403 and r.json()["error"]["code"] == "ROUTE_APPROVAL_INVALID"
        assert len(mock.calls) == 1


def test_non_worker_pools_are_refused_at_the_boundary_and_at_admission(real_routing, db):
    """§23 ARENA/COMPARATOR/LOCAL: no controlled execution path through the broker yet (ADR-0106) —
    refused at routing admission (POOL_NOT_SERVED), and by the proxy if a grant ever names one."""
    from aeos.core.errors import Blocked
    from aeos.models.planning import TaskSpec
    from aeos.models.routing import ModelRoute
    from aeos.routing import facade as routing

    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "worker.free-a"))
    route.pool = "LOCAL"
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        r = _post(real_routing)
        assert r.status_code == 403 and r.json()["error"]["code"] == "ROUTE_NOT_WORKER"
        assert "no broker serving path" in r.json()["error"]["message"] and len(mock.calls) == 0
    task = TaskSpec(domain="PERSONAL", data_classification="INTERNAL", role_key="feature-implementer",
                    model_route_policy={"pool": "LOCAL"}, task_key="t1", title="t", goal="g", project_id="p",
                    plan_version_id="pv", plan_version_hash="h", spec_hash="s")
    with pytest.raises(Blocked) as e:
        routing.select_route(db, task=task)
    assert e.value.code == "POOL_NOT_SERVED" and broker.SERVED_POOLS == ("WORKER",)
