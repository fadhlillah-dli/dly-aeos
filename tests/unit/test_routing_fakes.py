"""Test doubles for modules built in parallel (policy, cost, verification, doctor, inventory) and an
in-memory OS keyring. Installed per test via ``monkeypatch.setitem(sys.modules, ...)`` so the real
facades' lazy imports resolve to these fakes; nothing leaks between tests."""

from __future__ import annotations

import sys
import types
from datetime import datetime
from typing import Any

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from sqlalchemy import select

from aeos.core.timeutil import utcnow
from aeos.models.decisions import Decision

# ADR-0041 amendment 2026-10-25: a seeded free OpenCode Zen route carries the S07 client restriction ("free-tier chat
# models require OpenCode") and is activated only with the owner's explicit client-terms decision.
OWNER_CLIENT_TERMS = {"accepted": True, "note": "read the S07 client rule; owner accepts broker proxying for this pilot"}


def client_terms_for(route: Any) -> dict[str, Any] | None:
    """What the owner passes at activation when *route* carries an undecided provider client restriction."""
    from aeos.routing import service

    cr = service.client_restriction(route)
    return dict(OWNER_CLIENT_TERMS) if cr and cr.get("owner_decision") != service.CLIENT_TERMS_ACCEPTED else None


def accept_client_terms(route: Any) -> None:
    """For helpers that record a decision directly (not through activate_route): the data terms an owner client-terms
    decision leaves on the route. Call before computing the hash the decision binds to."""
    from aeos.routing import service

    cr = service.client_restriction(route)
    if cr and cr.get("owner_decision") != service.CLIENT_TERMS_ACCEPTED:
        route.data_terms = {**(route.data_terms or {}), "client_restriction": {
            **cr, "owner_decision": service.CLIENT_TERMS_ACCEPTED, "owner_note": OWNER_CLIENT_TERMS["note"]}}


class FakeRegistry:
    def __init__(self) -> None:
        self.office: dict[str, Any] = {"external_worker_allowed": False, "approved_worker_routes": [],
                                       "claude_max_allowed_for_office_data": False}
        self.usage: list[dict[str, Any]] = []
        self.quota: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.invalidations: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.invalidated_decisions: list[tuple[str, str]] = []


def _decisions_module(reg: FakeRegistry) -> types.ModuleType:
    mod = types.ModuleType("aeos.policy.decisions")

    # NOTE: the frozen facades call ``impl(**locals())``, which also passes ``impl`` itself; the
    # fakes accept and ignore it (reported as a shared change request).
    def record_decision(session, *, kind, actor_user_id, target_type, target_id, target_version=None,
                        target_hash=None, allowed_operations=None, auth_scope=None, project_id=None, domain=None,
                        rationale="", expires_at=None, details=None, **_ignored):
        d = Decision(kind=kind, actor_user_id=actor_user_id, actor_kind="HUMAN", target_type=target_type,
                     target_id=target_id, target_version=target_version, target_hash=target_hash,
                     allowed_operations=allowed_operations or [], auth_scope=auth_scope or {}, project_id=project_id,
                     domain=domain, rationale=rationale, expires_at=expires_at, details=details or {})
        session.add(d)
        session.flush()
        return d

    def find_active_decision(session, *, kind, target_type, target_id, target_hash=None, operation=None, **_ignored):
        q = select(Decision).where(Decision.kind == kind, Decision.target_type == target_type,
                                   Decision.target_id == target_id, Decision.status == "ACTIVE")
        for d in session.scalars(q.order_by(Decision.created_at.desc())):
            if target_hash is not None and d.target_hash != target_hash:
                continue
            if d.expires_at is not None and d.expires_at <= utcnow():
                continue
            return d
        return None

    def invalidate_decisions(session, *, target_type, target_id, reason, **_ignored):
        n = 0
        for d in session.scalars(select(Decision).where(Decision.target_type == target_type,
                                                        Decision.target_id == target_id, Decision.status == "ACTIVE")):
            d.status = "INVALIDATED"
            d.invalidation_reason = reason
            n += 1
        reg.invalidated_decisions.append((target_id, reason))
        session.flush()
        return n

    mod.record_decision = record_decision
    mod.find_active_decision = find_active_decision
    mod.invalidate_decisions = invalidate_decisions
    return mod


