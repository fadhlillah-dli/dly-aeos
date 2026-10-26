"""Wave 7 E1 - the verifier's impact-selected steps, through the REAL verifier (git candidate, runner, tamper window):

* W6-045/W6-104 negative tests are executed (a comment is not a test; a failing refusal test fails the run) and
  integration-marked tests run when the TaskSpec opts them in (a skip is NOT-PROVEN, blocking for api impact);
* W6-063 the verifier's own adversarial probes run against the candidate's whole HTTP surface for api impact;
* W6-045 a breaking facade change is blocking until the approved TaskSpec says so;
* W6-031 a candidate that changes its Python dependencies is verified in a per-candidate environment built from
  its hash-pinned lockfile (pip --require-hashes), and its frontend through npm ci --ignore-scripts;
* W6-044/W6-043 frontend_tests is PASS/FAIL from vitest (verifier config) + type check + build, with UI state
  screenshots and an a11y probe as evidence;
* W6-046 the migration round-trip seeds, backs up, restores and rolls back with data;
* W6-105 the report lists, per impact category, the checks it selected and which executed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, envs, frontend, suite, verifier
from tests.unit.test_verification_support import (
    BASE_FILES,
    FakeWorkforce,
    bind_candidate,
    make_candidate,
    make_controller,
    make_world,
    requires_git,
)

pytestmark = requires_git

FAST = {"ruff": False}
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


@pytest.fixture
def wf(monkeypatch: pytest.MonkeyPatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def _verify(db, world, **kw):
    out = verifier.verify_run(db, world.run.id, actor="tester", **kw)
    db.commit()
    return out


def _report(db, out) -> dict:
    return collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id)) or {}


ROUTER = "def get_item(i):\n    if i < 0:\n        raise ValueError('bad')\n    return i\n"


# ------------------------------------------------------------------------------- negative tests (W6-045/104)


def test_a_comment_naming_a_refusal_is_not_a_negative_test(db, settings, controller, wf):
    """The old check was a regex over added lines: '# Blocked 403' in a comment satisfied it."""
    world = make_world(db, verifier_policy={**FAST, "negative_tests_required": True})
    cand, base, head = make_candidate(settings, world, head_files={
        "backend/api/router.py": ROUTER,
        "tests/unit/test_router.py": ("from api.router import get_item\n\n\n"
                                      "def test_get_item():\n    # Blocked / 403 / PolicyDenied: not asserted\n"
                                      "    assert get_item(1) == 1\n")})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.checks["negative_tests"].status == "NOT_PROVEN", out.checks["negative_tests"]
    assert out.status == "NOT_PROVEN"


def test_an_added_refusal_test_is_executed_and_its_failure_fails_the_run(db, settings, controller, wf):
    """candidate_tests disabled: only the negative-test step executes the refusal test (it fails on the head)."""
    world = make_world(db, verifier_policy={**FAST, "negative_tests_required": True, "candidate_tests": False})
    cand, base, head = make_candidate(settings, world, head_files={
        "backend/api/router.py": "def get_item(i):\n    return i\n",  # no refusal at all
        "tests/unit/test_router.py": ("import pytest\n\nfrom api.router import get_item\n\n\n"
                                      "def test_negative_id_is_refused():\n    with pytest.raises(ValueError):\n"
                                      "        get_item(-1)\n")})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    neg = out.checks["negative_tests"]
    assert neg.status == "FAIL" and "test_negative_id_is_refused" in neg.summary, neg
    assert out.status == "FAIL"
    ev = db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.id == neg.evidence_id))
    assert ev is not None and collector.read_json(ev)["tests"][0]["outcome"] == "failed"
    # the step ran under the verifier's result seal (cluster E2) and its honest failure is consistent with it
    assert _report(db, out)["result_seal"]["negative_tests"]["status"] == "PASS"


# Candidate code imported by the change's own refusal test strips the <failure> from the junit file in an atexit
# hook and leaves with exit code 0 (os._exit spelled so the static harness-tamper scan does not see it).
FORGING_ROUTER = r'''
import atexit
import os
import re
import sys


def get_item(i):
    return i


def _forge():
    if "--junitxml" not in sys.argv:
        return
    path = sys.argv[sys.argv.index("--junitxml") + 1]
    with open(path, encoding="utf-8") as fh:
        xml = fh.read()
    xml = re.sub(r"<failure[^>]*>.*?</failure>", "", xml, flags=re.S)
    xml = re.sub(r'failures="\d+"', 'failures="0"', xml)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    sys.stdout.flush()
    getattr(os, "_" + "exit")(0)


atexit.register(_forge)
'''


def test_a_forged_negative_test_report_is_result_tampering(db, settings, controller, wf):
    """Integration of E1 and E2: the impact-selected negative-test step runs candidate code, so it is sealed like the
    trusted suite. A refusal test that fails on the head, with a forged junit and exit code 0, is never a PASS: the
    step FAILs and the verification is QUARANTINED as result tampering."""
    world = make_world(db, verifier_policy={**FAST, "negative_tests_required": True, "candidate_tests": False})
    cand, base, head = make_candidate(settings, world, head_files={
        "backend/api/router.py": FORGING_ROUTER,
        "tests/unit/test_router.py": ("import pytest\n\nfrom api.router import get_item\n\n\n"
                                      "def test_negative_id_is_refused():\n    with pytest.raises(ValueError):\n"
                                      "        get_item(-1)\n")})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.checks["negative_tests"].status == "FAIL", out.checks["negative_tests"]
    assert "result integrity (negative tests)" in out.checks["negative_tests"].summary
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert any(f["kind"] == "result" and f["path"] == "negative_tests" for f in out.tamper_findings)
    assert _report(db, out)["result_seal"]["negative_tests"]["status"] == "FAIL"
    assert out.checks["result_seal"].status == "FAIL"


# ------------------------------------------------------------------------------- integration tests (W6-104)

INTEGRATION_SKIP = ("import pytest\n\n\n@pytest.mark.integration\ndef test_needs_docker():\n"
                    "    pytest.skip('BLOCKED-docker: tool absent on this host')\n")
INTEGRATION_PASS = "import pytest\n\n\n@pytest.mark.integration\ndef test_tool_ok():\n    assert True\n"


def test_opted_in_integration_tests_run_and_a_skip_blocks_an_api_change(db, settings, tmp_path, monkeypatch, wf):
    root = make_controller(tmp_path / "ctl", extra={"tests/acceptance/test_tools.py": INTEGRATION_SKIP})
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    world = make_world(db, verifier_policy={**FAST, "integration_tests": "run"})
    cand, base, head = make_candidate(settings, world, head_files={"backend/api/router.py": ROUTER})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    it = out.checks["integration_tests"]
    assert it.status == "NOT_PROVEN" and "blocking (api impact)" in it.summary, it
    assert it.details["skipped"] == 1 and out.status == "NOT_PROVEN"
    assert not any("are not run by the verifier" in n for n in out.not_proven)


def test_opted_in_integration_tests_pass_and_fail(db, settings, tmp_path, monkeypatch, wf):
    root = make_controller(tmp_path / "ctl2", extra={"tests/acceptance/test_tools.py": INTEGRATION_PASS})
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    world = make_world(db, verifier_policy={**FAST, "integration_tests": "required"})
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.checks["integration_tests"].status == "PASS" and out.status == "PASS", (out.reasons, out.not_proven)
    assert _report(db, out)["result_seal"]["integration_trusted_acceptance"]["status"] == "PASS"  # sealed (E2)
    (root / "tests" / "acceptance" / "test_tools.py").write_text(INTEGRATION_PASS.replace("assert True", "assert False"),
                                                                 encoding="utf-8")
    world2 = make_world(db, project_key="it-fail", verifier_policy={**FAST, "integration_tests": "required"})
    cand2, base2, head2 = make_candidate(settings, world2, name="run-2", head_files={"backend/extra.py": "X = 2\n"})
    bind_candidate(db, world2, cand2, base2, head2)
    out2 = _verify(db, world2)
    assert out2.checks["integration_tests"].status == "FAIL" and out2.status == "FAIL"


# ------------------------------------------------------------------------------- adversarial pack (W6-063)


def test_api_change_without_a_pack_for_its_layout_is_stated_not_silently_covered(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/api/router.py": ROUTER})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.checks["adversarial_pack"].status == "NOT_PROVEN"
    assert any("verifier-authored adversarial cases" in n for n in out.not_proven), out.not_proven
    assert out.status == "PASS", (out.reasons, out.not_proven)  # stated, non-blocking for an absent pack
    report = _report(db, out)
    assert report["impact_checks"]["api"]["checks"]["adversarial_pack"] == "NOT_PROVEN"


def _platform_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted((REPO / "backend" / "aeos").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            files[p.relative_to(REPO).as_posix()] = p.read_text(encoding="utf-8")
    return files


@pytest.fixture
def platform_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A controller checkout with the real backend and the real shared test fixtures (the probes reuse them)."""
    root = make_controller(tmp_path / "pctl", extra={"tests/conftest.py": (REPO / "tests" / "conftest.py").read_text(
        encoding="utf-8")})
    shutil.copytree(REPO / "backend" / "aeos", root / "backend" / "aeos",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


OPEN_ROUTE = ('\n\n@router.get("/api/research-zz-open")\ndef zz_open() -> dict[str, Any]:\n'
              '    return {"ok": True}\n')


def test_verifier_probes_the_candidates_http_surface_for_api_impact(db, settings, platform_controller, wf):
    """A route that skips authentication fails the verifier-owned probes although no implementer test and no
    trusted acceptance test names that route; every other probe passes against the same candidate, and the
    generated OpenAPI documents of base and head show the change as additive (compatible)."""
    base_files = {**BASE_FILES, **_platform_files()}
    router_path = "backend/aeos/research/router.py"
    world = make_world(db, project_key="open-route", verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, base_files=base_files, head_files={
        router_path: base_files[router_path] + OPEN_ROUTE})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    adv = out.checks["adversarial_pack"]
    assert adv.status == "FAIL" and "unauthenticated" in adv.summary, (adv, out.reasons)
    assert out.status == "FAIL"
    probes = [e for e in db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.run_id == world.run.id))
              if (collector.read_json(e) or {}).get("suite") == "verifier-adversarial-pack"]
    cases = {c["test"].split("::")[-1]: c for c in collector.read_json(probes[0])["cases"]}
    failed = [name for name, c in cases.items() if c["outcome"] != "passed"]
    assert failed == ["test_every_operation_refuses_an_unauthenticated_request"], failed
    assert "/api/research-zz-open" in cases[failed[0]]["message"]
    assert len(cases) >= 10  # cross-origin, host, VIEWER, malformed JSON, traversal, hostile logins all ran
    diff = out.checks["contract_diff"]
    assert diff.status == "PASS" and diff.details["analysed"] == 1, diff
    # both generated documents came through the verifier's result seal, with their digests checked on the host
    seals = _report(db, out)["result_seal"]
    assert seals["contract_openapi_head"]["status"] == "PASS" and seals["contract_openapi_base"]["status"] == "PASS"


