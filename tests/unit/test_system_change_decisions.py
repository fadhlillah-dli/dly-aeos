"""§41/§43 (re-audit): "Perubahan policy, model, tool schema, hooks, prompt/memory, fallback, context strategy tetap
system change dengan eval/decision/rollback". Route-level changes already needed config-hash-bound evaluation and a
rollback target; generic POLICY decisions on those targets and memory acceptance did not.

Wave 7 (W6-114/W6-120/W6-008): the cited evaluation must be evidence of *exactly this change* - any COMPLETED
all-PASS EvalRun of an unrelated suite/configuration was accepted. A run is now bound to the change by its
configuration hash (target_hash) or by the changed AIConfiguration field carrying the new value (target_version);
a stage-model pin needs a run of every pinned model id; memory acceptance needs a run whose context parameters name
the record."""

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

CFG = {"model_id": "m", "model_version": "UNKNOWN", "provider": "opencode-zen", "provider_route": "r1",
       "harness": "opencode", "harness_version": "1.19.3", "prompt_revision": "wp-2", "tools": ["fs.read", "tests.run"],
       "policy_version": "pol-2", "context_params": {"strategy": "rag-v2"}}


def _owner(db) -> str:
    return db.scalar(select(User).where(User.username == "owner")).id


def _run(db, *, status="COMPLETED", gates=None, domain=None, suite="eval.small-bugfix", **cfg) -> EvalRun:
    run = EvalRun(suite_key=suite, configuration=CFG | cfg, configuration_hash="sha256:c-" + "-".join(
                      f"{k}={v}" for k, v in sorted(cfg.items())),
                  status=status, gate_results={"security": "PASS", "authority": "PASS"} if gates is None else gates,
                  mode="LIVE", submitted_domain=domain)
    db.add(run)
    db.flush()
    return run


def _policy(db, **kw):
    base = {"actor_user_id": _owner(db), "domain": Domain.PERSONAL, "kind": "POLICY", "rationale": "ubah kebijakan",
            "auth_scope": {"scope": "platform"}, "target_type": "prompt", "target_id": "worker-prompt"}
    return recording.record_generic_decision(db, **(base | kw))


def _sc(*ids, rollback="prompt rev r41"):
    return {"system_change": {"eval_run_ids": list(ids), "rollback_target": rollback}}


def test_policy_on_a_system_change_target_needs_evaluation_and_rollback(personal_client: AuthedClient, db):
    assert {"policy", "prompt", "memory", "fallback_chain", "context_strategy", "tool_schema", "hooks"} <= \
        catalog.SYSTEM_CHANGE_TARGET_TYPES
    with pytest.raises(ValidationFailed) as exc:
        _policy(db)
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_REQUIRED"
    failing = _run(db, gates={"security": "PASS", "authority": "FAIL"})
    other_domain = _run(db, domain="OFFICE")
    for ids in ([failing.id], [other_domain.id], [_run(db, status="FAILED").id]):
        with pytest.raises(ValidationFailed) as exc:
            _policy(db, target_version="wp-2", details=_sc(*ids))
        assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_REQUIRED"
    ok = _run(db)
    with pytest.raises(ValidationFailed) as exc:  # evaluation without a rollback target
        _policy(db, target_version="wp-2", details={"system_change": {"eval_run_ids": [ok.id]}})
    assert "rollback target" in exc.value.message
    # a forged "evaluated" flag is never taken from the caller: the record is rebuilt from the runs
    d = _policy(db, target_version="wp-2", details={"system_change": {
        "eval_run_ids": [ok.id], "rollback_target": "prompt rev r41", "evaluation": "EVALUATED",
        "eval_runs": [{"id": "fake"}]}})
    sc = d.details["system_change"]
    assert sc["evaluation"] == "EVALUATED" and [r["id"] for r in sc["eval_runs"]] == [ok.id]
    assert sc["eval_runs"][0]["binding"] == "prompt_revision=wp-2"
    assert sc["rollback_target"] == "prompt rev r41"
    assert sc["bound_to"] == {"target_type": "prompt", "target_id": "worker-prompt", "target_version": "wp-2"}


