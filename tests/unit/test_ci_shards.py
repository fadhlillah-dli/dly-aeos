"""CI shards (ADR-0115 amendment "CI in about ten minutes"): ``pytest --shard K/N`` in tests/conftest.py and the
weights rebuild script scripts/ci_durations.py.

Sharding may only split the suite, never drop a test: every collected file lands in exactly one shard, the split
is deterministic (the shard jobs of one OS compute it independently) and balanced by measured seconds."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from aeos.core.paths import repo_root
from tests.conftest import (
    SHARD_ENV,
    SHARD_WEIGHTS,
    assign_shards,
    default_shard_weight,
    load_shard_weights,
    parse_shard,
)

ROOT = repo_root()


def _repo_test_files() -> list[str]:
    return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("test_*.py")
                  if "__pycache__" not in p.parts)


def _durations_script():
    spec = importlib.util.spec_from_file_location("aeos_script_ci_durations", ROOT / "scripts" / "ci_durations.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------------------------------ assignment
@pytest.mark.parametrize("text, expected", [("1/2", (1, 2)), ("2/2", (2, 2)), (" 3 / 4 ", (3, 4)), ("1/1", (1, 1))])
def test_parse_shard_reads_k_of_n(text, expected):
    assert parse_shard(text) == expected


@pytest.mark.parametrize("text", ["", "1", "0/2", "3/2", "1/0", "a/b", "-1/2", "1/2/3", "1.0/2"])
def test_parse_shard_refuses_anything_but_k_of_n(text):
    with pytest.raises(ValueError, match="K/N"):
        parse_shard(text)


@pytest.mark.parametrize("total", [1, 2, 3, 4, 7])
def test_every_file_lands_in_exactly_one_shard(total):
    weights = load_shard_weights()
    files = sorted(set(_repo_test_files()) | set(weights) | {"tests/unit/test_brand_new_a.py",
                                                             "tests/unit/test_brand_new_b.py"})
    shards = assign_shards(files, total, weights)
    assert len(shards) == total
    flat = [f for s in shards for f in s]
    assert sorted(flat) == files and len(flat) == len(set(flat))
    assert all(s == sorted(s) for s in shards)


def test_the_split_is_deterministic_whatever_the_input_order_separator_or_hash_seed():
    weights = load_shard_weights()
    files = sorted(set(_repo_test_files()) | set(weights))
    expected = assign_shards(files, 2, weights)
    shuffled = list(files)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert assign_shards(shuffled, 2, weights) == expected
    assert assign_shards([f.replace("/", "\\") for f in files], 2, weights) == expected  # Windows separators
    assert assign_shards(files + files, 2, weights) == expected  # duplicates collapse
    probe = ("import json, sys; from tests.conftest import assign_shards, load_shard_weights; "
             "w = load_shard_weights(); print(json.dumps(assign_shards(json.loads(sys.stdin.read()), 2, w)))")
    env = {**os.environ, "PYTHONHASHSEED": "12345",
           "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "backend"), os.environ.get("PYTHONPATH", "")])}
    env.pop(SHARD_ENV, None)
    out = subprocess.run([sys.executable, "-c", probe], input=json.dumps(shuffled), capture_output=True, text=True,
                         cwd=ROOT, env=env, timeout=120, check=True).stdout
    assert json.loads(out) == expected


def test_lpt_packing_keeps_the_shards_balanced():
    """Greedy longest-processing-time: the spread between the heaviest and the lightest shard never exceeds the
    heaviest single file, and on the committed weights 2 or 3 shards are within 1 % of each other."""
    rng = random.Random(20261026)
    for _ in range(50):
        weights = {f"tests/unit/test_{i:03d}.py": round(rng.expovariate(0.2), 2) for i in range(rng.randint(1, 80))}
        for total in (2, 3, 4):
            loads = [sum(weights[f] for f in s) for s in assign_shards(weights, total, weights)]
            assert max(loads) - min(loads) <= max(weights.values()) + 1e-9
    committed = load_shard_weights()
    for total in (2, 3):
        loads = [sum(committed[f] for f in s) for s in assign_shards(committed, total, committed)]
        assert max(loads) - min(loads) <= 0.01 * sum(loads), loads


def test_files_without_a_measured_weight_get_the_median_and_a_stable_hash_tiebreak():
    weights = {"tests/unit/test_a.py": 10.0, "tests/unit/test_b.py": 2.0, "tests/unit/test_c.py": 1.0}
    assert default_shard_weight(weights) == 2.0 and default_shard_weight({}) == 1.0
    unknown = ["tests/unit/test_new_x.py", "tests/unit/test_new_y.py"]
    shards = assign_shards([*weights, *unknown], 2, weights)
    assert sorted(f for s in shards for f in s) == sorted([*weights, *unknown])
    # a (10) alone on shard 1; b (2), the two unknown files (2 each) and c (1) fill shard 2 up to 7 first
    assert shards[0] == ["tests/unit/test_a.py"]
    # equal weights: the lower SHA-256 of the path goes first, onto the lighter (or lower-numbered) shard
    p, q = "tests/unit/test_p.py", "tests/unit/test_q.py"
    first = min((p, q), key=lambda f: hashlib.sha256(f.encode("utf-8")).hexdigest())
    assert assign_shards([q, p], 2, {})[0] == [first]
    assert assign_shards([p, q], 2, {}, default=5.0)[0] == [first]
    assert assign_shards([], 3, weights) == [[], [], []]
    with pytest.raises(ValueError):
        assign_shards([p], 0, weights)


@pytest.mark.parametrize("body", [
    "[]", "{}", '{"files": []}', '{"files": {"tests/unit/test_x.py": -1}}', '{"files": {"tests/unit/test_x.py": true}}',
    '{"files": {"tests/unit/test_x.py": "3"}}', '{"files": {"tests/unit/test_x.py": NaN}}', '{"files": {"": 1}}',
    "not json",
])
def test_a_malformed_weights_file_is_refused(tmp_path: Path, body: str):
    path = tmp_path / "ci_durations.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_shard_weights(path)


def test_the_committed_weights_file_is_well_formed():
    weights = load_shard_weights()
    assert SHARD_WEIGHTS == ROOT / "tests" / "ci_durations.json"
    assert len(weights) > 100
    for rel in weights:
        assert rel.startswith("tests/") and rel.endswith(".py") and "\\" not in rel, rel
    data = json.loads(SHARD_WEIGHTS.read_text(encoding="utf-8"))
    assert set(data) == {"about", "source", "files"} and data["source"]


# ------------------------------------------------------------------------------------------------ pytest option
FAST_FILES = ["tests/unit/test_acceptance_matrix.py", "tests/unit/test_asvs_trace.py",
              "tests/unit/test_audit_append_only_guard.py", "tests/unit/test_core.py"]


def _pytest(*args: str, env_extra: dict[str, str] | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    if not env_extra or SHARD_ENV not in env_extra:
        env.pop(SHARD_ENV, None)
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], capture_output=True,
                          text=True, cwd=cwd, env=env, timeout=300)


def _collected(out: str) -> set[str]:
    return {ln.strip() for ln in out.splitlines() if "::" in ln and not ln.startswith(("ERROR", "FAILED"))}


def test_shards_of_a_real_collection_are_disjoint_and_add_up_to_the_whole_run():
    full = _pytest("--collect-only", "-q", *FAST_FILES)
    assert full.returncode == 0, full.stdout + full.stderr
    everything = _collected(full.stdout)
    parts = []
    for k in (1, 2):
        res = _pytest("--collect-only", "-q", "--shard", f"{k}/2", *FAST_FILES)
        assert res.returncode == 0, res.stdout + res.stderr
        assert f"AEOS test shard {k}/2: " in res.stdout
        parts.append(_collected(res.stdout))
    assert parts[0] and parts[1] and not parts[0] & parts[1]
    assert parts[0] | parts[1] == everything
    files = [{n.split("::", 1)[0] for n in p} for p in parts]
    assert not files[0] & files[1], "a file is never split across shards"


def test_the_environment_variable_selects_the_shard_and_is_not_inherited_by_test_processes(tmp_path: Path):
    """AEOS_TEST_SHARD is read once and removed, so a pytest process that a test starts is never sharded."""
    probe = tmp_path / "aeos_shard_env_probe.py"
    probe.write_text("import os, pathlib\n\ndef pytest_sessionfinish(session):\n"
                     "    pathlib.Path(os.environ['SHARD_PROBE_OUT']).write_text(repr(os.environ.get("
                     f"{SHARD_ENV!r})))\n", encoding="utf-8")
    out = tmp_path / "seen.txt"
    pythonpath = os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")])
    by_flag = _pytest("--collect-only", "-q", "--shard", "2/2", *FAST_FILES)
    res = _pytest("--collect-only", "-q", "-p", "aeos_shard_env_probe", *FAST_FILES,
                  env_extra={SHARD_ENV: "2/2", "SHARD_PROBE_OUT": str(out), "PYTHONPATH": pythonpath})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "AEOS test shard 2/2: " in res.stdout
    assert _collected(res.stdout) == _collected(by_flag.stdout)
    assert out.read_text(encoding="utf-8") == "None"


@pytest.mark.parametrize("shard", ["0/2", "3/2", "two"])
def test_an_invalid_shard_is_a_usage_error_never_a_silent_full_or_empty_run(shard: str):
    res = _pytest("--collect-only", "-q", "--shard", shard, FAST_FILES[0])
    assert res.returncode == pytest.ExitCode.USAGE_ERROR, res.stdout + res.stderr
    assert "K/N" in res.stderr


def test_shards_run_under_pytest_xdist_and_report_their_summary():
    pytest.importorskip("xdist")
    total = 0
    for k in (1, 2):
        res = _pytest("-q", "-p", "xdist", "-n", "2", "--shard", f"{k}/2", *FAST_FILES)
        assert res.returncode == 0, res.stdout + res.stderr
        line = next(ln for ln in res.stdout.splitlines() if ln.startswith(f"AEOS test shard {k}/2: "))
        line.encode("ascii")
        total += int(res.stdout.strip().splitlines()[-1].split(" passed")[0].split()[-1])
    full = _pytest("--collect-only", "-q", *FAST_FILES)
    assert total == len(_collected(full.stdout))


# ------------------------------------------------------------------------------------------------ weights script
JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="5" failures="0"
errors="0" skipped="0"><testcase classname="tests.unit.test_core" name="a" time="1.25"/>
<testcase classname="tests.unit.test_core" name="b" time="0.75"/>
<testcase classname="tests.unit.test_ci_shards" name="c" time="2.5"/>
<testcase classname="tests.unit.test_gone_away" name="d" time="9"/>
<testcase classname="tests.unit.test_core.TestGroup" name="e" time="1"/></testsuite></testsuites>
"""