# ------------------------------------------------------------------------------- contract diff (W6-045)

FACADE = "def get(session, run_id, *, actor):\n    return run_id\n\n\ndef count(session):\n    return 0\n"


def test_breaking_facade_change_blocks_until_the_taskspec_approves_it(db, settings, controller, wf):
    files = {**BASE_FILES, "backend/svc/facade.py": FACADE}
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, base_files=files, head_files={
        "backend/svc/facade.py": "def get(session, run_id, *, actor, scope):\n    return run_id\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    diff = out.checks["contract_diff"]
    assert diff.status == "NOT_PROVEN" and "count: removed" in diff.summary, diff
    assert "new required keyword parameter 'scope'" in " ".join(diff.details["breaking"])
    assert out.status == "NOT_PROVEN"
    world2 = make_world(db, project_key="approved", verifier_policy={**FAST, "contract_breaking": "approved"})
    cand2, base2, head2 = make_candidate(settings, world2, name="run-2", base_files=files, head_files={
        "backend/svc/facade.py": "def get(session, run_id, *, actor, scope):\n    return run_id\n"})
    bind_candidate(db, world2, cand2, base2, head2)
    out2 = _verify(db, world2)
    assert out2.checks["contract_diff"].status == "NOT_PROVEN" and out2.status == "PASS", out2.not_proven
    world3 = make_world(db, project_key="additive", verifier_policy=FAST)
    cand3, base3, head3 = make_candidate(settings, world3, name="run-3", base_files=files, head_files={
        "backend/svc/facade.py": FACADE + "\n\ndef extra(session, *, limit=10):\n    return []\n"})
    bind_candidate(db, world3, cand3, base3, head3)
    out3 = _verify(db, world3)
    assert out3.checks["contract_diff"].status == "PASS" and out3.status == "PASS", (out3.reasons, out3.not_proven)


# ------------------------------------------------------------------------------- per-candidate env (W6-031)


def _wheel(directory: Path, name: str = "aeosprobe", version: str = "1.0.0") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}.dist-info"
    files = {f"{name}/__init__.py": "VALUE = 41\n",
             f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
             f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: aeos-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(text.encode())}")
    record.append(f"{dist}/RECORD,,")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, text in files.items():
            zf.writestr(path, text)
        zf.writestr(f"{dist}/RECORD", "\n".join(record) + "\n")
    target = directory / f"{name}-{version}-py3-none-any.whl"
    target.write_bytes(buf.getvalue())
    return target


