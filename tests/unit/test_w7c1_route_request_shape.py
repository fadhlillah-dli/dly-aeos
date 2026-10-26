"""Wave-7 C1 (ADR-0041 amendment 2026-10-25, ADR-0111 amendment): the seeded routes' upstream request shape, the
S07 free-tier client restriction, the LOCAL loopback endpoint path, the worker reasoning level and the supervisor
prompt/policy/context binding.

E1 facts (documentation read 2026-10-25, not exercised live): S07 documents the OpenCode Zen OpenAI-compatible
endpoint ``https://opencode.ai/inference/openai/v1/chat/completions`` with BARE model ids (``opencode/<id>`` is the
OpenCode config format) and "Free-tier chat models require OpenCode; use paid models or Go from other clients"; the
Gemini API documents ``https://generativelanguage.googleapis.com/v1beta/openai/chat/completions``. The upstreams are
respx mocks (or a real loopback HTTP server for LOCAL); every rule on the AEOS side is the real one.

OpenCode Go (owner decision A, ADR-0041 amendment 2026-10-26; E1 Go docs read 2026-10-26): chat completions on
``https://opencode.ai/inference/go/openai/v1/chat/completions`` with the bare model id, ``Authorization: Bearer`` with
the Console key, the client's own User-Agent and a stable ``x-opencode-session`` per conversation; Go allows other
clients, so its routes carry no client restriction."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeos import __version__
from aeos.broker import facade as broker
from aeos.core.errors import PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.broker_ext import BrokerRequest
from aeos.models.cost import UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun
from aeos.routing import seed, service
from aeos.routing.router import route_view
from tests.conftest import VIEWER_PW, AuthedClient, create_admin, create_viewer, make_app
from tests.unit.test_routing_fakes import OWNER_CLIENT_TERMS, human_decision
from tests.unit.test_workforce_support import make_project

ZEN_CHAT = "https://opencode.ai/inference/openai/v1/chat/completions"
GO_CHAT = "https://opencode.ai/inference/go/openai/v1/chat/completions"
GO_GLM = "opencode-go/glm-5.3-flash"
GO_KIMI = "opencode-go/kimi-k2.7-code"
GO_KEY = "go-test-" + "o" * 40  # visibly fake
GEMINI_CHAT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
DS = "opencode-zen/deepseek-v4.1-flash-free"
GLM = "opencode-zen/glm-5.3-flash-free"
MIMO = "opencode-zen/mimo-v2.6-flash-free"
BEAM = "opencode-zen/beam-free"
QWEN = "opencode-zen/qwen3.8-flash"
CODEX = "opencode-zen/gpt-5.3-codex"
GEMINI = "gemini/gemini-3.8-flash-free-tier"
LOCAL = "local/qwen3.8-flash-next"
ZEN_KEY = "zen-test-" + "z" * 40  # visibly fake
GEMINI_KEY = "gem-test-" + "g" * 40
NOW = "2026-10-25"


@pytest.fixture
def world(db, secrets_memory):
    seed.run(db)
    admin = create_admin(db)
    secrets_memory.put("workers/opencode-zen", ZEN_KEY, actor="test")
    secrets_memory.put("workers/gemini", GEMINI_KEY, actor="test")
    secrets_memory.put("workers/opencode-go", GO_KEY, actor="test")
    db.commit()
    return admin


def _trial(db, admin, key: str, **kw: Any) -> ModelRoute:
    """The owner's TRIAL activation through the real service (terms re-read now)."""
    r = service.get_route(db, key)
    r.availability, r.availability_checked_at = "AVAILABLE", utcnow()
    route, _ = service.activate_route(db, key, actor_user_id=admin.id, mode="TRIAL",
                                      rationale="feasibility trial of the section 23 route",
                                      expected_config_hash=r.config_hash, terms_reviewed=True,
                                      terms_note=f"S07 pricing and privacy pages read {NOW}", **kw)
    db.commit()
    return route


def _ok(model: str) -> dict[str, Any]:
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 1}}


# ------------------------------------------------------------------------------------------- seeded request shape


def test_seeded_zen_routes_carry_the_documented_endpoint_and_bare_upstream_model(world, db):
    rows = {r.route_key: r for r in db.scalars(select(ModelRoute).where(ModelRoute.provider == "opencode-zen"))}
    assert rows and all(r.model_id.startswith("opencode/") for r in rows.values())  # OpenCode catalog id kept
    for key, r in rows.items():
        bare = key.split("/", 1)[1]
        shape = broker.upstream_request_shape(r)
        assert r.mode_params["upstream_model"] == bare and shape["model"] == bare, key
        assert r.endpoint.startswith("https://opencode.ai/inference/"), key
        if shape["api"] == "openai.chat_completions":
            assert shape["url"] == ZEN_CHAT and shape["code"] is None, key
        else:  # S07 documents another API family: the broker refuses instead of sending a request it cannot answer
            assert shape["code"] == "ROUTE_API_UNSUPPORTED", key
    assert rows[QWEN].mode_params["upstream_api"] == "anthropic.messages"
    assert rows[QWEN].endpoint == "https://opencode.ai/inference/anthropic/v1"
    assert rows[CODEX].mode_params["upstream_api"] == "openai.responses"
    assert rows[DS].mode_params["reasoning"] == "PROVIDER_DEFAULT"
    # S07 "Free-tier chat models require OpenCode": every free Zen route records it, undecided; paid ones do not
    for key, r in rows.items():
        cr = (r.data_terms or {}).get("client_restriction")
        if r.is_free == "YES":
            assert cr and cr["owner_decision"] == "UNDECIDED" and "require OpenCode" in cr["statement"], key
            assert service.client_restriction_problem(r), key
        else:
            assert cr is None, key
    assert "Not listed in the S07 model catalog" in rows["opencode-zen/longcat-2.5-preview-free"].notes


