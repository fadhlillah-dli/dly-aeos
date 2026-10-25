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


# ----------------------------------------------------------------------------- §25 non-root
def test_elevated_controller_never_gets_the_process_sandbox(db, monkeypatch):
    """§25 'non-root': with an elevated controller a process-sandbox worker would run elevated too."""
    process_sandbox_exception(db)
    monkeypatch.setattr(sandbox, "_elevation_probe", lambda: True)
    sandbox._cache.update(at=0.0, value=None)
    r = facade.sandbox_readiness(db, domain="PERSONAL")
    assert r["status"] == "BLOCKED" and any("elevated" in x for x in r["reasons"])
    rep = containment_report("process")
    assert next(x for x in rep["results"] if x["check"] == "non_root")["status"] == "FAIL" and rep["overall"] == "FAIL"
    monkeypatch.setattr(sandbox, "_elevation_probe", lambda: False)
    sandbox._cache.update(at=0.0, value=None)
    assert facade.sandbox_readiness(db, domain="PERSONAL")["status"] == "READY"


# ----------------------------------------------------------------------------- task-level requirement
def _task_req(db, **kw):
    args = {"domain": "PERSONAL", "risk_class": "R1", "tools_allowed": ["fs.read"], "egress_allowed": []}
    args.update(kw)
    return sandbox.task_sandbox_readiness(db, **args)


def test_shell_is_never_admitted_under_the_process_sandbox_even_with_allow_bash(db):
    """Master §25 'Worker tidak mendapat shell host' (ADR-0051 amendment 2026-10-19)."""
    d = process_sandbox_exception(db)
    d.details = {"allow_process_sandbox": True, "allow_bash": True}
    db.flush()
    for tools in (["bash"], ["bash:pytest *"]):
        r = _task_req(db, tools_allowed=tools)
        assert r["status"] == "BLOCKED" and r["code"] == "SHELL_NEEDS_CONTAINER", r
    assert _task_req(db)["status"] == "READY"


def test_egress_tasks_are_blocked_because_egress_cannot_be_enforced(db):
    process_sandbox_exception(db)
    r = _task_req(db, tools_allowed=["webfetch"], egress_allowed=["docs.example.com"])
    assert r["status"] == "BLOCKED" and r["code"] == "EGRESS_NOT_ENFORCEABLE"
    assert _task_req(db, tools_allowed=["webfetch"], egress_allowed=[])["status"] == "READY"  # webfetch stays denied


def test_risk_tier_requires_stronger_boundaries(db):
    """§25 'Pilihan VM/microVM/sandbox untuk risiko lebih tinggi': R3 needs the container (or explicit owner
    coverage), R4 needs a VM (not implemented -> BLOCKED), unknown risk is treated as needing a container."""
    from aeos.models.decisions import Decision

    d = process_sandbox_exception(db)
    assert _task_req(db, risk_class="R2")["status"] == "READY"
    r3 = _task_req(db, risk_class="R3")
    assert r3["status"] == "BLOCKED" and r3["code"] == "STRONG_SANDBOX_REQUIRED"
    assert _task_req(db, risk_class="??")["code"] == "STRONG_SANDBOX_REQUIRED"
    # the coverage is part of the decision's immutable binding: the owner records a new exception
    d.status = "SUPERSEDED"
    db.add(Decision(kind="EXCEPTION", actor_user_id="owner", actor_kind="HUMAN", target_type="worker_sandbox",
                    target_id="process", domain="PERSONAL", rationale="pilot incl. R3", details={
                        "allow_process_sandbox": True},
                    allowed_operations=["run_worker_process_sandbox", sandbox.PROCESS_R3_OPERATION]))
    db.flush()
    assert _task_req(db, risk_class="R3")["status"] == "READY"
    r4 = _task_req(db, risk_class="R4")
    assert r4["status"] == "BLOCKED" and r4["code"] == "VM_SANDBOX_REQUIRED"


# ----------------------------------------------------------------------------- container selection
def _docker_ok(monkeypatch, image_id="sha256:img1"):
    from aeos.worker_runtime import container

    monkeypatch.setattr(sandbox, "_docker_probe", lambda timeout=10.0: {
        "available": True, "evidence": "docker server 27 responded", "cli": "docker", "server_version": "27"})
    monkeypatch.setattr(container, "image_status", lambda: {"tag": container.image_tag(), "present": True,
                                                            "id": image_id, "evidence": "present"})
    sandbox._cache.update(at=0.0, value=None)