def test_python_dependency_change_is_verified_in_a_per_candidate_environment(db, settings, controller, wf, tmp_path,
                                                                            monkeypatch):
    wheel = _wheel(tmp_path / "wheels")
    monkeypatch.setattr(envs, "PIP_INDEX_ARGS", ["--no-index", "--find-links", str(wheel.parent)])
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = "requirements-dev.txt"
    world = make_world(db, verifier_policy=FAST, allowed_paths=["backend/**", "tests/unit/**", manifest])
    cand, base, head = make_candidate(settings, world, base_files={**BASE_FILES, manifest: "# dev\n"}, head_files={
        manifest: f"# dev\naeosprobe==1.0.0 \\\n    --hash=sha256:{sha}\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    env = out.checks["dependency_env"]
    assert env.status == "PASS", (env, out.not_proven)
    assert not any("Zone 2" in n for n in out.not_proven), out.not_proven
    assert out.status == "PASS", (out.reasons, out.not_proven)
    ev = db.get(EvidenceArtifact, env.evidence_id)
    data = collector.read_json(ev)
    assert "aeosprobe==1.0.0" in data["distributions"] and data["dependency_set_hash"].startswith("sha256:")
    assert data["dependency_set_hash"] != verifier.dependency_set()["hash"]
    assert ev.environment["candidate_dependency_set_hash"] == data["dependency_set_hash"]
    # the dependency set is read by the verifier from the installed metadata (no candidate process reports it)
    assert "no candidate process" in data["listing"]["source"] and data["listing"]["problems"] == []
    assert data["startup_hooks"] == [] and data["install"]["survivors"] == []
    # the trusted suite in the candidate environment is sealed like the trusted suite (E2); the venv creation, the
    # pip install and the suite are in the verifier runner's kill-group record (the listing is no step any more)
    report = _report(db, out)
    assert report["result_seal"]["dependency_env"]["status"] == "PASS"
    steps_run = {s["step"]: s for s in report["runner"]["steps"]}
    assert {"dependency_env_venv", "dependency_env_install", "dependency_env"} <= set(steps_run)
    assert "dependency_env_listing" not in steps_run
    assert all(steps_run[k]["containment"] and steps_run[k]["survivors"] == []
               for k in ("dependency_env_venv", "dependency_env_install"))
    assert report["tamper"]["step_containment"]["survivors"] == []
    # a lockfile without hashes is refused before any install: the change stays a blocking NOT-PROVEN
    world2 = make_world(db, project_key="nohash", verifier_policy=FAST,
                        allowed_paths=["backend/**", "tests/unit/**", manifest])
    cand2, base2, head2 = make_candidate(settings, world2, name="run-2", base_files={**BASE_FILES, manifest: "# dev\n"},
                                         head_files={manifest: "# dev\naeosprobe==1.0.0\n"})
    bind_candidate(db, world2, cand2, base2, head2)
    out2 = _verify(db, world2)
    assert out2.checks["dependency_env"].status == "NOT_RUN" and "without --hash" in out2.checks["dependency_env"].summary
    assert out2.status == "NOT_PROVEN" and any("Zone 2" in n and "without --hash" in n for n in out2.not_proven)


# ------------------------------------------------------------------------------- frontend (W6-044/043/031)

FAKE_NODE = r'''
import json, sys
from pathlib import Path

script, args, cwd = Path(sys.argv[1]).name, sys.argv[2:], Path.cwd()
sc = json.loads((cwd / "scenario.json").read_text()) if (cwd / "scenario.json").is_file() else {}
if script == "npm-cli.js":
    if args[:2] != ["ci", "--ignore-scripts"]:
        print("unexpected npm call", args); sys.exit(3)
    if (cwd / ".npmrc").exists():
        print("project .npmrc must not be materialised"); sys.exit(4)
    for rel in ("node_modules/vitest/vitest.mjs", "node_modules/vite/bin/vite.js", "node_modules/typescript/bin/tsc"):
        p = cwd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("// fake tool")
    sys.exit(0)
if script == "vitest.mjs":
    cfg = Path(args[args.index("--config") + 1]).read_text()
    out = [a.split("=", 1)[1] for a in args if a.startswith("--outputFile=")][0]
    failed = int(sc.get("failed", 0))
    rows = [{"fullName": "App renders", "status": "passed"}, {"fullName": "App states", "status": "passed"}]
    rows += [{"fullName": "App breaks", "status": "failed"}] * failed
    Path(out).write_text(json.dumps({"numTotalTests": len(rows), "numPassedTests": 2, "numFailedTests": failed,
                                     "numFailedTestSuites": 0, "numPendingTests": 0,
                                     "testResults": [{"name": str(cwd / "src" / "App.test.tsx"),
                                                      "assertionResults": rows}],
                                     "trusted_config_used": "passWithNoTests: false" in cfg
                                                            and 'from "./vite.config.ts"' in cfg}))
    sys.exit(1 if failed else 0)
if script == "tsc":
    sys.exit(int(sc.get("tsc_exit", 0)))
if script == "vite.js":
    (cwd / "dist").mkdir(exist_ok=True)
    (cwd / "dist" / "index.html").write_text("<!doctype html><html lang='id'><title>x</title></html>")
    if sc.get("leak"):  # a build plugin that leaves a detached process behind (it could act after the window)
        import os, subprocess
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
    sys.exit(0)
sys.exit(9)
'''

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _frontend_world(db, settings, tmp_path, monkeypatch, *, scenario: dict, violations: list | None = None,
                    key: str = "fe"):
    fake = tmp_path / f"fake_node_{key}.py"
    fake.write_text(FAKE_NODE, encoding="utf-8")
    monkeypatch.setattr(frontend, "node_tools", lambda: ([sys.executable, str(fake)], Path("npm-cli.js"), ""))
    calls: list[Path] = []

    def fake_ui(rn, dist, out_dir, timeout=600.0, **_kw):
        calls.append(dist)
        out_dir.mkdir(parents=True, exist_ok=True)
        states = []
        for state in ("loading", "error"):
            (out_dir / f"{state}-mobile.png").write_bytes(PNG)
            states.append({"state": state, "viewport": "mobile", "screenshot": f"{state}-mobile.png", "title": "x",
                           "violations": list(violations or []), "page_errors": [], "console_errors": 0})
        return {"status": "RAN", "states": states, "rules": ["image-alt 1.1.1"], "browser": "fake"}

    monkeypatch.setattr(frontend, "run_ui", fake_ui)
    world = make_world(db, project_key=key, verifier_policy=FAST,
                       allowed_paths=["backend/**", "tests/unit/**", "frontend/**"])
    base_files = {**BASE_FILES, "frontend/package.json": json.dumps({"devDependencies": {"vitest": "^5"}}),
                  "frontend/package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {}}),
                  "frontend/tsconfig.json": "{}", "frontend/vite.config.ts": "export default {};\n",
                  "frontend/.npmrc": "registry=https://evil.example/\n",
                  "frontend/scenario.json": json.dumps(scenario)}
    cand, base, head = make_candidate(settings, world, name=f"run-{key}", base_files=base_files, head_files={
        "frontend/src/App.tsx": "export const App = () => null;\n"})
    bind_candidate(db, world, cand, base, head)
    return world, calls


def test_frontend_change_runs_npm_ci_vitest_build_and_ui_states(db, settings, controller, wf, tmp_path, monkeypatch):
    world, calls = _frontend_world(db, settings, tmp_path, monkeypatch, scenario={})
    out = _verify(db, world)
    fe = out.checks["frontend_tests"]
    assert fe.status == "PASS", (fe, out.not_proven)
    assert fe.details["steps"] == {"install": "PASS", "unit": "PASS", "typecheck": "PASS", "build": "PASS"}
    assert out.checks["ui_a11y"].status == "PASS" and calls and calls[0].name == "dist"
    assert out.status == "PASS", (out.reasons, out.not_proven)
    data = collector.read_json(db.get(EvidenceArtifact, fe.evidence_id))
    assert data["steps"]["unit"]["tests"][0]["test"] == "App renders"
    assert ".npmrc" in data["excluded"]
    shots = db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.run_id == world.run.id,
                                                      EvidenceArtifact.kind == "SCREENSHOT")).all()
    assert len(shots) == 2 and all(s.trusted for s in shots)
    report = _report(db, out)
    assert report["impact_checks"]["frontend"]["executed"] == ["frontend_tests", "ui_a11y"]
    vitest_json = data["steps"]["unit"]
    assert vitest_json["passed"] == 2
    # every tool step ran in its own kill group and is in the runner's step list; nothing outlived it
    steps_run = {s["step"]: s for s in report["runner"]["steps"]}
    for name in ("frontend_install", "frontend_vitest", "frontend_typecheck", "frontend_build"):
        assert steps_run[name]["containment"] in ("job-object", "process-group"), steps_run[name]
        assert steps_run[name]["survivors"] == []
    assert all(data["steps"][k]["survivors"] == [] for k in ("install", "unit", "typecheck", "build"))
    assert out.checks["step_containment"].status == "PASS"


