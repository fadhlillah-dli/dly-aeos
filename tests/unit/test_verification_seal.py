"""§20 Verifier ENF / §34 evidence honesty (review W6-054): candidate code imported by a test runs inside the pytest
process whose report the verifier reads. The verifier-owned result seal (``aeos.verification.seal``) cross-checks
the junit report, the exit code and the terminal summary against what its own plugin streamed while pytest ran, so
a candidate that forges its report after the fact (``atexit`` rewrites the junit, ``os._exit(0)``) is QUARANTINED
instead of verifying as PASS."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, procs, runner, seal, suite, verifier
from aeos.verification.junit import parse_junit
from tests.unit.test_verification_support import (
    FakeWorkforce,
    bind_candidate,
    make_candidate,
    make_controller,
    make_world,
    requires_git,
    write_files,
)

FAST = {"ruff": False}

PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
GOOD_CALC = "def add(a, b):\n    return a + b\n"

# The audit's result-forgery probe: candidate code imported by a trusted test strips the <failure> from the junit
# file in an atexit hook and leaves with exit code 0 (os._exit spelled so the static harness-tamper scan does not
# see it - the dynamic seal must catch it on its own).
FORGING_CALC = r'''
import atexit
import os
import re
import sys


def add(a, b):
    return a - b


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


def _sealed_pytest(tmp_path: Path, tests: dict[str, str], backend: dict[str, str] | None = None,
                   *, sealed: bool = True, extra_env: dict[str, str] | None = None):
    """One pytest step exactly as the verifier runs it on the process runner (sealed, contained)."""
    wd = tmp_path / "wd"
    (wd / "r").mkdir(parents=True)
    seal.install_plugin(wd)
    write_files(wd / "t", tests)
    write_files(wd / "b", backend or {"calc.py": GOOD_CALC})
    ini = wd / "pytest.ini"
    ini.write_text(verifier.PYTEST_INI, encoding="utf-8")
    st = seal.StepSeal.new(wd, "probe")
    junit = wd / "r" / "j.xml"
    env = procs.scrubbed_env(pythonpath=[wd / "vp", wd / "b"], home=wd / "h", tmp=wd / "tmp", data_dir=wd / "d",
                             disable_plugin_autoload=True)
    env.update(st.child_env(str(st.keyfile)))
    env.update(extra_env or {})
    args = [sys.executable, "-m", "pytest", *(["-p", seal.PLUGIN_MODULE] if sealed else []), str(wd / "t"),
            "-c", str(ini), "--rootdir", str(wd / "t"), "-p", "no:cacheprovider", "-q", "--junitxml", str(junit)]
    res = procs.run_process(args, cwd=wd, env=env, timeout=180, contain=True)
    consumed = st.discard()
    report = parse_junit(junit) if junit.is_file() else None
    check = seal.check_pytest(st, stdout=res.stdout, returncode=res.returncode, timed_out=res.timed_out,
                              junit_path=junit, report=report, key_consumed=consumed)
    return st, res, report, check


def test_an_honest_run_is_consistent_and_the_key_never_reaches_candidate_code(tmp_path: Path):
    # candidate code imported at collection: the key file is already deleted and its variables are gone
    probe = ("import os\n\nLEFT = [p for p in os.listdir('r') if p.startswith('.vseal-')]\n\n\n"
             "def test_key_file_is_gone():\n"
             "    assert LEFT == []\n"
             "    assert 'AEOS_VSEAL_KEYFILE' not in os.environ and 'AEOS_VSEAL_NONCE' not in os.environ\n")
    _, res, report, check = _sealed_pytest(tmp_path, {"test_a.py": PASSING, "test_b.py": FAILING,
                                                      "test_c.py": probe})
    assert report is not None and report.failures == 0 and report.passed == 3, res.tail()
    assert check.status == "PASS", check.problems
    assert check.key_consumed is True and check.exit_status == 0 and check.events >= 5
    assert seal.PREFIX not in res.tail()  # channel lines are stripped from evidence tails


def test_a_failing_run_is_consistent_too(tmp_path: Path):
    _, res, report, check = _sealed_pytest(tmp_path, {"test_b.py": FAILING},
                                           {"calc.py": "def add(a, b):\n    return a - b\n"})
    assert res.returncode == 1 and report is not None and report.failures == 1
    assert check.status == "PASS", check.problems


def test_atexit_junit_rewrite_and_exit_code_forgery_is_detected(tmp_path: Path):
    """The audit probe: without the seal the forged junit reads 'passed 1, failures 0' with exit 0."""
    _, res, report, check = _sealed_pytest(tmp_path, {"test_b.py": FAILING}, {"calc.py": FORGING_CALC})
    assert res.returncode == 0 and report is not None and report.failures == 0 and report.passed == 1  # forged
    assert check.status == "FAIL", check.problems
    text = " ".join(check.problems)
    assert "exit status 1 but the process exited with 0" in text
    assert "junit report changed after pytest wrote it" in text
    assert "differ from what pytest streamed" in text


def test_a_session_that_never_ends_sealed_is_missing_not_pass(tmp_path: Path):
    """os._exit inside a test (or a fake junit written by candidate code): no sealed session end -> MISSING."""
    early = ("import os\n\n\ndef test_leave():\n"
             "    open(os.environ['FAKE_JUNIT'], 'w').write('<testsuite tests=\"1\"><testcase classname=\"x\" "
             "name=\"y\"/></testsuite>')\n"
             "    getattr(os, '_' + 'exit')(0)\n")
    _, res, report, check = _sealed_pytest(tmp_path, {"test_e.py": early},
                                           extra_env={"FAKE_JUNIT": str(tmp_path / "wd" / "r" / "j.xml")})
    assert res.returncode == 0 and report is not None and report.passed == 1  # the fake report alone would pass
    assert check.status == "MISSING" and "before pytest finished its session" in check.problems[-1]


def test_without_the_plugin_nothing_is_sealed(tmp_path: Path):
    _, _res, _report, check = _sealed_pytest(tmp_path, {"test_a.py": PASSING}, sealed=False)
    assert check.status == "MISSING" and "reported nothing" in check.problems[-1]
    assert check.key_consumed is False


def test_forged_channel_lines_and_terminal_summaries_are_detected(tmp_path: Path):
    st, res, report, check = _sealed_pytest(tmp_path, {"test_a.py": PASSING})
    assert check.status == "PASS"
    forged = res.stdout + "\n" + seal.PREFIX + '{"e":"finish","i":99,"n":"' + st.nonce + '","x":0} ' + "0" * 64 + "\n"
    bad = seal.check_pytest(st, stdout=forged, returncode=res.returncode, timed_out=False,
                            junit_path=tmp_path / "wd" / "r" / "j.xml", report=report)
    assert bad.status == "FAIL" and any("invalid MAC" in p for p in bad.problems)
    printed = res.stdout + "\n5 passed in 0.01s\n"  # a summary printed after pytest's own (atexit)
    bad2 = seal.check_pytest(st, stdout=printed, returncode=res.returncode, timed_out=False,
                             junit_path=tmp_path / "wd" / "r" / "j.xml", report=report)
    assert bad2.status == "FAIL" and any("terminal summary differs" in p for p in bad2.problems)


# An honest failing test whose output shows another seal session's lines - e.g. a self-build candidate's own verifier
# test that prints a nested run's stdout, or whose failing assertion's repr contains it.
NESTED_SEAL_OUTPUT = (
    "def test_nested_run_output_is_shown():\n"
    "    out = '\\n@@AEOS-VSEAL1 {\"e\":\"finish\",\"i\":0,\"n\":\"" + "f" * 32 + "\",\"x\":0} ' + '0' * 64 + '\\n'\n"
    "    print(out)\n"
    "    assert out == ''\n")


def test_another_sessions_seal_lines_never_read_as_tampering(tmp_path: Path):
    """Only lines naming this step's nonce are its channel. Before, any seal-format line with a MAC that did not
    verify was a forgery, so an honest failing test that printed a nested run's output was QUARANTINED."""
    _, res, report, check = _sealed_pytest(tmp_path, {"test_n.py": NESTED_SEAL_OUTPUT})
    assert res.returncode == 1 and report is not None and report.failures == 1
    assert check.status == "PASS", check.problems
    assert check.other_session_lines >= 2  # the printed line and the assertion's repr of it


