"""Run CI's static gates locally, before pushing (CONTRIBUTING.md, "Gates to run before opening a pull request").

    python scripts/gates.py                static gates, the commit-subject gate, frontend gates when frontend/ differs
    python scripts/gates.py --tests        also the backend suite (CI runs it in about ten minutes; default is off)
    python scripts/gates.py --json         machine-readable results
    python scripts/gates.py --hook         git pre-push mode, see scripts/git-hooks/pre-push

In hook mode the static gates run on the tip commit of each pushed ref only; the commit-message check covers
base..tip, so intermediate commits get that check and nothing else (and only when a base is known, else it is
SKIPPED with the reason). Run with the project .venv (or set
AEOS_GATES_PYTHON for the hook): ruff and mypy must be installed for sys.executable, else their gates FAIL.

The static gate list is the G1 step of .github/workflows/ci.yml; tests/unit/test_dev_gates.py fails when the two
diverge. Stdlib only, ASCII-only output. A gate that does not apply (frontend gates without changes or without
node_modules, the commit-message check without a base) is SKIPPED with its reason, never PASS; a missing ruff or
mypy is a FAIL.
Exit code 1 when any gate FAILs."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
COMMIT_SUBJECT = SCRIPTS / "commit_subject.py"

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"
ZERO_SHA_CHARS = frozenset("0")
DEFAULT_BASE = "origin/main"
TAIL_LINES = 40
GATE_TIMEOUT_SECONDS = 1800
# Variables git sets for hooks. A gate must find its repository from its own cwd, not from the pushing clone.
SCRUBBED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")


@dataclasses.dataclass(frozen=True)
class Gate:
    name: str
    argv: tuple[str, ...]  # argv[0] "python" means sys.executable; "npx" and "npm" are resolved on PATH
    cwd: str = "."  # relative to the checkout

    @property
    def command(self) -> str:
        return " ".join(self.argv)


# Same commands as the "G1 static" steps of the backend shard 1 jobs in .github/workflows/ci.yml, in the same order.
# CI runs the mypy pair on macos-latest only; locally both platforms always run (mypy needs no Windows host).
STATIC_GATES: tuple[Gate, ...] = (
    Gate("ruff", ("python", "-m", "ruff", "check", "backend", "tests", "scripts")),
    Gate("mypy-darwin", ("python", "-m", "mypy", "--platform", "darwin")),
    Gate("mypy-win32", ("python", "-m", "mypy", "--platform", "win32")),
    Gate("secret-scan", ("python", "scripts/secret_scan.py")),
)

# Gates run as `python -m <tool>` for these tools are checked for installation first, so a wrong interpreter gives a
# clear FAIL (never SKIPPED: a hook that skips mypy lets a type error through) instead of "No module named".
PYTHON_TOOLS = ("ruff", "mypy")

# The typecheck and unit steps of the frontend job in ci.yml. Its build step is not a gate here: it is slow and
# adds nothing a typecheck and the unit tests do not already show.
FRONTEND_GATES: tuple[Gate, ...] = (
    Gate("frontend-typecheck", ("npx", "tsc", "-b", "--noEmit"), "frontend"),
    Gate("frontend-unit", ("npm", "test"), "frontend"),
)

# The backend suite as CI runs it, without the shard flag.
TEST_ARGV = ("python", "-m", "pytest", "-q", "-m", "not integration and not network")


@dataclasses.dataclass
class Result:
    gate: str
    status: str
    seconds: float = 0.0
    detail: str = ""  # SKIPPED: the reason. FAIL: the command and the tail of its output.
    command: str = ""
    ref: str = ""  # hook mode: the pushed ref and sha


@dataclasses.dataclass(frozen=True)
class PushRef:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def is_deletion(self) -> bool:
        return is_zero_sha(self.local_sha)


def ascii_text(text: str) -> str:
    """Printable ASCII only (legacy Windows consoles): anything else becomes '?'."""
    return text.encode("ascii", "replace").decode("ascii")


def is_zero_sha(sha: str) -> bool:
    return bool(sha) and set(sha) <= ZERO_SHA_CHARS


def clean_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for git and for every gate: git's own repository variables removed."""
    out = dict(os.environ if env is None else env)
    for name in SCRUBBED_ENV:
        out.pop(name, None)
    return out


def _run(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), cwd=str(cwd), env=dict(env), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False,
                          timeout=GATE_TIMEOUT_SECONDS)


def git(args: Sequence[str], cwd: Path, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd, clean_env() if env is None else env)


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-TAIL_LINES:])


def resolve_argv(gate: Gate) -> list[str] | None:
    """The argv to execute, or None when the tool is not on PATH (npx.cmd and npm.cmd resolve on Windows)."""
    first = gate.argv[0]
    if first == "python":
        return [sys.executable, *gate.argv[1:]]
    found = shutil.which(first)
    return [found, *gate.argv[1:]] if found else None


