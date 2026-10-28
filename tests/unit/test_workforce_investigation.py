"""§19 "Beberapa hipotesis bug -> Read-only investigations terpisah dengan timebox; integrator memilih perbaikan
berbukti" (W6-052): an INVESTIGATION TaskSpec runs one read-only, timeboxed WorkerRun per hypothesis (sequential under
the pilot cap) with a scripted stand-in for OpenCode, produces reports only (never a candidate commit), and the human
integrator's choice of an EVIDENCED fix opens the plan's single repair IMPLEMENTATION TaskSpec."""

from __future__ import annotations

import sys

import pytest
from sqlalchemy import select

from aeos.core.db import session_factory
from aeos.core.errors import Blocked, Conflict
from aeos.models.decisions import Decision
from aeos.models.planning import TaskSpec
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.worker_runtime import executor, opencode
from aeos.workforce import investigation, scheduler
from tests.unit.test_workforce_support import (
    FakeModules,
    attach_repo,
    init_git_repo,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)

FAKE_ENGINE = r'''
import json, os, re, sys, time
scenario, task_file = sys.argv[1], sys.argv[2]
def ev(obj):
    print(json.dumps(obj), flush=True)
prompt = open(task_file, encoding="utf-8").read()
key = re.search(r"## Read-only investigation \(hypothesis \d+/\d+: ([a-z0-9_-]+)\)", prompt).group(1)
cfg = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
ev({"type": "tool_use", "part": {"type": "tool", "tool": "read", "state": {"status": "completed",
    "input": {"filePath": "src/app.py"}, "metadata": {"filepath": os.path.abspath("src/app.py")}}}})
def write():
    with open("src/app.py", "w", encoding="utf-8") as fh:
        fh.write("VALUE = 99\n")
    ev({"type": "tool_use", "part": {"type": "tool", "tool": "write", "state": {"status": "completed",
        "input": {"filePath": "src/app.py"}, "metadata": {"filepath": os.path.abspath("src/app.py")}}}})
if scenario == "write" and key == "h1":
    write()
    sys.exit(0)
if scenario == "silent_write" and key == "h1":  # a change no tool event reports: only the post-run diff sees it
    with open("src/app.py", "w", encoding="utf-8") as fh:
        fh.write("VALUE = 7\n")
report = {"schema_version": "1", "kind": "investigation", "hypothesis_key": key,
          "verdict": "SUPPORTED" if key == "h1" else "REFUTED",
          "summary": "edit permission=" + str(cfg["permission"].get("edit")),
          "findings": [{"title": "VALUE is 1", "detail": "the constant is wrong"}],
          "evidence_refs": ["src/app.py:1"] if key == "h1" else ["src/missing.py:3"],
          "proposed_fix": "set VALUE = 2 in src/app.py"}
ev({"type": "text", "part": {"text": "Findings below.\n```json\n" + json.dumps(report) + "\n```"}})
if scenario == "report_then_write" and key == "h1":  # a valid-looking report, then a write: still a violation
    write()
ev({"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 40, "output": 10}, "cost": 0}})
sys.exit(0)
'''

OPS = ["investigation", "investigation.timebox=120", "investigation.repair=fix",
       "hypothesis:h1=VALUE constant is wrong", "hypothesis:h2=the import path is broken"]


@pytest.fixture
def world(settings, monkeypatch, tmp_path):
    fakes = FakeModules().install(monkeypatch)
    engine = tmp_path / "fake_engine.py"
    engine.write_text(FAKE_ENGINE, encoding="utf-8")
    state = {"scenario": "ok"}
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, refresh=False: {
        "status": "INSTALLED", "binary": sys.executable, "version": "fake-1", "evidence": "test stand-in"})
    monkeypatch.setattr(opencode, "build_command", lambda binary, *, model, worktree, task_file: [
        sys.executable, str(engine), state["scenario"], str(task_file)])
    repo = tmp_path / "repo"
    init_git_repo(repo, {"src/app.py": "VALUE = 1\n", "README.md": "x\n"})
    with session_factory()() as db:
        p = make_project(db)
        attach_repo(db, p, repo)
        plan, pv, specs = make_plan(db, p, [
            {"task_key": "inv", "allowed_paths": ["src/**"], "capability_scope": {"operations": OPS},
             "expected_outputs": ["investigation report per hypothesis"], "limits": {"timeout_seconds": 600}},
            {"task_key": "fix", "allowed_paths": ["src/**"], "depends_on": ["inv"]}])
        fakes.approved[plan.id] = pv.id
        make_route(db)
        fakes.route_key = "worker.test-free"
        seed_and_enable_implementer(db)
        process_sandbox_exception(db)
        scheduler.ensure_executions(db, pv.id, actor="t", requested_by="owner")
        scheduler.refresh_readiness(db, pv.id)
        db.commit()
        ids = {"inv": specs[0].id, "fix": specs[1].id, "pv": pv.id}
    return ids, state