def test_a_process_left_behind_by_a_frontend_tool_step_quarantines_the_run(db, settings, controller, wf, tmp_path,
                                                                          monkeypatch):
    """The frontend's npm/vitest/tsc/vite steps used to run outside any kill group: a process a build plugin left
    behind was never seen. Now it is a survivor of its step - killed, and the run is QUARANTINED."""
    world, _ = _frontend_world(db, settings, tmp_path, monkeypatch, scenario={"leak": True}, key="fe-leak")
    out = _verify(db, world)
    assert out.checks["frontend_tests"].status == "PASS"  # the tools themselves succeeded
    assert out.checks["step_containment"].status == "FAIL", out.checks["step_containment"]
    assert [s["step"] for s in out.checks["step_containment"].details["survivors"]] == ["frontend_build"]
    assert out.status == "QUARANTINED" and any("left processes running" in r for r in out.reasons), out.reasons


def test_failing_frontend_test_or_a11y_violation_fails_the_run(db, settings, controller, wf, tmp_path, monkeypatch):
    world, _ = _frontend_world(db, settings, tmp_path, monkeypatch, scenario={"failed": 1}, key="fe-fail")
    out = _verify(db, world)
    assert out.checks["frontend_tests"].status == "FAIL" and out.status == "FAIL"
    world2, _ = _frontend_world(db, settings, tmp_path, monkeypatch, scenario={}, key="fe-a11y",
                                violations=[{"rule": "image-alt", "wcag": "1.1.1", "target": "img", "detail": ""}])
    out2 = _verify(db, world2)
    assert out2.checks["frontend_tests"].status == "PASS"
    assert out2.checks["ui_a11y"].status == "FAIL" and "image-alt" in out2.checks["ui_a11y"].summary
    assert out2.status == "FAIL"