def missing_python_tool(gate: Gate) -> str | None:
    """The tool of a `python -m <tool>` gate that is not installed for sys.executable, else None."""
    if gate.argv[:2] == ("python", "-m") and len(gate.argv) > 2 and gate.argv[2] in PYTHON_TOOLS:
        try:
            if importlib.util.find_spec(gate.argv[2]) is None:
                return gate.argv[2]
        except (ImportError, ValueError):
            return gate.argv[2]
    return None


def run_gate(gate: Gate, checkout: Path, env: Mapping[str, str], ref: str = "") -> Result:
    argv = resolve_argv(gate)
    if argv is None:
        return Result(gate.name, SKIPPED, 0.0, f"{gate.argv[0]} is not on PATH", gate.command, ref)
    tool = missing_python_tool(gate)
    if tool:
        return Result(gate.name, FAIL, 0.0, f"{tool} is not installed for {sys.executable}; use the project .venv "
                      "or set AEOS_GATES_PYTHON", gate.command, ref)
    started = time.perf_counter()
    try:
        proc = _run(argv, checkout / gate.cwd, env)
    except subprocess.TimeoutExpired:
        return Result(gate.name, FAIL, time.perf_counter() - started,
                      f"timed out after {GATE_TIMEOUT_SECONDS} s", gate.command, ref)
    except OSError as exc:
        return Result(gate.name, FAIL, time.perf_counter() - started, f"could not start: {exc}", gate.command, ref)
    seconds = time.perf_counter() - started
    if proc.returncode == 0:
        return Result(gate.name, PASS, seconds, "", gate.command, ref)
    return Result(gate.name, FAIL, seconds, f"exit {proc.returncode}\n{_tail(proc.stdout + proc.stderr)}",
                  gate.command, ref)


def commit_subject_gate(base: str, head: str) -> Gate:
    return Gate("commit-subjects", ("python", str(COMMIT_SUBJECT), f"{base}..{head}"))


def ref_exists(root: Path, ref: str, env: Mapping[str, str]) -> bool:
    return git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], root, env).returncode == 0


def frontend_changed(root: Path, base: str, env: Mapping[str, str]) -> bool | None:
    """Whether files under frontend/ differ from the base (committed, staged, unstaged or untracked);
    None when that cannot be told."""
    merge_base = git(["merge-base", base, "HEAD"], root, env)
    if merge_base.returncode != 0:
        return None
    diff = git(["diff", "--name-only", merge_base.stdout.strip(), "--", "frontend"], root, env)
    untracked = git(["ls-files", "--others", "--exclude-standard", "--", "frontend"], root, env)
    if diff.returncode != 0 or untracked.returncode != 0:
        return None
    return bool(diff.stdout.strip() or untracked.stdout.strip())


def frontend_results(root: Path, base: str | None, force: bool, env: Mapping[str, str]) -> list[Result]:
    reason = ""
    if not force:
        changed = frontend_changed(root, base, env) if base else None
        if changed is None:
            reason = "cannot tell whether frontend/ changed (no base ref; pass --base or --frontend)"
        elif not changed:
            reason = f"no files under frontend/ differ from {base}"
    if not reason and not (root / "frontend" / "node_modules").is_dir():
        reason = "frontend/node_modules is missing (run 'npm ci' in frontend/)"
    if reason:
        return [Result(g.name, SKIPPED, 0.0, reason, g.command) for g in FRONTEND_GATES]
    results: list[Result] = []
    for gate in FRONTEND_GATES:
        results.append(run_gate(gate, root, env))
    return results


def test_gate() -> Gate:
    """The backend suite on one xdist worker per logical CPU when xdist is importable, else serial."""
    argv: tuple[str, ...] = TEST_ARGV
    if importlib.util.find_spec("xdist") is not None:
        argv = (*argv, "-n", "logical")
    return Gate("tests", argv)


def parse_push_lines(text: str) -> list[PushRef]:
    """git's pre-push stdin: `<local ref> <local sha> <remote ref> <remote sha>` per line."""
    refs = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4:
            refs.append(PushRef(*parts))
    return refs