def _te(task_id):
    with session_factory()() as s:
        te = s.scalar(select(TaskExecution).where(TaskExecution.task_id == task_id))
        s.expunge_all()
        return te


def _run(run_id):
    with session_factory()() as s:
        r = s.get(WorkerRun, run_id)
        s.expunge_all()
        return r


def _lease(task_id):
    with session_factory()() as s:
        run = scheduler.acquire_lease(s, task_id, actor="t")
        s.commit()
        return run.id


def test_each_hypothesis_is_a_separate_read_only_run_and_the_integrator_opens_one_repair(settings, world):
    ids, _state = world
    assert _te(ids["inv"]).state == "READY"
    fix = _te(ids["fix"])
    assert fix.state == "BLOCKED" and fix.reason_code == "DEPENDENCY_NOT_ACCEPTED"
    with session_factory()() as s:
        leased = scheduler.admit_ready_tasks(s)  # one active worker: the investigation only
        s.commit()
    assert len(leased) == 1
    run1 = _run(leased[0])
    assert run1.run_type == "INVESTIGATION" and run1.caps["investigation"]["hypothesis"]["key"] == "h1"
    assert run1.caps["timeout_seconds"] == 120 and run1.caps["investigation"]["read_only"] is True
    out = executor.execute_run(settings, run1.id, heartbeat_seconds=0.2)
    assert out["status"] == "NEXT_HYPOTHESIS", out
    run1 = _run(run1.id)
    assert run1.execution_status == "REVIEW" and run1.outcome_code == "INVESTIGATION_REPORTED"
    assert run1.head_commit is None and not run1.quarantined  # a report, never a candidate commit
    rep = run1.caps["investigation_report"]
    assert rep["valid"] and rep["verdict"] == "SUPPORTED" and rep["evidenced"] is True
    assert rep["evidence_checked"] == [{"ref": "src/app.py:1", "exists": True}]
    assert rep["summary"] == "edit permission=deny"  # the engine really ran with the read-only config
    assert _te(ids["inv"]).state == "READY"  # next hypothesis, one run at a time
    run2_id = _lease(ids["inv"])
    run2 = _run(run2_id)
    assert run2.caps["investigation"]["hypothesis"]["key"] == "h2" and run2.attempt == 2
    out = executor.execute_run(settings, run2_id, heartbeat_seconds=0.2)
    assert out["status"] == "INVESTIGATION_COMPLETE", out
    rep2 = _run(run2_id).caps["investigation_report"]
    assert rep2["verdict"] == "REFUTED" and rep2["evidenced"] is False
    assert rep2["evidence_checked"] == [{"ref": "src/missing.py:3", "exists": False}]
    inv_te = _te(ids["inv"])
    assert inv_te.state == "REVIEW" and inv_te.reason_code == "INVESTIGATION_COMPLETE"
    assert "NEEDS_DECISION" in _run(run2_id).block_reason
    with session_factory()() as s:
        view = investigation.comparison(s, s.get(TaskSpec, ids["inv"]))
        assert view["complete"] and view["evidenced"] == ["h1"] and len(view["hypotheses"]) == 2
        with pytest.raises(Blocked) as exc:  # only an evidenced fix opens the repair
            investigation.choose_fix(s, ids["inv"], hypothesis_key="h2", actor="user:o", actor_user_id="owner",
                                     rationale="the import path looks suspicious")
        assert exc.value.code == "INVESTIGATION_FIX_NOT_EVIDENCED"
        scheduler.refresh_readiness(s, ids["pv"])
        assert scheduler.get_te(s, ids["fix"]).state == "BLOCKED"  # no choice yet
        out = investigation.choose_fix(s, ids["inv"], hypothesis_key="h1", actor="user:o", actor_user_id="owner",
                                       rationale="h1 is evidenced by src/app.py:1")
        s.commit()
    with session_factory()() as s:
        d = s.get(Decision, out["decision_id"])
        assert d.kind == "APPROVAL" and d.target_type == "investigation" and d.target_id == ids["inv"]
        assert d.target_hash == view["comparison_hash"] and d.details["hypothesis_key"] == "h1"
        fix = scheduler.get_te(s, ids["fix"])
        assert fix.state == "READY", fix.reason
        assert "investigation inv/h1" in fix.pending_findings[0] and "set VALUE = 2" in fix.pending_findings[0]
        adm = scheduler.admission_check(s, ids["fix"])
        assert "DEPENDENCY_NOT_ACCEPTED" not in {c for c, _ in adm.waits}
        runs = {r.id: r for r in s.scalars(select(WorkerRun).where(WorkerRun.task_id == ids["inv"]))}
        assert runs[run1.id].outcome_code == "INVESTIGATION_CHOSEN" and runs[run2_id].outcome_code == \
            "INVESTIGATION_NOT_CHOSEN"
        inv_te = scheduler.get_te(s, ids["inv"])
        assert inv_te.state == "REVIEW" and inv_te.reason_code == "INVESTIGATION_CHOSEN"  # never accepted
        with pytest.raises(Conflict):
            investigation.choose_fix(s, ids["inv"], hypothesis_key="h1", actor="user:o", actor_user_id="owner",
                                     rationale="choose it again please")
        # the run lifecycle codes changed (CHOSEN / NOT_CHOSEN) but what was investigated did not: every hypothesis
        # stays investigated, none is offered again, and the comparison the choice is bound to is unchanged
        task = s.get(TaskSpec, ids["inv"])
        after = investigation.comparison(s, task)
        assert all(h["investigated"] for h in after["hypotheses"]) and after["evidenced"] == ["h1"]
        assert [h["outcome_code"] for h in after["hypotheses"]] == ["INVESTIGATION_REPORTED", "INVESTIGATION_REPORTED"]
        assert after["comparison_hash"] == d.target_hash
        assert investigation.next_hypothesis(s, task, investigation.spec_of(task)) is None
        # hash-bound at use: if the reports on record change, the choice no longer opens the repair task
        r1 = s.get(WorkerRun, run1.id)
        r1.caps = {**r1.caps, "investigation_report": {**r1.caps["investigation_report"], "evidence_ref": "other"}}
        s.flush()
        adm = scheduler.admission_check(s, ids["fix"])
        assert any(c == "DEPENDENCY_NOT_ACCEPTED" for c, _ in adm.waits), adm.waits
        s.rollback()


