"""Records of passing full backend-suite runs, shared by every worktree of a clone (LESSONS L8, strike 3).

    python scripts/full_suite_record.py state                         '<head sha> <tree sha> clean|dirty', or 'unknown'
    python scripts/full_suite_record.py record --start STATE [-- ARGS]  after a PASS: append a record or say why not

scripts/run_tests.sh calls `state` before pytest starts and `record` after a passing run, with the pytest arguments
the caller gave. scripts/gates.py --hook (the pre-push hook) refuses to push a commit whose tree has no record. This
module is the one owner of the record format; gates.py imports it.

The record file is `<git common dir>/aeos-full-suite-runs` (git rev-parse --git-common-dir), so every worktree of the
clone shares it. One line per passing run: `<HEAD tree sha> <HEAD sha> <UTC time> <load average>`. A tree is
content-addressed: a merge tested in any worktree counts for the same tree pushed from another one.

A run is recorded only when all of these hold:

* it PASSED (run_tests.sh calls `record` only after exit 0);
* it was FULL: no pytest argument besides the ones in FULL_RUN_OPTIONS, which change output, speed or when to stop,
  never which tests are collected (`-q`, `-v`, `-x`, `--tb=...`, `-r...`, `-n N`, `--durations N`, `--color=...`).
  A path, `-k`, `-m`, `--lf`, `--deselect`, `-p` or any other argument makes the run partial;
* the working tree was clean (no staged, unstaged or untracked file that git does not ignore) both before pytest
  started and after it passed, and HEAD did not move in between: then HEAD's tree is exactly what was tested.

Otherwise it prints one line `run_tests: full-suite record: not recorded: <why>`. It never fails a run: every
problem is reported on that line and the exit code is 0. Stdlib only, ASCII output."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

RECORD_FILE = "aeos-full-suite-runs"
PREFIX = "run_tests: full-suite record:"
UNKNOWN = "unknown"
# Options that never change which tests are collected. Each maps to whether it takes a separate value.
FULL_RUN_OPTIONS: dict[str, bool] = {
    "-q": False, "-qq": False, "--quiet": False, "-v": False, "-vv": False, "--verbose": False,
    "-x": False, "--exitfirst": False, "-n": True, "--numprocesses": True, "--durations": True,
}
# Options written with their value in one word: --tb=short, -rA, -n4, --numprocesses=4, --durations=10, --color=no.
FULL_RUN_JOINED = re.compile(r"^(?:--tb=\w+|-r[a-zA-Z]+|-n\w+|-n=\w+|--numprocesses=\w+|--durations=\d+|--color=\w+)$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclasses.dataclass(frozen=True)
class State:
    head: str
    tree: str
    clean: bool

    @property
    def line(self) -> str:
        return f"{self.head} {self.tree} {'clean' if self.clean else 'dirty'}"


@dataclasses.dataclass(frozen=True)
class Record:
    tree: str
    head: str
    time: str
    load: str


def _git(args: Sequence[str], cwd: Path, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), env=None if env is None else dict(env), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False,
                          timeout=120)


def partial_reason(args: Sequence[str]) -> str | None:
    """None for a full run, else why the pytest arguments make it partial."""
    items = list(args)
    i = 0
    while i < len(items):
        arg = items[i]
        if arg in FULL_RUN_OPTIONS:
            i += 2 if FULL_RUN_OPTIONS[arg] else 1
            continue
        if FULL_RUN_JOINED.match(arg):
            i += 1
            continue
        return f"partial run (the pytest argument '{arg}' may select or deselect tests)"
    return None


def read_state(cwd: Path, env: Mapping[str, str] | None = None) -> State | None:
    """HEAD, its tree and whether the working tree is clean; None outside a git checkout or without a commit."""
    try:
        head = _git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd, env)
        tree = _git(["rev-parse", "--verify", "--quiet", "HEAD^{tree}"], cwd, env)
        status = _git(["status", "--porcelain", "--untracked-files=normal"], cwd, env)
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0 or tree.returncode != 0 or status.returncode != 0:
        return None
    return State(head.stdout.strip(), tree.stdout.strip(), not status.stdout.strip())


def parse_state(text: str) -> State | None:
    parts = text.split()
    if len(parts) != 3 or not all(_SHA.match(p) for p in parts[:2]) or parts[2] not in ("clean", "dirty"):
        return None
    return State(parts[0], parts[1], parts[2] == "clean")


def common_dir(cwd: Path, env: Mapping[str, str] | None = None) -> Path | None:
    try:
        proc = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd, env)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


def load_average() -> str:
    try:
        return ",".join(f"{x:.2f}" for x in os.getloadavg())
    except (AttributeError, OSError):
        return "n/a"  # Windows has no load average


def read_records(directory: Path) -> list[Record]:
    try:
        text = (directory / RECORD_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and _SHA.match(parts[0]) and _SHA.match(parts[1]):
            out.append(Record(*parts))
    return out


def find_record(directory: Path, tree: str) -> Record | None:
    """The latest passing full-suite record of this tree, or None."""
    found = [r for r in read_records(directory) if r.tree == tree]
    return found[-1] if found else None


def decide(start: State | None, end: State | None, args: Sequence[str]) -> str | None:
    """None when the run may be recorded, else why not."""
    partial = partial_reason(args)
    if partial:
        return partial
    if start is None or end is None:
        return "the state of the checkout could not be read (not a git checkout, or no commit yet)"
    if start.head != end.head or start.tree != end.tree:
        return "HEAD moved during the run"
    if not start.clean:
        return "the working tree was not clean when the run started (commit or stash every change, then run again)"
    if not end.clean:
        return "the working tree changed during the run"
    return None


def record(cwd: Path, start_text: str, args: Sequence[str], now: dt.datetime | None = None,
           load: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Append a record when the run qualifies; returns the one line to print. Never raises for expected problems."""
    end = read_state(cwd, env)
    why = decide(parse_state(start_text), end, args)
    if why is None and end is not None:
        directory = common_dir(cwd, env)
        if directory is None:
            why = "the git common directory could not be found"
        else:
            stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
            line = f"{end.tree} {end.head} {stamp} {load if load is not None else load_average()}\n"
            try:
                with open(directory / RECORD_FILE, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
            except OSError as exc:
                why = f"cannot write {directory / RECORD_FILE}: {exc}"
            else:
                return f"{PREFIX} recorded tree {end.tree[:12]} (HEAD {end.head[:12]}) in {directory / RECORD_FILE}"
    return f"{PREFIX} not recorded: {why}"


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="full_suite_record.py", description="Records of passing full-suite runs.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("state", help="print '<head> <tree> clean|dirty' of the current checkout, or 'unknown'")
    rec = sub.add_parser("record", help="after a passing run: record it when it was full and the tree clean")
    rec.add_argument("--start", required=True, help="the output of 'state' taken before pytest started")
    rec.add_argument("pytest_args", nargs=argparse.REMAINDER, help="the caller's pytest arguments, after --")
    args = parser.parse_args(argv)
    cwd = Path.cwd()
    try:
        if args.command == "state":
            state = read_state(cwd)
            print(state.line if state else UNKNOWN)
            return 0
        extra = list(args.pytest_args)
        if extra[:1] == ["--"]:
            extra = extra[1:]
        print(_ascii(record(cwd, args.start, extra)), flush=True)
    except Exception as exc:  # recording must never fail a test run
        print(_ascii(f"{PREFIX} not recorded: the recorder failed: {exc}"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
