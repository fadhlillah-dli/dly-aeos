"""Fakes for the cross-module impl modules that operations/control_center call through facades.

Other modules are built in parallel; in this worktree their impl modules (``aeos.verification.
collector``, ``aeos.workforce.scheduler`` ...) do not exist. These fakes implement the frozen facade
contracts minimally so the operations behaviour can be tested; ``block_module`` simulates a module
that is absent (``import`` raises ``ModuleNotFoundError``) so fail-closed paths are tested too.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from aeos.core.hashing import content_hash
from aeos.core.redaction import redact
from aeos.core.timeutil import utcnow


def block_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import name`` fail with ModuleNotFoundError (a missing module)."""
    monkeypatch.setitem(sys.modules, name, None)


def _install(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def fake_verification(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from aeos.models.evidence import EvidenceArtifact
    from aeos.verification.facade import TRUSTED_COLLECTORS

    calls: list[Any] = []

    def collect_evidence(session, *, kind, origin, collector_identity, claim, content=None, evidence_level="E0",
                         subject_type="", subject_id="", project_id=None, domain=None, run_id=None, task_id=None,
                         environment=None, classification="INTERNAL", mode="LIVE", **_facade_extra):
        ev = EvidenceArtifact(kind=kind, origin=origin, collector_identity=collector_identity,
                              trusted=origin in TRUSTED_COLLECTORS, claim=claim, content_hash=content_hash(content),
                              inline=redact(content) if isinstance(content, dict) else {},
                              evidence_level=evidence_level, subject_type=subject_type, subject_id=subject_id,
                              project_id=project_id, domain=domain, run_id=run_id, task_id=task_id,
                              environment=environment or {}, classification=classification, mode=mode)
        session.add(ev)
        session.flush()
        calls.append(ev)
        return ev

    _install(monkeypatch, "aeos.verification.collector", collect_evidence=collect_evidence)
    return calls


class FakeWorkforce:
    def __init__(self) -> None:
        self.paused = False
        self.cancelled: list[str] = []
        self.fail_runs: set[str] = set()

    def set_scheduling_paused(self, session, paused, *, actor, reason=""):
        self.paused = paused

    def scheduling_paused(self, session):
        return self.paused

    def cancel_run(self, session, run_id, *, reason, actor):
        from aeos.models.workforce import WorkerRun

        if run_id in self.fail_runs:
            raise RuntimeError("kill failed")
        run = session.get(WorkerRun, run_id)
        run.execution_status = "CANCELLED"
        self.cancelled.append(run_id)
        return run


def fake_workforce(monkeypatch: pytest.MonkeyPatch) -> FakeWorkforce:
    fw = FakeWorkforce()
    _install(monkeypatch, "aeos.workforce.scheduler", set_scheduling_paused=fw.set_scheduling_paused,
             scheduling_paused=fw.scheduling_paused, cancel_run=fw.cancel_run)
    return fw


def fake_broker(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def revoke_grants(session, *, run_id=None, all_active=False, reason):
        from aeos.models.routing import CapabilityGrant

        n = 0
        for g in session.query(CapabilityGrant).filter(CapabilityGrant.active.is_(True)):
            g.active = False
            g.revoked_at = utcnow()
            g.revoked_reason = reason[:128]
            n += 1
        calls.append({"all_active": all_active, "reason": reason, "revoked": n})
        return n

    _install(monkeypatch, "aeos.broker.grants", revoke_grants=revoke_grants)
    return calls


def fake_policy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    from aeos.models.decisions import Decision

    log: dict[str, list[Any]] = {"recorded": [], "invalidated": []}

    def record_decision(session, *, kind, actor_user_id, target_type, target_id, target_version=None,
                        target_hash=None, allowed_operations=None, auth_scope=None, project_id=None, domain=None,
                        rationale="", expires_at=None, details=None, **_facade_extra):
        d = Decision(kind=kind, actor_user_id=actor_user_id, target_type=target_type, target_id=target_id,
                     target_version=target_version, target_hash=target_hash,
                     allowed_operations=allowed_operations or [], auth_scope=auth_scope or {}, project_id=project_id,
                     domain=domain, rationale=rationale, expires_at=expires_at, details=details or {})
        session.add(d)
        session.flush()
        log["recorded"].append(d)
        return d

    def find_active_decision(session, **kw):
        return None

    def invalidate_decisions(session, *, target_type, target_id, reason, **_facade_extra):
        log["invalidated"].append((target_type, target_id, reason))
        return 1

    _install(monkeypatch, "aeos.policy.decisions", record_decision=record_decision,
             find_active_decision=find_active_decision, invalidate_decisions=invalidate_decisions)
    return log


def fake_doctor(monkeypatch: pytest.MonkeyPatch, states: dict[str, tuple[str, list[dict[str, Any]]]]) -> list[str]:
    from aeos.doctor.facade import CapabilityState

    invalidations: list[str] = []

    def capability_state(session, capability, *, domain=None, project_id=None):
        status, checks = states.get(capability.value, ("UNKNOWN", []))
        return CapabilityState(capability=capability, status=status, reasons=[] if status == "READY" else ["x"],
                               checks=checks)

    def invalidate(session, *, invalidator, reason=""):
        invalidations.append(invalidator)
        return 0

    _install(monkeypatch, "aeos.doctor.capabilities", capability_state=capability_state, invalidate=invalidate)
    return invalidations


def fake_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    from aeos.models.planning import Plan, PlanVersion

    def get_approved_version(session, plan_id):
        plan = session.get(Plan, plan_id)
        return session.get(PlanVersion, plan.approved_version_id) if plan and plan.approved_version_id else None

    _install(monkeypatch, "aeos.planning.service", get_approved_version=get_approved_version)


def fake_queue(monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]]) -> None:
    def queue_items(session, *, domain=None, project_id=None):
        return list(items)

    _install(monkeypatch, "aeos.policy.queue", queue_items=queue_items)


def test_block_module_makes_import_fail(monkeypatch):
    block_module(monkeypatch, "aeos.verification.collector")
    with pytest.raises(ImportError):
        import aeos.verification.collector  # noqa: F401
