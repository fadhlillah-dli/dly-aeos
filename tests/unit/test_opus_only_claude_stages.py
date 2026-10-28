"""ADR-0131 (owner decision 2026-10-29, R42): DLY AEOS never requests a Claude Fable model; every planning and review
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
    resolve_model,
)
from aeos.core.errors import ValidationFailed
from aeos.planning import service as planning
from aeos.planning.states import STAGES, StageSpec
from aeos.proposals import review as proposals_review
from aeos.verification import review as verification_review
from tests.unit.test_claude_bridge_parsing import FABLE_ANSWER, OPUS_OK, SCHEMA

FABLE_IDS = ("fable", "FABLE", " fable ", "claude-fable-5-1", "claude-fable-5-1[1m]", "Claude-Fable-5-1")
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


def test_an_unknown_model_is_refused_with_the_generic_message_not_the_fable_one():
    code, message = bridge.validate_request(_req("gpt-6")) or ("", "")
    assert code == "REQUEST_INVALID" and "fable" not in message.lower(), message


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


def test_a_fable_answer_fails_the_checkpoint_and_proposal_review_policy():
    problem = verification_review._model_policy_problem({}, "claude-fable-5-1",
                                                        verification_review.DEFAULT_REVIEW_MODEL)
    assert problem and "family fable" in problem and "requested family opus" in problem, problem
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
