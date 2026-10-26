"""Verifier-owned result seal for test steps that run candidate code (§20 Verifier ENF, §34; review W6-054).

Candidate code imported by a test runs *inside* the pytest process whose report the verifier reads. Without a
cross-check such code could rewrite the ``--junitxml`` file from an ``atexit`` hook and leave with ``os._exit(0)``,
and a failing trusted suite would read as PASS (the verifier's own checks - expected ids present, exit code
consistent with the junit - are defeated by forging both). The seal raises that bar:

* ``aeos_vseal`` - a pytest plugin owned by the verifier (``PLUGIN_SOURCE``, written into the run directory and put
  first on ``PYTHONPATH``) - is loaded with ``-p`` *before* collection, i.e. before any candidate module is imported.
  At import it reads a per-step random key from a key file in the run's report directory and deletes the file (so
  code imported later cannot read it), and duplicates the real stdout before pytest starts capturing.
* While the session runs it streams every collection/test outcome, and at the very end of ``pytest_sessionfinish``
  (``trylast``: after the junit writer) the session exit status, the sha256 of the junit file and pytest's own
  outcome counts - one HMAC-SHA256-authenticated, sequence-numbered line per event on that stdout.
* ``check_pytest`` (host side, after the step) recomputes everything: every line must carry a valid MAC, the step's
  nonce and the next sequence number; the session must have finished; the process exit code must equal the sealed
  exit status; the junit file must still have the sealed digest; the per-test outcomes in the junit must equal the
  streamed ones; the terminal summary line must equal pytest's own counts.

Any disagreement is ``FAIL`` (the verifier QUARANTINES the run as result tampering); no sealed session end at all is
``MISSING`` (the step cannot pass - fail closed), and so is a key file the plugin did not remove (code under test could
have read the key, so the seal proves nothing). Only lines naming this step's nonce belong to its channel: a seal line
of another session - a nested verifier run's output that a test printed, or that an assertion's repr shows when it
fails - is counted and ignored (it cannot authenticate as this step's, and taking it for a forgery would QUARANTINE an
honest failing test); a line naming this step's nonce with an invalid MAC is forged. Residual risk, stated on every
report: candidate code shares the
interpreter with the plugin, so code that deliberately subverts the plugin itself in memory (finds the key, patches
its hooks) is not excluded - the seal detects after-the-fact report/exit-code forgery, it is not a sandbox.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_MODULE = "aeos_vseal"
PLUGIN_DIR = "vp"  # run-dir entry holding the plugin (read-only in the container runner)
PREFIX = "@@AEOS-VSEAL1 "
NONCE_ENV = "AEOS_VSEAL_NONCE"
KEYFILE_ENV = "AEOS_VSEAL_KEYFILE"
OUTCOME_KEYS = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}

PLUGIN_SOURCE = r'''"""DLY AEOS verifier result seal (trusted, verifier-owned: aeos.verification.seal). Loaded with -p before
collection; never part of a candidate."""
import hashlib
import hmac
import json
import os

import pytest

_PREFIX = "@@AEOS-VSEAL1 "
_S = {"seq": 0, "fd": None, "key": b"", "nonce": "", "error": None}


def _init():
    _S["nonce"] = os.environ.pop("AEOS_VSEAL_NONCE", "")
    keyfile = os.environ.pop("AEOS_VSEAL_KEYFILE", "")
    try:
        with open(keyfile, "rb") as fh:
            _S["key"] = fh.read().strip()
    except OSError as exc:
        _S["error"] = "keyfile " + type(exc).__name__
    try:
        os.remove(keyfile)
    except OSError:
        pass
    try:
        _S["fd"] = os.dup(1)
    except OSError as exc:
        _S["error"] = "stdout " + type(exc).__name__


_init()


def emit(event, **data):
    fd, key = _S["fd"], _S["key"]
    if fd is None or not key:
        return
    body = json.dumps(dict(data, n=_S["nonce"], i=_S["seq"], e=event), sort_keys=True, separators=(",", ":"))
    _S["seq"] += 1
    mac = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
    buf = ("\n" + _PREFIX + body + " " + mac + "\n").encode("ascii")
    while buf:
        try:
            n = os.write(fd, buf)
        except OSError:
            return
        buf = buf[n:]


def _key(nodeid):
    from _pytest.junitxml import bin_xml_escape, mangle_test_address

    names = mangle_test_address(nodeid)
    classname = ".".join(names[:-1])
    name = str(bin_xml_escape(names[-1]))
    return classname + "::" + name if classname else name


def pytest_sessionstart(session):
    emit("start", pid=os.getpid(), err=_S["error"])


def pytest_collectreport(report):
    if not report.passed:
        emit("c", k=_key(report.nodeid), o="error" if report.failed else "skipped")


def pytest_runtest_logreport(report):
    emit("r", k=_key(report.nodeid), w=report.when, o=report.outcome)


def pytest_internalerror(excrepr, excinfo):
    emit("r", k="pytest::internal", w="internal", o="failed")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    path = getattr(session.config.option, "xmlpath", None)
    digest = None
    if path:
        try:
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            digest = None
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    counts = None
    if tr is not None:
        counts = {k: sum(1 for x in v if getattr(x, "count_towards_summary", True))
                  for k, v in (getattr(tr, "stats", None) or {}).items() if k}
    emit("finish", x=int(session.exitstatus), j=digest, t=counts)
'''

PLUGIN_SHA256 = hashlib.sha256(PLUGIN_SOURCE.encode("utf-8")).hexdigest()

_LINE = re.compile(re.escape(PREFIX) + r"([^\r\n]*)")
_SUMMARY = re.compile(r"^=*\s*(?P<parts>no tests ran|\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d+(?:\.\d+)?s(?: \([^)]*\))?"
                      r"\s*=*\s*$")
_PART = re.compile(r"(\d+) ([a-z]+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def install_plugin(workdir: Path) -> Path:
    """Write the plugin into the run directory (``<workdir>/vp/aeos_vseal.py``); returns that directory."""
    d = workdir / PLUGIN_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{PLUGIN_MODULE}.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    return d


def strip_seal_lines(text: str) -> str:
    """Output without the seal's channel lines (evidence tails stay readable)."""
    return _LINE.sub("", text or "")