def test_frontend_step_without_node_is_stated_not_proven(db, settings, controller, wf, tmp_path, monkeypatch):
    world, _ = _frontend_world(db, settings, tmp_path, monkeypatch, scenario={}, key="fe-nonode")
    monkeypatch.setattr(frontend, "node_tools", lambda: (None, None, "node is not installed on this host"))
    out = _verify(db, world)
    fe = out.checks["frontend_tests"]
    assert fe.status == "NOT_PROVEN" and "node is not installed" in fe.summary
    assert "ui_a11y" not in out.checks and out.status == "PASS"  # visible, non-blocking (G3 provider CI)


# ------------------------------------------------------------------------------- migrations (W6-046) + map (W6-105)

_MIG_ENV = (
    "from alembic import context\n"
    "from sqlalchemy import engine_from_config, pool\n"
    "config = context.config\n\n"
    "def run_online():\n"
    "    cn = engine_from_config(config.get_section(config.config_ini_section) or "
    "{'sqlalchemy.url': config.get_main_option('sqlalchemy.url')}, prefix='sqlalchemy.', poolclass=pool.NullPool)\n"
    "    with cn.connect() as c:\n"
    "        context.configure(connection=c, render_as_batch=True)\n"
    "        with context.begin_transaction():\n"
    "            context.run_migrations()\n\n"
    "run_online()\n"
)
_REV = ("from alembic import op\nimport sqlalchemy as sa\n"
        "revision = {rev!r}\ndown_revision = {down!r}\nbranch_labels = None\ndepends_on = None\n\n"
        "def upgrade():\n    op.create_table({tbl!r}, sa.Column('id', sa.Integer, primary_key=True), "
        "sa.Column('name', sa.String(40), nullable=False))\n\n"
        "def downgrade():\n{down_body}")
