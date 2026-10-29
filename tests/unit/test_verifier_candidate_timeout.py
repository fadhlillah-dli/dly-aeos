"""The verifier's candidate-test limit covers the measured serial run of this repository's tests (LESSONS L31).

The candidate tests run every selected test directory in one serial pytest process. On 2026-10-29 the platform's
tests/unit took about 2650 s serially (tests/ci_durations.json), so the former 900 s limit would have failed every
candidate of this repository by timeout. Red again when the suite outgrows the limit: then run the candidates in
parallel instead of raising the bound."""

from __future__ import annotations

import json
from pathlib import Path

from aeos.verification.verifier import SUITE_DIRS, VerifierLimits

DURATIONS = Path(__file__).resolve().parents[1] / "ci_durations.json"


def _serial_seconds() -> float:
    files = json.loads(DURATIONS.read_text(encoding="utf-8"))["files"]
    dirs = tuple(f"tests/{d}/" for d in SUITE_DIRS.values())
    return sum(seconds for name, seconds in files.items() if name.startswith(dirs))


def test_the_candidate_test_limit_covers_the_serial_candidate_suites():
    serial, limit = _serial_seconds(), VerifierLimits().unit_timeout
    assert serial > 0, "tests/ci_durations.json lists no candidate test directory"
    assert limit >= serial, (f"candidate tests need about {serial:.0f} s serially, the verifier allows {limit:.0f} s: "
                             "run the candidate tests in parallel instead of raising the limit further")


def test_the_default_is_within_the_policy_bound():
    assert VerifierLimits.from_policy({}).unit_timeout == VerifierLimits().unit_timeout <= 3600.0