def test_seeded_gemini_comparator_uses_the_openai_compatible_base(world, db):
    g = service.get_route(db, GEMINI)
    shape = broker.upstream_request_shape(g)
    assert g.endpoint == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert shape["url"] == GEMINI_CHAT and shape["model"] == "gemini-3.8-flash" and shape["code"] is None
    assert service.client_restriction(g) is None


def test_seeded_go_routes_carry_the_documented_go_endpoint_and_no_client_restriction(world, db):
    rows = {r.route_key: r for r in db.scalars(select(ModelRoute).where(ModelRoute.provider == "opencode-go"))}
    assert rows and all(r.model_id == key for key, r in rows.items())  # the OpenCode config id is kept
    for key, r in rows.items():
        shape = broker.upstream_request_shape(r)
        bare = key.split("/", 1)[1]
        assert shape["url"] == GO_CHAT and shape["model"] == bare and shape["code"] is None, key
        assert shape["api"] == "openai.chat_completions" and shape["reasoning_effort"] == "NOT_SENT", key
        assert service.client_restriction(r) is None and not service.request_shape_rejections(r), key
    # an older row without upstream_model still sends the bare id (the opencode-go/ config prefix is stripped)
    r = rows[GO_GLM]
    r.mode_params = {k: v for k, v in r.mode_params.items() if k != "upstream_model"}
    assert broker.upstream_request_shape(r)["model"] == "glm-5.3-flash"
    # the free Zen pilots keep their S07 client restriction: decision A seeds Go, it does not decide the Zen rule
    for key in (DS, GLM):
        assert service.client_restriction(service.get_route(db, key))["owner_decision"] == "UNDECIDED"


def test_seed_corrects_untouched_rows_and_never_rewrites_an_activated_one(world, db):
    old = {}
    for key in (DS, MIMO, GLM):
        r = service.get_route(db, key)
        r.endpoint = "https://opencode.ai/zen"  # the superseded seed value
        r.mode_params = {"credential_ref": "workers/opencode-zen"}
        r.data_terms = {k: v for k, v in r.data_terms.items() if k != "client_restriction"}
        r.config_hash = service.compute_config_hash(r)
        old[key] = r.config_hash
    g = service.get_route(db, GEMINI)
    g.endpoint = "https://generativelanguage.googleapis.com"
    g.config_hash = service.compute_config_hash(g)
    glm = service.get_route(db, GLM)
    glm.status = "TRIAL"  # activated by the owner on the old shape: its decision must not be silently voided
    db.flush()
    out = seed.run(db)
    assert {DS, MIMO, GEMINI} <= set(out["request_shape_corrected"]) and GLM not in out["request_shape_corrected"]
    assert out["request_shape_pending"] == [GLM] and out["approved"] == 0
    ds = service.get_route(db, DS)
    assert ds.endpoint == "https://opencode.ai/inference/openai/v1" and ds.mode_params["upstream_model"] == ds.route_key[13:]
    assert ds.config_hash == service.compute_config_hash(ds) != old[DS]
    assert ds.data_terms["client_restriction"]["owner_decision"] == "UNDECIDED"
    assert service.get_route(db, GEMINI).endpoint.endswith("/v1beta/openai")
    assert glm.endpoint == "https://opencode.ai/zen" and glm.config_hash == old[GLM]  # untouched
    shape = broker.upstream_request_shape(glm)
    assert shape["code"] == "ROUTE_ENDPOINT_SUPERSEDED" and "aeos seed" in shape["problem"]
    assert any("ROUTE_ENDPOINT_SUPERSEDED" in x for x in service.request_shape_rejections(glm))
    assert seed.run(db)["request_shape_corrected"] == []  # idempotent
    # the owner's way out of "pending": disabling invalidates its decisions, so the next seed corrects the request
    # shape only - the route stays DISABLED until the owner activates the corrected configuration
    service.disable_route(db, GLM, reason="superseded seed endpoint")
    out = seed.run(db)
    assert out["request_shape_corrected"] == [GLM] and out["request_shape_pending"] == []
    glm = service.get_route(db, GLM)
    assert glm.status == "DISABLED" and glm.endpoint == "https://opencode.ai/inference/openai/v1"
    assert glm.config_hash == service.compute_config_hash(glm) != old[GLM]
    assert broker.upstream_request_shape(glm)["code"] is None
    assert seed.run(db)["request_shape_corrected"] == []


# ------------------------------------------------------------------------------------------- feasibility probe


