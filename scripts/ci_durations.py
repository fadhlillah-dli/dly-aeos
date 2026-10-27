"""Rebuild tests/ci_durations.json (the CI shard weights) from JUnit XML reports.

CI splits the backend suite into shards by test FILE (tests/conftest.py ``--shard K/N``, ADR-0115 amendment
"CI in about ten minutes"). The split is only as even as the weights: seconds per test file. This script sums the
``<testcase time>`` of every case per test file over all given reports, so the shards can be rebalanced from what
CI really measured. Every backend shard job uploads its report as the artifact ``junit-backend-<os>-shard-<k>``;
download the reports of one run and run:

  python scripts/ci_durations.py junit-shard-1.xml junit-shard-2.xml --source "CI run 123, windows-latest"
  python scripts/ci_durations.py --check junit-*.xml    # only print the summary, write nothing

The shards of every OS use the SAME split, but the OSes weigh the files differently (a git-heavy file is slower on
macOS, a process-heavy one on Windows), and a macOS Intel or arm64 runner has a different speed than Windows. With
``--os LABEL FILE...`` (once per OS, all shards of that OS after the label) each OS is read on its own, scaled so
that every OS has the same total, and the file weights are the mean over the OSes. A split packed from those
weights is balanced for the slowest runner of each OS at once (a split packed from one OS alone is not):

  python scripts/ci_durations.py --os windows-latest w1.xml w2.xml --os macos-latest a1.xml a2.xml \\
      --os macos-26-intel i1.xml i2.xml --source "CI run 123"

Weights are relative. A file that one OS did not report counts as 0 seconds there.
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
ABOUT = ("Seconds per test file (JUnit testcase times; with --os the mean over the OSes, each scaled to the same "
         "total). Used only to balance the CI shards (tests/conftest.py --shard K/N); rebuild with "
         "scripts/ci_durations.py.")


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


def combine_per_os(per_os: dict[str, dict[str, float]]) -> dict[str, float]:
    """Mean over the OSes of each file's seconds, every OS scaled to the same total first (the mean of the totals), so
    a slow runner does not outweigh a fast one. A file missing from an OS counts as 0 there."""
    totals = {label: sum(files.values()) for label, files in per_os.items()}
    if not per_os or not all(t > 0 for t in totals.values()):
        raise ValueError("every OS needs at least one testcase with a time above zero")
    mean_total = sum(totals.values()) / len(totals)
    names = sorted({rel for files in per_os.values() for rel in files})
    return {rel: round(sum(files.get(rel, 0.0) * mean_total / totals[label] for label, files in per_os.items())
                       / len(per_os), 2) for rel in names}


def render(files: dict[str, float], source: str) -> str:
    return json.dumps({"about": ABOUT, "source": source, "files": files}, indent=1, sort_keys=True,
                      ensure_ascii=True, allow_nan=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild the CI shard weights from JUnit XML reports.")
    ap.add_argument("reports", nargs="*", type=Path, help="JUnit XML files (for example every shard of one CI run); "
                    "their seconds are summed. Use --os instead to weigh several OSes")
    ap.add_argument("--os", dest="per_os", action="append", nargs="+", metavar=("LABEL", "FILE"),
                    help="one OS: its label, then every JUnit XML file of that OS (repeat for each OS)")
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT, help="weights file to write")
    ap.add_argument("--source", default="", help="where the reports came from (CI run, OS); stored in the file")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root the test files are resolved against")
    ap.add_argument("--check", action="store_true", help="print the summary only, write nothing")
    args = ap.parse_args(argv)
    for group in args.per_os or []:  # `--os w1.xml w2.xml --os a1.xml a2.xml` would take w1.xml as the label and drop it
        if group[0].lower().endswith(".xml") or Path(group[0]).is_file():
            ap.error(f"--os needs a LABEL (for example windows-latest) before its files; {group[0]!r} is a file name")
    groups = {group[0]: [Path(p) for p in group[1:]] for group in args.per_os or []}
    if bool(args.reports) == bool(groups) or len(groups) != len(args.per_os or []) or not all(groups.values()):
        ap.error("give JUnit files (summed), or --os LABEL FILE... once per OS with at least one file; not both")
    root = args.root.resolve()
    all_reports = args.reports or [p for paths in groups.values() for p in paths]
    try:
        if groups:
            per_os = {label: durations(paths, root) for label, paths in groups.items()}
            for label, (per_file, *_rest) in per_os.items():
                print(f"  {label}: {len(per_file)} test files, {sum(per_file.values()):.1f} s")
            zero = [label for label, (per_file, *_rest) in per_os.items() if per_file and sum(per_file.values()) <= 0]
            for label in zero:
                print(f"ERROR  {label}: total 0 s (no testcase with a time above zero); the OSes cannot be scaled")
            if zero:
                return 2
            files = combine_per_os({label: r[0] for label, r in per_os.items()}) if all(r[0] for r in per_os.values()) else {}
            cases = sum(r[1] for r in per_os.values())
            unmapped = sorted({u for r in per_os.values() for u in r[2]})
            bad_time = [b for r in per_os.values() for b in r[3]]
        else:
            files, cases, unmapped, bad_time = durations(args.reports, root)
    except (JUnitParseError, ValueError) as exc:
        print(f"ERROR  {exc}")
        return 2
    if not files:
        print(f"ERROR  no testcase of {len(all_reports)} report(s) maps to a test file; nothing written")
        return 1
    total = sum(files.values())
    print(f"{len(files)} test files, {cases} testcases, {total:.1f} s in {len(all_reports)} report(s)")
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
    source = args.source or ", ".join(p.name for p in all_reports)
    args.output.write_text(render(files, source), encoding="utf-8", newline="\n")
    print(f"wrote {args.output.name} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