_DROP = {"session", "impl"}


def _simple(name: str, **fns: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in fns.items():
        setattr(mod, k, v)
    return mod


def install_fakes(monkeypatch: pytest.MonkeyPatch, *, policy: bool = True, cost: bool = True,
                  verification: bool = True, doctor: bool = True, inventory: bool = True) -> FakeRegistry:
    reg = FakeRegistry()
    if policy:
        monkeypatch.setitem(sys.modules, "aeos.policy.decisions", _decisions_module(reg))
        monkeypatch.setitem(sys.modules, "aeos.policy.office",
                            _simple("aeos.policy.office", office_policy=lambda session: dict(reg.office),
                                    # no company authorisation: office routes stay unauthorised (fail closed)
                                    company_authorisation=lambda session: None))
    if cost:
        from aeos.cost import service as real_cost  # quota states use the real implementation (recorded)

        def record_usage(**kw):
            reg.usage.append({k: v for k, v in kw.items() if k not in _DROP})
            return types.SimpleNamespace(id="usage-1", **{k: v for k, v in kw.items() if k not in _DROP})

        def set_quota_state(session, **kw):
            reg.quota.append(dict(kw))
            return real_cost.set_quota_state(session, **kw)

        monkeypatch.setitem(sys.modules, "aeos.cost.service", _simple("aeos.cost.service", record_usage=record_usage,
                                                                      set_quota_state=set_quota_state))
    if verification:
        def collect_evidence(**kw):
            reg.evidence.append({k: v for k, v in kw.items() if k not in _DROP})
            return types.SimpleNamespace(id=f"evidence-{len(reg.evidence)}")
        monkeypatch.setitem(sys.modules, "aeos.verification.collector",
                            _simple("aeos.verification.collector", collect_evidence=collect_evidence))
    if doctor:
        # the secret-reference scope rule (which names are private to a domain/project) stays the real one
        from aeos.doctor.capabilities import _private_owner

        def invalidate(session, *, invalidator, reason=""):
            reg.invalidations.append({"invalidator": invalidator, "reason": reason})
            return 1
        monkeypatch.setitem(sys.modules, "aeos.doctor.capabilities",
                            _simple("aeos.doctor.capabilities", invalidate=invalidate, _private_owner=_private_owner))
    if inventory:
        def record_observation(**kw):
            reg.observations.append({k: v for k, v in kw.items() if k not in _DROP})
            return types.SimpleNamespace(id="obs-1")
        monkeypatch.setitem(sys.modules, "aeos.inventory.observations",
                            _simple("aeos.inventory.observations", record_observation=record_observation))
    return reg


def human_decision(db, *, kind: str, target_type: str, target_id: str, target_hash: str | None = None,
                   domain: str | None = "PERSONAL", details: dict | None = None, auth_scope: dict | None = None,
                   expires_at: datetime | None = None, actor_kind: str = "HUMAN",
                   actor_user_id: str = "owner-id") -> Decision:
    d = Decision(kind=kind, actor_user_id=actor_user_id, actor_kind=actor_kind, target_type=target_type,
                 target_id=target_id, target_hash=target_hash, domain=domain, details=details or {},
                 auth_scope=auth_scope or {}, expires_at=expires_at, rationale="test decision")
    db.add(d)
    db.flush()
    return d


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise PasswordDeleteError("missing")
        del self.store[(service, username)]


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    try:
        yield kr
    finally:
        keyring.set_keyring(previous)


def test_fake_policy_module_roundtrip(db, monkeypatch):
    """The fake decision registry behaves like the frozen policy facade contract."""
    install_fakes(monkeypatch)
    from aeos.policy.facade import find_active_decision, record_decision

    d = record_decision(db, kind="TRIAL", actor_user_id="u", target_type="model_route", target_id="r", target_hash="h")
    assert find_active_decision(db, kind="TRIAL", target_type="model_route", target_id="r", target_hash="h").id == d.id
    assert find_active_decision(db, kind="TRIAL", target_type="model_route", target_id="r", target_hash="x") is None