def test_probe_on_seeded_arena_zen_route_posts_the_documented_url_and_bare_model(world, db):
    _trial(db, world, MIMO, client_terms=OWNER_CLIENT_TERMS)
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(ZEN_CHAT).mock(return_value=httpx.Response(200, json=_ok("mimo-v2.6-flash-free")))
        out = broker.probe_route(db, route_key=MIMO, actor="user:owner")
        db.commit()
        assert len(mock.calls) == 1
        sent = json.loads(up.calls[0].request.content)
        assert up.calls[0].request.headers["authorization"] == f"Bearer {ZEN_KEY}"
    assert sent["model"] == "mimo-v2.6-flash-free" and "reasoning_effort" not in sent and sent["max_tokens"] == 16
    assert out["outcome"] == "REACHABLE" and out["upstream_url"] == ZEN_CHAT
    assert out["model_id"] == "mimo-v2.6-flash-free" and out["catalog_model_id"] == "opencode/mimo-v2.6-flash-free"
    assert ZEN_KEY not in json.dumps(out)


def test_probe_on_seeded_beam_route_needs_its_reviewed_region(world, db):
    with pytest.raises(ValidationFailed) as ei:
        _trial(db, world, BEAM, client_terms=OWNER_CLIENT_TERMS)
    assert ei.value.code == "REGION_REVIEW_REQUIRED"
    _trial(db, world, BEAM, client_terms=OWNER_CLIENT_TERMS, region="ID")
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(ZEN_CHAT).mock(return_value=httpx.Response(200, json=_ok("beam-free")))
        assert broker.probe_route(db, route_key=BEAM, actor="user:owner")["outcome"] == "REACHABLE"
        assert json.loads(up.calls[0].request.content)["model"] == "beam-free"


def test_probe_on_the_comparator_route_uses_the_gemini_openai_base(world, db):
    _trial(db, world, GEMINI)
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(GEMINI_CHAT).mock(return_value=httpx.Response(200, json=_ok("gemini-3.8-flash")))
        out = broker.probe_route(db, route_key=GEMINI, actor="user:owner")
        db.commit()
        assert up.calls[0].request.headers["authorization"] == f"Bearer {GEMINI_KEY}"
        assert json.loads(up.calls[0].request.content)["model"] == "gemini-3.8-flash"
    assert out["outcome"] == "REACHABLE" and out["pool"] == "COMPARATOR" and out["upstream_url"] == GEMINI_CHAT


def test_client_restricted_route_is_never_activated_proxied_or_probed_without_the_owners_decision(world, db):
    with pytest.raises(ValidationFailed) as ei:
        _trial(db, world, MIMO)
    assert ei.value.code == "CLIENT_TERMS_DECISION_REQUIRED" and "require OpenCode" in ei.value.message
    assert service.get_route(db, MIMO).status == "CANDIDATE"
    # a TRIAL recorded without the decision (e.g. by an earlier build) still never reaches the upstream
    r = service.get_route(db, MIMO)
    r.status = "TRIAL"
    db.add(Decision(kind="TRIAL", actor_user_id=world.id, actor_kind="HUMAN", target_type="model_route", target_id=MIMO,
                    target_hash=service.compute_config_hash(r), domain="PERSONAL", rationale="older trial record",
                    allowed_operations=["route.use"]))
    db.commit()
    assert "require OpenCode" in (service.route_use_problem(db, r) or "")
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as denied:
            broker.probe_route(db, route_key=MIMO, actor="user:owner")
        assert not mock.calls
    assert denied.value.code == "CLIENT_TERMS_RESTRICTED"
    reasons = service.route_rejections(db, r, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False,
                                       min_context=None)
    assert any("client restriction" in x for x in reasons)
    # editing the row to look decided changes its identity: the old decision no longer authorises it
    r.data_terms = {**r.data_terms, "client_restriction": {**r.data_terms["client_restriction"],
                                                           "owner_decision": service.CLIENT_TERMS_ACCEPTED}}
    assert service.client_restriction_problem(r) is None
    assert "no ACTIVE human TRIAL decision" in (service.route_use_problem(db, r) or "")


def test_the_route_view_states_the_request_shape_restriction_and_evaluation_path(world, db):
    from types import SimpleNamespace

    p = SimpleNamespace(active_domain=None)
    view = route_view(db, service.get_route(db, MIMO), p)
    assert view["upstream_request"]["url"] == ZEN_CHAT and view["upstream_request"]["model"] == "mimo-v2.6-flash-free"
    assert view["upstream_request"]["status"] == "READY_SHAPE" and "E1" in view["upstream_request"]["note"]
    assert view["client_restriction"]["status"] == "OWNER_DECISION_REQUIRED"
    # MIMO is an ARENA route: evaluated only through the eval-only path (cluster C2, ADR-0111 W6-141); a COMPARATOR
    # route has no evaluation path in this build
    assert view["evaluation_path"]["status"] == "EVAL_ONLY" and "NOT-PROVEN" in view["evaluation_path"]["note"]
    assert "jalur khusus evaluasi" in view["execution_path"]["note"]
    comparator = db.scalars(select(ModelRoute).where(ModelRoute.pool == "COMPARATOR")).first()
    assert comparator is not None
    cview = route_view(db, comparator, p)
    assert cview["evaluation_path"]["status"] == "NOT_WIRED" and "NOT-PROVEN" in cview["evaluation_path"]["note"]
    assert "belum tersambung" in cview["execution_path"]["note"]
    assert route_view(db, service.get_route(db, QWEN), p)["upstream_request"]["status"] == "REFUSED"
    assert route_view(db, service.get_route(db, DS), p)["evaluation_path"]["status"] == "EVAL_RUN"
    assert route_view(db, service.get_route(db, "claude-native/opus"), p)["upstream_request"] is None
    local = route_view(db, service.get_route(db, LOCAL), p)
    assert local["upstream_request"]["code"] == "ROUTE_ENDPOINT" and local["budget_required"] is True


