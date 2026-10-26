# Contributing to DLY AEOS

DLY AEOS is proprietary software, and all rights are reserved (see [LICENSE](LICENSE)). The source
is public so that it can be read. It is not an open-source project.

## External pull requests are not accepted

The copyright holder does not accept external contributions. Pull requests from outside the
project are closed without review. Do not send patches by any other route either. Under section 6 of
[LICENSE](LICENSE), anything submitted anyway may be used by the copyright holder without obligation.

## What you can do

- **Report a bug.** Open an issue with the **Bug report** form. Say which version (tag or commit) and
  which host you used, and how to reproduce the bug.
- **Suggest a feature.** Open an issue with the **Feature request** form. Describe the problem you
  want solved, not only the solution.
- **Report a vulnerability.** Never use a public issue. Follow [SECURITY.md](SECURITY.md) (GitHub
  private vulnerability reporting).
- **Ask for a licence.** Open an issue with the **Licence request** form, titled "Licence request".
  Say what you want to do with the software and for how long. Permission exists only when the
  copyright holder gives it in writing, and it covers only its stated scope.

Opening an issue grants no licence. Running, copying or modifying DLY AEOS still needs the copyright
holder's prior written permission ([LICENSE](LICENSE) section 2). Reports based on reading the
source are welcome.

Never put secrets, tokens, passwords, personal data, company names, tenant addresses or unredacted
logs in an issue. Use obviously fake values.

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development notes for the owner

The rules for anyone changing the code are in [AGENTS.md](AGENTS.md). The design is the
[master architecture](docs/architecture/DLY_AEOS_Master_Architecture_Final.md).

### Setup

Run `.\scripts\bootstrap.ps1` (Windows) or `./scripts/bootstrap.sh` (macOS). It creates `.venv`,
installs the pinned `requirements.lock` plus the project with `--no-deps -e .`, backs up existing
data, builds the frontend, upgrades the database and runs the Doctor. See
[docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md) and [docs/SETUP_MACOS.md](docs/SETUP_MACOS.md).

### Gates to run before opening a pull request

CI runs the same gates in [.github/workflows/ci.yml](.github/workflows/ci.yml):

```bash
python -m ruff check backend tests scripts
python -m mypy --platform darwin
python -m mypy --platform win32
python scripts/secret_scan.py
python scripts/changelog.py --check
python -m pytest -q -n auto -m "not integration and not network"
cd frontend && npm ci && npx tsc -b --noEmit && npm test && npm run build
```

On Windows, run `python` from `.venv\Scripts\python.exe` and use `npx.cmd`/`npm.cmd` if script
execution is blocked. mypy must report no errors for `--platform darwin` and `--platform win32`;
`pyproject.toml` exempts no module (no `ignore_errors` override may be added).
`tests/unit/test_repository_hygiene.py` keeps the licence, version and repository files consistent.

`-n auto` runs the suite on parallel workers (pytest-xdist, part of the `dev` extra). It starts one
worker per physical core, and `-n logical` one per logical CPU. Parallel runs are proven on macOS
Apple Silicon only (locally, about three times faster with 4 workers). On Windows and Intel macOS they are
NOT-PROVEN until the first CI run of the sharded layout (ADR-0115 amendment). Leave out `-n` to run it serially,
for example to debug a single test.

### Run the gates locally

`scripts/gates.py` runs the static gates above for you (stdlib only; use the `.venv` Python). It prints one
line per gate with PASS, FAIL or SKIPPED and the seconds, and exits 1 when a gate fails.

```bash
python scripts/gates.py            # ruff, mypy darwin + win32, secret scan, commit subjects, frontend if it changed
python scripts/gates.py --tests    # also the backend suite (pytest -n logical when xdist is installed)
python scripts/gates.py --json     # machine-readable
```

The frontend gates (typecheck, unit tests) run only when files under `frontend/` differ from `origin/main` and
`frontend/node_modules` exists; otherwise they are SKIPPED and the reason is printed. The gate list is the G1
step of `ci.yml`; `tests/unit/test_dev_gates.py` fails when the two diverge.

