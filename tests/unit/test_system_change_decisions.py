"""§41/§43 (re-audit): "Perubahan policy, model, tool schema, hooks, prompt/memory, fallback, context strategy tetap
system change dengan eval/decision/rollback". Route-level changes already needed config-hash-bound evaluation and a
rollback target; generic POLICY decisions on those targets and memory acceptance did not."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.models.decisions import Decision
from aeos.models.identity import User
from aeos.models.research import EvalRun
from aeos.policy import catalog, recording
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project


def _owner(db) -> str:
    return db.scalar(select(User).where(User.username == "owner")).id


def _run(db, *, status="COMPLETED", gates=None, domain=None) -> EvalRun:
    run = EvalRun(suite_key="eval.small-bugfix", configuration={"model_id": "m"}, configuration_hash="sha256:c",
                  status=status, gate_results={"security": "PASS", "authority": "PASS"} if gates is None else gates,
                  mode="LIVE", submitted_domain=domain)
    db.add(run)
    db.flush()
    return run


def _policy(db, **kw):
    base = {"actor_user_id": _owner(db), "domain": Domain.PERSONAL, "kind": "POLICY", "rationale": "ubah kebijakan",
            "auth_scope": {"scope": "platform"}, "target_type": "prompt", "target_id": "worker-prompt"}
    return recording.record_generic_decision(db, **(base | kw))


def test_policy_on_a_system_change_target_needs_evaluation_and_rollback(personal_client: AuthedClient, db):
    assert {"policy", "prompt", "memory", "fallback_chain", "context_strategy", "tool_schema", "hooks"} <= \
        catalog.SYSTEM_CHANGE_TARGET_TYPES
    with pytest.raises(ValidationFailed) as exc:
        _policy(db)
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_REQUIRED"
    failing = _run(db, gates={"security": "PASS", "authority": "FAIL"})
    other_domain = _run(db, domain="OFFICE")
    for ids in ([failing.id], [other_domain.id], [_run(db, status="FAILED").id]):
        with pytest.raises(ValidationFailed):
            _policy(db, details={"system_change": {"eval_run_ids": ids, "rollback_target": "prompt rev r41"}})
    ok = _run(db)
    with pytest.raises(ValidationFailed) as exc:  # evaluation without a rollback target
        _policy(db, details={"system_change": {"eval_run_ids": [ok.id]}})
    assert "rollback target" in exc.value.message
    # a forged "evaluated" flag is never taken from the caller: the record is rebuilt from the runs
    d = _policy(db, details={"system_change": {"eval_run_ids": [ok.id], "rollback_target": "prompt rev r41",
                                               "evaluation": "EVALUATED", "eval_runs": [{"id": "fake"}]}})
    sc = d.details["system_change"]
    assert sc["evaluation"] == "EVALUATED" and [r["id"] for r in sc["eval_runs"]] == [ok.id]
    assert sc["rollback_target"] == "prompt rev r41"


@pytest.mark.parametrize("target_type", ["Prompt", "tool-schema", "Context Strategy", "planning_baseline"])
def test_spelling_variants_and_stage_model_pins_are_system_changes(personal_client: AuthedClient, db, target_type):
    """Review fix: the system-change set was matched verbatim, so ``Tool-Schema`` bypassed the gate, and the one
    generic POLICY a consumer reads as a model change (planning_baseline/stage_models, §15) was recorded as
    NOT_A_SYSTEM_CHANGE without a rollback target."""
    with pytest.raises(ValidationFailed) as exc:
        _policy(db, target_type=target_type, target_id="stage_models")
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_REQUIRED"


def test_stage_model_pin_is_recorded_with_its_evaluation_and_rollback(personal_client: AuthedClient, db):
    from aeos.planning import service as planning

    ok = _run(db, domain="PERSONAL")
    bad = _run(db, gates={"security": "FAIL"}, domain="PERSONAL")
    models = {"details": {"models": {"PRIMARY_PLANNING": "claude-opus-4-8"}}}
    with pytest.raises(ValidationFailed):  # the consumer's eval_run_id is validated, not only read later
        _policy(db, target_type="planning_baseline", target_id="stage_models",
                details={**models["details"], "eval_run_id": bad.id,
                         "system_change": {"rollback_target": "alias baseline (opus)"}})
    with pytest.raises(ValidationFailed) as exc:  # evaluated, but no rollback target
        _policy(db, target_type="planning_baseline", target_id="stage_models",
                details={**models["details"], "eval_run_id": ok.id})
    assert "rollback target" in exc.value.message
    d = _policy(db, target_type="planning_baseline", target_id="stage_models",
                details={**models["details"], "eval_run_id": ok.id,
                         "system_change": {"rollback_target": "alias baseline (opus)"}})
    sc = d.details["system_change"]
    assert sc["evaluation"] == "EVALUATED" and [r["id"] for r in sc["eval_runs"]] == [ok.id]
    db.flush()
    pinned, note = planning.configured_stage_models(db)
    assert note["decision_id"] == d.id and note["eval_run_id"] == ok.id


def test_governance_policy_is_stated_as_not_a_system_change(personal_client: AuthedClient, db):
    d = _policy(db, target_type="project_convention", target_id="p1")
    assert d.details["system_change"]["class"] == "NOT_A_SYSTEM_CHANGE"
    assert d.details["system_change"]["evaluation"] == "NOT_APPLICABLE"


def test_system_change_policy_through_the_api(personal_client: AuthedClient, db):
    body = {"kind": "POLICY", "target_type": "fallback_chain", "target_id": "worker", "auth_scope": {"x": "y"},
            "rationale": "fallback chain baru"}
    r = personal_client.post("/api/decisions", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "SYSTEM_CHANGE_EVIDENCE_REQUIRED"
    ok = _run(db, domain="PERSONAL")
    db.commit()
    r = personal_client.post("/api/decisions", json={**body, "details": {"system_change": {
        "eval_run_ids": [ok.id], "rollback_target": "fallback chain v1"}}})
    assert r.status_code == 201, r.text
    assert db.get(Decision, r.json()["id"]).details["system_change"]["evaluation"] == "EVALUATED"


def _memory(client: AuthedClient, pid: str, content: str, **kw):
    return client.post(f"/api/projects/{pid}/memory",
                       json={"content": content, "source_type": "human_statement", **kw}).json()["memory"]


def test_memory_acceptance_records_rollback_and_evaluation_status(personal_client: AuthedClient, db):
    p = create_project(personal_client)
    first = _memory(personal_client, p["id"], "Laporan memakai zona waktu Asia/Jakarta")
    acc = personal_client.post(f"/api/memory/{first['id']}/accept", json={}).json()["memory"]
    sc = acc["system_change"]
    assert sc["class"] == "MEMORY" and sc["evaluation"] == "NOT_EVALUATED"  # stated, never implied evaluated
    assert sc["rollback"]["how"] == f"POST /api/memory/{first['id']}/expire"
    d = db.get(Decision, acc["auth_decision_id"])
    assert d.details["system_change"]["evaluation"] == "NOT_EVALUATED"
    # a superseding memory records the previous accepted record as its rollback target, and an evaluation
    second = _memory(personal_client, p["id"], "Laporan memakai zona waktu UTC", supersedes_id=first["id"])
    bad = personal_client.post(f"/api/memory/{second['id']}/accept", json={"eval_run_ids": ["missing"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "SYSTEM_CHANGE_EVIDENCE_REQUIRED"
    ok = _run(db, domain="PERSONAL")
    db.commit()
    acc2 = personal_client.post(f"/api/memory/{second['id']}/accept", json={"eval_run_ids": [ok.id]}).json()["memory"]
    sc2 = acc2["system_change"]
    assert sc2["evaluation"] == "EVALUATED" and sc2["rollback"]["previous_memory_id"] == first["id"]
    assert sc2["rollback_target"] == f"memory_record:{first['id']}"
    pack = personal_client.post(f"/api/projects/{p['id']}/context-packs",
                                json={"include": ["memory"], "query": ["UTC"]}).json()["context_pack"]
    mem = [i for i in pack["items"] if i["kind"] == "memory"]
    assert mem and mem[0]["provenance"]["system_change_evaluation"] == "EVALUATED"