# ------------------------------------------------------------------------------------------- worker proxy


@pytest.fixture
def proxy(world, db, settings):
    p = make_project(db)
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="RUNNING", model_route_key=DS)
    db.add(run)
    db.flush()
    raw, _grant = broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="MODEL_PROXY",
                                     scope={"models": {"aeos-ds": DS}, "per_request_output_cap": 256},
                                     ttl_seconds=600, max_requests=10, max_tokens=10_000, max_cost_usd=0.0)
    db.commit()
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        yield {"client": c, "token": raw, "run": run}


def _call(proxy, **body: Any):
    payload = {"model": "aeos-ds", "messages": [{"role": "user", "content": "hi"}], **body}
    return proxy["client"].post("/broker/v1/chat/completions", json=payload,
                                headers={"Authorization": f"Bearer {proxy['token']}"})


def test_worker_proxy_on_the_seeded_zen_route_sends_the_bare_model_and_never_the_workers_reasoning(world, db, proxy):
    _trial(db, world, DS, client_terms=OWNER_CLIENT_TERMS)
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(ZEN_CHAT).mock(return_value=httpx.Response(200, json=_ok("deepseek-v4.1-flash-free")))
        r = _call(proxy, reasoning_effort="high")
        assert r.status_code == 200, r.text
        sent = json.loads(up.calls[0].request.content)
        assert str(up.calls[0].request.url) == ZEN_CHAT
        assert up.calls[0].request.headers["authorization"] == f"Bearer {ZEN_KEY}"
    assert sent["model"] == "deepseek-v4.1-flash-free" and "reasoning_effort" not in sent
    assert "x-opencode-session" not in up.calls[0].request.headers  # the Go client identification is Go-only
    assert r.json()["model"] == "aeos-ds" and ZEN_KEY not in r.text
    db.expire_all()
    req = db.scalar(select(BrokerRequest).where(BrokerRequest.outcome == "OK"))
    assert req.upstream_model == "deepseek-v4.1-flash-free"
    assert req.reasoning_effort == "NOT_SENT" and req.reasoning_requested == "high"  # dropped, and observed


def test_a_worker_sent_reasoning_value_is_recorded_only_as_a_known_level(world, db, proxy):
    from aeos.models.audit import Event

    _trial(db, world, DS, client_terms=OWNER_CLIENT_TERMS)
    junk = "free-text-" + "q" * 30  # the worker is untrusted: its free text is never persisted
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(ZEN_CHAT).mock(return_value=httpx.Response(200, json=_ok("deepseek-v4.1-flash-free")))
        assert _call(proxy, reasoning_effort=junk).status_code == 200
        assert _call(proxy, reasoning={"effort": "xhigh"}).status_code == 200  # OpenRouter-style field, dropped too
        assert _call(proxy).status_code == 200
        sent = [json.loads(c.request.content) for c in up.calls]
    assert len(sent) == 3 and all("reasoning_effort" not in b and "reasoning" not in b for b in sent)
    db.expire_all()
    reqs = db.scalars(select(BrokerRequest).where(BrokerRequest.outcome == "OK")).all()
    assert sorted(str(r.reasoning_requested) for r in reqs) == ["None", "UNRECOGNISED", "xhigh"]
    assert all(r.reasoning_effort == "NOT_SENT" for r in reqs)
    payloads = json.dumps([e.payload for e in db.scalars(select(Event).where(Event.kind == "run.model_call"))])
    assert junk not in payloads and "UNRECOGNISED" in payloads


def test_a_reasoning_level_is_route_configuration_never_the_workers(world, db, proxy):
    route = _trial(db, world, DS, client_terms=OWNER_CLIENT_TERMS)
    trial_hash = route.config_hash
    route.mode_params = {**route.mode_params, "reasoning": "low"}  # the owner states a level: a new configuration
    assert service.refresh_config_hash(db, route, reason="reasoning level low")
    db.commit()
    assert route.config_hash != trial_hash
    with respx.mock(assert_all_mocked=True) as mock:
        r = _call(proxy)  # the TRIAL was bound to the old configuration: refused before any upstream call
        assert r.status_code == 403 and r.json()["error"]["code"] == "ROUTE_APPROVAL_INVALID" and not mock.calls
    service.activate_route(db, DS, actor_user_id=world.id, mode="TRIAL", rationale="re-review with reasoning low",
                           expected_config_hash=route.config_hash)
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(ZEN_CHAT).mock(return_value=httpx.Response(200, json=_ok("deepseek-v4.1-flash-free")))
        assert _call(proxy, reasoning_effort="high").status_code == 200
        assert json.loads(up.calls[0].request.content)["reasoning_effort"] == "low"
    db.expire_all()
    req = db.scalars(select(BrokerRequest).where(BrokerRequest.outcome == "OK")).all()[-1]
    assert req.reasoning_effort == "low" and req.reasoning_requested == "high"
    bad = service.get_route(db, DS)
    bad.mode_params = {**bad.mode_params, "reasoning": "turbo"}
    assert broker.upstream_request_shape(bad)["code"] == "ROUTE_MODE_PARAMS"