The run also does what CI cannot do for you, and says what it did not do:

- **`merge-conflict`.** When `origin/main` exists, `git merge-tree --write-tree origin/main HEAD` must be clean. A
  conflict is a FAIL that lists the files: pushing such a branch costs a conflict-resolution commit and a full CI
  re-run. Any other failure of that command (for example unrelated histories, exit 128) is a FAIL with git's message.
  It needs git 2.38 or newer; only with an older git (usage error, exit 129) is the gate SKIPPED with that reason.
- **`NOT RUN: <job> (<reason>)`.** One line for every job of `ci.yml` that this run does not execute (the job ids
  are read from the workflow, so a new job shows up by itself): the backend shards, the verifier, bootstrap,
  browser e2e, the supply-chain report and the aggregators. `commit-subjects` is the only job that counts as run, and
  only when both of its steps ran here: the commit-subject gate and the `shellcheck` gate (SKIPPED when shellcheck
  is not installed; then the job stays NOT RUN). CI uses the shellcheck that ubuntu-latest ships (0.9.x), so a newer
  local one can accept what CI rejects. A local PASS is therefore never a CI PASS.
- **One verdict line, last:** `GATES: PASS|FAIL (<n> run, <m> failed, <k> not run)`; "not run" counts SKIPPED gates
  and the NOT RUN jobs. `--report path.json` writes the same verdict, every result and the NOT RUN list to a file,
  for tools that hide lines of long output. With `--json` the JSON is the only thing on stdout and the verdict goes
  to stderr, written after the JSON is flushed so that `2>&1` output also ends with it.
- **Environment facts first** (`python scripts/preflight.py --facts`, informational, never fails the run): the Python
  path and version, whether it is the project venv, whether pytest and pytest-xdist import, the node, npm and git
  versions, `core.hooksPath`, the shell and whether `gh auth status` succeeds (only the exit status is used; no token
  is read or printed). `--no-facts` skips it.

To run the static gates automatically before every push, enable the hook once in each clone:

```bash
git config core.hooksPath scripts/git-hooks
```

The hook checks out the tip commit of each pushed ref into a temporary detached worktree outside the repository,
runs the static gates there and removes it. Intermediate commits of the push do not get the static gates; they
get only the commit-message check over `base..tip`, and only when a base is known (the remote sha, or `origin/main`
for a new branch); with no base that check is SKIPPED and says so. A failing gate stops the push. Frontend gates are SKIPPED in
the hook (the temporary worktree has no `node_modules`). `git push --no-verify` skips the hook; CI stays the gate.

Before any gate the hook refuses two classes of wrong push (incident 2026-10-26: a delegated agent left a worktree
on a detached `HEAD`, so the push and the pull request went out on an old commit):

- **Wrong commit.** `HEAD` is detached and is neither a pushed commit nor an ancestor of one (the commits made on it
  are on no branch), or a pushed ref is `refs/heads/<current branch>` and its sha is not `HEAD`. The message prints
  both shas and the fix: check the ancestry (`git merge-base --is-ancestor <branch> HEAD`), then
  `git branch -f <branch> HEAD`. Pushing another branch from a clean `HEAD` stays allowed.