def test_evaluation_of_another_configuration_is_refused_as_unrelated(personal_client: AuthedClient, db):
    """W6-114: the run must carry the changed value (or be exactly the evaluated configuration)."""
    old_prompt = _run(db, prompt_revision="wp-1")
    with pytest.raises(ValidationFailed) as exc:  # the run evaluated the previous prompt revision
        _policy(db, target_version="wp-2", details=_sc(old_prompt.id))
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED"
    assert "prompt_revision='wp-1'" in exc.value.message
    with pytest.raises(ValidationFailed) as exc:  # no new value and no hash: nothing to bind the run to
        _policy(db, details=_sc(_run(db).id))
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED" and "target_version" in exc.value.message
    with pytest.raises(ValidationFailed) as exc:  # an UNKNOWN value binds nothing (§34)
        _policy(db, target_type="model", target_id="worker", target_version="UNKNOWN", details=_sc(_run(db).id))
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED"
    bound, unrelated = _run(db), _run(db, policy_version="pol-1")
    with pytest.raises(ValidationFailed) as exc:  # one unrelated run among bound ones is still refused
        _policy(db, target_type="policy_rule", target_id="worker", target_version="pol-2",
                details=_sc(bound.id, unrelated.id))
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED" and unrelated.id in exc.value.message
    # hooks have no AIConfiguration field: bound only by the exact configuration hash
    with pytest.raises(ValidationFailed) as exc:
        _policy(db, target_type="hooks", target_id="pre-commit", target_version="h2", details=_sc(bound.id))
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED" and "target_hash" in exc.value.message
    d = _policy(db, target_type="hooks", target_id="pre-commit", target_hash=bound.configuration_hash,
                details=_sc(bound.id))
    assert d.details["system_change"]["eval_runs"][0]["binding"].startswith("configuration_hash=")
    # the other mapped fields
    tools = _policy(db, target_type="tool_set", target_id="worker", target_version="tests.run, fs.read",
                    details=_sc(bound.id))
    assert tools.details["system_change"]["eval_runs"][0]["binding"] == "tools=tests.run, fs.read"
    with pytest.raises(ValidationFailed):  # a subset of the evaluated tool set is another configuration
        _policy(db, target_type="tool_set", target_id="worker", target_version="fs.read", details=_sc(bound.id))
    ctx = _policy(db, target_type="context_strategy", target_id="worker", target_version="RAG-v2", details=_sc(bound.id))
    assert ctx.details["system_change"]["eval_runs"][0]["binding"] == "context_params=rag-v2"
    route = _policy(db, target_type="fallback_chain", target_id="worker", target_version="r1", details=_sc(bound.id))
    assert route.details["system_change"]["eval_runs"][0]["binding"] == "provider_route=r1"
    harness = _policy(db, target_type="harness", target_id="opencode", target_version="opencode 1.19.3",
                      details=_sc(bound.id))
    assert harness.details["system_change"]["evaluation"] == "EVALUATED"


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

    ok = _run(db, domain="PERSONAL", model_id="claude-opus-4-8")
    bad = _run(db, gates={"security": "FAIL"}, domain="PERSONAL", model_id="claude-opus-4-8")
    models = {"models": {"PRIMARY_PLANNING": "claude-opus-4-8"}}
    stage = {"target_type": "planning_baseline", "target_id": "stage_models"}
    with pytest.raises(ValidationFailed):  # the consumer's eval_run_id is validated, not only read later
        _policy(db, **stage, details={**models, "eval_run_id": bad.id,
                                      "system_change": {"rollback_target": "alias baseline (opus)"}})
    with pytest.raises(ValidationFailed) as exc:  # evaluated, but no rollback target
        _policy(db, **stage, details={**models, "eval_run_id": ok.id})
    assert "rollback target" in exc.value.message
    d = _policy(db, **stage, details={**models, "eval_run_id": ok.id,
                                      "system_change": {"rollback_target": "alias baseline (opus)"}})
    sc = d.details["system_change"]
    assert sc["evaluation"] == "EVALUATED" and [r["id"] for r in sc["eval_runs"]] == [ok.id]
    assert sc["eval_runs"][0]["binding"] == "model_id=claude-opus-4-8"
    db.flush()
    pinned, note = planning.configured_stage_models(db)
    assert note["decision_id"] == d.id and note["eval_run_id"] == ok.id
    assert pinned == {"PRIMARY_PLANNING": "claude-opus-4-8"} and note["eval_runs"] == {"PRIMARY_PLANNING": ok.id}


