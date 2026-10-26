"""Routing, quota and cost (§12, §22, §23, §42, §43). §50 rows:

* 1.29 "Free quota habis / 429" -> backoff/queue or an approved FREE fallback; never a paid fallback;
* 1.30 "Fable Max quota habis" -> review waits; the gate is not skipped;
* 1.31 "Model alias/provider route berubah" -> recorded as a new configuration/UNKNOWN; the old
  baseline/approval is not inherited;
* 1.32 "Budget DB/gateway tidak sehat" -> admission refused when enforcement cannot be trusted;
* 3.4 "Model request sudah selesai" -> LAST_USED with run/time, not a continuously active session.

The OpenRouter HTTP API and the official claude child process are the only fakes.
"""

from __future__ import annotations

import json

import pytest
import respx
from sqlalchemy import func, select, text

from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.workforce import WorkerRun
from aeos.routing import facade as routing
from tests.acceptance.fakes import FakeClaude
from tests.acceptance.world import (
    WORKER_ROUTE,
    PlanningResponder,
    activate_route,
    admit,
    checks_by_id,
    create_project,
    plan_to_review,
    register_free_route,
    task,
    worker_ready,
)
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

OPENROUTER = "https://openrouter.ai/api/v1"


def _grant(db, run_id: str, project_id: str, models: dict[str, str]) -> str:
    from aeos.broker import facade as broker

    token, _ = broker.issue_grant(db, run_id=run_id, project_id=project_id, domain="PERSONAL", kind="MODEL_PROXY",
                                  scope={"models": models}, ttl_seconds=600)
    db.commit()
    return token