def _go_budget(db, admin) -> Decision:
    """The owner's BUDGET decision covering the Go routes it names (one decision through ``auth_scope.routes``)."""
    return human_decision(db, kind="BUDGET", target_type="model_budget", target_id="opencode-go",
                          auth_scope={"routes": [GO_GLM, GO_KIMI]}, actor_user_id=admin.id)


def test_go_route_activates_without_client_terms_but_needs_region_and_budget(world, db):
    budget = _go_budget(db, world)
    with pytest.raises(ValidationFailed) as ei:  # a workspace privacy setting restricts regions
        _trial(db, world, GO_GLM, budget_decision_id=budget.id)
    assert ei.value.code == "REGION_REVIEW_REQUIRED"
    with pytest.raises(ValidationFailed) as ei:  # a paid subscription route
        _trial(db, world, GO_GLM, region="EU")
    assert ei.value.code == "BUDGET_DECISION_REQUIRED"
    with pytest.raises(ValidationFailed) as ei:  # Go allows other clients: there is no client rule to decide
        _trial(db, world, GO_GLM, region="EU", budget_decision_id=budget.id, client_terms=OWNER_CLIENT_TERMS)
    assert ei.value.code == "CLIENT_TERMS_NOT_APPLICABLE"
    assert service.get_route(db, GO_GLM).status == "CANDIDATE"
    route = _trial(db, world, GO_GLM, region="EU", budget_decision_id=budget.id)
    assert route.status == "TRIAL" and route.region == "EU" and service.route_use_problem(db, route) is None
    from types import SimpleNamespace

    view = route_view(db, route, SimpleNamespace(active_domain=None))
    assert view["client_restriction"] is None and view["budget_required"] is True
    assert view["upstream_request"]["status"] == "READY_SHAPE" and view["upstream_request"]["url"] == GO_CHAT
    assert view["evaluation_path"]["status"] == "EVAL_RUN"
    # the budget copy names the plan fee, never per-token spend (ADR-0080 amendment 2026-10-26)
    assert view["billing"] == "SUBSCRIPTION" and "$10/month plan fee" in view["budget_note"]
    assert view["subscription"]["plan"] == "OpenCode Go" and view["subscription"]["state"] == "OK"
    assert view["subscription"]["spend_hidden"] is True  # this principal may not see every domain's usage


def test_go_route_takes_one_feasibility_probe_that_consumes_allowance_not_money(world, db):
    """ADR-0080 amendment 2026-10-26: the probe may run on a flat-fee route (it consumes plan allowance, not money);
    the provider's allowance figure is recorded apart from the marginal cost (0 within the plan)."""
    _trial(db, world, GO_GLM, region="EU", budget_decision_id=_go_budget(db, world).id)
    usage = {"prompt_tokens": 12, "completion_tokens": 1, "cost": 0,
             "opencode": {"allowance": 0.000004, "billing_source": "go"}}
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json={**_ok("glm-5.3-flash"), "usage": usage}))
        out = broker.probe_route(db, route_key=GO_GLM, actor="user:owner")
        db.commit()
        assert len(mock.calls) == 1
        sent = up.calls[0].request
    assert json.loads(sent.content)["model"] == "glm-5.3-flash" and sent.headers["authorization"] == f"Bearer {GO_KEY}"
    assert sent.headers["user-agent"] == f"dly-aeos-broker/{__version__}"
    assert sent.headers["x-opencode-session"].startswith("ses_")  # its own one-request conversation
    assert out["outcome"] == "REACHABLE" and out["cost_usd"] == 0 and GO_KEY not in json.dumps(out)
    assert out["billing"] == {"billing": "SUBSCRIPTION", "allowance_usd": "0.000004", "allowance_basis": "REPORTED",
                              "billing_source": "go", "beyond_plan_usd": None}
    led = db.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.route_key == GO_GLM))
    assert led.source == "broker-feasibility" and led.cost_usd == 0 and led.charged_usd == 0
    assert led.details["allowance_usd"] == "0.000004" and led.details["billing_source"] == "go"


@pytest.fixture
def go_proxy(world, db, settings):
    p = make_project(db)
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="RUNNING", model_route_key=GO_GLM)
    db.add(run)
    db.flush()
    raw, _grant = broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="MODEL_PROXY",
                                     scope={"models": {"aeos-go-glm": GO_GLM}, "per_request_output_cap": 256},
                                     ttl_seconds=600, max_requests=10, max_tokens=10_000, max_cost_usd=1.0)
    db.commit()
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        yield {"client": c, "token": raw, "run_id": run.id}