def test_stage_model_pin_needs_an_evaluation_of_exactly_each_pinned_model(personal_client: AuthedClient, db):
    """W6-008: any COMPLETED all-PASS EvalRun was accepted for a PRIMARY_PLANNING -> claude-opus-4-8 pin."""
    from aeos.planning import service as planning

    stage = {"target_type": "planning_baseline", "target_id": "stage_models"}
    other_model = _run(db, domain="PERSONAL", model_id="m")  # e.g. a worker evaluation
    with pytest.raises(ValidationFailed) as exc:
        _policy(db, **stage, details={"models": {"PRIMARY_PLANNING": "claude-opus-4-8"},
                                      **_sc(other_model.id, rollback="alias baseline (opus)")})
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED"
    assert "claude-opus-4-8" in exc.value.message
    opus = _run(db, domain="PERSONAL", model_id="claude-opus-4-8")
    with pytest.raises(ValidationFailed) as exc:  # two pinned models, only one evaluated
        _policy(db, **stage, details={"models": {"PRIMARY_PLANNING": "claude-opus-4-8",
                                                 "SYNTHESIS": "claude-opus-4-9"},
                                      **_sc(opus.id, rollback="alias baseline (opus)")})
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED" and "claude-opus-4-9" in exc.value.message
    with pytest.raises(ValidationFailed) as exc:  # a configuration hash alone never proves which model was pinned
        _policy(db, **stage, target_hash=other_model.configuration_hash,
                details={"models": {"PRIMARY_PLANNING": "claude-opus-4-8"},
                         **_sc(other_model.id, rollback="alias baseline (opus)")})
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED"
    # the consumer re-checks live: a decision recorded before the binding (or edited) with another model's run
    # is ignored, never applied
    legacy = Decision(kind="POLICY", actor_user_id=_owner(db), actor_kind="HUMAN", target_type="planning_baseline",
                      target_id="stage_models", rationale="pin", status="ACTIVE", domain="PERSONAL",
                      details={"models": {"PRIMARY_PLANNING": "claude-opus-4-8"}, "eval_run_id": other_model.id})
    db.add(legacy)
    db.flush()
    pinned, note = planning.configured_stage_models(db)
    assert pinned == {} and note["rejected"] == {"PRIMARY_PLANNING": "claude-opus-4-8"}
    assert "exactly this model" in note["rejected_reasons"]["PRIMARY_PLANNING"]


def test_governance_policy_is_stated_as_not_a_system_change(personal_client: AuthedClient, db):
    d = _policy(db, target_type="project_convention", target_id="p1")
    assert d.details["system_change"]["class"] == "NOT_A_SYSTEM_CHANGE"
    assert d.details["system_change"]["evaluation"] == "NOT_APPLICABLE"


