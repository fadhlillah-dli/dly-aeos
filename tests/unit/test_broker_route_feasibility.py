"""§23 "Arena tambahan ... uji kelayakan route dan data" / §48 arena (ADR-0111): ARENA/COMPARATOR/LOCAL routes
never serve worker tasks, but their feasibility can be tested through ONE controlled path - an owner-triggered
synthetic PUBLIC probe on a free route the owner activated (TRIAL, decision bound to the current config hash).
Real routing rule, real ledger, real evidence collector; the upstream is mocked (respx)."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.broker import feasibility
from aeos.core.errors import PolicyDenied
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.routing import ModelRoute
from aeos.verification import collector

UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
KEY = "sk-or-v1-" + "q" * 48


def _arena(db, key="arena.longcat-free", *, pool="ARENA", status="TRIAL", is_free="YES", classes=("PUBLIC",),
           domains=("PERSONAL",), decision=True, expires=None):
    from aeos.routing.service import compute_config_hash

    r = ModelRoute(route_key=key, pool=pool, provider="openrouter", model_id="vendor/longcat-preview:free",
                   status=status, eligible_domains=list(domains), is_free=is_free,
                   allowed_data_classes=list(classes), mode_params={"credential_ref": "workers/openrouter"})
    db.add(r)
    db.flush()
    r.config_hash = compute_config_hash(r)
    if decision:
        db.add(Decision(kind="TRIAL" if status == "TRIAL" else "ROUTE_ACTIVATION", actor_user_id="owner",
                        actor_kind="HUMAN", target_type="model_route", target_id=key, target_hash=r.config_hash,
                        domain="PERSONAL", rationale="owner trial after reading terms", allowed_operations=["route.use"],
                        expires_at=expires))
    db.commit()
    return r


@pytest.fixture
def key(secrets_memory):
    secrets_memory.put("workers/openrouter", KEY, actor="test")


def _ok(model="vendor/longcat-preview:free", cost=None):
    usage = {"prompt_tokens": 12, "completion_tokens": 2, **({"cost": cost} if cost is not None else {})}
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": "OK"}}], "usage": usage}


def test_activated_free_arena_route_is_probed_with_one_synthetic_public_request(db, key):
    _arena(db)
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        out = broker.probe_route(db, route_key="arena.longcat-free", actor="user:owner")
        db.commit()
        sent = json.loads(route.calls[0].request.content)
        assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
        assert len(mock.calls) == 1  # one request, no retry, no other route
    assert sent["messages"] == [{"role": "user", "content": feasibility.PROBE_PROMPT}]
    assert sent["max_tokens"] == feasibility.PROBE_MAX_TOKENS and sent["stream"] is False and "tools" not in sent
    assert sent["model"] == "vendor/longcat-preview:free"
    assert out["outcome"] == "REACHABLE" and out["answered"] and out["model_resolved"] == "vendor/longcat-preview:free"
    assert KEY not in json.dumps(out)
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert ev.kind == "PROBE" and ev.trusted and ev.evidence_level == "E3" and ev.subject_type == "model_route"
    assert collector.read_json(ev)["probe"]["data_class"] == "PUBLIC"
    led = db.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.route_key == "arena.longcat-free"))
    assert led is not None and led.input_tokens == 12 and led.source == "broker-feasibility"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.route_feasibility.probe")).result == "SUCCESS"
    r = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "arena.longcat-free"))
    assert r.status == "TRIAL" and r.pool == "ARENA"  # the observation changes nothing about the route


def test_arena_routes_still_never_serve_worker_tasks(db, key):
    """The probe path is the only one: the worker proxy keeps refusing ARENA pools at its boundary."""
    from aeos.broker.model_proxy import ProxyError, resolve_upstream

    _arena(db)
    with pytest.raises(ProxyError) as exc:
        resolve_upstream(db, "arena.longcat-free", domain="PERSONAL")
    assert exc.value.code == "ROUTE_NOT_WORKER"
    assert broker.SERVED_POOLS == ("WORKER",) and "ARENA" in broker.PROBE_POOLS


@pytest.mark.parametrize("kw,code", [
    (dict(decision=False), "ROUTE_APPROVAL_INVALID"),        # TRIAL without a hash-bound human decision
    (dict(status="CANDIDATE", decision=False), "ROUTE_NOT_APPROVED"),  # never activated by the owner
    (dict(is_free="NO"), "ROUTE_APPROVAL_INVALID"),          # paid: no budget path for a probe
    (dict(classes=("INTERNAL",)), "DATA_CLASS_NOT_ALLOWED"),  # route not cleared for PUBLIC data
    (dict(domains=("OFFICE",)), "ROUTE_DOMAIN"),              # OFFICE default deny
    (dict(pool="SUPERVISOR"), "ROUTE_NOT_PROBEABLE"),
    (dict(expires=utcnow() - timedelta(minutes=1)), "ROUTE_APPROVAL_INVALID"),  # expired TRIAL
])
def test_probe_is_refused_before_any_call(db, key, kw, code):
    _arena(db, **kw)
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as exc:
            broker.probe_route(db, route_key="arena.longcat-free", actor="user:owner")
        assert not mock.calls
    assert exc.value.code == code


def test_rate_limit_is_recorded_and_never_falls_back(db, key):
    _arena(db)
    _arena(db, key="arena.other-free")
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(429, headers={"retry-after": "120"},
                                                             json={"error": "quota"}))
        out = broker.probe_route(db, route_key="arena.longcat-free", actor="user:owner")
        db.commit()
        assert len(mock.calls) == 1
    assert out["outcome"] == "RATE_LIMITED" and out["retry_after_seconds"] == 120
    q = db.scalar(select(QuotaState).where(QuotaState.scope == "arena.longcat-free"))
    assert q is not None and q.state == "EXHAUSTED"


def test_unreachable_upstream_is_evidence_not_a_pass(db, key):
    _arena(db)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(side_effect=httpx.ConnectError("down"))
        out = broker.probe_route(db, route_key="arena.longcat-free", actor="user:owner")
        db.commit()
    assert out["outcome"] == "UNREACHABLE" and not out["answered"]
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert "UNREACHABLE" in ev.claim


def test_api_probe_needs_an_admin_in_the_personal_domain(admin_client, db, key):
    from aeos.core.enums import Domain

    _arena(db)
    admin_client.select_domain(Domain.PERSONAL)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        r = admin_client.post("/api/broker/routes/arena.longcat-free/feasibility")
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "REACHABLE"
        admin_client.select_domain(Domain.OFFICE)
        r = admin_client.post("/api/broker/routes/arena.longcat-free/feasibility")
        assert r.status_code == 403 and r.json()["error"]["code"] == "OFFICE_DEFAULT_DENY"
        assert len(mock.calls) == 1