def test_worker_proxy_on_a_go_route_sends_its_own_client_identity_and_a_stable_session(world, db, go_proxy):
    _trial(db, world, GO_GLM, region="EU", budget_decision_id=_go_budget(db, world).id)
    run_id = go_proxy["run_id"]
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_ok("glm-5.3-flash")))
        for _ in range(2):
            r = go_proxy["client"].post("/broker/v1/chat/completions", headers={
                "Authorization": f"Bearer {go_proxy['token']}"}, json={
                "model": "aeos-go-glm", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"})
            assert r.status_code == 200, r.text
        reqs = [c.request for c in up.calls]
    assert len(reqs) == 2 and all(str(q.url) == GO_CHAT for q in reqs)
    assert all(q.headers["authorization"] == f"Bearer {GO_KEY}" for q in reqs)  # only towards opencode.ai
    assert all(q.headers["user-agent"] == f"dly-aeos-broker/{__version__}" for q in reqs)  # not python-httpx
    sessions = {q.headers["x-opencode-session"] for q in reqs}
    assert sessions == {"ses_" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]}  # stable per run
    sid = sessions.pop()
    assert run_id not in sid and go_proxy["token"] not in sid and GO_KEY not in sid  # opaque, never a secret
    sent = json.loads(reqs[0].content)
    assert sent["model"] == "glm-5.3-flash" and "reasoning_effort" not in sent
    assert r.json()["model"] == "aeos-go-glm" and GO_KEY not in r.text
    db.expire_all()
    req = db.scalars(select(BrokerRequest).where(BrokerRequest.outcome == "OK")).all()[-1]
    assert req.provider == "opencode-go" and req.upstream_model == "glm-5.3-flash"


def test_worker_proxy_on_a_go_route_records_allowance_apart_from_money(world, db, go_proxy):
    """ADR-0080 amendment 2026-10-26: each Go call records the provider's marginal cost (0 within the plan) and the
    allowance it consumed - REPORTED (usage.opencode.allowance, also from a stream's usage chunk) or ESTIMATED from its
    tokens. The run's $0 hold is never charged as made-up dollars."""
    from aeos.cost import facade as cost
    from aeos.models.cost import BudgetReservation

    _trial(db, world, GO_GLM, region="EU", budget_decision_id=_go_budget(db, world).id)
    run = db.get(WorkerRun, go_proxy["run_id"])
    res = cost.reserve(db, cost.ReservationRequest(run_id=run.id, project_id=run.project_id, domain="PERSONAL",
                                                   route_key=GO_GLM, est_usd=0.0, est_tokens=10_000, est_requests=10))
    run.budget_reservation_id = res.id
    db.commit()
    assert res.pricing_basis == "SUBSCRIPTION"

    def usage(allowance: float) -> dict[str, Any]:
        return {"prompt_tokens": 12, "completion_tokens": 1, "cost": 0,
                "opencode": {"allowance": allowance, "billing_source": "go"}}

    chunks = [{"choices": [{"index": 0, "delta": {"content": "OK"}}], "model": "glm-5.3-flash"},
              {"choices": [], "usage": usage(0.00003)}]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(GO_CHAT).mock(side_effect=[
            httpx.Response(200, json={**_ok("glm-5.3-flash"), "usage": usage(0.00002)}),
            httpx.Response(200, json=_ok("glm-5.3-flash")),  # no cost, no allowance reported
            httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())])
        for extra in ({}, {}, {"stream": True}):
            r = go_proxy["client"].post("/broker/v1/chat/completions", headers={
                "Authorization": f"Bearer {go_proxy['token']}"}, json={
                "model": "aeos-go-glm", "messages": [{"role": "user", "content": "hi"}], **extra})
            assert r.status_code == 200, r.text
    db.expire_all()
    entries = list(db.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id == run.id)
                              .order_by(UsageLedgerEntry.at)))
    assert [(e.details["allowance_basis"], e.details["allowance_usd"]) for e in entries] == [
        ("REPORTED", "0.000020"), ("ESTIMATED", "0.000003"), ("REPORTED", "0.000030")]  # 12 x 0.15 + 1 x 0.50 / 1M
    assert [float(e.charged_usd) for e in entries] == [0.0, 0.0, 0.0]
    assert entries[1].cost_usd is None and entries[1].details["marginal_cost_unreported"] is True
    assert not any("cost_unknown_charged_reservation" in e.details for e in entries)
    hold = db.get(BudgetReservation, res.id)
    assert hold.status == "RESERVED" and float(hold.committed_usd) == 0 and hold.committed_requests == 3
    summary = db.get(WorkerRun, run.id).usage_summary
    assert summary["billing"] == "SUBSCRIPTION" and summary["allowance_usd"] == pytest.approx(0.000053)
    assert summary["allowance_not_reported_requests"] == 1 and summary["cost_unknown_requests"] == 1


def _go_call(go_proxy, **extra: Any):
    return go_proxy["client"].post("/broker/v1/chat/completions", headers={
        "Authorization": f"Bearer {go_proxy['token']}"}, json={
        "model": "aeos-go-glm", "messages": [{"role": "user", "content": "hi"}], **extra})