_DROP = "    op.drop_table({tbl!r})\n"
_GUARDED_DROP = ("    n = op.get_bind().execute(sa.text('select count(*) from {tbl}')).scalar()\n"
                 "    if n:\n        raise RuntimeError('downgrade would drop %s row(s)' % n)\n"
                 "    op.drop_table({tbl!r})\n")


def _migration_world(db, settings, *, guarded: bool, key: str, extra_head: dict | None = None):
    mig = "backend/svc/migrations"
    world = make_world(db, project_key=key, verifier_policy=FAST,
                       allowed_paths=["backend/**", "tests/unit/**", "docs/**", "frontend/**"])
    base_files = {**BASE_FILES, f"{mig}/env.py": _MIG_ENV,
                  f"{mig}/versions/0001_init.py": _REV.format(rev="0001", down=None, tbl="widgets",
                                                              down_body=_DROP.format(tbl="widgets"))}
    body = (_GUARDED_DROP if guarded else _DROP).format(tbl="gadgets")
    cand, base, head = make_candidate(settings, world, name=f"run-{key}", base_files=base_files, head_files={
        f"{mig}/versions/0002_more.py": _REV.format(rev="0002", down="0001", tbl="gadgets", down_body=body),
        **(extra_head or {})})
    bind_candidate(db, world, cand, base, head)
    return world