def test_a_key_file_left_behind_is_never_a_pass(tmp_path: Path):
    """The plugin read the key but could not remove its file: code under test could have read the key, so the seal
    proves nothing - the step cannot pass (MISSING), without blaming the candidate for tampering."""
    st, res, report, check = _sealed_pytest(tmp_path, {"test_a.py": PASSING})
    assert check.status == "PASS" and check.key_consumed is True
    left = seal.check_pytest(st, stdout=res.stdout, returncode=res.returncode, timed_out=False,
                             junit_path=tmp_path / "wd" / "r" / "j.xml", report=report, key_consumed=False)
    assert left.status == "MISSING" and left.problems == [seal.KEY_LEFT_PROBLEM]


def test_a_long_failing_run_keeps_its_whole_sealed_stream(tmp_path: Path):
    """The stdout evidence tail is bounded; the seal lines are collected in full beside it, so a run whose output
    exceeds the bound is still consistent (losing the first events would read as tampering)."""
    noisy = "import pytest\n\n\n@pytest.mark.parametrize('n', range(40))\ndef test_noisy(n):\n    assert n < 0, 'x' * 4000\n"
    wd = tmp_path / "wd"
    (wd / "r").mkdir(parents=True)
    seal.install_plugin(wd)
    write_files(wd / "t", {"test_n.py": noisy})
    ini = wd / "pytest.ini"
    ini.write_text(verifier.PYTEST_INI, encoding="utf-8")
    st = seal.StepSeal.new(wd, "noisy")
    junit = wd / "r" / "j.xml"
    env = procs.scrubbed_env(pythonpath=[wd / "vp"], home=wd / "h", tmp=wd / "tmp", data_dir=wd / "d",
                             disable_plugin_autoload=True)
    env.update(st.child_env(str(st.keyfile)))
    res = procs.run_process([sys.executable, "-m", "pytest", "-p", seal.PLUGIN_MODULE, str(wd / "t"), "-c", str(ini),
                             "--rootdir", str(wd / "t"), "-p", "no:cacheprovider", "-q", "--junitxml", str(junit)],
                            cwd=wd, env=env, timeout=180, contain=True, max_output=20_000)
    assert len(res.stdout) <= 20_000 and res.channel.count(seal.PREFIX) >= 40
    check = seal.check_pytest(st, stdout=res.stdout, channel=res.channel, returncode=res.returncode,
                              timed_out=res.timed_out, junit_path=junit, report=parse_junit(junit))
    assert check.status == "PASS", check.problems


