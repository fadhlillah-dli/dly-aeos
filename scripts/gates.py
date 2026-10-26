"""Run CI's static gates locally, before pushing (CONTRIBUTING.md, "Gates to run before opening a pull request").

    python scripts/gates.py                static gates, the commit-subject gate, frontend gates when frontend/ differs
    python scripts/gates.py --tests        also the backend suite (CI runs it in about ten minutes; default is off)
    python scripts/gates.py --json         machine-readable results
    python scripts/gates.py --hook         git pre-push mode, see scripts/git-hooks/pre-push
    python scripts/gates.py --report r.json   also write the full result (verdict included) to a JSON file

In hook mode the static gates run on the tip commit of each pushed ref only; the commit-message check covers
base..tip, so intermediate commits get that check and nothing else (and only when a base is known, else it is
SKIPPED with the reason). Run with the project .venv (or set
AEOS_GATES_PYTHON for the hook): ruff and mypy must be installed for sys.executable, else their gates FAIL.

The static gate list is the G1 step of .github/workflows/ci.yml; tests/unit/test_dev_gates.py fails when the two
diverge. Stdlib only, ASCII-only output. A gate that does not apply (frontend gates without changes or without
node_modules, the commit-message check without a base) is SKIPPED with its reason, never PASS; a missing ruff or
mypy is a FAIL.
Exit code 1 when any gate FAILs.

It also runs CI's shellcheck step when shellcheck is installed (SKIPPED with the reason otherwise; the version may differ
from CI's, so the commit-subjects job counts as run only when both its gates ran).

Beyond the CI gates it runs checks CI cannot make for you: a merge-conflict gate (`git merge-tree --write-tree
origin/main <tip>`: a branch that conflicts needs a merge commit and a full CI re-run), and in hook mode, before
any gate, a wrong-commit guard (a detached HEAD whose commits the pushed refs do not carry; a pushed branch ref that
is not the HEAD of the current branch) and an identity guard (every pushed commit is authored and committed by this
repository's `git config user.email`, with no AI identity or attribution: aeos.core.commit_identity).

The output ends with one `NOT RUN: <job> (<reason>)` line for every CI job this run does not execute, then exactly
one verdict line `GATES: PASS|FAIL (<n> run, <m> failed, <k> not run)` ("not run" counts SKIPPED gates and those
CI jobs), so a local PASS never reads as a CI PASS. `--report` writes the same facts to a file for tools that hide
output lines. Before everything, scripts/preflight.py --facts prints environment facts (informational only)."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from aeos.core.commit_identity import (  # noqa: E402
    CODE_ATTRIBUTION,
    CODE_RANGE,
    GITHUB,
    MAX_CHECKED_COMMITS,
    commit_problems,
    config_key,
    parse_cat_file_batch,
    validate_identity,
)

SCRIPTS = Path(__file__).resolve().parent
COMMIT_SUBJECT = SCRIPTS / "commit_subject.py"
PREFLIGHT = SCRIPTS / "preflight.py"
CI_WORKFLOW = Path(".github") / "workflows" / "ci.yml"

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"
ZERO_SHA_CHARS = frozenset("0")
DEFAULT_BASE = "origin/main"
TAIL_LINES = 40
GATE_TIMEOUT_SECONDS = 1800
# What the identity guard prints to run after `git rebase -r <base> --exec`: commits that origin/main already has keep
# their author (they are not this push's, and re-authoring them would cut the branch loose from main); the rest are
# re-authored to this repository's user.email.
AMEND_EXEC = (f"git merge-base --is-ancestor HEAD {DEFAULT_BASE} || "
              "git commit --amend --no-edit --reset-author --allow-empty")
SHELLCHECK_COMMAND = "shellcheck scripts/*.sh scripts/git-hooks/pre-push"  # the CI step, word for word
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


def shell_files(root: Path) -> list[str]:
    """The files of SHELLCHECK_COMMAND (what CI's shell glob expands to), relative to the checkout, forward slashes."""
    files = sorted(p.relative_to(root).as_posix() for p in (root / "scripts").glob("*.sh"))
    if (root / "scripts" / "git-hooks" / "pre-push").is_file():
        files.append("scripts/git-hooks/pre-push")
    return files


def shellcheck_result(root: Path, env: Mapping[str, str], ref: str = "") -> Result:
    """CI's shellcheck step (the commit-subjects job). SKIPPED with the reason when shellcheck is not installed or
    the checkout has no shell scripts: then that CI step stays NOT RUN. The version of shellcheck matters: CI uses
    the one ubuntu-latest ships (0.9.x at the time of writing); a newer local one may accept what CI rejects."""
    files = shell_files(root)
    if not files:
        return Result(SHELLCHECK, SKIPPED, 0.0, "no shell scripts in this checkout", SHELLCHECK_COMMAND, ref)
    return run_gate(Gate(SHELLCHECK, ("shellcheck", *files)), root, env, ref)


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


# ------------------------------------------------------------------ guards: hook mode, before any gate
WRONG_COMMIT = "wrong-commit"
SHELLCHECK = "shellcheck"
MERGE_TREE_USAGE = 129  # git < 2.38 has no `merge-tree --write-tree` and answers with its usage text
IDENTITY = "identity"
MERGE_CONFLICT = "merge-conflict"
MAX_LISTED = 10


def ref_label(ref: PushRef) -> str:
    return f"{ref.local_ref}@{ref.local_sha[:12]}"


def _is_ancestor(root: Path, older: str, newer: str, env: Mapping[str, str]) -> bool:
    return git(["merge-base", "--is-ancestor", older, newer], root, env).returncode == 0


def _branch_name(ref_name: str) -> str:
    return ref_name[len("refs/heads/"):] if ref_name.startswith("refs/heads/") else "<branch>"


def wrong_commit_guard(refs: Sequence[PushRef], root: Path, env: Mapping[str, str]) -> Result | None:
    """Refuse a push that would not carry the commit the working tree is on (incident 2026-10-26: a delegated agent
    left a worktree on a detached HEAD, so the push and the pull request went out on an old commit).
    (a) HEAD is detached and is neither a pushed commit nor an ancestor of one: the commits made on it are on no
    branch. (b) A pushed ref is the checked-out branch but its sha is not HEAD. Pushing other branches from a clean
    HEAD stays allowed. None when only deletions are pushed."""
    pushed = [r for r in refs if not r.is_deletion]
    if not pushed:
        return None
    head = git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], root, env)
    if head.returncode != 0:
        return Result(WRONG_COMMIT, SKIPPED, 0.0, "HEAD has no commit yet: there is nothing to compare the pushed "
                      "refs with", "", "HEAD")
    head_sha = head.stdout.strip()
    label = f"HEAD@{head_sha[:12]}"
    started = time.perf_counter()
    symbolic = git(["symbolic-ref", "-q", "HEAD"], root, env)
    problems: list[str] = []
    if symbolic.returncode != 0:
        if not any(r.local_sha == head_sha or _is_ancestor(root, head_sha, r.local_sha, env) for r in pushed):
            branch = _branch_name(pushed[0].local_ref)
            listed = "\n".join(f"  {r.local_ref} -> {r.local_sha}" for r in pushed[:MAX_LISTED])
            problems.append(
                f"HEAD is detached at {head_sha} and is not contained in any pushed ref (it is neither a pushed "
                f"commit nor an ancestor of one). Pushed:\n{listed}\n"
                "Commits made on a detached HEAD belong to no branch, so this push would go out without them.\n"
                "Fix: choose the branch HEAD belongs to, check that the branch is an ancestor of HEAD, then move "
                "it and switch to it:\n"
                f"  git merge-base --is-ancestor {branch} HEAD    (exit code 0 means it is)\n"
                f"  git branch -f {branch} HEAD && git switch {branch}\n"
                "Then push again.")
    else:
        current = symbolic.stdout.strip()
        for r in pushed:
            if r.local_ref == current and r.local_sha != head_sha:
                short = _branch_name(current)
                problems.append(
                    f"{r.local_ref} is pushed at {r.local_sha}, but HEAD of the checked-out branch {short} is "
                    f"{head_sha}: the pushed ref is stale, so the wrong commit would go out.\n"
                    f"Fix: compare them (git rev-parse {r.local_ref} HEAD); if HEAD is the commit you mean, check "
                    f"that the branch is an ancestor of HEAD (git merge-base --is-ancestor {short} HEAD) and move "
                    f"it (git branch -f {short} HEAD), then push again.")
    seconds = time.perf_counter() - started
    if problems:
        return Result(WRONG_COMMIT, FAIL, seconds, "\n".join(problems), "", label)
    return Result(WRONG_COMMIT, PASS, seconds, "", "", label)