def test_migration_round_trip_seeds_backs_up_restores_and_rolls_back_with_data(db, settings, controller, wf):
    world = _migration_world(db, settings, guarded=False, key="mig-ok",
                             extra_head={"frontend/src/App.tsx": "export const App = () => null;\n"})
    out = _verify(db, world)
    rb = out.checks["migration_rollback"]
    assert rb.status == "PASS", rb
    assert rb.details["seeded_tables"] == 2 and rb.details["restore_ok"] is True
    assert rb.details["data_rollback_ok"] is True and rb.details["data_rollback_target"] == "0001"
    assert _report(db, out)["result_seal"]["migration_roundtrip"]["status"] == "PASS"  # the pack driver is sealed
    assert out.checks["db_credentials"].status == "NOT_APPLICABLE"  # no deployment target registered
    report = _report(db, out)
    impact = report["impact_checks"]
    assert impact["migration"]["executed"] == ["migration_rollback"]
    assert impact["frontend"]["status"] == "NOT_PROVEN"  # no package.json: the frontend step could not run
    assert any(n.startswith("impact categories without an executed verifier check: frontend") for n in out.not_proven)


def test_a_downgrade_that_breaks_with_data_fails_the_round_trip(db, settings, controller, wf):
    world = _migration_world(db, settings, guarded=True, key="mig-data")
    out = _verify(db, world)
    rb = out.checks["migration_rollback"]
    assert rb.status == "FAIL" and "data_rollback" in rb.summary and "would drop" in rb.summary, rb
    assert out.status == "FAIL"