def test_go_call_without_usage_records_an_upper_bound_estimate_never_unknown(world, db, go_proxy):
    """No reservation (or a hold already settled) and an answer without any usage: the broker records the call's upper
    bound (clamped max_tokens + prompt size), so the allowance is ESTIMATED and the route is never locked out."""
    _trial(db, world, GO_GLM, region="EU", budget_decision_id=_go_budget(db, world).id)
    no_usage = {"model": "glm-5.3-flash", "choices": [{"message": {"role": "assistant", "content": "OK"}}]}
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=no_usage))
        r = _go_call(go_proxy, max_tokens=100)
        assert r.status_code == 200, r.text
        sent = json.loads(up.calls[0].request.content)
    assert sent["max_tokens"] == 100  # clamped to the grant's per-request cap (256)
    db.expire_all()
    (entry,) = db.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id == go_proxy["run_id"])).all()
    from aeos.broker import model_proxy

    assert entry.input_tokens is None and entry.details["allowance_basis"] == "ESTIMATED"
    assert entry.details["charged_tokens"] == model_proxy.request_token_bound(sent)
    from aeos.cost import facade as cost

    assert cost.allowance_status(db, service.get_route(db, GO_GLM))["state"] == "OK"


def test_go_call_billed_beyond_the_plan_stops_the_run_until_the_owner_reactivates(world, db, go_proxy):
    """Invariant 8: a call the provider bills from the pay-as-you-go balance (Extra Usage ON) without a usage.cost is
    flagged; the broker refuses the run's next request (and new admissions/probes) until the owner re-activates."""
    budget = _go_budget(db, world)
    _trial(db, world, GO_GLM, region="EU", budget_decision_id=budget.id)
    balance = {**_ok("glm-5.3-flash"), "usage": {"prompt_tokens": 12, "completion_tokens": 1,
                                                 "opencode": {"allowance": 0, "billing_source": "balance"}}}
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=balance))
        assert _go_call(go_proxy).status_code == 200
        r = _go_call(go_proxy)
        assert r.status_code == 403 and r.json()["error"]["code"] == "SUBSCRIPTION_BEYOND_PLAN", r.text
        assert len(up.calls) == 1  # the second request never left the broker
    db.expire_all()
    entry = db.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id == go_proxy["run_id"]))
    assert entry.details["beyond_plan_usd"] == "UNKNOWN" and entry.details["billing_source"] == "balance"
    _trial(db, world, GO_GLM, region="EU", budget_decision_id=budget.id)  # the owner re-activates the route
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_ok("glm-5.3-flash")))
        assert _go_call(go_proxy).status_code == 200


def test_provider_credentials_only_go_to_the_providers_own_host(world, db):
    r = service.get_route(db, DS)
    r.endpoint = "https://zen.example.com/v1"
    shape = broker.upstream_request_shape(r)
    assert shape["code"] == "ROUTE_ENDPOINT" and "opencode.ai" in shape["problem"]
    g = service.get_route(db, GEMINI)
    g.endpoint = "http://127.0.0.1:9/v1beta/openai"
    assert broker.upstream_request_shape(g)["code"] == "ROUTE_ENDPOINT"
    # the Go subscription key goes to https://opencode.ai only (another host, loopback or plain http fail closed)
    go = service.get_route(db, GO_GLM)
    for endpoint in ("https://go.example.com/inference/go/openai/v1", "http://127.0.0.1:9/inference/go/openai/v1",
                     "http://opencode.ai/inference/go/openai/v1"):
        go.endpoint = endpoint
        shape = broker.upstream_request_shape(go)
        assert shape["code"] == "ROUTE_ENDPOINT", endpoint
        assert "https://opencode.ai" in shape["problem"] or "must be https" in shape["problem"], endpoint
    assert any("ROUTE_ENDPOINT" in x for x in service.request_shape_rejections(go))


# ------------------------------------------------------------------------------------------- LOCAL loopback route