def test_terminal_summary_parser():
    assert seal.terminal_summary("x\n1 failed, 2 passed, 1 warning in 0.12s\n") == {
        "failed": 1, "passed": 2, "warnings": 1}
    assert seal.terminal_summary("==== 2 errors, 3 deselected in 75.10s (0:01:15) ====") == {
        "error": 2, "deselected": 3}
    assert seal.terminal_summary("no tests ran in 0.01s") == {}
    assert seal.terminal_summary("nothing here") is None


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


@requires_git
def test_a_candidate_that_forges_its_trusted_suite_report_is_quarantined(db, settings, controller, monkeypatch):
    """End to end on the process runner: the trusted acceptance suite fails against the buggy candidate, whose
    imported module rewrites the junit and exits 0. That used to verify as PASS; it is QUARANTINED now, with a HIGH
    alert and no repair loop (result tampering is special review, not an ordinary failure)."""
    wf = FakeWorkforce().install(monkeypatch)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/calc.py": FORGING_CALC})
    bind_candidate(db, world, cand, base, head)
    out = verifier.verify_run(db, world.run.id, actor="tester")
    db.commit()
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert out.quarantine_reason and "forged or broke its test result" in out.quarantine_reason
    assert any(f["kind"] == "result" and f["path"] == "trusted_suite" for f in out.tamper_findings)
    assert out.checks["trusted_suite"].status == "FAIL" and out.checks["result_seal"].status == "FAIL"
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert data["result_seal"]["trusted_suite"]["status"] == "FAIL"
    assert any("result integrity" in r for r in data["reasons"])
    assert any("result seal" in r for r in data["residual_risks"])  # the in-process limit is stated
    assert not wf.of("request_repair")


@requires_git
def test_an_honest_candidate_still_passes_sealed(db, settings, controller, monkeypatch):
    FakeWorkforce().install(monkeypatch)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = verifier.verify_run(db, world.run.id, actor="tester")
    db.commit()
    assert out.status == "PASS", (out.reasons, out.not_proven)
    assert out.checks["result_seal"].status == "PASS"
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert {k: v["status"] for k, v in data["result_seal"].items()} == {"preflight": "PASS", "trusted_suite": "PASS",
                                                                         "candidate_tests": "PASS"}


