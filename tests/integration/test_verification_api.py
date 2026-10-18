"""Verification/Delivery HTTP API (§34, §35). §50 rows: "VIEWER memanggil write endpoint langsung" →
denied; "Dashboard Personal mencoba run/evidence Office" → 404; "Submit tanpa domain" → refused;
every write is SUPER_ADMIN and audited."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.verification import collector, suite
from tests.conftest import VIEWER_PW, AuthedClient, create_viewer
from tests.unit.test_verification_support import (
    FakeBridge,
    FakeWorkforce,
    bind_candidate,
    claude_result,
    fake_secret,
    install_doctor,
    install_planning,
    make_candidate,
    make_controller,
    make_world,
    record_verification,
    requires_git,
    review_payload,
)


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def test_domain_required(admin_client: AuthedClient):
    r = admin_client.get("/api/evidence")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_evidence_scoped_to_domain_and_redacted(personal_client: AuthedClient, db, wf):
    personal = make_world(db, project_key="personal-app")
    office = make_world(db, domain="OFFICE", project_key="office-app")
    token = fake_secret()
    ev_p = collector.collect_evidence(db, kind="LOG", origin="collector", collector_identity="c",
                                      claim="personal log", content=f"token={token}", project_id=personal.project.id,
                                      evidence_level="E3")
    ev_o = collector.collect_evidence(db, kind="LOG", origin="collector", collector_identity="c", claim="office log",
                                      content="office", project_id=office.project.id)
    ev_platform = collector.collect_evidence(db, kind="PROBE", origin="doctor", collector_identity="d",
                                             claim="platform probe", content="ok")
    ev_office_domain = collector.collect_evidence(db, kind="PROBE", origin="doctor", collector_identity="d",
                                                  claim="office probe", content="x", domain="OFFICE")
    db.commit()
    r = personal_client.get("/api/evidence")
    assert r.status_code == 200
    body = r.json()
    ids = {i["id"] for i in body["items"]}
    assert ev_p.id in ids and ev_platform.id in ids
    assert ev_o.id not in ids and ev_office_domain.id not in ids
    assert body["source"].startswith("evidence_artifacts") and body["checked_at"]
    assert set(body["level_explanations"]) == {"E0", "E1", "E2", "E3", "E4"}
    item = next(i for i in body["items"] if i["id"] == ev_p.id)
    assert item["level_explanation"] and item["trusted"] is True
    d = personal_client.get(f"/api/evidence/{ev_p.id}").json()
    assert d["integrity"] == "VERIFIED" and token not in d["content"] and "[REDACTED]" in d["content"]
    assert personal_client.get(f"/api/evidence/{ev_o.id}").status_code == 404
    assert personal_client.get("/api/evidence?trusted=false").json()["items"] == []
    assert personal_client.get("/api/evidence?level=E9").status_code == 422


def test_cross_domain_run_is_404(personal_client: AuthedClient, db, wf):
    office = make_world(db, domain="OFFICE", project_key="office-app")
    r = personal_client.post(f"/api/runs/{office.run.id}/verify", json={})
    assert r.status_code == 404
    r = personal_client.get(f"/api/delivery/gates?task_id={office.task.id}")
    assert r.status_code == 404


def test_viewer_cannot_write(client: AuthedClient, db, wf):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    w = make_world(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    for url in (f"/api/runs/{w.run.id}/verify", f"/api/runs/{w.run.id}/review",
                f"/api/runs/{w.run.id}/acceptance/evaluate", f"/api/tasks/{w.task.id}/prepare-pr",
                "/api/delivery/provider-capabilities/record", f"/api/delivery/governance/{w.task.id}/handoff"):
        r = client.post(url, json={})
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE", url
    assert client.get("/api/evidence").status_code == 200


@requires_git
def test_verify_review_accept_gates_flow(personal_client: AuthedClient, db, settings, wf, tmp_path: Path,
                                         monkeypatch):
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload())).install(monkeypatch)
    w = make_world(db, verifier_policy={"ruff": False}, repo="fadhlillah-dli/dly-aeos")
    install_planning(monkeypatch, w.version)
    cand, base, head = make_candidate(settings, w, head_files={"backend/extra.py": "def mul(a, b):\n    return a * b\n"})
    bind_candidate(db, w, cand, base, head)

    r = personal_client.post(f"/api/runs/{w.run.id}/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PASS"
    ev = personal_client.get(f"/api/evidence?run_id={w.run.id}").json()["items"]
    kinds = {e["kind"] for e in ev}
    assert {"VERIFICATION_REPORT", "TEST_REPORT", "SCOPE_CHECK", "DIFF", "SECRET_SCAN"} <= kinds

    r = personal_client.post(f"/api/runs/{w.run.id}/review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["review"]["verdict"] == "PASS-CONTROLLED" and body["review"]["is_acceptance"] is False
    assert body["acceptance"]["acceptance_status"] == "ACCEPTED"

    acc = personal_client.get(f"/api/acceptance?task_id={w.task.id}").json()["items"]
    assert acc[0]["valid"] is True and acc[0]["head_commit"] == head
    reviews = personal_client.get(f"/api/reviews?run_id={w.run.id}").json()["items"]
    assert reviews[0]["review"]["outcome"]
    assert personal_client.get(f"/api/reviews/{reviews[0]['id']}").status_code == 200

    g = personal_client.get(f"/api/delivery/gates?task_id={w.task.id}").json()
    by = {x["gate"]: x["status"] for x in g["gates"]}
    assert by["G2"] == "PASS" and by["G4"] == "PASS" and by["G0"] == "PASS"
    assert by["G1"] == "NOT_PROVEN", "no provider CI evidence yet"
    assert g["ready_for_human_merge"] is False and g["merge_authority"] == "HUMAN_ONLY"

    summary = personal_client.get("/api/verification/summary").json()["items"]
    row = next(i for i in summary if i["run_id"] == w.run.id)
    assert row["verifier"]["status"] == "PASS" and row["reviewer"]["verdict"] == "PASS-CONTROLLED"
    assert row["accepted"]["status"] == "ACCEPTED" and row["worker_reported"]["trust"] == "UNTRUSTED_CLAIM"

    bundle = personal_client.get(f"/api/delivery/bundle?task_id={w.task.id}").json()
    assert "Merge authority: human only" in bundle["markdown"]

    h = personal_client.post(f"/api/delivery/governance/{w.task.id}/handoff", json={})
    assert h.status_code == 200 and h.json()["output"]["result"] == "NOT_CONFIGURED"
    gs = personal_client.get(f"/api/delivery/governance?task_id={w.task.id}").json()
    assert gs["latest"]["output"]["result"] == "NOT_CONFIGURED"


def test_prepare_pr_without_acceptance_is_blocked_and_audited(personal_client: AuthedClient, db, wf):
    w = make_world(db, repo="fadhlillah-dli/dly-aeos")
    r = personal_client.post(f"/api/tasks/{w.task.id}/prepare-pr", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACCEPTANCE_REQUIRED"
    db.expire_all()
    rows = audit.list_events(db, action_prefix="delivery.pr.prepare")
    assert rows and rows[0].result == "DENIED"


def test_provider_capabilities_requires_registered_repo(personal_client: AuthedClient, db, wf, secrets_memory):
    make_world(db, repo="fadhlillah-dli/dly-aeos")
    assert personal_client.get("/api/delivery/provider-capabilities?repo=someone/else").status_code == 404
    r = personal_client.get("/api/delivery/provider-capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["repo"] == "fadhlillah-dli/dly-aeos" and body["merge_gate"] == "UNKNOWN"
    assert body["credential"].startswith("MISSING") and body["enforcement_tested"] == "NOT-PROVEN"


def test_prs_list_and_review_manual_result(personal_client: AuthedClient, db, wf, monkeypatch):
    w = make_world(db)
    record_verification(db, w)
    install_doctor(monkeypatch, status="UNKNOWN")
    FakeBridge().install(monkeypatch)
    r = personal_client.post(f"/api/runs/{w.run.id}/review")
    assert r.status_code == 200
    rev = r.json()["review"]
    assert rev["status"] == "NEEDS_DECISION" and rev["verdict"] == "NOT-PROVEN"
    r = personal_client.post(f"/api/reviews/{rev['id']}/manual-result", json={"structured": review_payload()})
    assert r.status_code == 200, r.text
    assert r.json()["review"]["mode"] == "MANUAL_FALLBACK"
    assert r.json()["acceptance"]["acceptance_status"] == "ACCEPTED"
    prs = personal_client.get("/api/delivery/prs").json()
    assert prs["items"] == [] and prs["merge_authority"] == "HUMAN_ONLY"


def test_cli_gates_and_capabilities(settings, db, wf, monkeypatch):
    from aeos.delivery.cli import app as delivery_cli
    from aeos.verification.cli import app as verify_cli

    w = make_world(db, repo="fadhlillah-dli/dly-aeos")
    install_planning(monkeypatch, w.version)
    runner = CliRunner()
    res = runner.invoke(verify_cli, ["gates", w.task.id])
    assert res.exit_code == 0, res.output
    assert "G0 Input / policy" in res.output and "HUMAN_ONLY" in res.output
    res.output.encode("ascii")  # ASCII-only output
    res = runner.invoke(delivery_cli, ["capabilities", "--repo", "fadhlillah-dli/dly-aeos"])
    assert res.exit_code == 0, res.output
    assert "merge gate: UNKNOWN" in res.output
    res.output.encode("ascii")
    res = runner.invoke(verify_cli, ["run", "does-not-exist"])
    assert res.exit_code == 2 and "NOT_FOUND" in res.output


def test_refused_privileged_actions_are_audited(personal_client: AuthedClient, db, wf):
    """Every privileged action leaves an audit row, including refusals (AGENTS.md)."""
    w = make_world(db, run_state="RUNNING")
    r = personal_client.post(f"/api/runs/{w.run.id}/verify", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RUN_NOT_IN_REVIEW"
    r = personal_client.post(f"/api/runs/{w.run.id}/acceptance/evaluate", json={})
    assert r.status_code == 409
    r = personal_client.post(f"/api/delivery/governance/{w.task.id}/handoff", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACCEPTANCE_REQUIRED"
    db.expire_all()
    for action in ("verification.run", "verification.acceptance.decide", "delivery.governance.handoff"):
        rows = audit.list_events(db, action_prefix=action)
        assert rows and rows[0].result in ("DENIED", "FAILED") and rows[0].target_id in (w.run.id, w.task.id), action
