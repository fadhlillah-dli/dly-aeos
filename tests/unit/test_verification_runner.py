"""§20 "Verifier ENF ... isolated runner" (and §25/§21 for candidate code the verifier executes): the verifier
runs candidate code in the container runner only when the container boundary is proven on this host (fresh PASS
containment report + the pinned verifier image), otherwise on the WEAK process runner with the residual risk
stated; OFFICE and ``verifier_policy.isolated_runner: required`` are BLOCKED instead (fail closed). Against a
scripted docker CLI (tests/unit/worker_container_support.py): this proves the AEOS side - selection, the
hardened argv, the mounts, path mapping and lifecycle - not the isolation of a real Docker host."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, runner, suite, verifier
from aeos.worker_runtime import container, sandbox
from aeos.worker_runtime.containment import containment_report, record_containment_report
from tests.unit.test_verification_support import (
    FakeWorkforce,
    bind_candidate,
    make_candidate,
    make_controller,
    make_world,
    requires_git,
)
from tests.unit.worker_container_support import install_fake_docker

pytestmark = requires_git
FAST = {"ruff": False}


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


@pytest.fixture
def wf(monkeypatch: pytest.MonkeyPatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


@pytest.fixture
def docker(monkeypatch, tmp_path, settings):
    fake = install_fake_docker(monkeypatch, tmp_path, verifier_image=container.verifier_image_tag())
    yield fake
    sandbox._cache.update(at=0.0, value=None)


def _prove_container(db, domain: str = "PERSONAL") -> None:
    rep = containment_report("container", domain=domain)
    assert rep["overall"] == "PASS"
    record_containment_report(db, rep, actor="cli:test")
    db.commit()


def _verify(db, world, **kw):
    out = verifier.verify_run(db, world.run.id, actor="tester", **kw)
    db.commit()
    return out


def _mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]


def test_proven_container_boundary_runs_candidate_code_in_the_verifier_container(db, settings, controller, wf,
                                                                                  docker):
    _prove_container(db)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "def mul(a, b):\n    return a * b\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "PASS", (out.reasons, out.not_proven)
    assert out.checks["trusted_suite"].status == "PASS" and out.checks["candidate_tests"].status == "PASS"
    report = db.get(EvidenceArtifact, out.report_evidence_id)
    view = verifier.report_view(report)
    assert view["verifier_isolation"] == "CONTAINER" and view["verifier_runner"] == "container"
    data = collector.read_json(report)
    assert data["runner"]["verifier_image"] == container.verifier_image_tag()
    assert data["runner"]["verifier_network"] == "none" and data["runner"]["steps"]
    assert all(s["removed"] >= 0 for s in data["runner"]["steps"])
    assert data["residual_risks"] == list(runner.CONTAINER_RESIDUAL_RISKS)
    # every step that executed candidate code went through the hardened docker argv
    runs = docker.verifier_runs()
    assert len(runs) >= 4  # preflight, collection, trusted suite, candidate tests
    data_dir = str(settings.data_dir).lower()
    trusted_only_steps = 0
    for argv in runs:
        joined = " ".join(argv)
        for flag in ("--read-only", "--network none", "--cap-drop ALL", "--security-opt no-new-privileges",
                     "--pull never", "--rm", "--pids-limit", "--memory"):
            assert flag in joined, flag
        user = argv[argv.index("--user") + 1]
        assert not user.startswith("0"), user
        for forbidden in ("docker.sock", "docker_engine", "--privileged", "SSH_AUTH_SOCK", "--network host", "-v "):
            assert forbidden not in joined, forbidden
        for m in _mounts(argv):
            src = m.split("source=", 1)[1].split(",target=", 1)[0]
            tgt = m.split(",target=", 1)[1].split(",", 1)[0]
            assert tgt.startswith("/v/"), m
            assert not src.lower().endswith("aeos.sqlite3")
            if tgt in ("/v/s", "/v/tr", "/v/pytest.ini", "/v/bs", "/v/vp") or tgt.startswith("/v/x"):
                # candidate snapshot / trusted suite / trusted code / the verifier's result-seal plugin: read-only
                assert m.endswith(",readonly"), m
            elif tgt == "/v/r":
                assert not m.endswith(",readonly")  # only the report directory is writable
            else:
                raise AssertionError(f"unexpected mount {m}")
            # the private run directory (system temp, never under the data dir) or the trusted controller backend
            # only - never the data dir, the DB or the controller checkout root
            assert src.lower() != data_dir and src != str(controller)
            assert not src.lower().startswith(data_dir), src
            assert "aeos-vw-" in src or src == str(controller / "backend"), src
        env = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
        assert env["HOME"] == "/home/verifier" and env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "SSH_AUTH_SOCK" not in env and not any(k.startswith(("AWS_", "GITHUB", "ANTHROPIC")) for k in env)
        assert argv[argv.index(container.verifier_image_tag()) + 1] == "python"
        # least privilege: the trusted controller backend is mounted only for the step that imports it (the
        # expected-id collection), never for a step that runs candidate code
        outside = [m for m in _mounts(argv) if ",target=/v/x" in m]
        if env.get("PYTHONPATH", "").startswith("/v/x"):
            trusted_only_steps += 1
            assert len(outside) == 1 and outside[0].endswith(",readonly"), outside
        else:
            assert not outside, ("a candidate step mounts the controller checkout", outside)
        if "pytest" in argv and "--collect-only" not in argv:
            # every pytest step that runs candidate code is sealed: the verifier's plugin first on PYTHONPATH and
            # loaded with -p before collection, its key file in the report dir (never in the environment)
            assert env["PYTHONPATH"].startswith("/v/vp:"), env["PYTHONPATH"]
            assert argv[argv.index("pytest") + 1:argv.index("pytest") + 3] == ["-p", "aeos_vseal"]
            assert env["AEOS_VSEAL_KEYFILE"].startswith("/v/r/.vseal-") and "KEY=" not in joined
    assert trusted_only_steps == 1  # the collection, and nothing else
    assert docker.containers() == []  # nothing left behind
    assert data["result_seal"]["trusted_suite"]["status"] == "PASS", data["result_seal"]


def test_unproven_boundary_falls_back_to_the_weak_runner_and_says_why(db, settings, controller, wf, docker):
    """Docker answers but no containment report PASSed: the WEAK process runner, with the reason and the
    residual risk on the report (never presented as isolated)."""
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "PASS"
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert data["verifier_isolation"] == "WEAK" and data["verifier_runner"] == "process"
    assert any("containment" in r for r in data["runner"]["runner_not_isolated_because"])
    assert data["runner"]["remedies"] and "verifier.Dockerfile" in data["runner"]["remedies"][0]
    assert any("NOT-PROVEN" in r and "absolute path" in r for r in data["residual_risks"])
    assert docker.verifier_runs() == []


def test_missing_verifier_image_keeps_the_weak_runner(db, settings, controller, wf, docker):
    _prove_container(db)
    docker.configure(verifier_image_present=False)
    st = runner.runner_status(db, domain="PERSONAL")
    assert st["runner"] == "process" and st["isolation"] == "WEAK"
    assert any("verifier image" in r and "not built" in r for r in st["reasons"])
    assert st["residual_risks"] == list(runner.PROCESS_RESIDUAL_RISKS)


def test_no_database_session_is_weak_with_a_plain_reason(db, settings, docker):
    """A Doctor run without the DB cannot read the containment proof: WEAK (fail closed) with that reason, not an
    opaque 'unreadable (AttributeError)'."""
    _prove_container(db)
    st = runner.runner_status(None, domain="PERSONAL")
    assert st["isolation"] == "WEAK" and st["runner"] == "process"
    assert any("no database session" in r for r in st["reasons"]), st["reasons"]


def test_ready_status_comes_only_from_a_fresh_pass_for_this_domain(db, settings, docker):
    _prove_container(db)
    assert runner.runner_status(db, domain="PERSONAL")["isolation"] == "CONTAINER"
    office = runner.runner_status(db, domain="OFFICE")  # the OFFICE profile has its own containment report
    assert office["isolation"] == "WEAK" and office["reasons"]


def test_office_verification_without_the_isolated_runner_is_blocked(db, settings, controller, wf):
    """§11/§23: OFFICE needs a verified strong boundary; the verifier never runs OFFICE candidate code on the
    WEAK process runner (fail closed)."""
    world = make_world(db, domain="OFFICE", verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "BLOCKED" and out.blocked_code == "VERIFIER_ISOLATION_UNAVAILABLE"
    assert "trusted_suite" not in out.checks and not wf.of("request_repair")


def test_taskspec_can_require_the_isolated_runner(db, settings, controller, wf):
    world = make_world(db, verifier_policy={**FAST, "isolated_runner": "required"})
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "BLOCKED" and out.blocked_code == "VERIFIER_ISOLATION_UNAVAILABLE"
    assert any("isolated_runner" in r for r in out.reasons)


def test_docker_failure_is_blocked_not_a_test_failure(db, settings, controller, wf, docker, monkeypatch):
    """A docker-side failure (exit 125: daemon error / image missing with --pull never) proves nothing about the
    candidate: BLOCKED, no repair attempt is requested."""
    _prove_container(db)
    from aeos.verification import procs

    real = procs.run_process

    def failing(args, **kw):
        if container.verifier_image_tag() in args:
            return procs.ProcResult(args=list(args), returncode=125, stdout="", stderr="docker: error", timed_out=False,
                                    duration_s=0.1)
        return real(args, **kw)

    monkeypatch.setattr(runner, "run_process", failing)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "BLOCKED" and out.blocked_code == "VERIFIER_RUNNER_FAILED"
    assert not wf.of("request_repair")


def test_container_argv_refuses_a_docker_socket_or_foreign_target(docker):
    with pytest.raises(container.ContainerError):
        container.verifier_argv(domain="PERSONAL", verify_id="x", mounts=[("/var/run/docker.sock", "/v/s", True)],
                                env={}, inner_argv=["python"])
    with pytest.raises(container.ContainerError):
        container.verifier_argv(domain="PERSONAL", verify_id="x", mounts=[("/home/owner", "/home/verifier", True)],
                                env={}, inner_argv=["python"])
    argv = container.verifier_argv(domain="OFFICE", verify_id="x", mounts=[], env={}, inner_argv=["python"])
    assert "10004:10004" in argv and "aeos.domain=OFFICE" in argv


def test_path_mapping_round_trips(tmp_path):
    rn = runner.ContainerRunner(workdir=tmp_path / "wd", domain="PERSONAL", run_id="r" * 12, image="img")
    host = tmp_path / "wd" / "s" / "backend" / "calc.py"
    assert rn.to_container(str(host)) == "/v/s/backend/calc.py"
    assert rn.to_host("/v/s/backend/calc.py") == host
    outside = tmp_path / "ctl" / "backend"
    assert rn.share(outside) == "/v/x0" and rn.to_container(str(outside / "aeos")) == "/v/x0/aeos"
    assert rn.basetemp("b1") == "/tmp/b1"


def test_process_runner_counts_an_escaped_pipe_holder_as_a_survivor_and_states_degraded_groups(tmp_path):
    """A step whose output pipe stayed open after its whole kill group was killed had a process outside the group:
    a survivor that could not be killed (QUARANTINED), never a clean step. A step whose kill group fell back to the
    environment-marker sweep is a containment gap (NOT-PROVEN), and describe() reports what the steps really got."""
    from aeos.verification.procs import PIPE_HELD_ERROR

    rn = runner.ProcessRunner(workdir=tmp_path)
    strong = "job-object" if sys.platform == "win32" else "process-group"
    assert rn.describe()["verifier_step_containment"] == strong
    rn.steps += [
        {"step": "clean", "containment": strong, "survivors": [], "containment_errors": []},
        {"step": "escaped", "containment": strong, "survivors": [], "containment_errors": [PIPE_HELD_ERROR]},
        {"step": "orphan", "containment": strong, "survivors": [{"pid": 4242, "name": "python"}],
         "containment_errors": []},
        {"step": "weak", "containment": "marker-sweep", "survivors": [],
         "containment_errors": ["job object unavailable (5)"]},
    ]
    surv = {s["step"]: s["survivors"] for s in rn.survivors()}
    assert set(surv) == {"escaped", "orphan"}
    assert surv["escaped"][0]["pid"] is None and "outside the kill group" in surv["escaped"][0]["name"]
    gaps = rn.containment_gaps()
    assert len(gaps) == 1 and gaps[0].startswith("weak: kill group marker-sweep") and "job object" in gaps[0]
    assert rn.describe()["verifier_step_containment"] == ",".join(sorted({strong, "marker-sweep"}))
    container_rn = runner.ContainerRunner(workdir=tmp_path / "c", domain="PERSONAL", run_id="r" * 12, image="img")
    assert container_rn.survivors() == [] and container_rn.containment_gaps() == []


def test_container_runner_runs_the_migration_round_trip_in_the_writable_report_dir(db, settings, controller, wf,
                                                                                    docker):
    """§16/§31 migration round-trip under the container runner: the throwaway DB must live in the only writable
    mount (/v/r) - the container's root filesystem and the rest of the run dir are read-only (review 2026-10-19:
    the DB was placed at /v/mig.sqlite3, so every container round-trip FAILED)."""
    from tests.unit.test_verification_verifier import _alembic_candidate

    _prove_container(db)
    world = _alembic_candidate(db, settings, good=True)
    out = _verify(db, world)
    assert out.verifier_runner == "container"
    assert out.checks["migration_rollback"].status == "PASS", out.checks["migration_rollback"].summary
    assert out.status == "PASS", (out.reasons, out.not_proven)
    mig_runs = [a for a in docker.verifier_runs() if any("alembic" in x for x in a)]
    assert mig_runs, "the round-trip ran through the hardened verifier container"
    assert mig_runs[0][-1] == "/v/r/mig.sqlite3"


def test_a_container_worker_run_is_never_verified_on_the_weak_runner(db, settings, controller, wf, docker):
    """Isolation is never downgraded: code a worker produced inside the container sandbox is not executed on the
    host by the WEAK process runner when the verifier container is unavailable (here: the verifier image is not
    built) - BLOCKED with the remedy, no repair attempt, nothing executed."""
    _prove_container(db)
    docker.configure(verifier_image_present=False)
    world = make_world(db, verifier_policy=FAST)
    world.run.sandbox_kind = "container"
    db.commit()
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "BLOCKED" and out.blocked_code == "VERIFIER_ISOLATION_UNAVAILABLE", (out.status, out.reasons)
    assert any("never downgraded" in r and "container sandbox" in r for r in out.reasons), out.reasons
    assert "trusted_suite" not in out.checks and "candidate_tests" not in out.checks
    assert not wf.of("request_repair")
    assert docker.verifier_runs() == []
    # a process-sandbox run on the same host still uses the (stated) WEAK runner
    assert not runner.isolation_required({}, "PERSONAL", "process")
    assert runner.isolation_required({}, "PERSONAL", "container") and runner.isolation_required({}, "PERSONAL", "vm")


def test_verifier_workdir_is_private_and_outside_the_data_dir(settings):
    """The run dir every candidate-visible path lives in is never under the data dir (no DB-path derivation from
    HOME/TMP/AEOS_DATA_DIR/cwd), and is private to the controller user."""
    import os
    import shutil
    import stat

    wd = runner.new_workdir()
    try:
        assert wd.is_dir() and "aeos-vw-" in wd.name
        data_root = settings.layout.root.resolve()
        assert data_root not in wd.parents and wd != data_root
        if os.name != "nt":
            assert stat.S_IMODE(wd.stat().st_mode) == 0o700
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def test_verifier_image_installs_exactly_the_pinned_lock():
    """sandbox/verifier.Dockerfile: a pinned base image, a non-root user, and only the reviewed lock installed
    (no unpinned packages, no invalid pip flag such as ``--require-hashes=false`` that breaks the build)."""
    import re

    from aeos.core.paths import repo_root

    text = (repo_root() / "sandbox" / "verifier.Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"^FROM\s+(\S+)", text, re.M)
    assert base and ":" in base.group(1) and not base.group(1).endswith(":latest")
    assert re.search(r"^USER\s+10003:10003\s*$", text, re.M)
    installs = [m.group(1) for m in re.finditer(r"pip install ([^\n&]*)", text)]
    assert installs, "the image installs the lock"
    for args in installs:
        tokens = [t for t in args.split() if t != "\\"]  # a trailing line continuation
        assert "--no-deps" in tokens and "-r" in tokens, args
        assert not [t for t in tokens if t.startswith("--") and t.endswith(("=false", "=true"))], args
        positional = [t for i, t in enumerate(tokens) if not t.startswith("-") and tokens[i - 1] != "-r"]
        assert not positional, f"unpinned packages installed outside the lock: {positional}"