def _record(db, *, domain="PERSONAL", overall="PASS", image_id="sha256:img1", age_days=0):
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.models.identity import SystemSetting
    from aeos.worker_runtime import container

    key = sandbox.containment_setting_key("container", domain)
    db.merge(SystemSetting(key=key, value={
        "kind": "container", "domain": domain, "overall": overall, "image": container.image_tag(),
        "image_id": image_id, "platform": __import__("sys").platform,
        "checked_at": (utcnow() - timedelta(days=age_days)).isoformat()}))
    db.flush()


def test_container_is_selected_only_with_a_fresh_passing_report_for_the_current_image(db, monkeypatch):
    _docker_ok(monkeypatch)
    process_sandbox_exception(db)
    assert facade.sandbox_readiness(db, domain="PERSONAL")["kind"] == "process"  # no report yet
    _record(db, overall="FAIL")
    assert facade.sandbox_readiness(db, domain="PERSONAL")["kind"] == "process"
    _record(db, image_id="sha256:older-image")
    assert facade.sandbox_readiness(db, domain="PERSONAL")["kind"] == "process"
    _record(db, age_days=30)
    assert facade.sandbox_readiness(db, domain="PERSONAL")["kind"] == "process"
    _record(db)
    r = facade.sandbox_readiness(db, domain="PERSONAL")
    assert r["status"] == "READY" and r["kind"] == "container" and r["profile"] == "PERSONAL"
    # with the container proven, shell tools are no longer refused for being a host shell
    assert _task_req(db, tools_allowed=["bash:pytest *"])["status"] == "READY"
    assert _task_req(db, risk_class="R3")["status"] == "READY"


def test_office_needs_its_own_proven_container_profile(db, monkeypatch):
    """§11 'OFFICE execution with separate identity/process and separate filesystem/egress'."""
    _docker_ok(monkeypatch)
    process_sandbox_exception(db)
    _record(db, domain="PERSONAL")
    office = facade.sandbox_readiness(db, domain="OFFICE")
    assert office["status"] == "BLOCKED" and any("--domain OFFICE" in x for x in office["reasons"])
    _record(db, domain="OFFICE")
    office = facade.sandbox_readiness(db, domain="OFFICE")
    assert office["status"] == "READY" and office["kind"] == "container" and office["profile"] == "OFFICE"
    from aeos.worker_runtime import container

    personal, off = container.profile_for("PERSONAL"), container.profile_for("OFFICE")
    assert personal.network != off.network and personal.gateway != off.gateway and personal.user != off.user


def test_container_names_never_collide_for_runs_sharing_a_prefix():
    """Found on a real Docker host: the probe and cancel runs of the containment suite both truncated to
    'aeos-run-containment-' and the second launch failed with a docker name conflict."""
    import re

    names = [sandbox.container_name(r) for r in ("containment-probe-120000000001", "containment-cancel-120000000002",
                                                 "containment-probe-120000000001")]
    assert len(set(names)) == 3  # a retry of the same run id gets a fresh name as well
    assert all(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", n) and len(n) <= 63 for n in names)
    assert "containment-probe-120000000001" in names[0]
    odd = sandbox.container_name("run/with spaces:and:colons")
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", odd)
    argv = sandbox.container_command(image="aeos-worker:1", run_id="containment-cancel-1", worktree="/w",
                                     config_dir="/c", inner_argv=["x"], env={})
    assert argv[argv.index("--name") + 1].startswith("aeos-run-containment-cancel-1-")
    assert "aeos.run_id=containment-cancel-1" in argv  # teardown/reap still go by the full id label


def test_worker_status_shows_the_container_proven_only_with_a_fresh_pass(settings, monkeypatch):
    from typer.testing import CliRunner

    from aeos.cli import app, ensure_database
    from aeos.core.db import session_scope
    from aeos.worker_runtime import opencode

    monkeypatch.setattr(opencode, "opencode_status", lambda refresh=False: {
        "status": "MISSING", "version": None, "pinned_version": "1", "functional": "NOT_PROVEN"})
    _docker_ok(monkeypatch)
    ensure_database()

    def container_line(*args: str) -> str:  # the console wraps long lines; judge the unwrapped text
        flat = " ".join(CliRunner().invoke(app, ["worker", "status", *args]).output.split())
        return flat.split("sandbox container", 1)[1].split("sandbox vm", 1)[0]

    assert "strength=NOT_PROVEN" in container_line() and "PROVEN for" not in container_line()
    with session_scope() as s:
        _record(s)
    assert "strength=PROVEN for PERSONAL (containment PASS" in container_line()
    assert "strength=NOT_PROVEN" in container_line("--domain", "OFFICE")


def test_remedy_names_the_implemented_container_suite(db):
    r = facade.sandbox_readiness(db, domain="PERSONAL")
    assert any("docker build" in x and "aeos worker containment --kind container" in x for x in r["remedies"])