def test_a_write_during_an_investigation_is_a_violation_and_nothing_is_committed(settings, world):
    ids, state = world
    state["scenario"] = "write"
    run_id = _lease(ids["inv"])
    out = executor.execute_run(settings, run_id, heartbeat_seconds=0.2)
    assert out["status"] == "NEXT_HYPOTHESIS", out
    run = _run(run_id)
    assert run.execution_status == "FAILED" and run.outcome_code == "SCOPE_VIOLATION" and run.quarantined
    assert run.head_commit is None
    assert any(v["rule"] == "investigation-read-only" for v in run.quarantine_reasons)
    with session_factory()() as s:
        hyps = investigation.hypotheses_state(s, s.get(TaskSpec, ids["inv"]), investigation.spec_of(
            s.get(TaskSpec, ids["inv"])))
        assert hyps[0]["investigated"] and not hyps[0]["evidenced"] and not hyps[1]["investigated"]


def test_a_report_from_a_run_that_then_wrote_a_file_is_never_an_evidenced_fix(settings, world):
    """§19 "integrator memilih perbaikan berbukti": a SUPPORTED report with a reference that exists does not count
    when the same run broke the read-only rule - the run is a SCOPE_VIOLATION and its claim is inconclusive."""
    ids, state = world
    state["scenario"] = "report_then_write"
    run_id = _lease(ids["inv"])
    out = executor.execute_run(settings, run_id, heartbeat_seconds=0.2)
    assert out["status"] == "NEXT_HYPOTHESIS", out
    run = _run(run_id)
    assert run.execution_status == "FAILED" and run.outcome_code == "SCOPE_VIOLATION" and run.head_commit is None
    assert run.caps["investigation_report"]["evidenced"] is True  # what the report alone would claim
    assert run.caps["investigation_outcome"] == "SCOPE_VIOLATION"
    with session_factory()() as s:
        task = s.get(TaskSpec, ids["inv"])
        h1 = investigation.hypotheses_state(s, task, investigation.spec_of(task))[0]
        assert h1["investigated"] and h1["outcome_code"] == "SCOPE_VIOLATION" and h1["evidenced"] is False


