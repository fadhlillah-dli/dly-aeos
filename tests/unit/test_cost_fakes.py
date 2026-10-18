"""Test doubles shared by module group H tests (cost, proposals, evaluation, research).

``aeos.policy.decisions`` is implemented by the policy module in parallel; tests install a minimal
in-memory-equivalent implementation that writes real ``Decision`` rows so binding/immutability
behaviour is exercised against the actual table and triggers.
"""

from __future__ import annotations

import sys
import types
from typing import Any

from sqlalchemy import select

from aeos.core.timeutil import utcnow
from aeos.models.decisions import Decision


def install_fake_policy(monkeypatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"record": [], "invalidate": []}
    mod = types.ModuleType("aeos.policy.decisions")

    def record_decision(session, *, kind, actor_user_id, target_type, target_id, target_version=None,
                        target_hash=None, allowed_operations=None, auth_scope=None, project_id=None, domain=None,
                        rationale="", expires_at=None, details=None, **_):
        d = Decision(kind=kind, actor_user_id=actor_user_id, actor_kind="HUMAN", target_type=target_type,
                     target_id=target_id, target_version=target_version, target_hash=target_hash,
                     allowed_operations=allowed_operations or [], auth_scope=auth_scope or {}, project_id=project_id,
                     domain=domain, rationale=rationale, expires_at=expires_at, details=details or {}, status="ACTIVE")
        session.add(d)
        session.flush()
        calls["record"].append(d)
        return d

    def find_active_decision(session, *, kind, target_type, target_id, target_hash=None, operation=None, **_):
        q = select(Decision).where(Decision.kind == kind, Decision.target_type == target_type,
                                   Decision.target_id == target_id, Decision.status == "ACTIVE")
        if target_hash:
            q = q.where(Decision.target_hash == target_hash)
        return session.scalars(q).first()

    def invalidate_decisions(session, *, target_type, target_id, reason, **_):
        rows = list(session.scalars(select(Decision).where(Decision.target_type == target_type,
                                                           Decision.target_id == target_id,
                                                           Decision.status == "ACTIVE")))
        for d in rows:
            d.status = "INVALIDATED"
            d.invalidated_at = utcnow()
            d.invalidation_reason = reason
        session.flush()
        calls["invalidate"].append((target_type, target_id, reason, len(rows)))
        return len(rows)

    mod.record_decision = record_decision
    mod.find_active_decision = find_active_decision
    mod.invalidate_decisions = invalidate_decisions
    monkeypatch.setitem(sys.modules, "aeos.policy.decisions", mod)
    return calls


def remove_policy(monkeypatch) -> None:
    """Make ``import aeos.policy.decisions`` fail (registry unavailable)."""
    monkeypatch.setitem(sys.modules, "aeos.policy.decisions", None)


def test_fake_policy_records_real_decision_rows(db, monkeypatch):
    calls = install_fake_policy(monkeypatch)
    from aeos.policy import facade as policy

    d = policy.record_decision(db, kind="BUDGET", actor_user_id="u1", target_type="budget", target_id="GLOBAL:*:MONTH",
                               target_hash="sha256:abc")
    assert d.id and calls["record"]
    assert policy.invalidate_decisions(db, target_type="budget", target_id="GLOBAL:*:MONTH", reason="t") == 1
    assert db.get(Decision, d.id).status == "INVALIDATED"


def test_remove_policy_makes_registry_unavailable(db, monkeypatch):
    remove_policy(monkeypatch)
    from aeos.policy import facade as policy

    try:
        policy.record_decision(db, kind="BUDGET", actor_user_id="u", target_type="budget", target_id="x")
    except ImportError:
        return
    raise AssertionError("expected ImportError when the registry is unavailable")
