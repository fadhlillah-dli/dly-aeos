"""§25 tool admission "schema": AEOS computes the hash of the tool set an engine really offers the model (from the
worker contract test's captured requests) and records it as an observation, so a schema change is drift against
the hash approved at admission - not only when someone records an observation by hand."""

from __future__ import annotations

from aeos.inventory import drift as drift_mod
from aeos.inventory import facade as inventory
from aeos.inventory.seed import run as seed_run
from aeos.models.inventory import ComponentObservation, ComponentReference
from aeos.worker_runtime import contract

READ = {"type": "function", "function": {"name": "read", "description": "Read a file",
                                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
WRITE = {"type": "function", "function": {"name": "write", "description": "Write a file",
                                          "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                                                          "content": {"type": "string"}}}}}


def test_tool_schema_hash_is_canonical_and_changes_with_the_schema():
    h = inventory.tool_schema_hash([READ, WRITE])
    assert h and h.startswith("sha256:")
    assert inventory.tool_schema_hash([WRITE, READ, READ]) == h, "order and duplicates do not matter"
    changed = {"type": "function", "function": {**READ["function"], "parameters": {"type": "object", "properties": {
        "path": {"type": "string"}, "offset": {"type": "integer"}}}}}
    assert inventory.tool_schema_hash([changed, WRITE]) != h
    mcp = [{"name": "read", "description": "Read a file", "inputSchema": READ["function"]["parameters"]}]
    assert inventory.tool_schema_hash(mcp) == inventory.tool_schema_hash([READ])
    assert inventory.tool_schema_hash([]) is None and inventory.tool_schema_hash([{"type": "function"}]) is None


def _report(tool_hash: str) -> dict:
    return {"kind": "WORKER_CONTRACT_TEST", "engine": "opencode", "engine_version": "1.17.2", "overall": "PASS",
            "platform": "win32", "pinned_version": "1.17.2", "summary": {"PASS": 1},
            "results": [{"check": "tool_schema", "status": "PASS", "evidence": "tools offered: ['read', 'write']",
                         "tool_schema_hash": tool_hash, "tools": ["read", "write"]}]}


def test_contract_test_records_the_observed_tool_schema_and_drift_compares_it(settings, db):
    seed_run(db)
    db.commit()
    approved = inventory.tool_schema_hash([READ, WRITE])
    comp = db.get(ComponentReference, "opencode")
    comp.details = {**(comp.details or {}), "tool_schema_hash": approved}
    db.commit()
    ev_id = contract.record_contract_test(db, _report(approved), actor="cli:test")
    db.commit()
    obs = db.query(ComponentObservation).filter_by(component_id="opencode", dimension="functional").one()
    assert obs.detail["tool_schema_hash"] == approved and obs.evidence_id == ev_id and obs.status == "VERIFIED"
    assert not [i for i in drift_mod.compute_drift(db, evaluated={"opencode"}) if i.key == "tool_schema"]

    widened = inventory.tool_schema_hash([READ, WRITE, {"type": "function", "function": {"name": "bash"}}])
    contract.record_contract_test(db, _report(widened), actor="cli:test")
    db.commit()
    hit = next(i for i in drift_mod.compute_drift(db, evaluated={"opencode"}) if i.key == "tool_schema")
    assert hit.detail["observed_tool_schema_hash"] == widened and hit.detail["approved_tool_schema_hash"] == approved