def test_the_weights_script_sums_seconds_per_file_and_round_trips(tmp_path: Path, capsys):
    script = _durations_script()
    a = tmp_path / "shard-1.xml"
    b = tmp_path / "shard-2.xml"
    a.write_text(JUNIT, encoding="utf-8")
    b.write_text(JUNIT.replace('time="2.5"', 'time="0.5"'), encoding="utf-8")
    assert script.case_file("tests.unit.test_core.TestGroup") == "tests/unit/test_core.py"
    assert script.case_file("tests.unit.test_gone_away") is None
    out = tmp_path / "weights.json"
    assert script.main([str(a), str(b), "-o", str(out), "--source", "unit test"]) == 0
    printed = capsys.readouterr().out
    printed.encode("ascii")
    assert "tests.unit.test_gone_away" in printed  # reported, left out
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["files"] == {"tests/unit/test_ci_shards.py": 3.0, "tests/unit/test_core.py": 6.0}
    assert data["source"] == "unit test"
    assert load_shard_weights(out) == data["files"]
    again = tmp_path / "again.json"
    assert script.main([str(b), str(a), "-o", str(again), "--source", "unit test"]) == 0
    assert again.read_bytes() == out.read_bytes()  # deterministic output


def test_the_weights_script_refuses_reports_it_cannot_use(tmp_path: Path):
    script = _durations_script()
    bad = tmp_path / "bad.xml"
    bad.write_text('<!DOCTYPE x [<!ENTITY a "b">]><testsuites/>', encoding="utf-8")
    out = tmp_path / "w.json"
    assert script.main([str(bad), "-o", str(out)]) == 2 and not out.exists()
    empty = tmp_path / "empty.xml"
    empty.write_text('<testsuites><testsuite name="pytest" tests="0"/></testsuites>', encoding="utf-8")
    assert script.main([str(empty), "-o", str(out)]) == 1 and not out.exists()
    good = tmp_path / "good.xml"
    good.write_text(JUNIT, encoding="utf-8")
    assert script.main([str(good), "-o", str(out), "--check"]) == 0 and not out.exists()