- **Identity.** Every commit the push adds (not reachable from the remote sha or from `origin/main`) must have
  author and committer e-mail equal to this repository's `git config user.email`, no AI identity and no AI
  attribution (`aeos.core.commit_identity` and `aeos.core.attribution`, the broker's own checks). The message names
  the commits and the exact fix (`git config user.email`, then `git rebase -r <base> --exec '<amend>'`, where
  `<amend>` is `git merge-base --is-ancestor HEAD origin/main || git commit --amend --no-edit --reset-author
  --allow-empty`: commits that `origin/main` already has keep their author); it never prints an address. The address is set per repository, never committed.

The hook picks its Python in this order: `AEOS_GATES_PYTHON` (an existing executable; the hook stops with an error
if it is not), the `.venv` of the checkout, the `.venv` of the main checkout (a linked worktree has none of its
own, found only when the common git directory is a `.git`; never link or junction one into a worktree), then `python3` or `python` from `PATH` with a warning. Gates
that need ruff or mypy report FAIL, not SKIPPED, when the chosen Python does not have them.

### Run the tests with a preflight

`scripts/run_tests.sh [--deadline MINUTES] [--] [pytest arguments]` runs the backend suite as CI does
(`pytest -q -m "not integration and not network" -n logical`, then your arguments). It exists because a background
test run once failed on a missing dependency and again because zsh does not word-split an unquoted variable: the
script is `bash` with `set -euo pipefail` and keeps its arguments in arrays. Before pytest starts it runs
`python -m pip check` and imports `pytest` and `xdist`; a missing or conflicting package stops the run (exit 2) with
the package named, and pytest is never started. `--deadline` (or `AEOS_TEST_DEADLINE_MIN`) stops the run after that
many minutes with exit 124, through `scripts/run_with_deadline.py` because macOS has no `timeout`. The interpreter is
chosen as the pre-push hook does (`AEOS_GATES_PYTHON`, the `.venv`, the main checkout's `.venv`, `python3`). The last
line of output is always `RUN_TESTS: PASS rc=<n>` or `RUN_TESTS: FAIL rc=<n>`, so grep for it instead of trusting a
tail of the log. macOS arm64 is proven; Windows and Intel macOS are NOT-PROVEN (Windows runs the suite with
`python -m pytest` directly). CI lints the shell scripts with `shellcheck scripts/*.sh scripts/git-hooks/pre-push`.

Commit subjects follow Conventional Commits, `type(scope)?!?: description` (types `feat`, `fix`, `docs`,
`test`, `ci`, `build`, `chore`, `refactor`, `perf`, `style`, `revert`, `security`; at most 100 characters;
merge commits are exempt from the format; `Revert "<inner>"` and `Reapply "<inner>"`, as `git revert` writes
them, pass when the inner subject passes (it keeps the 100-character limit) or is a merge subject such as
`Merge pull request #7 from ...`). The same check rejects an AI attribution in the message, the author or the
committer, with the rules of `backend/aeos/core/attribution.py` (AGENTS.md invariant 11). Check a range with `python scripts/commit_subject.py origin/main..HEAD`. Commits
up to the baseline in that script are not checked; older history does not comply.

### CI layout

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs these jobs for every pull request and every
push to `main` ([ADR-0115](docs/decisions/ADR-0115-public-repository-on-github-free.md), amendment
"CI in about ten minutes"):

- **Backend shards.** On each host OS (`windows-latest`, `macos-latest`, `macos-26-intel`) the full
  backend suite is split by test file into 2 shards: `backend shard 1/2 [G1,G2] (<os>)` and
  `backend shard 2/2 [G2] (<os>)`. Each shard runs `pytest -n logical --shard K/2`. Shard 1 also runs
  the G1 static checks (ruff and the secret scan, plus mypy on `macos-latest`). Each shard uploads its
  JUnit XML as the artifact `junit-backend-<os>-shard-<k>`.
- **Commit subjects.** `commit subjects [G1]` runs only on pull requests (`ubuntu-latest`, full history) and
  checks the subjects and the no-attribution rule (message, author and committer e-mail, merge commits
  included) of the pull request's commits with `scripts/commit_subject.py`, and runs `shellcheck` over the shell
  scripts (`scripts/*.sh`, `scripts/git-hooks/pre-push`).
- **Verifier.** `backend verifier [G2] (macos-latest)` runs the real trusted-suite verifier test.
- **Required names are aggregators.** The ruleset on `main` requires `backend [G1,G2] (<os>)` for each
  OS. These are small jobs on `ubuntu-latest` that wait for every backend job of their OS. They pass only
  when each of those jobs succeeded: a failed, skipped or cancelled job fails them. The other required
  checks are real jobs: `bootstrap [G2] (<os>)`, `frontend [G1,G3]` and
  `browser e2e + a11y smoke [G3]`. The supply-chain report is `[NO-GATE]`. Keep these names when you
  change the workflow; `tests/unit/test_ci_gates.py` checks them.
- **Changelog check.** The job `browser e2e + a11y smoke [G3]` first runs
  `python scripts/changelog.py --check` (stdlib only, a few seconds).
- **Concurrency.** A new push to a pull request cancels that pull request's older run. Runs on `main`
  are never cancelled, so every merge commit gets its own CI result.
- **Caches.** The pip, npm and Playwright browser downloads are cached, keyed on `requirements.lock`,
  `frontend/package-lock.json` and the Playwright version.

To run one CI shard locally, use the same arguments: the shards of a run never overlap and together
cover every collected test.

```bash
python -m pytest -q -n auto -m "not integration and not network" --shard 1/2
python -m pytest -q -n auto -m "not integration and not network" --shard 2/2
```

`--shard K/N` can also come from the `AEOS_TEST_SHARD` environment variable, and the command-line flag
wins if both are set. Shards are packed from measured seconds per test file in
[tests/ci_durations.json](tests/ci_durations.json). A test file that is not listed there gets the
median weight. To rebalance from real CI times, download the `junit-backend-<os>-shard-*` artifacts of
one run (normally `windows-latest`, the slowest OS) and run:

```bash
python scripts/ci_durations.py junit-shard-1.xml junit-shard-2.xml --source "CI run <id>, windows-latest"
```

Commit the rewritten `tests/ci_durations.json`. The weights only move files between shards: no test
is added or dropped.

### Rules for every change

- **NOT-PROVEN stays visible.** A pull request says what is proven, at which evidence level, and
  what is still NOT-PROVEN. Nothing is shown as a pass without evidence.
- **No secrets** in files, commit messages, logs or test fixtures. The secret and PII scan must
  stay clean.
- **No attribution lines.** Commit messages and files carry no co-author trailers for tools or
  assistants and no "generated by" footers. Use conventional commit messages, for example
  `fix(doctor): ...`. The `commit subjects [G1]` job checks the commit messages and the author and
  committer e-mails for this on pull requests.
- **Commit identity is local.** The name and e-mail used for commits are set per code host in the
  local git configuration (`git config user.name` / `git config user.email`, or an `includeIf`
  block). They are never committed to a file in the repository.
- **Migrations stay linear.** Alembic has one chain and one head. `aeos db upgrade` takes a backup
  first.
- **Docs follow the code.** Update the ADRs (`docs/decisions`), the
  [coverage matrix](docs/IMPLEMENTATION_COVERAGE.md) and the runbooks.
- **Changelog by fragment.** Add one fragment file under [changelog.d/](changelog.d/README.md)
  (`changelog.d/<short-slug>.md`); do not edit [CHANGELOG.md](CHANGELOG.md) in a pull request. Two pull
  requests that edit `[Unreleased]` conflict on every merge. `python scripts/changelog.py --check` validates
  the fragments, and CI runs it.
- **Merge to `main` is the owner's decision.** It happens only through a pull request that passes
  the required CI checks under the ruleset on `main` (no force-push, no deletion). Any delegation of
  that authority is an owner decision recorded as an ADR in `docs/decisions`. DLY AEOS workers never
  inherit it: they can push only `aeos/*` task branches.

### Releasing

1. Set the new version in `pyproject.toml`, `frontend/package.json` (and the root entry of
   `frontend/package-lock.json`) and `CITATION.cff`, together with `date-released`.
2. Run `python scripts/changelog.py --assemble` (add `--dry-run` first to see it) to move the fragments
   into `[Unreleased]` of `CHANGELOG.md`. Then move the `[Unreleased]` entries under a new version heading,
   and add its compare/tag link.
3. Write the release notes in `docs/releases/vX.Y.Z.md`.
4. After the release pull request is merged, tag the merge commit `vX.Y.Z` and publish the GitHub
   Release with the release notes as its body.
