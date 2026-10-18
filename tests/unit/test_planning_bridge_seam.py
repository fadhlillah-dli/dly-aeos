"""Planning -> ClaudeNativeBridge -> Cost seam with the REAL bridge (scripted fake binary) and the REAL cost
module (§12, §15, §42, §50 "Fable Max quota habis", "Model alias/provider route berubah", "Submit prompt
tanpa domain"; ADR-0030/0040 amendments 2026-10-18)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from aeos.claude_bridge import bridge
from aeos.claude_bridge import facade as bridge_facade
from aeos.core.enums import Domain
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.planning import PlanningContext, PlanningRun
from aeos.models.portfolio import RepositoryReference
from aeos.planning import contracts, service
from tests.conftest import create_admin
from tests.e2e.fakes import FakeClaude
from tests.unit.test_planning_support import (
    FakeBridge,
    challenge_output,
    final_output,
    make_project,
    sample_content,
    synthesis_output,
)

PROMPT = "Rencanakan fitur pencatatan tugas harian dengan login lokal dan laporan mingguan."


@pytest.fixture
def owner(db):
    return create_admin(db)


@pytest.fixture
def claude(monkeypatch, tmp_path):
    fake = FakeClaude(monkeypatch, tmp_path, outputs={
        "PRIMARY_PLANNING": sample_content(), "CHALLENGE_REVIEW": challenge_output(),
        "SYNTHESIS": synthesis_output(), "FINAL_REVIEW": final_output()})
    requests: list = []
    real = bridge_facade.run_structured

    def spy(request):
        requests.append(request)
        return real(request)

    monkeypatch.setattr(bridge_facade, "run_structured", spy)
    fake.requests = requests  # type: ignore[attr-defined]
    return fake


def _plan(db, owner, project=None):
    project = project or make_project(db)
    plan = service.create_plan(db, project=project, domain=project.domain, title="Rencana", prompt=PROMPT,
                               attachment_ids=[], actor_user_id=owner.id)
    db.commit()
    return plan


def test_every_request_carries_domain_and_project_and_usage_is_native(db, owner, claude, settings):
    plan = _plan(db, owner)
    service.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "READY_FOR_HUMAN_REVIEW", plan.state_reason
    assert [r.purpose for r in claude.requests] == ["planning.primary", "planning.challenge", "planning.synthesis",
                                                    "planning.final"]
    for r in claude.requests:
        assert (r.domain, r.project_id, r.context_files, r.allowed_tools) == ("PERSONAL", plan.project_id, [], [])
    usage = list(db.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.project_id == plan.project_id)))
    assert len(usage) == 4 and {u.category for u in usage} == {"NATIVE_CLAUDE"}
    assert all(u.domain == "PERSONAL" and u.cost_usd is None and u.details["billing"] == "SUBSCRIPTION"
               for u in usage)
    assert {u.route_key for u in usage} == {"claude-native/opus", "claude-native/fable"}
    for run in db.scalars(select(PlanningRun).where(PlanningRun.plan_id == plan.id)):
        trace = (settings.data_dir / run.trace_ref).read_text(encoding="utf-8")
        assert "costUSD" not in trace and "total_cost_usd" not in trace  # §42: never dollars


def test_quota_exhaustion_checkpoints_waits_and_resumes_the_same_stage(db, owner, claude):
    claude.set_override("CHALLENGE_REVIEW", quota=True)
    plan = _plan(db, owner)
    service.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "NEEDS_DECISION" and plan.checkpoint["stage"] == "CHALLENGE_REVIEW"
    assert plan.checkpoint["hard_stop"]["code"] == "QUOTA_EXHAUSTED"
    assert [c["stage"] for c in claude.calls()] == ["PRIMARY_PLANNING", "CHALLENGE_REVIEW"]  # no retry, no skip
    q = db.scalar(select(QuotaState).where(QuotaState.provider == "claude-native", QuotaState.scope == "fable"))
    assert q is not None and q.state == "EXHAUSTED" and q.source == "observed-limit-message"  # via cost facade
    claude.set_override("CHALLENGE_REVIEW")
    service.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    calls = claude.calls()
    assert calls[2]["stage"] == "CHALLENGE_REVIEW" and calls[2]["model"] == "fable"  # same stage, same model
    assert plan.state == "READY_FOR_HUMAN_REVIEW"
    db.expire_all()
    q = db.scalar(select(QuotaState).where(QuotaState.provider == "claude-native", QuotaState.scope == "fable"))
    assert q.state == "UNKNOWN" and q.source == "observed-successful-call"  # never "OK"


def test_off_policy_model_from_the_bridge_is_a_hard_stop(db, owner, claude):
    claude.set_override("FINAL_REVIEW", resolved="claude-opus-4-8")  # §12: fable answered by another model
    plan = _plan(db, owner)
    service.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "NEEDS_DECISION" and plan.checkpoint["stage"] == "FINAL_REVIEW"
    assert plan.checkpoint["hard_stop"]["code"] == "MODEL_POLICY_MISMATCH"
    run = db.get(PlanningRun, plan.checkpoint["run_id"])
    assert run.status == "MODEL_MISMATCH" and run.model_resolved == "claude-opus-4-8"
    assert [c["model"] for c in claude.calls()].count("fable") == 2  # challenge + one final; no retry
    assert "verdict" not in plan.checkpoint  # the off-policy verdict was never accepted


@pytest.mark.parametrize("code", ["DOMAIN_MISMATCH", "DOMAIN_REQUIRED", "PROJECT_NOT_FOUND", "CONTEXT_DOMAIN_DENIED"])
def test_domain_refusals_are_hard_stops(db, owner, monkeypatch, tmp_path, code):
    fake = FakeBridge(monkeypatch, tmp_path)

    def refuse(req):
        fake.calls.append(req)
        return bridge_facade.ClaudeResult(ok=False, structured=None, text="", model_requested=req.model,
                                          model_resolved=None, engine_version=None, session_id=None, usage={},
                                          duration_ms=None, error_code=code, error=f"{code} (test)")

    monkeypatch.setattr(bridge_facade, "run_structured", refuse)
    plan = _plan(db, owner)
    service.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "NEEDS_DECISION" and plan.checkpoint["hard_stop"]["code"] == code
    assert len(fake.calls) == 1 and not fake.packages  # no manual package as a way around the refusal


def test_real_bridge_refuses_a_mismatched_domain_without_starting_the_binary(db, claude):
    project = make_project(db)
    res = bridge.run_structured(bridge_facade.ClaudeRequest(
        purpose="planning.primary", role="Primary Planner", model="opus", prompt=PROMPT,
        json_schema=contracts.schema("plan_content"), project_id=project.id, domain=Domain.OFFICE.value))
    assert not res.ok and res.error_code in ("DOMAIN_MISMATCH", "OFFICE_POLICY_DENIED")
    res = bridge.run_structured(bridge_facade.ClaudeRequest(
        purpose="planning.primary", role="Primary Planner", model="opus", prompt=PROMPT,
        json_schema=contracts.schema("plan_content"), project_id=project.id, domain=None))
    assert not res.ok and res.error_code == "DOMAIN_REQUIRED"
    assert claude.calls() == []


def test_planning_contract_schemas_fit_the_windows_command_line():
    for name in ("plan_content", "challenge_findings", "synthesis", "final_review"):
        schema_json = json.dumps(contracts.schema(name), separators=(",", ":"), ensure_ascii=False)
        argv = bridge.build_argv("C:/Program Files/Claude/claude.exe", schema_json=schema_json, model="opus",
                                 tools=[], effort=None)
        assert bridge.cmdline_length(argv) < bridge.MAX_CMDLINE_CHARS - 512, name


def _property_paths(schema, path=""):
    out = set()
    if isinstance(schema, dict):
        for name, sub in (schema.get("properties") or {}).items():
            out.add(f"{path}/{name}")
            out |= _property_paths(sub, f"{path}/{name}")
        for key in ("items", "additionalProperties"):
            if isinstance(schema.get(key), dict):
                out |= _property_paths(schema[key], f"{path}[{key}]")
        for key in ("allOf", "anyOf", "oneOf"):
            for i, sub in enumerate(schema.get(key) or []):
                out |= _property_paths(sub, f"{path}<{key}{i}>")
    return out


@pytest.mark.parametrize("name", ["plan_content", "challenge_findings", "synthesis", "final_review"])
def test_annotation_stripping_keeps_property_names_and_validation(name):
    """The fallback for an over-long command line drops only annotations: properties *named* title/description
    (plan_content has eleven) stay defined, so the stripped schema accepts/rejects the same documents."""
    from aeos.claude_bridge.parsing import schema_errors

    full = contracts.schema(name)
    stripped = bridge._strip_annotations(full)
    assert _property_paths(stripped) == _property_paths(full)
    assert len(json.dumps(stripped)) < len(json.dumps(full))
    sample = {"plan_content": sample_content(), "challenge_findings": challenge_output(),
              "synthesis": synthesis_output(), "final_review": final_output()}[name]
    assert schema_errors(full, sample) == [] and schema_errors(stripped, sample) == []
    broken = json.loads(json.dumps(sample))
    if name == "plan_content":
        broken["requirements"][0].pop("title")
    elif name == "synthesis":
        broken["content"]["requirements"][0].pop("title")
    else:
        broken["findings" if name == "final_review" else "risks"] = [{"severity": "HIGH"}]
    assert schema_errors(full, broken) and schema_errors(stripped, broken)  # still refused without annotations


def test_schema_over_the_command_line_limit_is_sent_stripped_and_still_valid(db, claude, monkeypatch):
    """When the full schema does not fit the Windows command line, the bridge sends the stripped schema; the
    official client (here the fake) still receives every property, and the output is validated in full."""
    project = make_project(db)
    full = contracts.schema("plan_content")
    full_len = bridge.cmdline_length(bridge.build_argv(
        "claude", schema_json=json.dumps(full, separators=(",", ":"), ensure_ascii=False), model="opus", tools=[],
        effort=None))
    monkeypatch.setattr(bridge, "MAX_CMDLINE_CHARS", full_len)  # full schema no longer fits (512 margin)
    res = bridge.run_structured(bridge_facade.ClaudeRequest(
        purpose="planning.primary", role="Primary Planner", model="opus", prompt=PROMPT, json_schema=full,
        project_id=project.id, domain=Domain.PERSONAL.value))
    assert res.ok, (res.error_code, res.error)
    argv = claude.calls()[0]["argv"]
    sent = json.loads(argv[argv.index("--json-schema") + 1])
    assert {"title", "description"} <= set(full) and not {"title", "description"} & set(sent)  # annotations dropped
    assert _property_paths(sent) == _property_paths(full)  # ... but no property definition
    assert "title" in sent["properties"]["requirements"]["items"]["properties"]  # a property named "title"


def test_existing_project_inventory_uses_the_registered_checkout(db, owner):
    from aeos.portfolio import seed

    seed.run(db)
    db.commit()
    from aeos.portfolio.facade import platform_project

    plan = _plan(db, owner, platform_project(db))
    ctx = db.get(PlanningContext, plan.context_id)
    assert ctx.repo_inventory["status"] == "AVAILABLE", ctx.repo_inventory
    assert ctx.repo_inventory["commit"] == ctx.repo_inventory["inventory"]["commit"]
    assert ctx.repo_inventory["inventory"]["repository"]["full_name"]


def test_existing_project_without_a_checkout_reports_unknown_not_a_signature_error(db, owner):
    project = make_project(db, kind="EXISTING")
    db.add(RepositoryReference(project_id=project.id, provider="GITHUB", full_name="owner/app",
                               url="https://github.com/owner/app"))
    db.commit()
    plan = _plan(db, owner, project)
    inv = db.get(PlanningContext, plan.context_id).repo_inventory
    assert inv["status"] == "UNKNOWN" and "LOCAL_CHECKOUT_MISSING" in inv["reason"]
    assert "TypeError" not in inv["reason"]
