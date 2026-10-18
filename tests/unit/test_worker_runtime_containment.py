"""Containment report executed for real in a child process (§25, §50, §51 P1 'Containment, egress,
secret broker ... cancel ... belum dijalankan')."""

from __future__ import annotations

import sys

from aeos.worker_runtime.containment import containment_report, record_containment_report


def test_process_containment_reports_honestly(settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "s" * 40)  # must not reach the sandbox
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "g" * 36)
    rep = containment_report("process")
    results = {r["check"]: r for r in rep["results"]}
    assert rep["kind"] == "process" and rep["platform"] == sys.platform
    # Honest: a process sandbox has no egress filter.
    assert results["egress_non_broker"]["status"] == "NOT_ENFORCED"
    assert results["read_outside_worktree"]["status"] == "NOT_ENFORCED"
    assert results["write_outside_worktree"]["status"] == "NOT_ENFORCED"
    # What the process sandbox does guarantee:
    assert results["env_secrets"]["status"] == "PASS", results["env_secrets"]
    assert results["home_redirected"]["status"] == "PASS", results["home_redirected"]
    assert results["home_ssh_redirected"]["status"] == "PASS"
    assert results["cancel_kills_tree"]["status"] == "PASS", results["cancel_kills_tree"]
    assert rep["overall"] == "WEAK"  # never reported as PASS while items are NOT_ENFORCED
    assert "sk-or-v1" not in str(rep) and "ghp_" not in str(rep)


def test_report_is_recorded(db):
    rep = {"kind": "process", "overall": "WEAK", "checked_at": "x", "results": [], "summary": {}}
    record_containment_report(db, rep, actor="cli:test")
    from aeos.models.identity import SystemSetting

    assert db.get(SystemSetting, "worker_runtime.containment.process").value["overall"] == "WEAK"
    assert rep["evidence_id"] is None  # verification module absent -> not claimed as evidence