class _LocalModel(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        type(self).seen.append({"path": self.path, "body": body, "auth": self.headers.get("authorization")})
        data = json.dumps(_ok(body.get("model", ""))).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:  # keep test output clean
        return


@pytest.fixture
def local_server():
    _LocalModel.seen = []
    server = HTTPServer(("127.0.0.1", 0), _LocalModel)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_local_route_gets_an_owner_set_loopback_endpoint_and_is_probed_without_budget(world, db, local_server):
    with pytest.raises(ValidationFailed) as ei:  # no endpoint yet: nothing the broker could call
        _trial(db, world, LOCAL)
    assert ei.value.code == "ROUTE_REQUEST_SHAPE_REFUSED" and "set-endpoint" in ei.value.message
    for bad in ("http://192.168.1.5:8080/v1", "https://127.0.0.1:8443/v1", "http://localhost.example:80/v1",
                "http://user:pw@127.0.0.1:8080/v1"):
        with pytest.raises(ValidationFailed):
            service.set_local_endpoint(db, LOCAL, endpoint=bad, rationale="local llama.cpp server")
    with pytest.raises(ValidationFailed) as ei:
        service.set_local_endpoint(db, DS, endpoint=local_server, rationale="not a local route at all")
    assert ei.value.code == "ROUTE_NOT_LOCAL"
    before = service.get_route(db, LOCAL).config_hash
    route, change = service.set_local_endpoint(db, LOCAL, endpoint=local_server, rationale="local llama.cpp server",
                                               upstream_model="qwen3.8-flash-next-q4",
                                               build={"quantization": "Q4_K_M", "inference_backend": "llama.cpp"})
    db.commit()
    assert change["config_changed"] and route.config_hash != before and not change["needs_new_trial"]
    assert service.is_local_loopback(route) and not service.needs_budget(route)  # is_free stays UNKNOWN
    assert route.is_free == "UNKNOWN"
    _trial(db, world, LOCAL)  # no BUDGET decision: a loopback LOCAL route spends no money through the broker
    trial_hash = service.get_route(db, LOCAL).config_hash
    assert service.route_use_problem(db, service.get_route(db, LOCAL)) is None
    out = broker.probe_route(db, route_key=LOCAL, actor="user:owner")  # a real loopback HTTP round trip
    db.commit()
    assert out["outcome"] == "REACHABLE" and out["upstream_url"] == f"{local_server}/chat/completions"
    assert _LocalModel.seen[-1]["path"] == "/v1/chat/completions" and _LocalModel.seen[-1]["auth"] is None
    assert _LocalModel.seen[-1]["body"]["model"] == "qwen3.8-flash-next-q4"
    # another endpoint is another configuration: the TRIAL no longer authorises it -> a new TRIAL is needed
    route, change = service.set_local_endpoint(db, LOCAL, endpoint=local_server.replace("/v1", "/v2"),
                                               rationale="moved the server path")
    db.commit()
    assert change["needs_new_trial"] and route.config_hash != trial_hash
    with pytest.raises(PolicyDenied) as denied:
        broker.probe_route(db, route_key=LOCAL, actor="user:owner")
    assert denied.value.code == "ROUTE_APPROVAL_INVALID"


def test_local_endpoint_api_is_admin_only_and_loopback_only(admin_client: AuthedClient, db, local_server):
    seed.run(db)
    db.commit()
    url = f"/api/routes/{LOCAL}/endpoint"
    r = admin_client.post(url, json={"endpoint": "http://10.0.0.2:8080/v1", "rationale": "local server on LAN"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ENDPOINT_NOT_LOOPBACK"
    r = admin_client.post(f"/api/routes/{DS}/endpoint", json={"endpoint": local_server, "rationale": "wrong route"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ROUTE_NOT_LOCAL"
    r = admin_client.post(url, json={"endpoint": local_server, "rationale": "llama.cpp on this host",
                                     "quantization": "Q4_K_M"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config_changed"] is True and body["route"]["endpoint"] == local_server
    assert body["route"]["upstream_request"]["url"] == f"{local_server}/chat/completions"
    assert body["route"]["budget_required"] is False
    from aeos.models.audit import AuditEvent

    assert db.query(AuditEvent).filter_by(action="route.set_endpoint", target_id=LOCAL).count() == 1
    create_viewer(db)
    admin_client.post("/api/auth/logout")
    admin_client.login("viewer", VIEWER_PW)
    r = admin_client.post(url, json={"endpoint": local_server, "rationale": "viewer tries to change"})
    assert r.status_code == 403


# ------------------------------------------------------------------------------------------- supervisor binding


def test_supervisor_call_binds_prompt_revision_policy_version_and_context_strategy(db, monkeypatch):
    from tests.unit.test_routing_fakes import install_fakes

    reg = install_fakes(monkeypatch)
    seed.run(db)
    opus = service.get_route(db, "claude-native/opus")
    assert opus.prompt_revision is None and opus.policy_version is None and opus.context_strategy is None
    out = service.observe_supervisor_call(db, model="opus", engine_version="2.1.301")
    assert out["changed"] is False  # first binding is not a change
    runtime = service.current_supervisor_runtime()
    assert opus.prompt_revision == runtime["prompt_revision"] and opus.prompt_revision.startswith("planning-prompts/")
    assert opus.policy_version == runtime["policy_version"] and opus.policy_version.startswith("pol-")
    assert opus.context_strategy == runtime["context_strategy"] and opus.context_strategy.startswith("planning-")
    assert opus.config_hash == service.compute_config_hash(opus)
    bound = opus.config_hash
    assert service.observe_supervisor_call(db, model="opus", engine_version="2.1.301")["changed"] is False
    before = len(reg.invalidations)
    monkeypatch.setattr(service, "current_policy_version", lambda: "pol-changedpolicy")
    changed = service.observe_supervisor_call(db, model="opus", engine_version="2.1.301")
    assert changed["changed"] is True and any(x.startswith("policy_version ") for x in changed["reasons"])
    assert opus.config_hash != bound and opus.policy_version == "pol-changedpolicy"
    assert service.eval_summary(opus)["state"] == "NEEDS_EVALUATION"
    # a policy/prompt change does not touch the bridge: the bridge capability proof is not invalidated
    assert len(reg.invalidations) == before
    from aeos.planning import prompts

    monkeypatch.setattr(prompts, "PROMPT_REVISION", "planning-prompts/99")
    again = service.observe_supervisor_call(db, model="opus", engine_version="2.1.301")
    assert again["changed"] is True and opus.prompt_revision.startswith("planning-prompts/99@")
    upgraded = service.observe_supervisor_call(db, model="opus", engine_version="2.2.0")  # the bridge itself changed
    assert upgraded["changed"] is True and len(reg.invalidations) > before
