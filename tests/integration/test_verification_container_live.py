"""§20 "Verifier ENF: read-only suite, isolated runner, evidence collector" on a REAL Docker host: the independent
verifier runs candidate code inside the pinned ``dly-aeos-verifier`` image (``--network none``, read-only root,
non-root, no capabilities, only the run directory mounted) once the container boundary is proven on this host
(a real PASS container containment report, the same proof that admits container workers).

Needs Docker, the worker image (for the containment proof) and the verifier image; skips cleanly without them
(``BLOCKED-docker-verifier``). A host that has all three must PASS - never a fake: nothing here is scripted."""

from __future__ import annotations

from pathlib import Path

import pytest

from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, runner, suite, verifier
from tests.unit.test_verification_support import (
    FakeWorkforce,
    bind_candidate,
    make_candidate,
    make_controller,
    make_world,
    requires_git,
)

pytestmark = [pytest.mark.integration, requires_git]
FAST = {"ruff": False}


def _require_real_docker() -> None:
    from aeos.worker_runtime import container, sandbox

    sandbox._cache.update(at=0.0, value=None)
    caps = sandbox.sandbox_capabilities(refresh=True)["levels"]["container"]
    if not caps["available"]:
        pytest.skip(f"BLOCKED-docker-verifier: {caps['evidence']} (install/start Docker, ADR-0051/ADR-0107)")
    image = container.image_status()
    if not image["present"]:
        pytest.skip(f"BLOCKED-docker-verifier: {image['evidence']}")
    vimage = container.verifier_image_status()
    if not vimage["present"]:
        pytest.skip(f"BLOCKED-docker-verifier: {vimage['evidence']}")


@pytest.fixture
def proven(db, settings):
    """A real container containment report for PERSONAL, recorded the way ``aeos worker containment`` does."""
    from aeos.worker_runtime import sandbox
    from aeos.worker_runtime.containment import containment_report, record_containment_report

    _require_real_docker()
    rep = containment_report("container", domain="PERSONAL")
    failing = {r["check"]: r["evidence"] for r in rep["results"] if r["status"] != "PASS"}
    assert rep["overall"] == "PASS", failing
    record_containment_report(db, rep, actor="cli:test")
    db.commit()
    yield rep
    sandbox._cache.update(at=0.0, value=None)


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


# Candidate tests run inside the verifier container: they see neither the controller DB (by the absolute host path
# a WEAK-runner candidate could open), nor a network, nor a writable root filesystem.
CONTAINED_PROBE = r'''
import os
import socket

DB = __DB__


def test_controller_db_is_not_reachable():
    assert not os.path.exists(DB)


def test_no_network():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(("1.1.1.1", 443))
        connected = True
    except OSError:
        connected = False
    finally:
        s.close()
    assert not connected


def test_root_filesystem_is_read_only():
    try:
        with open("/aeos-probe.txt", "w") as fh:
            fh.write("x")
        wrote = True
    except OSError:
        wrote = False
    assert not wrote


def test_runs_as_non_root():
    assert os.getuid() != 0


def test_only_the_run_directory_entries_are_mounted():
    points = set()
    with open("/proc/self/mountinfo", encoding="utf-8") as fh:
        for line in fh:
            point = line.split(" ")[4]
            if point == "/v" or point.startswith("/v/"):
                points.add(point)
    # the candidate step sees its snapshot, the trusted-suite copy, the ini, the verifier's seal plugin and the
    # report dir - never the controller checkout (/v/x*), the data dir or the host home
    assert points <= {"/v/s", "/v/tr", "/v/pytest.ini", "/v/vp", "/v/r"}, points
    assert not os.path.exists("/var/run/docker.sock") and not os.path.exists("/run/docker.sock")
'''


def test_real_verifier_container_runs_candidate_code_isolated(db, settings, controller, proven, monkeypatch):
    wf = FakeWorkforce().install(monkeypatch)
    world = make_world(db, verifier_policy=FAST)
    probe = CONTAINED_PROBE.replace("__DB__", repr(str(settings.layout.db_path)))
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n",
                                                                   "tests/unit/test_probe.py": probe})
    bind_candidate(db, world, cand, base, head)
    out = verifier.verify_run(db, world.run.id, actor="it", request_repair_on_fail=False)
    db.commit()
    assert out.verifier_runner == "container" and out.verifier_isolation == "CONTAINER", out.reasons
    assert out.status == "PASS", (out.status, out.reasons, out.not_proven,
                                  {k: (v.status, v.summary) for k, v in out.checks.items()})
    assert out.checks["trusted_suite"].status == "PASS"
    assert out.checks["candidate_tests"].status == "PASS", out.checks["candidate_tests"].summary
    assert out.checks["trust_tables"].status == "PASS" and out.checks["controller_integrity"].status == "PASS"
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert data["residual_risks"] == list(runner.CONTAINER_RESIDUAL_RISKS)
    assert runner.RESULT_FORGERY_RISK in data["residual_risks"]  # the in-process limit is stated in a container too
    assert {k: v["status"] for k, v in data["result_seal"].items()} == {"preflight": "PASS", "trusted_suite": "PASS",
                                                                         "candidate_tests": "PASS"}
    steps = data["runner"]["steps"]
    assert len(steps) >= 4 and all(s["exit_code"] in (0, 1, 5) for s in steps), steps
    assert not wf.of("request_repair")


def test_real_verifier_container_quarantines_a_forged_report(db, settings, controller, proven, monkeypatch):
    """The audit's forgery probe inside the real container: the candidate module rewrites the junit in the writable
    report mount and exits 0. The container does not stop that (same test process); the result seal does."""
    from tests.unit.test_verification_seal import FORGING_CALC

    FakeWorkforce().install(monkeypatch)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/calc.py": FORGING_CALC})
    bind_candidate(db, world, cand, base, head)
    out = verifier.verify_run(db, world.run.id, actor="it", request_repair_on_fail=False)
    db.commit()
    assert out.verifier_runner == "container", out.reasons
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert any(f["kind"] == "result" and f["path"] == "trusted_suite" for f in out.tamper_findings)