def _worker_call(client: AuthedClient, token: str, model: str = "m"):
    saved = dict(client.client.cookies)
    client.client.cookies.clear()
    try:
        return client.client.post("/broker/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                                  json={"model": model, "messages": [{"role": "user", "content": "kerjakan T1"}]})
    finally:
        client.client.cookies.update(saved)


# ---------------------------------------------------------------------------------------------- 1.29
def test_free_quota_429_backs_off_and_never_falls_back_to_paid(personal_client: AuthedClient, db, monkeypatch, vault):
    vault.put("workers/openrouter", "sk-or-v1-" + "k" * 48, actor="owner")
    # the pilot is an OpenRouter :free model (decision C: PUBLIC tasks only)
    w = worker_ready(personal_client, db, monkeypatch, free_tier=True,
                     tasks=[task("T1", data_classification="PUBLIC"),
                            task("T2", paths=["docs/**"], data_classification="PUBLIC")])
    # a second FREE approved route on the chain, and a PAID route that is never an automatic fallback. The second free
    # route is not another OpenRouter :free model: those share the account's free-model limit, so a 429 of one stops
    # them all (decision C) and none of them is a fallback for another
    register_free_route(db, "worker.acc-free-2", model_id="vendor/acc-two")
    activate_route(personal_client, "worker.acc-free-2")
    register_free_route(db, "worker.acc-paid", model_id="vendor/acc-paid", is_free="NO",
                        pricing={"prompt": "0.000003", "completion": "0.000015"})
    from aeos.routing import service as rsvc

    first = rsvc.get_route(db, WORKER_ROUTE)
    first.fallback_chain = ["worker.acc-paid", "worker.acc-free-2"]
    rsvc.refresh_config_hash(db, first, reason="fallback chain reviewed")
    db.commit()
    activate_route(personal_client, WORKER_ROUTE)
    waits = dict(personal_client.get(f"/api/tasks/{w['tasks']['T1']}/admission").json()["waits"])
    assert "route.registry: STALE" in waits["CAPABILITY_NOT_READY"]  # route config drift -> recheck before admission
    from aeos.core.enums import Capability
    from tests.acceptance.world import doctor_ready

    doctor_ready(db, Capability.CAN_RUN_WORKER)  # the recheck after the reviewed change
    run = db.get(WorkerRun, admit(db)[0])
    token = _grant(db, run.id, w["project"]["id"], {"m": WORKER_ROUTE})
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        upstream = router.post(f"{OPENROUTER}/chat/completions").respond(
            429, headers={"Retry-After": "120"}, json={"error": {"message": "free-models-per-day exhausted"}})
        r = _worker_call(personal_client, token)
    assert r.status_code == 429 and r.headers.get("retry-after") == "120"
    assert upstream.call_count == 1, "exactly one upstream attempt, no retry storm and no other model tried"
    sent = json.loads(upstream.calls[0].request.content)
    assert sent["model"] == "vendor/acc-model:free" and "sk-or-v1" not in json.dumps(sent)
    db.expire_all()
    q = db.scalar(select(QuotaState).where(QuotaState.scope == WORKER_ROUTE))
    assert q.state == "EXHAUSTED" and q.source == "observed-429" and q.resets_at is not None
    assert db.get(WorkerRun, run.id).quota_status == "RATE_LIMITED"
    # the next task is routed only to an approved FREE route; the paid route is never chosen automatically
    t2 = db.get(__import__("aeos.models.planning", fromlist=["TaskSpec"]).TaskSpec, w["tasks"]["T2"])
    decision = routing.fallback_route(db, task=t2, failed_route_key=WORKER_ROUTE)
    assert decision.route.route_key == "worker.acc-free-2"
    assert any("paid fallback is never automatic" in x or "not itself eligible" in x
               for x in decision.rejected.get("worker.acc-paid", []))
    # when the free fallback is exhausted too, the task waits (queue/backoff) - still no paid route
    routing.note_rate_limited(db, provider="openrouter", scope="worker.acc-free-2", retry_after_seconds=300)
    db.commit()
    from aeos.core.errors import Blocked

    with pytest.raises(Blocked) as exc:
        routing.fallback_route(db, task=t2, failed_route_key=WORKER_ROUTE)
    assert exc.value.code == "NO_ELIGIBLE_FALLBACK"
    adm = personal_client.get(f"/api/tasks/{w['tasks']['T2']}/admission").json()
    assert adm["admitted"] is False and adm.get("route") != "worker.acc-paid"
    assert db.scalar(select(func.count()).select_from(WorkerRun).where(WorkerRun.model_route_key == "worker.acc-paid")) == 0


# ---------------------------------------------------------------------------------------------- 1.30
def test_fable_quota_exhausted_makes_review_wait_and_never_skips_the_gate(personal_client: AuthedClient, db,
                                                                         monkeypatch):
    from aeos.planning import runner

    planner = PlanningResponder()
    state = {"exhausted": True, "stages": []}

    def responder(argv, prompt):
        title = FakeClaude.schema_of(argv).get("title", "")
        state["stages"].append(title.rsplit(".", 1)[-1])
        if title.endswith("challenge-findings/1") and state["exhausted"]:
            return (1, json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                   "result": "Claude AI usage limit reached|1793000000", "session_id": "s-q"}), "")
        return planner(argv, prompt)

    FakeClaude(monkeypatch, responder)
    project = create_project(personal_client, name="Quota")
    plan = personal_client.post(f"/api/projects/{project['id']}/plans",
                                json={"title": "quota", "prompt": "Aplikasi pencatat tugas pribadi dengan ekspor CSV"}).json()
    personal_client.post(f"/api/plans/{plan['id']}/run")
    runner.join_all(60)
    waiting = personal_client.get(f"/api/plans/{plan['id']}").json()
    assert waiting["state"] == "NEEDS_DECISION" and "Kuota" in waiting["state_reason"]
    assert waiting["approved_version"] is None and waiting["allowed"]["approve"] is False
    challenge = next(s for s in waiting["stages"] if s["stage"] == "CHALLENGE_REVIEW")
    assert challenge["latest"]["status"] == "QUOTA_EXHAUSTED"
    q = db.scalar(select(QuotaState).where(QuotaState.provider == "claude-native", QuotaState.scope == "fable"))
    assert q is not None and q.state == "EXHAUSTED"
    assert state["stages"].count("challenge-findings/1") == 1 and "synthesis/1" not in state["stages"]
    # after the quota resets the pipeline resumes AT the challenge stage (nothing skipped, no fallback model)
    state["exhausted"] = False
    personal_client.post(f"/api/plans/{plan['id']}/run")
    runner.join_all(60)
    done = personal_client.get(f"/api/plans/{plan['id']}").json()
    assert done["state"] == "READY_FOR_HUMAN_REVIEW"
    assert state["stages"].count("plan-content/1") == 1 and state["stages"].count("challenge-findings/1") == 2
    assert state["stages"][-2:] == ["synthesis/1", "final-review/1"]  # every later gate still ran


# ---------------------------------------------------------------------------------------------- 1.31
def test_supervisor_alias_resolving_to_a_new_model_is_a_new_configuration(personal_client: AuthedClient, db,
                                                                          monkeypatch, host, vault):
    from tests.acceptance.world import seed_catalog

    seed_catalog(db)
    fake = FakeClaude(monkeypatch, PlanningResponder())
    project = create_project(personal_client, name="Alias")
    plan_to_review(personal_client, project["id"])
    opus = personal_client.get("/api/routes/claude-native/opus").json()
    assert opus["resolved_version"] == "claude-opus-5-5" and opus["identity_status"] == "VERIFIED"
    old_hash = opus["config_hash"]
    personal_client.post("/api/doctor/run", json={"group": "MODEL_ROUTE"})
    # the alias now resolves to another model id (provider-side change)
    fake.resolved["opus"] = "claude-opus-5-6"
    plan_to_review(personal_client, project["id"], title="kedua")
    changed = personal_client.get("/api/routes/claude-native/opus").json()
    assert changed["resolved_version"] == "claude-opus-5-6" and changed["config_hash"] != old_hash
    assert changed["config_changed_at"] is not None
    reg = checks_by_id(personal_client)["route.registry"]
    assert reg["effective_status"] == "STALE", "checks depending on config:routes must be re-run"
    # an answer from outside the policy family is never accepted as the old baseline
    fake.resolved["fable"] = "claude-opus-4-8"
    r = personal_client.post(f"/api/projects/{project['id']}/plans",
                             json={"title": "ketiga", "prompt": "Aplikasi pencatat tugas pribadi ekspor CSV"})
    pid = r.json()["id"]
    personal_client.post(f"/api/plans/{pid}/run")
    from aeos.planning import runner

    runner.join_all(60)
    third = personal_client.get(f"/api/plans/{pid}").json()
    assert third["state"] != "READY_FOR_HUMAN_REVIEW"
    ch = next(s for s in third["stages"] if s["stage"] == "CHALLENGE_REVIEW")["latest"]
    assert ch["status"] in ("FAILED", "MODEL_MISMATCH") and ch["error_code"] in ("MODEL_POLICY_MISMATCH", "MODEL_MISMATCH")