def test_system_change_policy_through_the_api(personal_client: AuthedClient, db):
    body = {"kind": "POLICY", "target_type": "fallback_chain", "target_id": "worker", "auth_scope": {"x": "y"},
            "rationale": "fallback chain baru"}
    r = personal_client.post("/api/decisions", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "SYSTEM_CHANGE_EVIDENCE_REQUIRED"
    ok = _run(db, domain="PERSONAL", provider_route="worker.free-b")
    db.commit()
    evidence = {"details": {"system_change": {"eval_run_ids": [ok.id], "rollback_target": "fallback chain v1"}}}
    r = personal_client.post("/api/decisions", json={**body, **evidence})
    assert r.status_code == 422 and r.json()["error"]["code"] == "SYSTEM_CHANGE_EVIDENCE_UNRELATED"
    r = personal_client.post("/api/decisions", json={**body, **evidence, "target_version": "worker.free-b"})
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
    # W6-120: an evaluation that never had this record in its context is not evidence of accepting it
    unrelated = _run(db, domain="PERSONAL")
    db.commit()
    bad = personal_client.post(f"/api/memory/{second['id']}/accept", json={"eval_run_ids": [unrelated.id]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "SYSTEM_CHANGE_EVIDENCE_UNRELATED"
    ok = _run(db, domain="PERSONAL", context_params={"memory_record_ids": [second["id"]]})
    db.commit()
    acc2 = personal_client.post(f"/api/memory/{second['id']}/accept", json={"eval_run_ids": [ok.id]}).json()["memory"]
    sc2 = acc2["system_change"]
    assert sc2["evaluation"] == "EVALUATED" and sc2["rollback"]["previous_memory_id"] == first["id"]
    assert sc2["eval_runs"][0]["binding"] == f"context_params={second['id']}"
    assert sc2["rollback_target"] == f"memory_record:{first['id']}"
    pack = personal_client.post(f"/api/projects/{p['id']}/context-packs",
                                json={"include": ["memory"], "query": ["UTC"]}).json()["context_pack"]
    mem = [i for i in pack["items"] if i["kind"] == "memory"]
    assert mem and mem[0]["provenance"]["system_change_evaluation"] == "EVALUATED"


def test_facade_without_a_stated_change_binds_nothing(personal_client: AuthedClient, db):
    """Fail closed: a caller that does not state the change never gets an EVALUATED record."""
    from aeos.policy import facade

    run = _run(db)
    with pytest.raises(ValidationFailed) as exc:
        facade.system_change_evidence(db, domain="PERSONAL", eval_run_ids=[run.id], rollback_target="v1 config")
    assert exc.value.code == "SYSTEM_CHANGE_EVIDENCE_UNRELATED"
    out = facade.system_change_evidence(db, domain="PERSONAL", eval_run_ids=[run.id], rollback_target="v1 config",
                                        change={"target_type": "prompt", "target_id": "x", "target_version": "wp-2"})
    assert out["evaluation"] == "EVALUATED"


def test_cli_records_a_system_change_and_a_stage_model_pin(settings, db):
    """W6-114/W6-008: ``aeos policy decide`` had no --eval-run/--rollback-target, so every system-change POLICY
    failed closed from the CLI, and the supervisor stage-model pin was API-only."""
    from typer.testing import CliRunner

    from aeos.cli import app
    from aeos.planning import service as planning
    from tests.conftest import ADMIN_PW, create_admin

    create_admin(db)
    prompt_run = _run(db, domain="PERSONAL")
    opus = _run(db, domain="PERSONAL", model_id="claude-opus-4-8")
    db.commit()
    runner = CliRunner()
    base = ["policy", "decide", "--kind", "POLICY", "--as", "owner", "--domain", "PERSONAL",
            "--rationale", "prompt revisi baru sudah dievaluasi"]
    no_eval = runner.invoke(app, [*base, "--target-type", "prompt", "--target-id", "worker"], input=ADMIN_PW + "\n")
    assert no_eval.exit_code == 1 and "SYSTEM_CHANGE_EVIDENCE_REQUIRED" in no_eval.output
    unrelated = runner.invoke(app, [*base, "--target-type", "prompt", "--target-id", "worker", "--target-version",
                                    "wp-9", "--eval-run", prompt_run.id, "--rollback-target", "prompt wp-1"],
                              input=ADMIN_PW + "\n")
    assert unrelated.exit_code == 1 and "SYSTEM_CHANGE_EVIDENCE_UNRELATED" in unrelated.output
    assert unrelated.output.isascii()  # CLI output stays ASCII (legacy Windows consoles)
    ok = runner.invoke(app, [*base, "--target-type", "prompt", "--target-id", "worker", "--target-version", "wp-2",
                             "--eval-run", prompt_run.id, "--rollback-target", "prompt wp-1"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert ok.output.isascii() and "evaluation=EVALUATED" in ok.output and "prompt_revision=wp-2" in ok.output
    pin = runner.invoke(app, [*base, "--stage-model", "PRIMARY_PLANNING=claude-opus-4-8", "--eval-run", opus.id,
                              "--rollback-target", "alias baseline (opus)"], input=ADMIN_PW + "\n")
    assert pin.exit_code == 0, pin.output
    assert "planning_baseline:stage_models" in pin.output and "model_id=claude-opus-4-8" in pin.output
    db.expire_all()
    pinned, note = planning.configured_stage_models(db)
    assert pinned == {"PRIMARY_PLANNING": "claude-opus-4-8"} and note["eval_run_id"] == opus.id
    wrong = runner.invoke(app, [*base, "--stage-model", "PRIMARY_PLANNING", "--eval-run", opus.id,
                                "--rollback-target", "alias"], input=ADMIN_PW + "\n")
    assert wrong.exit_code != 0
