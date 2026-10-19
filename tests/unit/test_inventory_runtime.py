"""Usage/liveness and OBSERVED relations from run/lease/ledger/outbox records (§36, §38).

Audit items covered (C4 inventory): §38 Usage/liveness with run/operation and time window; §36 OBSERVED lens fed
by runner/broker/provider events; §36 OBSERVED (traffic) relations distinct from DESIGN/CONFIG; §35 "Sedang
digunakan oleh task ..."; §37 consuming tasks/routes; §37 eval IDs; §36 zoom-in shows authorized runs;
§38 "model request yang selesai ditandai LAST_USED".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aeos.core.errors import ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.inventory import facade, views
from aeos.inventory.seed import run as seed_run
from aeos.models.cost import UsageLedgerEntry
from aeos.models.workforce import Lease, WorkerRun


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


def _run(db, *, status="RUNNING", lease_ttl=120, finished=False, domain="PERSONAL", task_key="T-1",
         route_key=None):
    now = utcnow()
    run = WorkerRun(project_id="p-1", domain=domain, engine="opencode", execution_status=status, task_key=task_key,
                    started_at=now - timedelta(seconds=30), model_route_key=route_key, mode="LIVE",
                    finished_at=now if finished else None)
    db.add(run)
    db.flush()
    lease = Lease(task_id="t-1", run_id=run.id, owner="ctl", acquired_at=now, expires_at=now + timedelta(
        seconds=lease_ttl), state="ACTIVE")
    db.add(lease)
    db.flush()
    run.lease_id = lease.id
    db.commit()
    return run, lease


def test_active_run_with_valid_lease_is_in_use_with_task_reference(seeded):
    run, lease = _run(seeded)
    usage = views.component_detail(seeded, "opencode", domain="PERSONAL")["lifecycle"]["usage"]
    assert usage["value"] == "IN_USE" and usage["refs"]["run_id"] == run.id and usage["refs"]["lease_id"] == lease.id
    assert "Sedang digunakan oleh task T-1" in usage["reason"] and usage["expires_at"]
    rows = {r["component_id"]: r for r in views.list_components(seeded, domain="PERSONAL", page_size=200)["items"]}
    assert rows["opencode"]["dimensions"]["usage"]["value"] == "IN_USE"
    assert rows["worker-runtime"]["dimensions"]["usage"]["value"] == "IN_USE"
    # zoom-in: the run is shown on the component it uses
    m = views.system_map(seeded, lens="OBSERVED", domain="PERSONAL")
    node = next(n for n in m["nodes"] if n["id"] == "opencode")
    assert node["present"] and node["status"] == "IN_USE" and node["runs"][0]["run_id"] == run.id
    edge = next(e for e in m["edges"] if (e["source"], e["target"], e["relation"]) ==
                ("worker-runtime", "opencode", "runs_with"))
    assert edge["status"] == "OBSERVED" and edge["lens"] == "OBSERVED" and edge["scope"]["refs"]["run_id"] == run.id
    # another domain never sees this run
    office = views.system_map(seeded, lens="OBSERVED", domain="OFFICE")
    assert all(not n.get("runs") for n in office["nodes"])


def test_expired_lease_is_stale_never_in_use_and_finished_run_is_last_used(seeded):
    run, lease = _run(seeded)
    lease.expires_at = utcnow() - timedelta(seconds=1)
    seeded.commit()
    usage = views.component_detail(seeded, "opencode", domain="PERSONAL")["lifecycle"]["usage"]
    assert usage["value"] == "STALE" and "lease" in usage["reason"]
    run.execution_status, run.finished_at = "DONE", utcnow()
    seeded.commit()
    usage = views.component_detail(seeded, "opencode", domain="PERSONAL")["lifecycle"]["usage"]
    assert usage["value"] == "LAST_USED" and usage["refs"]["run_id"] == run.id
    assert views.component_detail(seeded, "opencode", domain="PERSONAL")["evidence"]["last_used_at"]


def test_finished_model_request_is_last_used_with_an_observed_broker_edge(seeded):
    seeded.add(UsageLedgerEntry(provider="openrouter", category="WORKER_API", source="broker", run_id="r-9",
                                route_key="or-free", domain="PERSONAL", mode="LIVE"))
    seeded.commit()
    d = views.component_detail(seeded, "openrouter", domain="PERSONAL")
    usage = d["lifecycle"]["usage"]
    assert usage["value"] == "LAST_USED" and usage["refs"]["run_id"] == "r-9" and "bukan proses" in usage["reason"]
    m = views.system_map(seeded, lens="OBSERVED", domain="PERSONAL")
    edge = next(e for e in m["edges"] if (e["source"], e["target"]) == ("capability-broker", "openrouter"))
    assert edge["status"] == "OBSERVED" and edge["provenance"] == "record:usage_ledger"
    # the uninstrumented DESIGN copy of the same relation is not shown next to the observed one
    assert not [e for e in m["edges"] if (e["source"], e["target"]) == ("capability-broker", "openrouter")
                and e["status"] == "UNKNOWN"]
    # after the observed-edge TTL the edge is STALE, not green
    row = seeded.query(UsageLedgerEntry).one()
    row.at = utcnow() - views.OBSERVED_EDGE_TTL - timedelta(minutes=1)
    seeded.commit()
    m = views.system_map(seeded, lens="OBSERVED", domain="PERSONAL")
    edge = next(e for e in m["edges"] if (e["source"], e["target"]) == ("capability-broker", "openrouter"))
    assert edge["status"] == "STALE"
    # an OFFICE ledger row never reaches the PERSONAL view
    seeded.add(UsageLedgerEntry(provider="gemini", category="WORKER_API", source="broker", domain="OFFICE",
                                mode="LIVE"))
    seeded.commit()
    assert views.component_detail(seeded, "gemini-api", domain="PERSONAL")["lifecycle"]["usage"]["value"] == \
        "NEVER_OBSERVED"


def test_provider_of_an_active_run_is_never_in_use(seeded):
    from aeos.models.routing import ModelRoute

    seeded.add(ModelRoute(route_key="or-free", pool="WORKER", provider="openrouter", model_id="x:free",
                          status="APPROVED", eligible_domains=["PERSONAL"]))
    seeded.commit()
    run, _lease = _run(seeded, route_key="or-free")
    assert views.component_detail(seeded, "opencode", domain="PERSONAL")["lifecycle"]["usage"]["value"] == "IN_USE"
    assert views.component_detail(seeded, "openrouter", domain="PERSONAL")["lifecycle"]["usage"]["value"] != "IN_USE"
    seeded.add(UsageLedgerEntry(provider="openrouter", category="WORKER_API", source="broker", run_id=run.id,
                                domain="PERSONAL", mode="LIVE"))
    seeded.commit()
    usage = views.component_detail(seeded, "openrouter", domain="PERSONAL")["lifecycle"]["usage"]
    assert usage["value"] == "LAST_USED", "a finished model request is LAST_USED, not a running session"


def test_mock_records_are_not_usage(seeded):
    seeded.add(UsageLedgerEntry(provider="openrouter", category="WORKER_API", source="broker", mode="MOCK",
                                domain="PERSONAL"))
    seeded.commit()
    assert views.component_detail(seeded, "openrouter", domain="PERSONAL")["lifecycle"]["usage"]["value"] == \
        "NEVER_OBSERVED"


def test_consuming_routes_and_runs_and_eval_ids(seeded):
    from aeos.models.research import EvalRun
    from aeos.models.routing import ModelRoute

    seeded.add(ModelRoute(route_key="or-free", pool="WORKER", provider="openrouter", model_id="x/y:free",
                          status="APPROVED", eligible_domains=["PERSONAL"]))
    seeded.add(EvalRun(suite_key="suite-a", configuration={}, configuration_hash="h", route_key="or-free",
                       status="COMPLETED", mode="LIVE"))
    seeded.commit()
    _run(seeded, route_key="or-free", finished=True, status="DONE")
    d = views.component_detail(seeded, "openrouter", domain="PERSONAL")
    ct = d["usage_role"]["consuming_tasks"]
    assert ct["status"] == "KNOWN" and ct["routes"][0]["route_key"] == "or-free"
    assert ct["items"] and ct["items"][0]["route_key"] == "or-free"
    assert d["evidence"]["eval_runs"][0]["suite_key"] == "suite-a"
    assert any(c["source"].startswith("eval_run:") for c in d["evidence"]["claims"])
    na = views.component_detail(seeded, "git", domain="PERSONAL")["usage_role"]["consuming_tasks"]
    assert na["status"] == "NONE_RECORDED" and na["reason"]


def test_record_relation_and_usage_observation_facades(seeded):
    rel = facade.record_relation_observation(seeded, source_id="publisher", target_id="google-docs",
                                             relation="publishes_to", observer="publisher", run_id="job-1",
                                             domain="PERSONAL", operation="WRITE")
    seeded.commit()
    assert rel.lens == "OBSERVED" and rel.last_observed_at is not None and rel.provenance == "publisher:job-1"
    rels = views.component_relations(seeded, "publisher", domain="PERSONAL")
    assert any(r["lens"] == "OBSERVED" and r["target"] == "google-docs" for r in rels["outgoing"])
    assert not [r for r in views.component_relations(seeded, "publisher", domain="OFFICE")["outgoing"]
                if r["lens"] == "OBSERVED"], "a PERSONAL observed edge is not shown to OFFICE"
    with pytest.raises(ValidationFailed):
        facade.record_usage_observation(seeded, component_id="opencode", status="IN_USE", observer="runner")
    obs = facade.record_usage_observation(seeded, component_id="opencode", status="IN_USE", observer="runner",
                                          run_id="r-1", ttl_seconds=60, domain="PERSONAL")
    assert obs.dimension == "usage" and obs.scope["run_id"] == "r-1"
    with pytest.raises(ValidationFailed):
        facade.record_relation_observation(seeded, source_id="publisher", target_id="not-a-component",
                                           relation="publishes_to", observer="x")


def test_usage_instrumentation_gap_clears_when_records_exist(seeded):
    cov = views.coverage(seeded, domain="PERSONAL")
    assert any(g["code"] == "NO_USAGE_INSTRUMENTATION" for g in cov["gaps"])
    seeded.add(UsageLedgerEntry(provider="claude-native", category="NATIVE_CLAUDE", source="claude-bridge",
                                domain="PERSONAL", mode="LIVE"))
    seeded.commit()
    cov = views.coverage(seeded, domain="PERSONAL")
    assert not any(g["code"] == "NO_USAGE_INSTRUMENTATION" for g in cov["gaps"])
    usage = views.component_detail(seeded, "claude-native-bridge", domain="PERSONAL")["lifecycle"]["usage"]
    assert usage["value"] == "LAST_USED"