@dataclass
class StepSeal:
    """One step's channel identity. The key lives in a file only the plugin reads (and deletes) at import."""

    name: str
    nonce: str
    key: bytes
    keyfile: Path

    @classmethod
    def new(cls, workdir: Path, name: str) -> StepSeal:
        nonce = secrets.token_hex(16)
        key = secrets.token_hex(32).encode("ascii")
        reports = workdir / "r"
        reports.mkdir(parents=True, exist_ok=True)
        keyfile = reports / f".vseal-{nonce}.key"
        fd = os.open(str(keyfile), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return cls(name=name, nonce=nonce, key=key, keyfile=keyfile)

    def child_env(self, keyfile_path: str) -> dict[str, str]:
        """Variables the plugin reads at import (*keyfile_path* as the child sees it)."""
        return {NONCE_ENV: self.nonce, KEYFILE_ENV: keyfile_path}

    def discard(self) -> bool:
        """Remove the key file after the step; True when the plugin had already consumed it."""
        consumed = not self.keyfile.exists()
        try:
            self.keyfile.unlink(missing_ok=True)
        except OSError:
            pass
        return consumed


@dataclass
class SealResult:
    status: str  # PASS | FAIL (inconsistent/forged: tamper) | MISSING (no sealed session end: fail closed)
    problems: list[str] = field(default_factory=list)
    events: int = 0
    exit_status: int | None = None
    junit_sha256: str | None = None
    key_consumed: bool | None = None
    plugin_sha256: str = PLUGIN_SHA256
    # seal lines of other sessions in the output (a nested run a test printed): never this step's channel
    other_session_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


KEY_LEFT_PROBLEM = ("the seal's key file was still there after the step (the plugin did not remove it): code under "
                    "test could have read the key, so the seal proves nothing")


def _events(text: str, seal: StepSeal) -> tuple[list[dict[str, Any]], list[str], int]:
    """This step's authenticated events, the problems, and the number of seal lines of *other* sessions (ignored)."""
    events: list[dict[str, Any]] = []
    problems: list[str] = []
    others = 0
    for m in _LINE.finditer(text or ""):
        body, _, mac = m.group(1).rpartition(" ")
        if seal.nonce not in body:
            # another session's line (a nested verifier run's output a test printed or an assertion repr shows):
            # it cannot authenticate as this step's (other key); counted, never a forgery of this step's result
            others += 1
            continue
        expect = hmac.new(seal.key, body.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
        if not body or not hmac.compare_digest(expect, mac.strip()):
            problems.append("a result-channel line naming this step carries an invalid MAC (forged or corrupted)")
            continue
        try:
            ev = json.loads(body)
        except ValueError:
            problems.append("an authenticated result-channel line is not readable")
            continue
        if not isinstance(ev, dict) or ev.get("n") != seal.nonce:
            problems.append("an authenticated result-channel line belongs to another step")
            continue
        if ev.get("i") != len(events):
            problems.append(f"result-channel sequence broken at event {len(events)} (lines removed or replayed)")
        events.append(ev)
    return events, list(dict.fromkeys(problems)), others


def _worst(pairs: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, outcome in pairs:
        if key not in out or _RANK.get(outcome, 3) > _RANK.get(out[key], 3):
            out[key] = outcome
    return out


def _streamed_outcomes(events: list[dict[str, Any]]) -> dict[str, str]:
    """The junit-equivalent outcome per test key, from the streamed reports (pytest's junitxml rules)."""
    pairs: list[tuple[str, str]] = []
    for ev in events:
        k, o = str(ev.get("k") or ""), str(ev.get("o") or "")
        if ev.get("e") == "c":
            pairs.append((k, o))
        elif ev.get("e") == "r":
            when = ev.get("w")
            if when == "call":
                pairs.append((k, {"passed": "passed", "failed": "failed"}.get(o, "skipped")))
            elif o == "failed":
                pairs.append((k, "error"))
            elif o == "skipped":
                pairs.append((k, "skipped"))
    return _worst(pairs)


def terminal_summary(text: str) -> dict[str, int] | None:
    """The counts of pytest's final summary line (``3 passed, 1 failed in 0.12s``), or None when absent."""
    for line in reversed(strip_seal_lines(text).splitlines()):
        m = _SUMMARY.match(_ANSI.sub("", line).strip())
        if not m:
            continue
        counts: dict[str, int] = {}
        if m.group("parts") != "no tests ran":
            for n, word in _PART.findall(m.group("parts")):
                word = {"errors": "error", "warning": "warnings"}.get(word, word)
                counts[word] = counts.get(word, 0) + int(n)
        return counts
    return None


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def check_pytest(seal: StepSeal, *, stdout: str, returncode: int | None, timed_out: bool,
                 junit_path: Path | None, report: Any, key_consumed: bool | None = None,
                 channel: str | None = None) -> SealResult:
    """Cross-check one sealed pytest step (see the module docstring). *report* is the parsed ``JUnitReport``;
    *channel* the full set of seal lines (``ProcResult.channel``) when *stdout* is only a trimmed tail."""
    events, problems, others = _events(channel or stdout, seal)
    res = SealResult(status="PASS", events=len(events), key_consumed=key_consumed, other_session_lines=others)
    finish = [e for e in events if e.get("e") == "finish"]
    if not finish:
        why = ("the verifier result seal reported nothing (plugin not loaded, or its channel closed)" if not events
               else "the test process ended before pytest finished its session (no sealed session end: os._exit, "
                    "crash or kill)")
        res.status = "FAIL" if problems else "MISSING"
        res.problems = [*problems, why]
        return res
    if key_consumed is False:
        # the plugin ran (sealed events exist) but its key file is still there: fail closed, never a PASS
        problems.append(KEY_LEFT_PROBLEM)
    fin = finish[-1]
    res.exit_status, res.junit_sha256 = fin.get("x"), fin.get("j")
    if len(finish) > 1 or events[-1] is not fin:
        problems.append("result-channel events after the sealed session end")
    if not timed_out and returncode != fin.get("x"):
        problems.append(f"pytest ended its session with exit status {fin.get('x')} but the process exited with "
                        f"{returncode} (exit code changed after the session)")
    actual = _sha256(junit_path)
    if fin.get("j") != actual:
        problems.append("the junit report changed after pytest wrote it (digest at session end differs)")
    if report is not None:
        sealed = _streamed_outcomes(events)
        junit = _worst([(c.key, c.outcome) for c in report.cases])
        diff = sorted(k for k in set(sealed) | set(junit) if sealed.get(k) != junit.get(k))
        if diff:
            examples = ", ".join(f"{k} ({sealed.get(k, 'absent')} streamed, {junit.get(k, 'absent')} in junit)"
                                 for k in diff[:3])
            problems.append(f"{len(diff)} test outcome(s) in the junit report differ from what pytest streamed while "
                            f"running: {examples}")
    counts = fin.get("t")
    summary = terminal_summary(stdout)
    if not isinstance(counts, dict):
        problems.append("pytest's terminal reporter counts are missing from the sealed session end")
    elif summary is None:
        problems.append("pytest's terminal summary line is missing")
    else:
        mismatch = [k for k in OUTCOME_KEYS if int(counts.get(k) or 0) != int(summary.get(k) or 0)]
        if mismatch:
            problems.append("the terminal summary differs from pytest's own counts (" + ", ".join(
                f"{k}: {summary.get(k, 0)} printed, {counts.get(k, 0)} counted" for k in mismatch) + ")")
    if problems:
        # a key file left behind alone is MISSING (a harness defect: the step cannot pass, but nothing was forged);
        # any disagreement is FAIL (result tampering)
        res.status = "MISSING" if problems == [KEY_LEFT_PROBLEM] else "FAIL"
        res.problems = list(dict.fromkeys(problems))
    return res


def sealed_result(seal: StepSeal, stdout: str, event: str = "result") -> tuple[dict[str, Any] | None, list[str]]:
    """The payload of the last authenticated *event* line of a non-pytest driver (e.g. the migration round-trip),
    or None with the problems (fail closed: an unsealed result is never used)."""
    events, problems, _others = _events(stdout, seal)
    found = [e for e in events if e.get("e") == event]
    if not found:
        return None, [*problems, f"no sealed '{event}' line (the driver ended before reporting)"]
    data = found[-1].get("d")
    return (data if isinstance(data, dict) else None), problems