def test_a_change_no_tool_event_reported_is_caught_by_the_post_run_diff(settings, world):
    """Defence in depth: the read-only rule is checked on the worktree after the run, not only on tool events."""
    ids, state = world
    state["scenario"] = "silent_write"
    run_id = _lease(ids["inv"])
    out = executor.execute_run(settings, run_id, heartbeat_seconds=0.2)
    assert out["status"] == "NEXT_HYPOTHESIS", out
    run = _run(run_id)
    assert run.execution_status == "FAILED" and run.outcome_code == "SCOPE_VIOLATION" and run.head_commit is None
    assert any(v["rule"] == "investigation-read-only" and v["detail"] == "file changed"
               for v in run.quarantine_reasons), run.quarantine_reasons
    with session_factory()() as s:
        task = s.get(TaskSpec, ids["inv"])
        assert investigation.hypotheses_state(s, task, investigation.spec_of(task))[0]["evidenced"] is False


@pytest.mark.parametrize("ops, tools, problem", [
    (["investigation", "hypothesis:h1=one thing"], [], "investigation.repair"),
    (OPS, ["bash"], "read-only investigation never gets"),
    (["investigation", "investigation.repair=fix"], [], "1..5 hypotheses"),
    (["investigation", "investigation.timebox=5", "investigation.repair=fix", "hypothesis:h1=one thing"], [],
     "timebox"),
])
def test_invalid_investigation_specs_are_never_admitted(db, monkeypatch, ops, tools, problem):
    fakes = FakeModules().install(monkeypatch)
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "inv", "capability_scope": {"operations": ops},
                                         "tools_allowed": tools},
                                        {"task_key": "fix", "depends_on": ["inv"]}])
    fakes.approved[plan.id] = pv.id
    problems = investigation.spec_problems(db, specs[0], investigation.spec_of(specs[0]))
    assert any(problem in x for x in problems), problems
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "BLOCKED" and te.reason_code == "INVESTIGATION_INVALID"


def test_read_only_config_denies_every_write_tool_whatever_the_taskspec_asks():
    cfg = opencode.build_config(broker_base_url="http://127.0.0.1:9/b", token="t", model="m",
                                tools_allowed=["bash", "webfetch"], egress_allowed=["example.org"], read_only=True)
    perm = cfg["permission"]
    assert {perm[t] for t in ("read", "glob", "grep", "list")} == {"allow"}
    assert perm["edit"] == perm["todowrite"] == perm["bash"] == perm["webfetch"] == "deny"
    normal = opencode.build_config(broker_base_url="http://127.0.0.1:9/b", token="t", model="m", tools_allowed=[],
                                   egress_allowed=[])["permission"]
    assert normal["edit"] == "allow"


def test_the_investigation_template_is_part_of_the_worker_prompt_revision(monkeypatch):
    from aeos.worker_runtime import investigation as inv_rt

    assert executor.worker_prompt_revision() == executor.WORKER_PROMPT_REVISION
    monkeypatch.setattr(inv_rt, "PROMPT_RULES", (*inv_rt.PROMPT_RULES, "- changed rule"))
    assert executor.worker_prompt_revision() != executor.WORKER_PROMPT_REVISION


