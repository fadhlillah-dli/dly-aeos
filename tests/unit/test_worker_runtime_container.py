"""Container sandbox (§11, §25, ADR-0051 amendment): launch argv, internal network + broker gateway, teardown,
orphan reaping and the containment-suite verdict - against a scripted docker CLI (real child processes).
A real Docker host proves the isolation itself (tests/acceptance/test_containment.py, integration)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aeos.worker_runtime import container, processes, sandbox
from aeos.worker_runtime.containment import containment_report, record_containment_report
from tests.unit.worker_container_support import compliant_probe, install_fake_docker


@pytest.fixture
def docker(monkeypatch, tmp_path, settings):
    fake = install_fake_docker(monkeypatch, tmp_path)
    yield fake
    sandbox._cache.update(at=0.0, value=None)


def _by(report):
    return {r["check"]: r for r in report["results"]}


def test_worker_argv_is_hardened_and_mounts_only_worktree_and_config(tmp_path, docker):
    prof = container.profile_for("PERSONAL")
    argv = container.worker_argv(profile=prof, image=container.image_tag(), run_id="run-123456789012345",
                                 worktree=tmp_path / "wt", config_dir=tmp_path / "cfg", inner_argv=["run", "x"],
                                 env=container.worker_env("run-123456789012345", {"OPENCODE_CONFIG": "/aeos/opencode.json"}),
                                 limits={"max_memory_mb": 1024, "max_processes": 128})
    joined = " ".join(argv)
    for flag in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--user 10001:10001",
                 "--network aeos-worker", "--memory 1024m", "--pids-limit 128", "--label aeos.run_id=run-123456789012345"):
        assert flag in joined, flag
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert len(mounts) == 2 and mounts[0].endswith("target=/workspace") and mounts[1].endswith("target=/aeos,readonly")
    for forbidden in ("docker.sock", "--privileged", "/.ssh", "--network host", "SSH_AUTH_SOCK", "-v "):
        assert forbidden not in joined, forbidden
    env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert "HOME=/home/worker" in env and not any("TOKEN" in e or "aeoscap_" in e for e in env)
    office = container.worker_argv(profile=container.profile_for("OFFICE"), image="img", run_id="r", worktree=tmp_path,
                                   config_dir=tmp_path, inner_argv=[], env={})
    assert "--user 10002:10002" in " ".join(office) and "--network aeos-worker-office" in " ".join(office)


def test_network_is_created_internal_and_an_external_one_is_refused(docker):
    prof = container.profile_for("PERSONAL")
    assert container.ensure_network(prof) == {"network": "aeos-worker", "internal": True, "created": True}
    create = next(c for c in docker.calls() if c[:2] == ["network", "create"])
    assert "--internal" in create
    (docker.state / "networks" / "aeos-worker").write_text("false", encoding="utf-8")
    with pytest.raises(container.ContainerError) as exc:
        container.ensure_network(prof)
    assert exc.value.code == "NETWORK_NOT_INTERNAL"


def test_gateway_forwards_only_the_broker_path_and_is_reused(docker, settings):
    prof = container.profile_for("PERSONAL")
    container.ensure_network(prof)
    assert container.ensure_gateway(prof, broker_port=8787, image=container.image_tag())["reused"] is False
    run = next(c for c in docker.calls() if c[:2] == ["run", "-d"])
    assert "--read-only" in run and "--cap-drop" in run and "host.docker.internal:host-gateway" in run
    assert container.GATEWAY_JS in run and "'/broker/v1/'" in container.GATEWAY_JS and "403" in container.GATEWAY_JS
    assert any(c[:3] == ["network", "connect", "bridge"] for c in docker.calls())
    assert container.ensure_gateway(prof, broker_port=8787, image=container.image_tag())["reused"] is True
    assert container.broker_url(prof) == "http://aeos-broker-gw:8787/broker/v1"


def test_kill_controller_removes_the_run_container_by_label(tmp_path, docker):
    """§22/§25 kill controller: killing the docker CLI client does not stop a container - teardown does."""
    rid = "run-kill-0001"
    argv = container.worker_argv(profile=container.profile_for("PERSONAL"), image=container.image_tag(), run_id=rid,
                                 worktree=tmp_path, config_dir=tmp_path, inner_argv=["-e", "x"], env={},
                                 entrypoint="node")
    processes.register_container(rid)
    mp = processes.spawn(rid, argv, cwd=tmp_path, env=container.docker_cli_env(), capture_stdout=False)
    import time

    deadline = time.monotonic() + 30
    while not docker.containers() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert docker.containers(), "the cancel-probe container started"
    res = processes.kill_run_processes(rid)
    assert docker.containers() == [] and "container-rm" in res["methods"] and res["containers_remaining"] == []
    assert not processes.is_alive(mp.pid, mp.create_time)


def test_orphan_containers_of_inactive_runs_are_reaped(docker):
    import json

    (docker.state / "containers").mkdir(exist_ok=True)
    for cid, rid in (("aaa", "active-run"), ("bbb", "dead-run")):
        (docker.state / "containers" / f"{cid}.json").write_text(json.dumps({"run_id": rid}), encoding="utf-8")
    assert container.reap_orphans({"active-run"}) == ["dead-run"]
    assert docker.containers() == ["aaa"]


def test_containment_suite_passes_only_with_positive_evidence(docker, db, settings):
    rep = containment_report("container")
    by = _by(rep)
    assert rep["overall"] == "PASS", {k: v for k, v in by.items() if v["status"] != "PASS"}
    assert set(container.ISOLATION_CHECKS) <= set(by) and rep["evidence_level"] == "E2"
    assert rep["image"] == container.image_tag() and rep["image_id"] == "sha256:fakeimage"
    # the probe ran inside the worker image with the hardened argv
    probe = next(c for c in docker.calls() if c[:1] == ["run"] and any(a.endswith("probe.js") for a in c))
    assert "--read-only" in probe and "--network" in probe and "--user" in probe
    record_containment_report(db, rep, actor="cli:test")
    db.flush()
    r = sandbox.sandbox_readiness(db, domain="PERSONAL")
    assert r["status"] == "READY" and r["kind"] == "container"


@pytest.mark.parametrize("leak,check", [
    ({"egress": [{"host": "1.1.1.1", "port": 443, "connected": True}]}, "egress_non_broker"),
    ({"mounts": [{"point": "/"}, {"point": "/workspace"}, {"point": "/home/owner/.ssh"}]}, "mounts_allowlisted"),
    ({"identity": {"uid": 0, "gid": 0}}, "non_root"),
    ({"writes": {"root": True, "etc": False, "usr": False, "config": False, "worktree": True}}, "read_only_root"),
    ({"sockets": {"docker": True, "ssh_agent": False, "dbus": False, "run_user": False}}, "docker_socket_absent"),
    ({"host_paths": {"outside_file": False, "db": True, "data_dir": True, "real_ssh": False, "controller": False}},
     "read_data_db"),
    ({"cgroup": {"memory": "max", "pids": "max"}}, "resource_limits"),
    ({"gateway_filter": {"status": 200, "gateway": "forwarded"}}, "gateway_path_filter"),
    ({"credentials": {"git_credentials": False, "gh_hosts": True, "helpers": []}}, "credential_helpers_absent"),
])
def test_any_observed_leak_fails_the_suite(docker, leak, check):
    docker.configure(probe={**compliant_probe(), **leak})
    rep = containment_report("container")
    assert rep["overall"] == "FAIL" and _by(rep)[check]["status"] == "FAIL", _by(rep)[check]


def test_missing_image_or_external_network_is_never_a_pass(docker):
    docker.configure(image_present=False)
    rep = containment_report("container")
    assert rep["overall"] == "NOT_PROVEN" and "docker build" in rep["remedy"]
    assert {r["status"] for r in rep["results"]} == {"NOT_RUN"}
    docker.configure(image_present=True)
    (docker.state / "networks").mkdir(exist_ok=True)
    (docker.state / "networks" / "aeos-worker").write_text("false", encoding="utf-8")
    rep = containment_report("container")
    assert rep["overall"] == "FAIL" and _by(rep)["network_setup"]["status"] == "FAIL"


def test_unparsable_probe_output_fails(docker):
    docker.configure(probe="not json")
    rep = containment_report("container")
    assert rep["overall"] != "PASS"


def test_evaluate_probe_never_passes_an_unreported_section():
    results = container.evaluate_probe({}, profile=container.profile_for("PERSONAL"))
    assert {r["status"] for r in results} == {"NOT_RUN"}
    assert container._summary(results)[0] == "NOT_PROVEN"
    partial = {k: v for k, v in compliant_probe().items() if k != "sockets"}
    by = {r["check"]: r["status"] for r in container.evaluate_probe(partial, profile=container.profile_for("PERSONAL"))}
    assert by["keyring_unreachable"] == "NOT_RUN" and by["docker_socket_absent"] == "NOT_RUN"


def test_office_profile_report_is_recorded_separately(docker, db):
    rep = containment_report("container", domain="OFFICE")
    assert rep["domain"] == "OFFICE" and rep["network"] == "aeos-worker-office"
    probe = next(c for c in docker.calls() if c[:1] == ["run"] and any(a.endswith("probe.js") for a in c))
    assert "10002:10002" in probe
    record_containment_report(db, rep, actor="cli:test")
    from aeos.models.identity import SystemSetting

    assert db.get(SystemSetting, "worker_runtime.containment.container.office").value["domain"] == "OFFICE"
    assert db.get(SystemSetting, "worker_runtime.containment.container") is None


def test_probe_scripts_are_fixed_text(tmp_path: Path):
    # The in-container probe never reads secret content and reports reachability only.
    assert "readFileSync(a.db_path" not in container.PROBE_JS
    assert "canRead(a.db_path)" in container.PROBE_JS and "process.stdout.write(JSON.stringify(r)" in container.PROBE_JS
