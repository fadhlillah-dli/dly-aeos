"""Capability grants (§25, §26): hashed tokens, run binding, scope/expiry/revocation, atomic caps."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from aeos.broker import facade as broker
from aeos.broker import grants
from aeos.core.db import session_factory
from aeos.core.errors import Forbidden, Unauthenticated, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent, Event
from aeos.models.routing import CapabilityGrant

SCOPE = {"models": {"m1": "worker.test-free"}, "per_request_output_cap": 512}


def _issue(db, **kw):
    args = {"run_id": "run-1", "project_id": "proj-1", "domain": "PERSONAL", "kind": "MODEL_PROXY", "scope": SCOPE,
            "ttl_seconds": 600}
    args.update(kw)
    return broker.issue_grant(db, **args)


def test_token_only_hash_stored_and_never_logged(db):
    raw, g = _issue(db)
    assert raw.startswith("aeoscap_") and len(raw) > 40
    assert g.token_hash == grants.hash_token(raw) and raw not in g.token_hash
    db.flush()
    dump = " ".join(str(r) for r in db.execute(text("SELECT * FROM capability_grants")).fetchall())
    assert raw not in dump
    for model in (AuditEvent, Event):
        for row in db.scalars(select(model)):
            assert raw not in str(getattr(row, "details", "")) + str(getattr(row, "payload", ""))
    assert redact_text(f"token {raw}") != f"token {raw}"  # registered for redaction


def test_view_never_contains_token_or_hash(db):
    raw, g = _issue(db)
    v = grants.grant_view(g)
    assert "token_hash" not in v and raw not in str(v) and g.token_hash not in str(v)


def test_authenticate_valid_wrong_kind_revoked_expired_unknown(db):
    raw, g = _issue(db)
    assert grants.authenticate(db, raw, kind="MODEL_PROXY").id == g.id
    with pytest.raises(Forbidden):
        grants.authenticate(db, raw, kind="GIT_BRANCH")
    with pytest.raises(Unauthenticated):
        grants.authenticate(db, "aeoscap_unknown", kind="MODEL_PROXY")
    with pytest.raises(Unauthenticated):
        grants.authenticate(db, None, kind="MODEL_PROXY")
    raw2, g2 = _issue(db, run_id="run-2")
    g2.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    with pytest.raises(Unauthenticated) as exc:
        grants.authenticate(db, raw2, kind="MODEL_PROXY")
    assert exc.value.code == "CAPABILITY_TOKEN_EXPIRED"
    assert broker.revoke_grants(db, run_id="run-1", reason="cancel") == 1
    with pytest.raises(Unauthenticated) as exc:
        grants.authenticate(db, raw, kind="MODEL_PROXY")
    assert exc.value.code == "CAPABILITY_TOKEN_REVOKED"


def test_scope_and_ttl_validation(db):
    with pytest.raises(ValidationFailed):
        _issue(db, scope={})
    with pytest.raises(ValidationFailed):
        _issue(db, kind="GIT_BRANCH", scope={"repo": "o/r", "branch": "main"})
    with pytest.raises(ValidationFailed):
        _issue(db, scope={"models": {"m": "r"}, "note": "sk-or-v1-" + "a" * 40})
    with pytest.raises(ValidationFailed):
        _issue(db, ttl_seconds=5)
    with pytest.raises(ValidationFailed):
        _issue(db, ttl_seconds=10 * 24 * 3600)
    with pytest.raises(ValidationFailed):
        _issue(db, kind="ADMIN")
    with pytest.raises(ValueError):
        _issue(db, domain="SOMEWHERE")
    with pytest.raises(ValidationFailed):
        broker.revoke_grants(db, reason="x")


def test_request_cap_atomic_under_concurrency(settings):
    sf = session_factory()
    with sf() as s:
        _raw, g = _issue(s, max_requests=3)
        s.commit()
        gid = g.id
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def go():
        with sf() as s:
            barrier.wait()
            ok = grants.consume_request(s, gid)
            s.commit()
            wins.append(ok)

    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert wins.count(True) == 3
    with sf() as s:
        assert s.get(CapabilityGrant, gid).used_requests == 3


def test_usage_counters(db):
    _raw, g = _issue(db, max_tokens=1000, max_cost_usd=0.5)
    grants.add_usage(db, g.id, tokens=300, cost_usd=0.1)
    grants.add_usage(db, g.id, tokens=200, cost_usd=None)
    db.refresh(g)
    rem = grants.remaining(g)
    assert rem["tokens"] == 500 and abs(rem["cost_usd"] - 0.4) < 1e-9
