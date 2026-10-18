"""Sandbox levels and the CAN_RUN_WORKER sandbox requirement (§25 'Jika boundary yang disyaratkan
belum tersedia, pekerjaan tersebut BLOCKED', ADR-0051)."""

from __future__ import annotations

import pytest

from aeos.worker_runtime import facade, sandbox
from aeos.worker_runtime.containment import containment_report
from tests.unit.test_workforce_support import process_sandbox_exception


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    sandbox._cache.update(at=0.0, value=None)
    yield
    sandbox._cache.update(at=0.0, value=None)


def test_capabilities_report_process_weak_and_container_missing():
    caps = sandbox.sandbox_capabilities(refresh=True)
    assert caps["levels"]["process"]["available"] and caps["levels"]["process"]["strength"] == "WEAK"
    assert caps["levels"]["process"]["enforces"]["egress"] is False
    assert caps["levels"]["container"]["available"] is False
    assert "docker CLI not found" in caps["levels"]["container"]["evidence"]
    assert caps["levels"]["vm"]["available"] is False


def test_blocked_without_exception_decision(db):
    r = facade.sandbox_readiness(db, domain="PERSONAL")
    assert r["status"] == "BLOCKED" and r["kind"] is None
    assert any("EXCEPTION" in x for x in r["reasons"])


def test_ready_with_personal_exception_lists_residual_risks(db):
    d = process_sandbox_exception(db)
    r = facade.sandbox_readiness(db, domain="PERSONAL")
    assert r["status"] == "READY" and r["kind"] == "process" and r["decision_id"] == d.id
    assert any("egress" in x for x in r["residual_risks"])


@pytest.mark.parametrize("kw", [{"domain": "OFFICE"}, {"allow": False}, {"expires_in": -10}])
def test_invalid_exceptions_do_not_count(db, kw):
    process_sandbox_exception(db, **kw)
    assert facade.sandbox_readiness(db, domain="PERSONAL")["status"] == "BLOCKED"


def test_office_always_blocked_even_with_exception(db):
    process_sandbox_exception(db)
    process_sandbox_exception(db, domain="OFFICE")
    r = facade.sandbox_readiness(db, domain="OFFICE")
    assert r["status"] == "BLOCKED"


def test_container_available_but_unproven_is_not_ready(db, monkeypatch):
    monkeypatch.setattr(sandbox, "_docker_probe", lambda timeout=10.0: {
        "available": True, "evidence": "docker server 27 responded", "cli": "docker", "server_version": "27"})
    sandbox._cache.update(at=0.0, value=None)
    r = facade.sandbox_readiness(db, domain="PERSONAL")
    assert r["status"] == "BLOCKED"
    assert any("NOT_PROVEN" in x for x in r["reasons"])


def test_container_containment_not_proven_without_docker():
    rep = containment_report("container")
    assert rep["overall"] == "NOT_PROVEN"
    assert {x["status"] for x in rep["results"]} == {"NOT_RUN"}


def test_container_command_is_hardened():
    argv = sandbox.container_command(image="aeos-worker:1", run_id="r" * 20, worktree="/w", config_dir="/c",
                                     inner_argv=["opencode", "run"], env={"HOME": "/home/worker"})
    joined = " ".join(argv)
    for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit", "--user 10001:10001"):
        assert flag in joined
    assert "docker.sock" not in joined and "--privileged" not in joined