def _git_config(root: Path, key: str, env: Mapping[str, str]) -> str:
    proc = git(["config", "--get", key], root, env)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def identity_guard(ref: PushRef, base: str | None, root: Path, env: Mapping[str, str]) -> Result:
    """Every commit this push adds (reachable from the pushed tip, not from the base nor from origin/main) is
    authored and committed by this repository's `git config user.email` and carries no AI identity or attribution.
    The judgement is aeos.core.commit_identity (the broker's own check), not a copy of it."""
    label = ref_label(ref)
    if base is None:
        return Result(IDENTITY, SKIPPED, 0.0, f"no base: the remote sha is zero or unknown and {DEFAULT_BASE} is "
                      "missing, so the commits this push adds cannot be told", "", label)
    started = time.perf_counter()
    exclude = [base]
    if base != DEFAULT_BASE and ref_exists(root, DEFAULT_BASE, env):
        exclude.append(DEFAULT_BASE)  # commits already on main are not this push's; their committer may be the host
    listed = git(["rev-list", ref.local_sha, *[f"^{e}" for e in exclude]], root, env)
    if listed.returncode != 0:
        return Result(IDENTITY, FAIL, time.perf_counter() - started,
                      f"git rev-list failed: {listed.stderr.strip()}", "", label)
    shas = listed.stdout.split()
    if not shas:
        return Result(IDENTITY, PASS, time.perf_counter() - started, "", "", label)
    if len(shas) > MAX_CHECKED_COMMITS:
        return Result(IDENTITY, FAIL, time.perf_counter() - started,
                      f"{CODE_RANGE}: this push adds {len(shas)} commits (limit {MAX_CHECKED_COMMITS}); "
                      "the range is not what you meant", "", label)
    name, email = _git_config(root, "user.name", env), _git_config(root, "user.email", env)
    own = validate_identity(name, email)
    if own:
        return Result(IDENTITY, FAIL, time.perf_counter() - started,
                      f"this repository's git identity is not usable: {own}.\n"
                      "Fix: set it for this repository only, to the owner's address for this code host:\n"
                      '  git config user.name "<your name>"\n  git config user.email "<your address>"', "", label)
    try:
        proc = subprocess.run(["git", "cat-file", "--batch"], cwd=str(root), env=dict(env), capture_output=True,
                              input=("\n".join(shas) + "\n").encode("ascii"), check=False,
                              timeout=GATE_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise ValueError(proc.stderr.decode("utf-8", "replace").strip() or f"git exit {proc.returncode}")
        records = parse_cat_file_batch(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return Result(IDENTITY, FAIL, time.perf_counter() - started, f"cannot read the pushed commits: {exc}", "", label)
    problems = commit_problems(records, provider=GITHUB, expected_email=email)
    seconds = time.perf_counter() - started
    if not problems:
        return Result(IDENTITY, PASS, seconds, "", "", label)
    lines = []
    for p in problems[:MAX_LISTED]:
        detail = p["detail"].replace(f"is not the configured {GITHUB} identity ({config_key(GITHUB)})",
                                     "is not this repository's git config user.email")
        lines.append(f"  {p['sha'][:12]} {p['code']}: {detail}")
    if len(problems) > MAX_LISTED:
        lines.append(f"  (+{len(problems) - MAX_LISTED} more)")
    since = base[:12] if len(base) >= 40 else base
    # Printed commands are paste-safe: no inline '#' comment (zsh passes it on), and the exec string in double quotes
    # (one argument in cmd.exe, PowerShell and sh; cmd.exe does not treat single quotes as quoting).
    fix = ["Fix, for the commits listed (never print or commit the address; it stays in local git config).",
           "First check that this repository's address is the owner's address for this code host:",
           "  git config user.email",
           "Then re-author the commits listed above:",
           f'  git rebase -r {since} --exec "{AMEND_EXEC}"',
           f"  (-r keeps merges; commits that {DEFAULT_BASE} already has are left as they are)"]
    if any(p["code"] == CODE_ATTRIBUTION for p in problems):
        fix.append(f"  an AI attribution in a message or identity must be removed: git rebase -i {since} (reword) "
                   "or amend the author")
    return Result(IDENTITY, FAIL, seconds, "\n".join([f"{len(problems)} problem(s) in {len(shas)} pushed commit(s):",
                                                      *lines, *fix]), "", label)


def merge_conflict_result(root: Path, env: Mapping[str, str], tip: str = "HEAD", ref: str = "") -> Result:
    """FAIL when merging `tip` into origin/main would conflict (git merge-tree --write-tree: exit 1 = conflicts), with
    the conflicting files. A branch that conflicts needs a merge commit and then a full CI re-run. Any other non-zero
    exit (for example 128, unrelated histories) is a FAIL too, with git's own message. SKIPPED, with the reason, only
    when origin/main is missing or git is older than 2.38 (usage error, exit 129)."""
    if not ref_exists(root, DEFAULT_BASE, env):
        return Result(MERGE_CONFLICT, SKIPPED, 0.0, f"{DEFAULT_BASE} not found (git fetch origin)", "", ref)
    argv = ["merge-tree", "--write-tree", "--name-only", "--no-messages", DEFAULT_BASE, tip]
    command = "git " + " ".join(argv)
    started = time.perf_counter()
    try:
        proc = git(argv, root, env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(MERGE_CONFLICT, SKIPPED, 0.0, f"git merge-tree did not run: {exc}", command, ref)
    seconds = time.perf_counter() - started
    if proc.returncode == 0:
        return Result(MERGE_CONFLICT, PASS, seconds, "", command, ref)
    if proc.returncode == 1:
        files: list[str] = []
        for line in proc.stdout.splitlines()[1:]:  # line 1 is the tree id of the (conflicted) result
            if line.strip() and line not in files:
                files.append(line)
        shown = "\n".join(f"  {f}" for f in files[:MAX_LISTED * 3])
        more = f"\n  (+{len(files) - MAX_LISTED * 3} more)" if len(files) > MAX_LISTED * 3 else ""
        return Result(MERGE_CONFLICT, FAIL, seconds,
                      f"merging into {DEFAULT_BASE} would conflict in {len(files)} file(s):\n{shown}{more}\n"
                      f"Fix: git fetch origin, then git merge {DEFAULT_BASE} (or rebase), resolve the files and "
                      "commit. Pushing it as is costs a conflict-resolution commit and a full CI re-run.",
                      command, ref)
    message = _tail(proc.stderr or proc.stdout)
    if proc.returncode == MERGE_TREE_USAGE:
        # git before 2.38 has no `--write-tree`: the only exit code that means "this git cannot tell".
        return Result(MERGE_CONFLICT, SKIPPED, seconds, "git merge-tree --write-tree needs git 2.38 or newer "
                      f"(exit {proc.returncode}): {' '.join(message.splitlines()[:1])}".strip(), command, ref)
    # Any other failure (for example 128: unrelated histories) means the merge could not be made: not a pass.
    return Result(MERGE_CONFLICT, FAIL, seconds,
                  f"git merge-tree could not merge {tip} into {DEFAULT_BASE} (exit {proc.returncode}):\n{message}",
                  command, ref)


# ------------------------------------------------------------------ runs
def run_hook(stdin_text: str, root: Path = ROOT) -> list[Result]:
    env = clean_env()
    refs = parse_push_lines(stdin_text)
    targets = hook_targets(refs, root, env)
    results: list[Result] = []
    wrong = wrong_commit_guard(refs, root, env)
    if wrong is not None:
        results.append(wrong)
    if wrong is None or wrong.status != FAIL:
        results.extend(identity_guard(ref, base, root, env) for ref, base in targets)
    if any(r.status == FAIL for r in results):
        return results  # a refused push does not spend minutes on the gates
    for ref, base in targets:
        label = ref_label(ref)
        results.append(merge_conflict_result(root, env, ref.local_sha, label))
        try:
            with detached_worktree(root, ref.local_sha, env) as tmp:
                results.extend(run_gate(g, tmp, env, label) for g in STATIC_GATES)
                results.append(shellcheck_result(tmp, env, label))
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
    results = [merge_conflict_result(root, env)]
    results.extend(run_gate(g, root, env) for g in STATIC_GATES)
    results.append(shellcheck_result(root, env))
    if base:
        results.append(run_gate(commit_subject_gate(base, "HEAD"), root, env))
    else:
        results.append(Result("commit-subjects", SKIPPED, 0.0,
                              f"no base ref ({DEFAULT_BASE} not found; pass --base)"))
    results.extend(frontend_results(root, base, args.frontend, env))
    if args.tests:
        results.append(run_gate(test_gate(), root, env))
    return results


# ------------------------------------------------------------------ what this run did not do
# Reasons by job id prefix, first match wins. Job ids come from the `jobs:` keys of ci.yml, so a job added to the
# workflow shows up as NOT RUN ("no local equivalent") without anyone editing this file.
NOT_RUN_REASONS: tuple[tuple[str, str], ...] = (
    ("backend-shards-", "test shards run on that CI host; locally only the G1 static gates run, and --tests runs the "
                        "suite once on this host (Windows and Intel macOS stay NOT-PROVEN)"),
    ("backend-verifier-", "the real trusted-suite verifier test is not part of a local run"),
    ("backend-", "aggregator of the backend job results; nothing to run locally"),
    ("frontend", "typecheck and unit tests run as gates when frontend/ changed; npm run build does not"),
    ("browser-e2e", "needs Playwright browsers and a built frontend; not run locally"),
    ("bootstrap", "the bootstrap runs on windows-latest, macos-latest and macos-26-intel only"),
    ("supply-chain", "report-only SBOM, licence, vulnerability and SAST scans; not run locally"),
)
# job id -> the gates that run it here. A job counts as run only when EVERY one of its gates PASSed or FAILed: the
# commit-subjects job also has the shellcheck step, so a run without shellcheck must still say NOT RUN for it.
LOCALLY_RUN_JOBS: dict[str, tuple[str, ...]] = {"commit-subjects": ("commit-subjects", SHELLCHECK)}


def ci_job_ids(root: Path) -> list[str]:
    """The job ids of ci.yml (the keys under `jobs:`), read without a YAML library."""
    try:
        text = (root / CI_WORKFLOW).read_text(encoding="utf-8")
    except OSError:
        return []
    ids: list[str] = []
    in_jobs = False
    for line in text.splitlines():
        if re.match(r"^jobs:\s*(#.*)?$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        match = re.match(r"^  ([A-Za-z_][\w-]*):\s*(#.*)?$", line)
        if match:
            ids.append(match.group(1))
    return ids


def not_run_jobs(results: Sequence[Result], root: Path = ROOT) -> list[tuple[str, str]]:
    """(job id, reason) for every CI job of ci.yml that this run did not execute."""
    ids = ci_job_ids(root)
    if not ids:
        return [("all CI jobs", f"{CI_WORKFLOW.as_posix()} was not found or has no jobs")]
    ran = {r.gate for r in results if r.status in (PASS, FAIL)}
    out: list[tuple[str, str]] = []
    for job in ids:
        needed = LOCALLY_RUN_JOBS.get(job)
        if needed and all(g in ran for g in needed):
            continue
        if needed:
            missing = ", ".join(g for g in needed if g not in ran)
            reason = f"not all of its steps ran here: {missing} skipped or absent (see SKIPPED above)"
        else:
            reason = next((why for prefix, why in NOT_RUN_REASONS if job.startswith(prefix)), "no local equivalent")
        out.append((job, reason))
    return out


@dataclasses.dataclass(frozen=True)
class Verdict:
    run: int
    failed: int
    not_run: int

    @property
    def status(self) -> str:
        return FAIL if self.failed else PASS

    @property
    def line(self) -> str:
        return f"GATES: {self.status} ({self.run} run, {self.failed} failed, {self.not_run} not run)"


def verdict_of(results: Sequence[Result], not_run: Sequence[tuple[str, str]]) -> Verdict:
    """`run` counts gates that PASSed or FAILed; `not run` counts SKIPPED gates and the CI jobs not executed."""
    return Verdict(run=sum(r.status in (PASS, FAIL) for r in results),
                   failed=sum(r.status == FAIL for r in results),
                   not_run=sum(r.status == SKIPPED for r in results) + len(not_run))


def run_facts() -> list[str]:
    """The lines of scripts/preflight.py --facts. Informational: nothing here can fail a run."""
    try:
        proc = subprocess.run([sys.executable, str(PREFLIGHT), "--facts"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"preflight: facts unavailable: {exc}"]
    lines = proc.stdout.strip().splitlines()
    return lines or [f"preflight: facts unavailable (exit {proc.returncode})"]


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


def render_not_run(not_run: Sequence[tuple[str, str]]) -> str:
    return ascii_text("\n".join(f"NOT RUN: {job} ({reason})" for job, reason in not_run))


def to_json(results: Sequence[Result], mode: str, not_run: Sequence[tuple[str, str]] = (),
            facts: Sequence[str] = ()) -> str:
    verdict = verdict_of(results, not_run)
    payload = {"mode": mode, "ok": verdict.status == PASS, "verdict": verdict.line,
               "counts": {"run": verdict.run, "failed": verdict.failed, "not_run": verdict.not_run},
               "results": [{"gate": r.gate, "status": r.status, "seconds": round(r.seconds, 2),
                            "command": r.command, "detail": r.detail, "ref": r.ref} for r in results],
               "not_run_jobs": [{"job": job, "reason": reason} for job, reason in not_run],
               "facts": list(facts)}
    return json.dumps(payload, indent=2)  # ensure_ascii keeps the output ASCII


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run CI's static gates locally.")
    parser.add_argument("--tests", action="store_true", help="also run the backend suite (pytest -n logical if xdist)")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    parser.add_argument("--report", metavar="PATH.json",
                        help="also write the full result, verdict included, to this file (for output filters)")
    parser.add_argument("--no-facts", action="store_true", help="skip scripts/preflight.py --facts")
    parser.add_argument("--base", help=f"base ref for the commit-subject and frontend gates (default {DEFAULT_BASE})")
    parser.add_argument("--frontend", action="store_true", help="run the frontend gates even if frontend/ is unchanged")
    parser.add_argument("--hook", action="store_true", help="git pre-push mode: read the push lines from stdin")
    parser.add_argument("hook_args", nargs="*", help="remote name and url, as git passes them to pre-push (ignored)")
    return parser


def hook_root() -> Path:
    """The repository being pushed from: git runs the hook in the root of its working tree."""
    proc = git(["rev-parse", "--show-toplevel"], Path.cwd())
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else ROOT


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    facts = [] if args.no_facts else run_facts()
    if not args.json and facts:
        print(ascii_text("environment (informational):\n" + "\n".join(f"  {line}" for line in facts)))
    root = ROOT
    if args.hook:
        root = hook_root()
        results = run_hook(sys.stdin.read(), root)
        if args.tests:
            print("--tests is ignored in --hook mode (static gates only)", file=sys.stderr)
    else:
        results = run_local(args)
    not_run = not_run_jobs(results, root)
    verdict = verdict_of(results, not_run)
    mode = "hook" if args.hook else "local"
    if args.report:
        try:
            report = Path(args.report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(to_json(results, mode, not_run, facts) + "\n", encoding="utf-8")
        except OSError as exc:
            print(ascii_text(f"gates: cannot write the report {args.report}: {exc}"), file=sys.stderr)
            return 2
    if args.json:
        print(to_json(results, mode, not_run, facts), flush=True)  # flushed first: with 2>&1 the verdict must come last
        print(verdict.line, file=sys.stderr, flush=True)  # stdout stays one JSON document
    else:
        if not results:
            print("gates: nothing to check (every pushed ref is a deletion)")
        else:
            print(render_table(results))
        failures = render_failures(results, args.hook)
        if failures:
            print(failures)
        if not_run:
            print(render_not_run(not_run))
        print(verdict.line)
    return 1 if verdict.failed else 0


if __name__ == "__main__":
    sys.exit(main())