# The import preflight imports the candidate's packages (candidate code runs) to prove they resolve inside the
# snapshot. A module that recognises the preflight driver (``python -c``) prints a forged "all inside" mapping and
# leaves before the driver's sealed end - and behaves normally under pytest, so the trusted suite passes.
FORGED_PREFLIGHT = r'''
import json
import os
import sys

if sys.argv and sys.argv[0] == "-c":
    print(json.dumps({"calc": __file__, "extra": __file__}))
    sys.stdout.flush()
    getattr(os, "_" + "exit")(0)
X = 1
'''

# ... or prints a line in the seal's format naming the step (the nonce is readable in the loaded plugin; the key file
# is already gone, so the MAC cannot be valid) and carries on.
FAKE_SEAL_LINE = r'''
import sys

if sys.argv and sys.argv[0] == "-c":
    _n = sys.modules["aeos_vseal"]._S["nonce"]
    print('@@AEOS-VSEAL1 {"d":{"extra":"/elsewhere/extra.py"},"e":"result","i":0,"n":"' + _n + '"} ' + "0" * 64)
X = 1
'''

# ... a seal-format line of another session (a guessed nonce): never this step's channel, so it is neither used as
# the mapping nor taken for a forgery - the sealed mapping of the driver itself decides.
OTHER_SESSION_LINE = r'''
import sys

if sys.argv and sys.argv[0] == "-c":
    print('@@AEOS-VSEAL1 {"d":{"extra":"/elsewhere/extra.py"},"e":"result","i":0,"n":"x"} ' + "0" * 64)
X = 1
'''


@requires_git
def test_a_preflight_that_never_reaches_its_sealed_end_is_never_a_pass(db, settings, controller, monkeypatch):
    """Before the seal the forged last stdout line was taken as the import mapping and the run verified as PASS."""
    FakeWorkforce().install(monkeypatch)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": FORGED_PREFLIGHT})
    bind_candidate(db, world, cand, base, head)
    out = verifier.verify_run(db, world.run.id, actor="tester")
    db.commit()
    assert out.status == "NOT_PROVEN", (out.status, out.reasons, out.not_proven)
    assert out.checks["trusted_suite"].status == "PASS"  # the module behaves under pytest
    assert any("preflight produced no sealed result" in n for n in out.not_proven), out.not_proven
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert data["result_seal"]["preflight"]["status"] == "MISSING"


@requires_git
def test_a_forged_seal_line_in_the_preflight_is_result_tampering(db, settings, controller, monkeypatch):
    wf = FakeWorkforce().install(monkeypatch)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": FAKE_SEAL_LINE})
    bind_candidate(db, world, cand, base, head)
    out = verifier.verify_run(db, world.run.id, actor="tester")
    db.commit()
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert any(f["kind"] == "result" and f["path"] == "preflight" and "invalid MAC" in f["change"]
               for f in out.tamper_findings), out.tamper_findings
    assert not wf.of("request_repair")
    assert not wf.of("set_scheduling_paused")  # nothing stays in effect: no integrity hold


@requires_git
def test_another_sessions_seal_line_in_the_preflight_is_ignored(db, settings, controller, monkeypatch):
    FakeWorkforce().install(monkeypatch)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": OTHER_SESSION_LINE})
    bind_candidate(db, world, cand, base, head)
    out = verifier.verify_run(db, world.run.id, actor="tester")
    db.commit()
    assert out.status == "PASS", (out.status, out.reasons, out.not_proven)
    assert not out.tamper_findings
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert data["result_seal"]["preflight"]["status"] == "PASS"


def test_every_report_states_the_in_process_forgery_risk():
    """Both runners: a container separates candidate code from the host, not from its own test process."""
    for risks in (runner.PROCESS_RESIDUAL_RISKS, runner.CONTAINER_RESIDUAL_RISKS):
        assert runner.RESULT_FORGERY_RISK in risks
    assert "subverts the plugin itself" in runner.RESULT_FORGERY_RISK
    assert "NOT-PROVEN" in runner.RESULT_FORGERY_RISK
    # the process runner also states that a later step reads what an earlier candidate step left in the run dir
    assert any("run directory is writable by candidate code" in r for r in runner.PROCESS_RESIDUAL_RISKS)


def test_the_plugin_name_cannot_be_shadowed_by_a_candidate(tmp_path: Path):
    backend = tmp_path / "cand" / "backend"
    write_files(backend, {"aeos_vseal.py": "X = 1\n"})
    (tmp_path / "ctl" / "backend").mkdir(parents=True)
    assert verifier.shadowed_modules(backend, tmp_path / "ctl" / "backend") == ["aeos_vseal"]