def test_report_extraction_ignores_narration_and_rejects_authority_fields(tmp_path):
    from aeos.worker_runtime import investigation as inv_rt

    good = {"schema_version": "1", "kind": "investigation", "hypothesis_key": "h1", "verdict": "SUPPORTED",
            "findings": [], "evidence_refs": ["a.py:1", "../outside.py", "/etc/passwd"]}
    import json as _json

    log = [_json.dumps({"type": "text", "part": {"text": "thinking {not json}"}}),
           _json.dumps({"type": "text", "part": {"text": "final " + _json.dumps(good)}})]
    assert inv_rt.extract_report(log)["verdict"] == "SUPPORTED"
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    s = inv_rt.summarise(good, hypothesis_key="h1", repo=tmp_path)
    assert s["evidenced"] and [r["exists"] for r in s["evidence_checked"]] == [True, False, False]
    bad = {**good, "approve": True}
    assert not inv_rt.summarise(bad, hypothesis_key="h1", repo=tmp_path)["valid"]
    assert not inv_rt.summarise(good, hypothesis_key="h2", repo=tmp_path)["valid"]  # another hypothesis
    assert inv_rt.extract_report([_json.dumps({"type": "text", "part": {"text": "no report"}})]) is None


def test_viewer_cannot_choose_and_the_view_is_domain_scoped(client, db):
    from aeos.core.enums import Domain
    from tests.conftest import VIEWER_PW, create_admin, create_viewer

    create_admin(db)
    create_viewer(db)
    p = make_project(db)
    _plan, _pv, specs = make_plan(db, p, [{"task_key": "inv", "capability_scope": {"operations": OPS}},
                                          {"task_key": "fix", "depends_on": ["inv"]}])
    scheduler.ensure_executions(db, specs[0].plan_version_id, actor="t")
    db.commit()
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.get(f"/api/tasks/{specs[0].id}/investigation")
    assert r.status_code == 200 and [h["key"] for h in r.json()["hypotheses"]] == ["h1", "h2"]
    r = client.post(f"/api/tasks/{specs[0].id}/investigation/choose",
                    json={"hypothesis_key": "h1", "rationale": "viewer tries to choose"})
    assert r.status_code == 403


def test_a_sensitive_refusal_blocks_the_investigation_instead_of_queueing_the_next_hypothesis(settings, world):
    """Differential review 2026-10-28 (ADR-0127): the broker refused a model request of the h1 run because it carried a
    credential. The next hypothesis would read the same content and be refused again: the task goes BLOCKED with a
    readable reason, the outcome code is kept, h1 stays uninvestigated, and only the owner clears it (never a tick)."""
    from aeos.broker import facade as broker
    from aeos.models.broker_ext import BrokerRequest

    ids, _state = world
    run_id = _lease(ids["inv"])
    with session_factory()() as s:
        run = s.get(WorkerRun, run_id)
        s.add(BrokerRequest(grant_id="g", run_id=run_id, project_id=run.project_id, domain=run.domain,
                            outcome=broker.SENSITIVE_BLOCKED_OUTCOME, status_code=403,
                            error_summary=f"{broker.SENSITIVE_CONTENT_BLOCKED}: github_token"))
        s.commit()
    out = executor.execute_run(settings, run_id, heartbeat_seconds=3600)
    assert out["status"] == "BLOCKED", out
    run = _run(run_id)
    assert run.execution_status == "FAILED" and run.outcome_code == "SENSITIVE_CONTENT_BLOCKED"
    assert run.caps[investigation.OUTCOME_CAP] == "SENSITIVE_CONTENT_BLOCKED"
    te = _te(ids["inv"])
    assert te.state == "BLOCKED" and te.reason_code == "SENSITIVE_CONTENT_BLOCKED", (te.state, te.reason_code)
    assert "credential" in te.reason and "h1" in te.reason
    with session_factory()() as s:
        task = s.get(TaskSpec, ids["inv"])
        assert investigation.next_hypothesis(s, task, investigation.spec_of(task))["key"] == "h1"  # not investigated
        scheduler.refresh_readiness(s, ids["pv"])
        s.commit()
    te = _te(ids["inv"])
    assert te.state == "BLOCKED" and te.reason_code == "SENSITIVE_CONTENT_BLOCKED"  # a tick never clears it
