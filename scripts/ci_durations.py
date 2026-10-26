"""Rebuild tests/ci_durations.json (the CI shard weights) from JUnit XML reports.

CI splits the backend suite into shards by test FILE (tests/conftest.py ``--shard K/N``, ADR-0115 amendment
"CI in about ten minutes"). The split is only as even as the weights: seconds per test file. This script sums the
``<testcase time>`` of every case per test file over all given reports, so the shards can be rebalanced from what
CI really measured. Every backend shard job uploads its report as the artifact ``junit-backend-<os>-shard-<k>``;
download the reports of one run (all shards of one OS cover the whole suite, normally windows-latest, the slowest)
and run:

  python scripts/ci_durations.py junit-shard-1.xml junit-shard-2.xml --source "CI run 123, windows-latest"
  python scripts/ci_durations.py --check junit-*.xml    # only print the summary, write nothing

Weights are relative: a sum over several OSes stays comparable as long as every file was measured in every report.
Files are keyed by their path relative to the repository root (forward slashes). A testcase whose file no longer
exists is reported and left out. ASCII output only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from aeos.verification.junit import JUnitParseError, parse_junit  # noqa: E402 - size-bounded, DTD rejected

DEFAULT_OUT = ROOT / "tests" / "ci_durations.json"
ABOUT = ("Seconds per test file (sum of JUnit testcase times). Used only to balance the CI shards "
         "(tests/conftest.py --shard K/N); rebuild with scripts/ci_durations.py.")


def case_file(classname: str, root: Path = ROOT) -> str | None:
    """``tests.unit.test_x`` or ``tests.unit.test_x.TestClass`` -> ``tests/unit/test_x.py`` (the longest dotted
    prefix that is an existing file under *root*); None when no such file exists."""
    parts = [p for p in classname.split(".") if p]
    for end in range(len(parts), 0, -1):
        rel = "/".join(parts[:end]) + ".py"
        if (root / rel).is_file():
            return rel
    return None


def durations(reports: list[Path], root: Path = ROOT) -> tuple[dict[str, float], int, list[str], list[str]]:
    """(seconds per file, testcases read, classnames that map to no existing file, ids of cases skipped because
    their time is not finite)."""
    per_file: dict[str, float] = {}
    cases = 0
    unmapped: set[str] = set()
    bad_time: list[str] = []
    for report in reports:
        for case in parse_junit(report).cases:
            cases += 1
            if not math.isfinite(case.time):
                bad_time.append(f"{case.classname}::{case.name}")
                continue
            rel = case_file(case.classname, root)
            if rel is None:
                unmapped.add(case.classname or "<no classname>")
                continue
            per_file[rel] = per_file.get(rel, 0.0) + max(case.time, 0.0)
    return {k: round(v, 2) for k, v in sorted(per_file.items())}, cases, sorted(unmapped), bad_time


def render(files: dict[str, float], source: str) -> str:
    return json.dumps({"about": ABOUT, "source": source, "files": files}, indent=1, sort_keys=True,
                      ensure_ascii=True, allow_nan=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild the CI shard weights from JUnit XML reports.")
    ap.add_argument("reports", nargs="+", type=Path, help="JUnit XML files (for example every shard of one CI run)")
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT, help="weights file to write")
    ap.add_argument("--source", default="", help="where the reports came from (CI run, OS); stored in the file")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root the test files are resolved against")
    ap.add_argument("--check", action="store_true", help="print the summary only, write nothing")
    args = ap.parse_args(argv)
    try:
        files, cases, unmapped, bad_time = durations(args.reports, args.root.resolve())
    except JUnitParseError as exc:
        print(f"ERROR  {exc}")
        return 2
    if not files:
        print(f"ERROR  no testcase of {len(args.reports)} report(s) maps to a test file; nothing written")
        return 1
    total = sum(files.values())
    print(f"{len(files)} test files, {cases} testcases, {total:.1f} s in {len(args.reports)} report(s)")
    heaviest = sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    for rel, sec in heaviest:
        print(f"  {sec:9.2f} s  {rel}")
    if unmapped:
        print(f"WARNING  {len(unmapped)} classname(s) map to no existing test file and were left out: "
              + ", ".join(unmapped[:5]) + (" ..." if len(unmapped) > 5 else ""))
    if bad_time:
        print(f"WARNING  {len(bad_time)} testcase(s) with a time that is not finite were skipped: "
              + ", ".join(bad_time[:5]) + (" ..." if len(bad_time) > 5 else ""))
    if args.check:
        return 0
    source = args.source or ", ".join(p.name for p in args.reports)
    args.output.write_text(render(files, source), encoding="utf-8", newline="\n")
    print(f"wrote {args.output.name} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
