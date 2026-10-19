"""Claude supervisor at material exceptions and final acceptance (master §42 "Claude supervisor dipanggil
pada planning, checkpoint integrasi, exception material dan final acceptance").

Both reviews are ADVISORY: the answer is DATA for the owner. No DecisionRequest is resolved, no Decision is
recorded and no acceptance/merge status changes (human-only AUTH); the other domain's requests and plans
are never reviewed from this session; the facts of a final-acceptance review are computed, not narrated.
"""

from __future__ import annotations

from sqlalchemy import func, select

from aeos.claude_bridge import bridge
from aeos.claude_bridge.facade import ClaudeResult
from aeos.core.enums import Domain
from aeos.models.decisions import Decision
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.policy_ext import DecisionRequest
from aeos.models.portfolio import Project
from aeos.models.workforce import TaskExecution, WorkerRun

ANSWER = {"assessment": "Pengecualian ini melemahkan isolasi.", "concerns": ["tanpa batas waktu"],
          "questions_for_owner": ["Sampai kapan berlaku?"]}


def _fake(seen: list):
    def run(request, settings=None):
        seen.append(request)
        return ClaudeResult(ok=True, structured=ANSWER, text="", model_requested="opus",
                            model_resolved="claude-opus-5-5", engine_version="2.1.302", session_id=None, usage={},
                            duration_ms=1, mode="NATIVE")
    return run


def _request(db, *, kind="EXCEPTION", domain="PERSONAL") -> DecisionRequest:
    from aeos.policy import facade as policy

    rid = policy.request_decision(db, kind=kind, title="Izinkan process sandbox",
                                  reason="Docker belum terpasang; worker berjalan di process sandbox",
                                  requested_by="scheduler", domain=domain, target_type="worker_sandbox",
                                  target_id="process", details={"note": "owner@example.com minta cepat"})
    db.commit()
    return db.get(DecisionRequest, rid)


def test_material_exception_review_is_advisory_and_domain_scoped(personal_client, db, monkeypatch):
    seen: list = []
    monkeypatch.setattr(bridge, "run_structured", _fake(seen))
    req = _request(db)
    decisions_before = db.scalar(select(func.count()).select_from(Decision))
    r = personal_client.post("/api/claude/review-exception", json={"request_id": req.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "NARRATION" and body["concerns"] == ["tanpa batas waktu"]
    assert "bukan keputusan" in body["label"] and body["request_status_unchanged"] == "OPEN"
    sent = seen[-1]
    assert sent.purpose == "review.exception" and sent.allowed_tools == [] and sent.domain == "PERSONAL"
    assert "UNTRUSTED DATA" in sent.prompt and "owner@example.com" not in sent.prompt  # PII minimised
    db.expire_all()
    assert db.get(DecisionRequest, req.id).status == "OPEN"  # only a human Decision resolves it
    assert db.scalar(select(func.count()).select_from(Decision)) == decisions_before
    # the OFFICE domain's request and a non-material request kind are not reviewed from this session
    office = _request(db, domain="OFFICE")
    assert personal_client.post("/api/claude/review-exception", json={"request_id": office.id}).status_code == 404
    route = _request(db, kind="ROUTE_ACTIVATION")
    assert personal_client.post("/api/claude/review-exception", json={"request_id": route.id}).status_code == 404
    assert len(seen) == 1


def test_office_exception_review_goes_through_the_office_gate(admin_client, db, monkeypatch):
    seen: list = []
    real_preflight = bridge.preflight

    def gated(request, settings=None):
        seen.append(request)
        problem = real_preflight(request)
        assert problem is not None  # no company authorisation recorded: refused before any model call
        return ClaudeResult(ok=False, structured=None, text="", model_requested="opus", model_resolved=None,
                            engine_version=None, session_id=None, usage={}, duration_ms=None,
                            error_code=problem[0], error=problem[1], mode="NATIVE")

    monkeypatch.setattr(bridge, "run_structured", gated)
    req = _request(db, domain="OFFICE")
    admin_client.select_domain(Domain.OFFICE)
    body = admin_client.post("/api/claude/review-exception", json={"request_id": req.id}).json()
    assert body["status"] == "UNAVAILABLE" and body["error_code"] == "OFFICE_POLICY_DENIED"
    assert seen[-1].domain == "OFFICE"


def _executed_plan(db, domain: str = "PERSONAL") -> tuple[Plan, list[WorkerRun]]:
    project = Project(project_key=f"fa-{domain.lower()}", name="Final", domain=domain)
    db.add(project)
    db.flush()
    plan = Plan(project_id=project.id, domain=domain, title="Ekspor faktur")
    db.add(plan)
    db.flush()
    pv = PlanVersion(plan_id=plan.id, project_id=project.id, domain=domain, version=1, content={},
                     manifest_hash="sha256:pv")
    db.add(pv)
    db.flush()
    plan.approved_version_id = pv.id
    runs = []
    for key, accepted in (("t1", "ACCEPTED"), ("t2", None)):
        spec = TaskSpec(plan_version_id=pv.id, plan_version_hash="sha256:pv", project_id=project.id, domain=domain,
                        task_key=key, title=key, goal="g", spec_hash=f"sha256:{key}")
        db.add(spec)
        db.flush()
        db.add(TaskExecution(task_id=spec.id, project_id=project.id, domain=domain, plan_id=plan.id,
                             plan_version_id=pv.id, task_key=key, state="DONE" if accepted else "REVIEW"))
        run = WorkerRun(task_id=spec.id, project_id=project.id, domain=domain, execution_status="SUCCEEDED",
                        verifier_status="PASS", acceptance_status=accepted, merged_status=None)
        db.add(run)
        runs.append(run)
    db.commit()
    return plan, runs


def test_final_acceptance_review_reports_facts_and_changes_nothing(personal_client, db, monkeypatch):
    seen: list = []
    monkeypatch.setattr(bridge, "run_structured", _fake(seen))
    plan, runs = _executed_plan(db)
    r = personal_client.post("/api/claude/review-final-acceptance", json={"plan_id": plan.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["facts"] == {"tasks": 2, "accepted": 1, "done": 1, "all_accepted": False}  # computed, not narrated
    assert body["status"] == "NARRATION" and "bukan keputusan" in body["label"]
    sent = seen[-1]
    assert sent.purpose == "review.final_acceptance" and sent.domain == "PERSONAL"
    assert sent.project_id == plan.project_id and sent.allowed_tools == []
    db.expire_all()
    assert [db.get(WorkerRun, r.id).acceptance_status for r in runs] == ["ACCEPTED", None]
    office_plan, _ = _executed_plan(db, domain="OFFICE")
    assert personal_client.post("/api/claude/review-final-acceptance",
                                json={"plan_id": office_plan.id}).status_code == 404
    draft = Plan(project_id=plan.project_id, domain="PERSONAL", title="Draft")
    db.add(draft)
    db.commit()
    r = personal_client.post("/api/claude/review-final-acceptance", json={"plan_id": draft.id})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_NOT_APPROVED"
    assert len(seen) == 1
