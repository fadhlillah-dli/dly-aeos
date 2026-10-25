"""§08 group Recovery "config/model rollback" + §33 runbook "model rollback": the Doctor checks that every
active worker route keeps a restorable previous configuration (or a disable-route rollback), never claiming the
rollback itself was exercised."""

from __future__ import annotations

from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.runner import make_context, run_check
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from tests.unit.test_inventory_fakes import base_runner

H_OLD = "sha256:" + "1" * 64
H_NEW = "sha256:" + "2" * 64


def _check(settings, db):
    load_all()
    return run_check(REGISTRY["recovery.route_rollback"], make_context(settings, db, runner=base_runner()))


def _route(db, key, status="APPROVED"):
    r = ModelRoute(route_key=key, pool="WORKER", provider="openrouter", model_id=f"vendor/{key}", status=status,
                   config_hash=H_NEW)
    db.add(r)
    db.flush()
    return r


def _activation(db, key, *, target_hash, rollback=None, snapshot=None, kind="ROUTE_ACTIVATION", status="ACTIVE"):
    d = Decision(kind=kind, actor_user_id="owner", actor_kind="HUMAN", target_type="model_route", target_id=key,
                 target_hash=target_hash, domain="PERSONAL", status=status, rationale="owner activation",
                 details={"rollback_target": rollback, **({"config_snapshot": snapshot} if snapshot else {})})
    db.add(d)
    db.flush()
    return d


def test_no_active_route_is_not_applicable(settings, db):
    res = _check(settings, db)
    assert res.status == "NOT_APPLICABLE" and res.observed_evidence["active_routes"] == 0


def test_every_active_route_has_a_restorable_rollback(settings, db):
    _route(db, "worker-a")
    prev = _activation(db, "worker-a", target_hash=H_OLD, snapshot={"model_id": "vendor/old"}, status="SUPERSEDED")
    _activation(db, "worker-a", target_hash=H_NEW, rollback={
        "action": "RESTORE_PREVIOUS_CONFIGURATION", "decision_id": prev.id, "config_hash": H_OLD})
    _route(db, "worker-b", status="TRIAL")
    _activation(db, "worker-b", target_hash=H_NEW, kind="TRIAL", rollback={"action": "DISABLE_ROUTE"})
    db.commit()
    res = _check(settings, db)
    assert res.status == "CONFIGURED", res.summary
    assert "NOT-PROVEN" in res.summary and res.observed_evidence["rollback_exercised"] == "NOT-PROVEN"
    assert {r["route_key"]: r["restorable"] for r in res.observed_evidence["routes"]} == {"worker-a": True,
                                                                                         "worker-b": True}
    assert res.observed_evidence["runbook"] is True


def test_lost_previous_configuration_or_missing_target_is_not_ready(settings, db):
    _route(db, "worker-a")
    prev = _activation(db, "worker-a", target_hash=H_OLD, status="SUPERSEDED")  # no config snapshot kept
    _activation(db, "worker-a", target_hash=H_NEW, rollback={
        "action": "RESTORE_PREVIOUS_CONFIGURATION", "decision_id": prev.id, "config_hash": H_OLD})
    _route(db, "worker-c")  # APPROVED without any ACTIVE activation decision
    db.commit()
    res = _check(settings, db)
    assert res.status == "MISSING" and res.error_code == "ROUTE_ROLLBACK_NOT_READY"
    assert "worker-a" in res.summary and "worker-c" in res.summary
    assert res.remediation and "model-rollback" in res.remediation["source"]
