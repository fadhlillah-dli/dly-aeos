"""Claude explains Doctor results (master §08 "Claude can explain Doctor results"): narration only, the check
row is untrusted data, nothing about the check or capability changes."""

from __future__ import annotations

from aeos.claude_bridge import bridge
from aeos.claude_bridge.facade import ClaudeResult
from aeos.core.timeutil import utcnow
from aeos.models.inventory import CheckResult


def _result(ok=True):
    return ClaudeResult(ok=ok, structured={"explanation": "Port 8787 dipakai proses lain.", "next_steps": ["aeos serve --port 8790"]}
                        if ok else None, text="", model_requested="opus", model_resolved="claude-opus-5-5",
                        engine_version="2.1.302", session_id=None, usage={}, duration_ms=1,
                        error_code=None if ok else "BRIDGE_BLOCKED", error=None if ok else "blocked", mode="NATIVE")


def test_explanation_is_narration_and_never_changes_the_check(admin_client, db, monkeypatch):
    secret = "ghp_" + "Z" * 36
    db.add(CheckResult(check_id="host.port", title="Port lokal", group="HOST", status="FAILED", severity="HIGH",
                       summary=f"Port dipakai; token {secret}", observed_evidence={"port": 8787},
                       error_code="PORT_IN_USE", remediation={"operation": "pilih port lain"},
                       last_checked_at=utcnow()))
    db.commit()
    seen = []

    def fake_run(request, settings=None):
        seen.append(request)
        return _result()

    monkeypatch.setattr(bridge, "run_structured", fake_run)
    r = admin_client.post("/api/claude/explain-check", json={"check_id": "host.port"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "NARRATION" and "bukan bukti" in body["label"]
    assert body["next_steps"] == ["aeos serve --port 8790"] and body["check_status_unchanged"] == "FAILED"
    req = seen[0]
    assert req.allowed_tools == [] and req.purpose == "doctor.explain"
    assert "UNTRUSTED DATA" in req.prompt and secret not in req.prompt  # redacted before it leaves
    db.expire_all()
    row = db.query(CheckResult).filter_by(check_id="host.port").one()
    assert row.status == "FAILED" and row.summary.startswith("Port dipakai")  # unchanged


def test_unavailable_bridge_is_reported_and_missing_results_are_404(admin_client, db, monkeypatch):
    monkeypatch.setattr(bridge, "run_structured", lambda request, settings=None: _result(ok=False))
    assert admin_client.post("/api/claude/explain-check", json={"check_id": "nope"}).status_code == 404
    db.add(CheckResult(check_id="runtime.db", title="DB", group="RUNTIME", status="UNKNOWN", last_checked_at=utcnow()))
    db.commit()
    body = admin_client.post("/api/claude/explain-check", json={"check_id": "runtime.db"}).json()
    assert body["status"] == "UNAVAILABLE" and body["error_code"] == "BRIDGE_BLOCKED"


def test_office_check_rows_go_through_the_office_gate_and_never_reach_a_personal_session(admin_client, db,
                                                                                         monkeypatch):
    """§23 OFFICE default deny: an OFFICE-scoped check row is OFFICE data - the request carries the domain so the
    bridge preflight refuses it without a company authorisation; a PERSONAL session explains its own row
    even when the OFFICE row of the same check is newer (review finding)."""
    from datetime import timedelta

    from aeos.core.enums import Domain

    now = utcnow()
    db.add(CheckResult(check_id="adapter.tracker", title="Tracker", group="PROJECT_ADAPTERS", status="FAILED",
                       summary="personal tracker token missing", scope={"domain": "PERSONAL"},
                       last_checked_at=now - timedelta(minutes=5)))
    db.add(CheckResult(check_id="adapter.tracker", title="Tracker", group="PROJECT_ADAPTERS", status="FAILED",
                       summary="OFFICE-ONLY-DETAIL jira.company.example", scope={"domain": "OFFICE"},
                       last_checked_at=now))
    db.commit()
    seen = []
    real_preflight = bridge.preflight

    def gated_run(request, settings=None):
        seen.append(request)
        problem = real_preflight(request)
        return _result(ok=problem is None) if problem is None else ClaudeResult(
            ok=False, structured=None, text="", model_requested="opus", model_resolved=None, engine_version=None,
            session_id=None, usage={}, duration_ms=None, error_code=problem[0], error=problem[1], mode="NATIVE")

    monkeypatch.setattr(bridge, "run_structured", gated_run)
    admin_client.select_domain(Domain.PERSONAL)
    body = admin_client.post("/api/claude/explain-check", json={"check_id": "adapter.tracker"}).json()
    assert body["status"] == "NARRATION"
    assert seen[-1].domain == "PERSONAL" and "OFFICE-ONLY-DETAIL" not in seen[-1].prompt
    admin_client.select_domain(Domain.OFFICE)
    body = admin_client.post("/api/claude/explain-check", json={"check_id": "adapter.tracker"}).json()
    assert seen[-1].domain == "OFFICE"
    assert body["status"] == "UNAVAILABLE" and body["error_code"] == "OFFICE_POLICY_DENIED"