def test_worker_route_changed_by_the_provider_loses_its_approval(personal_client: AuthedClient, db, monkeypatch,
                                                                vault):
    # the pilot is the OpenRouter :free model the catalog lists (decision C: a PUBLIC task)
    w = worker_ready(personal_client, db, monkeypatch, free_tier=True, tasks=[task("T1", data_classification="PUBLIC")])
    assert personal_client.get(f"/api/tasks/{w['tasks']['T1']}/admission").json()["admitted"] is True
    before = personal_client.get(f"/api/routes/{WORKER_ROUTE}").json()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(f"{OPENROUTER}/models").respond(200, json={"data": [
            {"id": "vendor/acc-model:free", "name": "Acc", "context_length": 65536,
             "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]}]})
        router.get(f"{OPENROUTER}/key").respond(401, json={"error": {"message": "no key"}})
        r = personal_client.post("/api/routes/refresh-catalog")
        assert r.status_code == 200, r.text
    after = personal_client.get(f"/api/routes/{WORKER_ROUTE}").json()
    assert after["config_hash"] != before["config_hash"] and after["context_window"] == 65536
    assert after["active_decision"] is None, "the approval bound to the old configuration hash does not carry over"
    adm = personal_client.get(f"/api/tasks/{w['tasks']['T1']}/admission").json()
    assert adm["admitted"] is False and "ROUTE_INELIGIBLE" in {c for c, _ in adm["waits"]}
    assert admit(db) == []


# ---------------------------------------------------------------------------------------------- 1.32
def test_untrusted_budget_state_refuses_admission(personal_client: AuthedClient, db, monkeypatch):
    w = worker_ready(personal_client, db, monkeypatch, tasks=[task("T1")])
    # someone edits the budget row outside the decision flow (DB tampering / corrupted gateway state)
    from aeos.cost import service as cost

    cost.ensure_default_budget(db)
    db.commit()
    db.execute(text("UPDATE budgets SET limit_usd = 1000 WHERE is_default = 1"))
    db.commit()
    assert admit(db) == []
    db.expire_all()
    te = db.scalar(select(__import__("aeos.models.workforce", fromlist=["TaskExecution"]).TaskExecution))
    assert te.state == "READY" and "cannot be trusted" in te.reason and te.task_id == w["tasks"]["T1"]
    assert db.scalar(select(func.count()).select_from(WorkerRun)) == 0


def test_missing_cost_module_refuses_admission(personal_client: AuthedClient, db, monkeypatch, absent_modules):
    worker_ready(personal_client, db, monkeypatch, tasks=[task("T1")])
    absent_modules("aeos.cost.service")
    assert admit(db) == []
    assert db.scalar(select(func.count()).select_from(WorkerRun)) == 0


# ---------------------------------------------------------------------------------------------- 3.4
def test_finished_model_request_is_last_used_not_an_active_session(personal_client: AuthedClient, db, monkeypatch,
                                                                    vault):
    vault.put("workers/openrouter", "sk-or-v1-" + "k" * 48, actor="owner")
    w = worker_ready(personal_client, db, monkeypatch, tasks=[task("T1")])
    run = db.get(WorkerRun, admit(db)[0])
    token = _grant(db, run.id, w["project"]["id"], {"m": WORKER_ROUTE})
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{OPENROUTER}/chat/completions").respond(200, json={
            "id": "c1", "object": "chat.completion", "model": "vendor/acc-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0}})
        assert _worker_call(personal_client, token).status_code == 200
    route = personal_client.get(f"/api/routes/{WORKER_ROUTE}").json()
    assert route["last_used"]["status"] == "LAST_USED" and route["last_used"]["run_id"] == run.id
    assert route["last_used"]["at"]
    ledger = db.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id == run.id))
    assert ledger is not None and ledger.input_tokens == 12
    usage = personal_client.get("/api/inventory/components/openrouter").json()["lifecycle"]["usage"]
    assert usage["value"] != "IN_USE", "a finished request is never shown as a running model session"
