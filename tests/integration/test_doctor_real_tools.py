"""Doctor + reconciliation against the real host (read-only probes). Skips cleanly when a tool is absent.

These never call a model, never use the network and never write outside the test data dir.
"""

from __future__ import annotations

import shutil

import pytest

from aeos.doctor.runner import run_doctor
from aeos.inventory.probes import SystemProbeRunner
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run

pytestmark = pytest.mark.integration


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_real_host_runtime_and_repository_checks(settings, db):
    report = run_doctor(settings, db, groups=["HOST", "REPOSITORY", "RUNTIME"], runner=SystemProbeRunner())
    db.commit()
    by_id = {r.check_id: r for r in report.results}
    assert by_id["runtime.python"].status == "VERIFIED"
    assert by_id["repo.root"].status == "VERIFIED"
    assert by_id["repo.state"].status == "VERIFIED"
    assert all(r.status in {"MISSING", "CONFIGURED", "CONNECTED", "VERIFIED", "FAILED", "EXPIRED", "UNKNOWN",
                            "NOT_APPLICABLE", "STALE"} for r in report.results)


@pytest.mark.skipif(shutil.which("claude") is None, reason="official Claude Code not installed")
def test_real_claude_binary_and_native_auth_probe(settings, db):
    report = run_doctor(settings, db, groups=["CLAUDE_NATIVE"], runner=SystemProbeRunner())
    by_id = {r.check_id: r for r in report.results}
    assert by_id["claude.binary"].status == "VERIFIED"
    assert by_id["claude.auth"].status in {"CONNECTED", "MISSING", "FAILED", "UNKNOWN"}
    assert "email" not in by_id["claude.auth"].observed_evidence
    # without --functional the check only reads the bridge's recorded proof: none on a fresh data dir, and
    # no `claude -p` call is made implicitly (NOT-PROVEN -> manual fallback, never VERIFIED by narration)
    assert by_id["claude.capability_test"].status == "UNKNOWN"
    assert by_id["claude.capability_test"].error_code == "CAPABILITY_TEST_NOT_RUN"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_real_core_reconciliation_is_read_only(settings, db):
    seed_run(db)
    run = reconcile(db, settings, runner=SystemProbeRunner())
    db.commit()
    assert run.status in ("COMPLETED", "PARTIAL")
    assert run.component_status["git"]["status"] == "OK"
