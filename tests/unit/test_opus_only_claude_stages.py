"""ADR-0131 (owner decisions 2026-10-28, R38 and R42): DLY AEOS never requests a Claude Fable model; every planning and review
stage uses Opus. Three guards, each red with its own message and each with a near miss that stays green:

(a) no planning stage, bridge policy entry, checkpoint review default or proposal review model names Fable;
(b) a bridge request for the Fable family is refused before any process starts, with its own error and message;
(c) a response whose resolved model is a Fable id fails the Opus family check of every stage (fail closed).

The parser still RECOGNISES the fable family (``parsing.FAMILIES``): that is how a Fable answer is named and then
rejected, never silently accepted.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from aeos.claude_bridge import binary as binmod
from aeos.claude_bridge import bridge
from aeos.claude_bridge.facade import ClaudeRequest
from aeos.claude_bridge.parsing import (
    ALLOWED_REQUEST_FAMILIES,
    FAMILIES,
    REFUSED_REQUEST_FAMILIES,
    family_of,
    parse_output,
    refused_family,
    resolve_model,
)
from aeos.core.errors import PolicyDenied, ValidationFailed
from aeos.planning import service as planning
from aeos.planning.states import STAGES, StageSpec
from aeos.proposals import review as proposals_review
from aeos.verification import review as verification_review
from tests.unit.test_claude_bridge_parsing import FABLE_ANSWER, OPUS_OK, SCHEMA

ZWSP, BOM, SOFT_HYPHEN = chr(0x200B), chr(0xFEFF), chr(0x00AD)  # built at run time: no invisible text in the file
FULLWIDTH_FABLE = "".join(chr(0xFF00 + ord(c) - 0x20) for c in "fable")
FABLE_IDS = ("fable", "FABLE", " fable ", "claude-fable-5-1", "claude-fable-5-1[1m]", "Claude-Fable-5-1", "fable[1m]",
             "Fable 5.1", f"fa{ZWSP}ble", f"{BOM}fable", f"claude-fa{SOFT_HYPHEN}ble-5-1", FULLWIDTH_FABLE)
# near misses that only contain the word: not the family (refused as unknown models, never as Fable)
NOT_FABLE = ("fableish", "notfable", "claude-fables-1")
PROMPT = "Rencanakan fitur kecil. Kembalikan {\"ok\": true}."


# ------------------------------------------------------------------------------------------------ (a) static


def fable_findings(*, stages: tuple[StageSpec, ...], policy: dict[str, Any], review_default: str,
                   proposal_model: str) -> list[str]:
    """Every place a Claude stage would request (or accept) the Fable family, one readable finding each."""
    out: list[str] = []
    for s in stages:
        if family_of(s.model) == "fable":
            out.append(f"planning stage {s.state.value} requests {s.model!r} (family fable)")
        if s.family == "fable":
            out.append(f"planning stage {s.state.value} accepts the fable family")
    for fam in policy.get("allowed_families") or []:
        if family_of(fam) == "fable":
            out.append(f"bridge policy allows family {fam!r}")
    for purpose, model in sorted((policy.get("stages") or {}).items()):
        if family_of(model) == "fable":
            out.append(f"bridge policy stage {purpose} requests {model!r}")
    if family_of(review_default) == "fable":
        out.append(f"checkpoint review default model {review_default!r} is in the fable family")
    if family_of(proposal_model) == "fable":
        out.append(f"proposal review model {proposal_model!r} is in the fable family")
    return out


def test_no_claude_stage_names_fable():
    findings = fable_findings(stages=STAGES, policy=bridge.SUPERVISOR_POLICY,
                              review_default=verification_review.DEFAULT_REVIEW_MODEL,
                              proposal_model=proposals_review.REVIEW_MODEL)
    assert findings == [], "ADR-0131: no Claude stage may request Fable: " + "; ".join(findings)
    assert [(s.model, s.family) for s in STAGES] == [("opus", "opus")] * 4, "every planning stage is opus/opus"
    assert set(bridge.SUPERVISOR_POLICY["stages"].values()) == {"opus"}, "every bridge policy stage is opus"


def test_fable_finder_bites_on_each_place_and_passes_opus():
    """Bite test of the static guard: each violating place is named in its own finding; opus is a near miss."""
    # built from the real stage specs, but every model/family is set here, so the fixture does not depend on them
    bad_stages = tuple(replace(s, model="fable", family="fable") if s.state.value == "CHALLENGE_REVIEW"
                       else replace(s, model="opus", family="opus") for s in STAGES)
    bad_policy = {"allowed_families": ["opus", "fable"], "stages": {"planning.final": "claude-fable-5-1",
                                                                    "planning.primary": "opus"}}
    assert fable_findings(stages=bad_stages, policy=bad_policy, review_default="fable",
                          proposal_model="claude-fable-5-1") == [
        "planning stage CHALLENGE_REVIEW requests 'fable' (family fable)",
        "planning stage CHALLENGE_REVIEW accepts the fable family",
        "bridge policy allows family 'fable'",
        "bridge policy stage planning.final requests 'claude-fable-5-1'",
        "checkpoint review default model 'fable' is in the fable family",
        "proposal review model 'claude-fable-5-1' is in the fable family",
    ]
    near = tuple(replace(s, model="claude-opus-5-5", family="opus") for s in STAGES)
    assert fable_findings(stages=near, policy={"allowed_families": ["opus"], "stages": {"planning.final": "opus"}},
                          review_default="claude-opus-5-5", proposal_model="opus") == []


def test_planning_baseline_is_opus_only_with_all_four_stages():
    base = planning.PLANNING_BASELINE
    assert base["name"] == "opus-only-4-stage", "the baseline is named for the Opus-only pipeline"
    assert "ADR-0131" in base["source"] and "§02, §15, §17" in base["source"], \
        "the baseline cites master §02/§15/§17 as amended by ADR-0131"
    assert [s["stage"] for s in base["stages"]] == ["PRIMARY_PLANNING", "CHALLENGE_REVIEW", "SYNTHESIS",
                                                     "FINAL_REVIEW"], "all four stages stay (none skipped)"
    assert {(s["model"], s["family"]) for s in base["stages"]} == {("opus", "opus")}
    assert base["skippable"] is False


def test_a_plan_recorded_with_a_fable_baseline_requests_opus():
    """A plan created before ADR-0131 keeps its baseline as history, but its next stage never requests Fable."""
    from aeos.models.planning import Plan

    old = Plan(policy_baseline={"stages": [{"stage": s.state.value, "model": "fable", "family": "fable"}
                                           for s in STAGES]})
    for spec in STAGES:
        assert planning.stage_model(old, spec) == "opus", f"{spec.state.value} must request opus, not the old fable"
    pinned = Plan(policy_baseline={"stages": [{"stage": "CHALLENGE_REVIEW", "model": "claude-opus-5-5"}]})
    challenge = next(s for s in STAGES if s.state.value == "CHALLENGE_REVIEW")
    assert planning.stage_model(pinned, challenge) == "claude-opus-5-5"  # near miss: an Opus pin is kept


def test_parser_still_recognises_fable_but_no_request_may_name_it():
    assert "fable" in FAMILIES, "the parser must keep naming a Fable answer so it can be rejected"
    assert "fable" not in ALLOWED_REQUEST_FAMILIES and "fable" in REFUSED_REQUEST_FAMILIES
    assert "opus" in ALLOWED_REQUEST_FAMILIES


# ------------------------------------------------------------------------------------------------ (b) refusal


def _req(model: str, **kw: Any) -> ClaudeRequest:
    base: dict[str, Any] = {"purpose": "planning.challenge", "role": "Planning Challenger", "model": model,
                            "prompt": PROMPT, "json_schema": SCHEMA, "domain": "PERSONAL", "allowed_tools": []}
    return ClaudeRequest(**{**base, **kw})


@pytest.mark.parametrize("model", FABLE_IDS)
def test_a_fable_request_is_refused_with_its_own_message(model):
    problem = bridge.validate_request(_req(model))
    assert problem is not None, f"a request for {model!r} must be refused"
    code, message = problem
    assert code == "MODEL_FAMILY_REFUSED", f"{model!r}: refused with {code}, not the family refusal"
    assert "never requests the Claude fable family" in message and "ADR-0131" in message, message
    assert "planning and review stages use opus" in message, message


@pytest.mark.parametrize("model", ["opus", "OPUS", "claude-opus-5-5", "claude-opus-5-5[1m]", "sonnet"])
def test_opus_and_other_allowed_requests_pass_the_family_gate(model):
    assert bridge.validate_request(_req(model)) is None, f"{model!r} is an allowed request (near miss)"


@pytest.mark.parametrize("model", ["gpt-6", *NOT_FABLE])
def test_an_unknown_model_is_refused_with_the_generic_message_not_the_fable_one(model):
    assert refused_family(model) is None, f"{model!r} only contains the word; it is not the fable family"
    code, message = bridge.validate_request(_req(model)) or ("", "")
    assert code == "REQUEST_INVALID" and "never requests" not in message, message


@pytest.mark.parametrize("model", ["fable", "claude-fable-5-1"])
def test_a_fable_request_never_starts_the_binary(settings, monkeypatch, model):
    from tests.unit.test_claude_bridge_run import _probe

    started: list[Any] = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: started.append(a))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    res = bridge.run_structured(_req(model), settings=settings)
    assert not res.ok and res.error_code == "MODEL_FAMILY_REFUSED", res.error_code
    assert "never requests the Claude fable family" in res.error, res.error
    assert res.model_requested == model and res.model_resolved is None and res.structured is None
    assert not started, "no Claude process may start for a Fable request"
    ok = bridge.validate_request(_req("opus"))
    assert ok is None  # near miss: the same request for opus passes the gate


def test_a_fable_manual_package_is_refused_too(settings, tmp_path):
    """The manual package has the same preflight as the bridge (ADR-0040 addendum): no Fable package either."""
    from aeos.claude_bridge.facade import write_manual_package

    with pytest.raises(ValidationFailed) as e:
        write_manual_package(_req("fable"), package_id="pkg-fable-refused")
    assert e.value.code == "MODEL_FAMILY_REFUSED" and "ADR-0131" in e.value.message
    assert write_manual_package(_req("opus"), package_id="pkg-opus-ok").is_dir()  # near miss


@pytest.mark.parametrize("model", ["fable", "claude-fable-5-1", "Fable 5.1"])
def test_a_fable_capability_test_starts_nothing_and_stores_nothing(settings, db, monkeypatch, model):
    """Review finding F2: the capability test probed the binary and the login and stored a FAILED row before the
    request was refused. Now the family is checked first."""
    from aeos.models.claude_bridge_ext import ClaudeCapabilityTest
    from tests.unit.test_claude_bridge_run import _probe
    from tests.unit.test_routing_fakes import install_fakes

    install_fakes(monkeypatch)
    probed: list[bool] = []
    started: list[Any] = []

    def probe(settings, refresh=False):
        probed.append(refresh)
        return _probe(found=False)

    monkeypatch.setattr(binmod, "probe", probe)
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: started.append(a))
    out = bridge.capability_test(model, actor="tester", settings=settings)
    assert out["status"] == "REFUSED" and out["error_code"] == "MODEL_FAMILY_REFUSED", out["status"]
    assert "never requests the Claude fable family" in out["error"], out["error"]
    assert probed == [], "a refused family must not probe the binary or the login"
    assert not started and out["persisted"] is False and out["id"] is None
    db.expire_all()
    assert db.query(ClaudeCapabilityTest).count() == 0, "a refused request is not a capability observation"
    near = bridge.capability_test("opus", actor="tester", settings=settings)  # near miss: opus probes and is stored
    assert probed == [True] and near["status"] == "BLOCKED" and near["id"] is not None


def test_the_capability_test_accepts_only_opus():
    from aeos.claude_bridge.router import CapabilityTestBody

    assert CapabilityTestBody().model == "opus" and CapabilityTestBody(model="opus").model == "opus"
    with pytest.raises(ValidationError) as e:
        CapabilityTestBody(model="fable")
    assert "opus" in str(e.value)


# ------------------------------------------------------------------------------------------------ (c) response


@pytest.mark.parametrize("spec", STAGES, ids=[s.state.value for s in STAGES])
def test_a_fable_answer_fails_the_family_check_of_every_stage(spec):
    assert not planning.model_matches(spec.family, "claude-fable-5-1"), \
        f"{spec.state.value}: a Fable answer must never match the stage family {spec.family!r}"
    assert not planning.model_matches(spec.family, "Claude-Fable-5-1[1m]"), f"{spec.state.value}: case/suffix"
    assert planning.model_matches(spec.family, "claude-opus-5-5"), f"{spec.state.value}: opus is the near miss"
    parsed = parse_output(json.dumps(FABLE_ANSWER), "", returncode=0, schema=SCHEMA, model_requested=spec.model)
    assert not parsed.ok and parsed.error_code == "MODEL_POLICY_MISMATCH", \
        f"{spec.state.value}: a Fable answer to an {spec.model!r} request must be a policy mismatch"
    assert parsed.model_resolved == "claude-fable-5-1" and parsed.structured is None
    near = parse_output(json.dumps(OPUS_OK), "", returncode=0, schema=SCHEMA, model_requested=spec.model)
    assert near.ok and near.model_resolved == "claude-opus-5-5"


@pytest.mark.parametrize("requested", ["fable", "claude-fable-5-1"])
def test_a_fable_answer_is_refused_even_when_the_request_said_fable(requested):
    """Review finding F1(a): a package or record written before ADR-0131 requested fable; its Fable answer matched
    the request and passed. A refused family never counts, whatever was requested."""
    parsed = parse_output(json.dumps(FABLE_ANSWER), "", returncode=0, schema=SCHEMA, model_requested=requested)
    assert not parsed.ok and parsed.error_code == "MODEL_POLICY_MISMATCH", parsed.error_code
    assert "the Claude fable family is never accepted, whatever was requested" in parsed.error, parsed.error
    res = resolve_model(requested, {"claude-fable-5-1": {"outputTokens": 10}})
    assert not res.policy_ok and "never accepted" in res.reason, res.reason
    problem = verification_review._model_policy_problem({}, "claude-fable-5-1", requested)
    assert problem and "which DLY AEOS never accepts" in problem and "request a new review" in problem, problem
    # near miss: an opus answer to an opus request passes all three
    assert parse_output(json.dumps(OPUS_OK), "", returncode=0, schema=SCHEMA, model_requested="opus").ok
    assert resolve_model("opus", {"claude-opus-5-5": {"outputTokens": 10}}).policy_ok
    assert verification_review._model_policy_problem({}, "claude-opus-5-5", "opus") is None


def test_a_fable_answer_fails_the_checkpoint_and_proposal_review_policy():
    problem = verification_review._model_policy_problem({}, "claude-fable-5-1",
                                                        verification_review.DEFAULT_REVIEW_MODEL)
    assert problem and "Claude fable family" in problem and "never accepts" in problem, problem
    assert verification_review._model_policy_problem({}, "claude-opus-5-5",
                                                     verification_review.DEFAULT_REVIEW_MODEL) is None
    usage = {"claude-fable-5-1": {"outputTokens": 10}}
    res = resolve_model(proposals_review.REVIEW_MODEL, usage)
    assert not res.policy_ok and "family fable" in res.reason and "requested family opus" in res.reason, res.reason
    assert resolve_model(proposals_review.REVIEW_MODEL, {"claude-opus-5-5": {"outputTokens": 10}}).policy_ok


# ------------------------------------------------------------------------------------------------ route seed


def test_the_fable_route_is_kept_as_history_and_cannot_be_activated(db):
    from aeos.routing import seed
    from aeos.routing import service as routing

    seed.run(db)
    db.commit()
    route = routing.get_route(db, "claude-native/fable")
    assert route.pool == "SUPERVISOR" and route.status == "CANDIDATE", "kept as a catalog row, never active"
    assert route.notes.startswith("NOT USED") and "ADR-0131" in route.notes, route.notes
    with pytest.raises(ValidationFailed) as e:
        routing.activate_route(db, route.route_key, actor_user_id="owner", mode="TRIAL",
                               rationale="try the fable supervisor", expected_config_hash=route.config_hash)
    assert "supervisor routes are governed by the planning policy" in e.value.message
    opus = routing.get_route(db, "claude-native/opus")
    assert "ADR-0131" in opus.notes and not opus.notes.startswith("NOT USED")  # near miss: the route in use


# ------------------------------------------------------------------------------------- records written before ADR-0131


@pytest.fixture
def wf(monkeypatch):
    from tests.unit.test_verification_support import FakeWorkforce

    return FakeWorkforce().install(monkeypatch)


@pytest.fixture
def world(db):
    from tests.unit.test_verification_support import make_world

    return make_world(db)


def _waiting_review(db, world, monkeypatch, *, model_requested: str):
    from tests.unit.test_verification_support import FakeBridge, install_doctor, record_verification

    install_doctor(monkeypatch, status="UNKNOWN")  # no proven bridge: the review waits for a manual result
    FakeBridge().install(monkeypatch, manual=False)
    record_verification(db, world)
    waiting = verification_review.review_run(db, world.run.id, actor="t").review
    waiting.model_requested = model_requested  # "fable": a package written before ADR-0131
    db.commit()
    return waiting


def test_a_waiting_review_requested_from_fable_is_never_imported(db, world, wf, monkeypatch):
    """Review finding F1(b): a waiting review whose stored request said fable, plus a Fable result envelope, became
    COMPLETED with a pass verdict. Now the import is refused before anything is read."""
    from tests.unit.test_verification_support import claude_envelope, review_payload

    waiting = _waiting_review(db, world, monkeypatch, model_requested="fable")
    with pytest.raises(PolicyDenied) as e:
        verification_review.collect_manual_review(
            db, waiting.id, actor="owner", structured=claude_envelope(review_payload(), model="claude-fable-5-1"))
    assert e.value.code == "MODEL_FAMILY_REFUSED", e.value.code
    assert "requested from 'fable' (Claude fable family)" in e.value.message, e.value.message
    assert f"aeos verify review {world.run.id}" in e.value.message, e.value.message
    db.refresh(waiting)
    assert waiting.status == "NEEDS_DECISION" and not wf.of("mark_accepted")


def test_a_waiting_opus_review_with_an_opus_envelope_completes(db, world, wf, monkeypatch):
    """Near miss of the refusal above: the same import for an opus review completes and is accepted."""
    from tests.unit.test_verification_support import claude_envelope, review_payload

    waiting = _waiting_review(db, world, monkeypatch, model_requested="opus")
    out = verification_review.collect_manual_review(
        db, waiting.id, actor="owner", structured=claude_envelope(review_payload(), model="claude-opus-5-5"))
    db.commit()
    assert out.review.status == "COMPLETED" and out.review.verdict == "PASS-CONTROLLED"
    assert out.acceptance and out.acceptance["acceptance_status"] == "ACCEPTED"


@pytest.mark.parametrize(("requested", "resolved", "counted"), [
    ("fable", "claude-fable-5-1", False),  # completed by Fable before the decision
    ("opus", "claude-fable-5-1", False),
    ("opus", "claude-opus-5-5", True),  # near miss
])
def test_a_review_completed_by_fable_is_not_counted_for_new_acceptance(db, world, wf, requested, resolved, counted):
    """Review finding F1(d): acceptance counted a COMPLETED review that a Fable model answered for a fable request."""
    from aeos.verification import acceptance
    from tests.unit.test_verification_support import make_review, record_verification

    report = record_verification(db, world)
    rec = make_review(db, world, report, model_resolved=resolved)
    rec.model_requested = requested
    db.commit()
    out = acceptance.evaluate_acceptance(db, world.run.id)
    refusals = [r for r in out.reasons if "not counted for acceptance" in r]
    if counted:
        assert out.acceptance_status == "ACCEPTED" and not refusals, out.reasons
    else:
        assert out.acceptance_status == "REJECTED", out.reasons
        assert refusals and "Claude fable family" in refusals[0] and "never accepts" in refusals[0], out.reasons


def test_a_planning_package_written_for_fable_is_refused_and_never_pointed_to(db, monkeypatch, tmp_path):
    """Review finding F1(c): a planning manual package written for Fable could be imported, and its instructions
    still sent the owner to a `--model fable` package. Now the import is refused before anything changes, the
    instructions say to run the plan again, and the re-run (opus) imports normally (the near miss)."""
    from aeos.models.planning import PlanningRun
    from tests.conftest import create_admin
    from tests.unit.test_planning_support import FakeBridge, make_project

    owner = create_admin(db)
    fake = FakeBridge(monkeypatch, tmp_path, blocked={"CHALLENGE_REVIEW"})
    project = make_project(db)
    plan = planning.create_plan(db, project=project, domain=project.domain, title="Rencana", attachment_ids=[],
                                prompt="Bangun aplikasi catatan pribadi dengan sinkronisasi.", actor_user_id=owner.id)
    db.commit()
    planning.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "AWAITING_MANUAL_CLAUDE"
    old = db.get(PlanningRun, plan.checkpoint["run_id"])
    old.model_requested = "fable"  # a package written before ADR-0131
    db.commit()
    fake.complete_manual(plan.checkpoint["package_id"])
    with pytest.raises(PolicyDenied) as e:
        planning.import_manual_result(db, plan.id, actor=owner.id)
    assert e.value.code == "MODEL_FAMILY_REFUSED", e.value.code
    assert "written for 'fable' (Claude fable family)" in e.value.message and "Run the plan again" in e.value.message
    db.refresh(plan)
    assert plan.state == "AWAITING_MANUAL_CLAUDE", "a refused import changes nothing"
    manual = planning.manual_instructions(plan, db)
    assert manual and manual["refused"]["code"] == "MODEL_FAMILY_REFUSED" and manual["package_path"] is None
    text = " ".join(manual["steps"])
    assert "--model fable" not in text and "Jangan jalankan paket ini" in text and "aeos plan run" in text, text
    assert planning.plan_detail(db, plan)["allowed"]["import_manual"] is False
    # near miss: running again writes a new (opus) package that imports and completes the pipeline
    planning.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "AWAITING_MANUAL_CLAUDE" and db.get(PlanningRun, old.id).status == "ABANDONED"
    new = db.get(PlanningRun, plan.checkpoint["run_id"])
    assert new.model_requested == "opus" and "steps" in (planning.manual_instructions(plan, db) or {})
    assert planning.plan_detail(db, plan)["allowed"]["import_manual"] is True
    fake.complete_manual(plan.checkpoint["package_id"])
    planning.import_manual_result(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "READY_FOR_HUMAN_REVIEW", plan.state_reason


def test_a_real_manual_package_written_for_fable_is_refused_when_read(settings, tmp_path):
    """The real package reader refuses a package whose package.json requested Fable, with or without a result."""
    from aeos.claude_bridge.facade import read_manual_result, write_manual_package

    pkg = write_manual_package(_req("opus"), package_id="pkg-old-fable")
    assert read_manual_result("pkg-old-fable") is None  # near miss: an opus package without a result yet
    meta = json.loads((pkg / "package.json").read_text(encoding="utf-8"))
    (pkg / "package.json").write_text(json.dumps({**meta, "model_requested": "fable"}), encoding="utf-8")
    res = read_manual_result("pkg-old-fable")
    assert res is not None and not res.ok and res.error_code == "MODEL_FAMILY_REFUSED", res
    assert "written for 'fable' (Claude fable family)" in res.error and "uses opus" in res.error, res.error
    (pkg / "result.json").write_text(json.dumps(FABLE_ANSWER), encoding="utf-8")
    res = read_manual_result("pkg-old-fable")
    assert res is not None and res.error_code == "MODEL_FAMILY_REFUSED" and res.structured is None