def hook_targets(refs: Sequence[PushRef], root: Path, env: Mapping[str, str]) -> list[tuple[PushRef, str | None]]:
    """(pushed ref, base for its commit-subject range). Deletions are skipped. The base is the remote sha, or
    origin/main for a new branch; None when neither exists in this clone."""
    targets: list[tuple[PushRef, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        if ref.is_deletion:
            continue
        base: str | None = DEFAULT_BASE
        if not is_zero_sha(ref.remote_sha) and ref_exists(root, ref.remote_sha, env):
            base = ref.remote_sha
        if base == DEFAULT_BASE and not ref_exists(root, DEFAULT_BASE, env):
            base = None
        if (ref.local_sha, base or "") not in seen:
            seen.add((ref.local_sha, base or ""))
            targets.append((ref, base))
    return targets


@contextlib.contextmanager
def detached_worktree(root: Path, sha: str, env: Mapping[str, str]) -> Iterator[Path]:
    """A detached worktree of `sha` in a fresh temporary directory outside the repository. Nothing is ever linked
    into it (no symlink, no junction): `git worktree remove` once followed a junction into a real venv."""
    tmp = Path(tempfile.mkdtemp(prefix="aeos-gates-")).resolve()
    try:
        proc = git(["worktree", "add", "--detach", str(tmp), sha], root, env)
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
        yield tmp
    finally:
        git(["worktree", "remove", "--force", str(tmp)], root, env)
        shutil.rmtree(tmp, ignore_errors=True)  # only what git left behind; never follows a link
        git(["worktree", "prune"], root, env)


def run_hook(stdin_text: str, root: Path = ROOT) -> list[Result]:
    env = clean_env()
    results: list[Result] = []
    for ref, base in hook_targets(parse_push_lines(stdin_text), root, env):
        label = f"{ref.local_ref}@{ref.local_sha[:12]}"
        try:
            with detached_worktree(root, ref.local_sha, env) as tmp:
                results.extend(run_gate(g, tmp, env, label) for g in STATIC_GATES)
                if base:
                    results.append(run_gate(commit_subject_gate(base, ref.local_sha), tmp, env, label))
                else:
                    results.append(Result("commit-subjects", SKIPPED, 0.0,
                                          f"no base: the remote sha is zero or unknown and {DEFAULT_BASE} is missing",
                                          "", label))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            results.append(Result("worktree", FAIL, 0.0, str(exc), "", label))
        results.extend(Result(g.name, SKIPPED, 0.0, "hook mode: the temporary worktree has no frontend/node_modules",
                              g.command, label) for g in FRONTEND_GATES)
    return results


def run_local(args: argparse.Namespace, root: Path = ROOT) -> list[Result]:
    env = clean_env()
    base = args.base or (DEFAULT_BASE if ref_exists(root, DEFAULT_BASE, env) else None)
    results = [run_gate(g, root, env) for g in STATIC_GATES]
    if base:
        results.append(run_gate(commit_subject_gate(base, "HEAD"), root, env))
    else:
        results.append(Result("commit-subjects", SKIPPED, 0.0,
                              f"no base ref ({DEFAULT_BASE} not found; pass --base)"))
    results.extend(frontend_results(root, base, args.frontend, env))
    if args.tests:
        results.append(run_gate(test_gate(), root, env))
    return results


def render_table(results: Sequence[Result]) -> str:
    hooked = any(r.ref for r in results)
    lines = []
    last_ref = None
    for r in results:
        if hooked and r.ref != last_ref:
            lines += ["", f"push: {r.ref}"]
            last_ref = r.ref
        lines.append(f"  {r.gate:<22} {r.status:<8} {r.seconds:>8.1f}")
    header = f"  {'gate':<22} {'result':<8} {'seconds':>8}"
    out = [header, "  " + "-" * (len(header) - 2), *lines]
    for r in results:
        if r.status == SKIPPED:
            out.append(f"SKIPPED {r.gate}: {r.detail}")
    return ascii_text("\n".join(out))


def render_failures(results: Sequence[Result], hook: bool) -> str:
    failed = [r for r in results if r.status == FAIL]
    if not failed:
        return ""
    out = []
    for r in failed:
        out.append(f"\n--- {r.gate} FAILED{' (' + r.ref + ')' if r.ref else ''}")
        if r.command:
            out.append(f"command: {r.command}")
        out.append(r.detail)
    if hook:
        out.append("\nThe push was stopped. The output above is the last lines of each failed gate. To see all of it, "
                   "fix or check out the commit and run: python scripts/gates.py")
        out.append("(git push --no-verify skips this hook. That is git's choice and not this tool's to close.)")
    else:
        out.append("\nRun the failed command shown above for its full output.")
    return ascii_text("\n".join(out))


def to_json(results: Sequence[Result], mode: str) -> str:
    payload = {"mode": mode, "ok": not any(r.status == FAIL for r in results),
               "results": [{"gate": r.gate, "status": r.status, "seconds": round(r.seconds, 2),
                            "command": r.command, "detail": r.detail, "ref": r.ref} for r in results]}
    return json.dumps(payload, indent=2)  # ensure_ascii keeps the output ASCII


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run CI's static gates locally.")
    parser.add_argument("--tests", action="store_true", help="also run the backend suite (pytest -n logical if xdist)")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    parser.add_argument("--base", help=f"base ref for the commit-subject and frontend gates (default {DEFAULT_BASE})")
    parser.add_argument("--frontend", action="store_true", help="run the frontend gates even if frontend/ is unchanged")
    parser.add_argument("--hook", action="store_true", help="git pre-push mode: read the push lines from stdin")
    parser.add_argument("hook_args", nargs="*", help="remote name and url, as git passes them to pre-push (ignored)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.hook:
        results = run_hook(sys.stdin.read())
        if args.tests:
            print("--tests is ignored in --hook mode (static gates only)", file=sys.stderr)
    else:
        results = run_local(args)
    mode = "hook" if args.hook else "local"
    if args.json:
        print(to_json(results, mode))
    else:
        if not results:
            print("gates: nothing to check (every pushed ref is a deletion)")
        else:
            print(render_table(results))
        print(render_failures(results, args.hook))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
