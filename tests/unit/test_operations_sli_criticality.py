"""SLI targets per criticality, measured samples and error budget (master §33 "SLI/SLO ditentukan sesuai
criticality", §46 Google SRE "error budget", §13 Metric) and the outcome SLI."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.evidence import AcceptanceRecord
from aeos.models.operations_ext import OpsAlert, OpsSliSample
from aeos.operations import sli
from tests.unit.test_operations_fakes import fake_workforce
from tests.unit.test_operations_sli import _by_key, make_task


def test_same_sli_has_different_targets_per_criticality(settings, db):
    hi, _, _ = make_task(db, Domain.PERSONAL, "payments", "QUEUED", age_seconds=600)
    lo, _, _ = make_task(db, Domain.PERSONAL, "wiki", "QUEUED", age_seconds=600)
    sli.set_criticality(db, hi.id, "HIGH", actor_type="cli", actor_id="owner", rationale="money")
    sli.set_criticality(db, lo.id, "LOW", actor_type="cli", actor_id="owner", rationale="internal notes")
    sli.set_target(db, "queue_age_seconds", maximum=300, minimum=None, actor_type="cli", actor_id="owner",
                   criticality="HIGH")
    sli.set_target(db, "queue_age_seconds", maximum=3600, minimum=None, actor_type="cli", actor_id="owner",
                   criticality="LOW")
    db.commit()
    item = _by_key(sli.compute(db, domain=Domain.PERSONAL, settings=settings))["queue_age_seconds"]
    assert item["by_criticality"]["HIGH"]["status"] == "BREACH" and item["by_criticality"]["HIGH"]["projects"] == 1
    assert item["by_criticality"]["LOW"]["status"] == "WITHIN_TARGET"
    assert item["status"] == "BREACH" and item["target"] is None  # no global target: still NO generic number
    with pytest.raises(ValidationFailed):
        sli.set_target(db, "budget_drift_usd", maximum=1, minimum=None, actor_type="cli", actor_id="o",
                       criticality="HIGH")  # not measured per project
    with pytest.raises(ValidationFailed):
        sli.set_criticality(db, hi.id, "CRITICAL!", actor_type="cli", actor_id="o")


def test_outcome_sli_counts_accepted_outcomes_per_domain(settings, db):
    project, spec, _ = make_task(db, Domain.PERSONAL, "alpha", "DONE")
    office, ospec, _ = make_task(db, Domain.OFFICE, "omega", "DONE")
    db.add(AcceptanceRecord(task_id=spec.id, run_id="r1", project_id=project.id, artifact_hash="h",
                            acceptance_status="ACCEPTED"))
    db.add(AcceptanceRecord(task_id=ospec.id, run_id="r2", project_id=office.id, artifact_hash="h",
                            acceptance_status="ACCEPTED"))
    db.commit()
    assert _by_key(sli.compute(db, domain=Domain.PERSONAL, settings=settings))["accepted_outcomes"]["value"] == 1
    assert _by_key(sli.compute(db, domain=Domain.OFFICE, settings=settings))["accepted_outcomes"]["value"] == 1


def test_error_budget_exhaustion_alerts_pauses_and_recovery_lifts(settings, db, monkeypatch):
    fw = fake_workforce(monkeypatch)
    from aeos.workforce import scheduler

    monkeypatch.setattr(scheduler, "pause_info", lambda s: {"paused": fw.paused, "by": fw.by}, raising=False)
    fw.by = None

    def set_paused(session, paused, *, actor, reason=""):
        fw.paused, fw.by = paused, actor

    monkeypatch.setattr(scheduler, "set_scheduling_paused", set_paused)
    assert sli.error_budget(db, domain=Domain.PERSONAL)["status"] == "NO_POLICY"
    make_task(db, Domain.PERSONAL, "alpha", "QUEUED", age_seconds=900)
    sli.set_target(db, "queue_age_seconds", maximum=60, minimum=None, actor_type="cli", actor_id="owner")
    sli.set_error_budget_policy(db, window_days=28, objective=0.9, action="pause_scheduling", actor_type="cli",
                                actor_id="owner", rationale="pause features when queue SLO burns")
    db.commit()
    result = sli.evaluate(db, domain=Domain.PERSONAL, settings=settings)
    db.commit()
    assert db.scalar(select(func.count()).select_from(OpsSliSample)) >= len(sli.SLI_DEFS)
    budget = {b["key"]: b for b in result["error_budget"]["items"]}
    q = budget["queue_age_seconds"]
    assert q["status"] == "EXHAUSTED" and q["bad"] == 1 and q["samples"] == 1 and q["burn_rate"] == 10.0
    assert result["error_budget_action"]["changed"] == "paused" and fw.paused is True and fw.by == "ops.error_budget"
    alert = db.scalar(select(OpsAlert).where(OpsAlert.kind == "error_budget_exhausted"))
    assert alert is not None and alert.status == "OPEN" and alert.severity == "HIGH"
    # the queue drains and enough good evaluations accrue: the budget recovers and our pause is lifted
    for te in db.scalars(select(sli.TaskExecution)):
        te.state = "DONE"
    db.commit()
    for _ in range(10):
        result = sli.evaluate(db, domain=Domain.PERSONAL, settings=settings)
        db.commit()
    assert {b["key"]: b for b in result["error_budget"]["items"]}["queue_age_seconds"]["status"] == "OK"
    assert fw.paused is False
    db.refresh(alert)
    assert alert.status == "RESOLVED"


def test_error_budget_never_lifts_a_pause_it_did_not_set(settings, db, monkeypatch):
    fw = fake_workforce(monkeypatch)
    from aeos.workforce import scheduler

    fw.paused = True  # e.g. an emergency stop or a human pause
    monkeypatch.setattr(scheduler, "pause_info", lambda s: {"paused": True, "by": "user:owner"}, raising=False)
    db.add(OpsSliSample(at=utcnow() - timedelta(hours=1), sli_key="queue_age_seconds", scope="PERSONAL",
                        value=1.0, status="WITHIN_TARGET"))
    sli.set_error_budget_policy(db, window_days=7, objective=0.99, action="pause_scheduling", actor_type="cli",
                                actor_id="owner")
    from aeos.models.identity import SystemSetting

    db.add(SystemSetting(key=sli.ERROR_BUDGET_PAUSE_KEY, value={"paused_at": utcnow().isoformat(), "keys": ["x"]}))
    db.commit()
    out = sli.apply_error_budget_policy(db, sli.error_budget(db, domain=Domain.PERSONAL))
    assert out["changed"] is None and fw.paused is True
