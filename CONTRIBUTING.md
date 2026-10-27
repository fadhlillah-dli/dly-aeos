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

The plain run covers ruff, mypy for darwin and win32, the secret scan, `shellcheck` and `actionlint` (each SKIPPED
with the reason when it is not installed, see "Install actionlint and shellcheck" below), the commit subjects and the
frontend gates if `frontend/` changed. `--tests` also runs the backend suite (pytest `-n logical` when xdist is installed). `--json`
prints machine-readable results.

```bash
python scripts/gates.py
python scripts/gates.py --tests
python scripts/gates.py --json
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
  browser e2e, the supply-chain report and the aggregators. `static-checks` counts as run only when ruff, both mypy
  platforms, the secret scan, `shellcheck`, `actionlint` and the actionlint bite control all ran here (a SKIPPED one,
  for example without the tool installed, keeps the job NOT RUN); `commit-subjects` counts as run when the
  commit-subject gate ran. CI uses the shellcheck that ubuntu-latest ships (0.9.x) and a pinned actionlint, so a
  different local version can accept what CI rejects. A local PASS is therefore never a CI PASS.
- **One verdict line, last:** `GATES: PASS|FAIL (<n> run, <m> failed, <k> not run)`; "not run" counts SKIPPED gates
  and the NOT RUN jobs. `--report path.json` writes the same verdict, every result and the NOT RUN list to a file,
  for tools that hide lines of long output. With `--json` the JSON is the only thing on stdout and the verdict goes
  to stderr, written after the JSON is flushed so that `2>&1` output also ends with it.
- **Environment facts first** (`python scripts/preflight.py --facts`, informational, never fails the run): the Python
  path and version, whether it is the project venv, whether pytest and pytest-xdist import, the node, npm and git
  versions, `core.hooksPath`, the shell and whether `gh auth status` succeeds (only the exit status is used; no token
  is read or printed). `--no-facts` skips it.

#### Install actionlint and shellcheck

CI lints the workflows with `actionlint`, which lints the `run:` blocks with `shellcheck`. Locally both are optional:
without them `scripts/gates.py` SKIPS the `shellcheck`, `actionlint` and `actionlint-control` gates (never PASS) and
the `static-checks` job stays NOT RUN.

- **macOS:** `brew install actionlint shellcheck`.
- **Windows:** the Scoop main bucket and winget have manifests for both (`scoop install actionlint shellcheck`;
  `winget install rhysd.actionlint koalaman.shellcheck`). That the packages exist is checked; installing them on the
  owner's Windows machine is NOT-PROVEN.
- **Versions:** CI downloads actionlint v1.7.12 from its release page and compares the tarball with a SHA-256 pinned in
  `ci.yml` before it extracts anything (a mismatch fails the step); shellcheck is the one ubuntu-latest ships. Your
  local version can differ, so a local PASS or FAIL is a hint and CI is the proof. Bump the pinned version and its
  checksum together (take the digest from the release's `checksums.txt`).
- **What runs:** `actionlint` (no arguments) lints every file of `.github/workflows`. The bite control lints
  `tests/fixtures/actionlint/bad-workflow.yml`, which must fail with the `matrix.oss` finding, and
  `near-miss-workflow.yml`, which must pass; the fixtures live outside `.github/workflows` on purpose.

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
  the commits and the exact fix (`git config user.email`, then `git rebase -r <base> --exec "<amend>"` with the exec string in double quotes so that cmd.exe, PowerShell and sh read it as one argument, where
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

- **Backend shards.** On each host OS the full backend suite is split by test file: 3 shards on `windows-latest`
  (Windows jobs do not use the macOS slots), 2 on each macOS host (`macos-latest`, `macos-26-intel`). The jobs are
  named `backend shard K/N [G2] (<os>)` and each runs `pytest -n logical --shard K/N`. Every shard is the same job
  and uploads its JUnit XML as the artifact `junit-backend-<os>-shard-<k>`.
- **Static checks.** `static checks [G1] (ubuntu-latest)` runs ruff, mypy for `--platform darwin` and
  `--platform win32`, the secret scan, `shellcheck` over the shell scripts (`scripts/*.sh`,
  `scripts/git-hooks/pre-push`) and `actionlint` over the workflows once for all host OSes, on pull requests and on
  `main`. The actionlint release is pinned by version and SHA-256, and a bite control lints a known-bad and a
  near-miss fixture on every run. The three required `backend [G1,G2] (<os>)` aggregators need the job, so a failing
  static check fails them all.
- **Commit subjects.** `commit subjects [G1]` runs only on pull requests (`ubuntu-latest`, full history) and
  checks the subjects and the no-attribution rule (message, author and committer e-mail, merge commits
  included) of the pull request's commits with `scripts/commit_subject.py`. It is the one job that differs between
  a pull request and a push to `main` (`tests/unit/test_ci_gates.py` lists it as the only event-conditional job, so a
  new check cannot silently skip `main`).
- **Pull request title.** `.github/workflows/pr-title.yml` (job `pr title [G1]`, `ubuntu-latest`, read-only token) checks
  the title with `python scripts/commit_subject.py --subject "<title>"`: the title becomes the merge-commit body and,
  with a squash merge, the subject on `main`, so it follows the same Conventional Commits and no-attribution rules.
  The title reaches the script through an environment variable, never through an expression inside `run:`. The check
  is not in any ruleset: making it required is an owner decision.
- **Verifier.** `backend verifier [G2] (macos-latest)` runs the real trusted-suite verifier test.
- **Required names are aggregators.** The ruleset on `main` requires `backend [G1,G2] (<os>)` for each
  OS. These are small jobs on `ubuntu-latest` that wait for the static job and every backend job of their OS. They pass only
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

(`windows-latest` uses `--shard K/3`.)

`--shard K/N` can also come from the `AEOS_TEST_SHARD` environment variable, and the command-line flag
wins if both are set. Shards are packed from measured seconds per test file in
[tests/ci_durations.json](tests/ci_durations.json). A test file that is not listed there gets the
median weight. To rebalance from real CI times, download the `junit-backend-<os>-shard-*` artifacts of
one run and give each OS its own group; the OSes are scaled to the same total and averaged, because one split
serves all of them and they weigh files differently:

```bash
python scripts/ci_durations.py --os windows-latest w1.xml w2.xml w3.xml --os macos-latest a1.xml a2.xml \
    --os macos-26-intel i1.xml i2.xml --source "CI run <id>"
```

Commit the rewritten `tests/ci_durations.json`. The weights only move files between shards: no test
is added or dropped.

### Developer tools

Two standalone helpers in `scripts/` (stdlib only, ASCII output, last line is the verdict):

- **`scripts/worktree.py`** makes git worktrees safe. `python scripts/worktree.py add <name> <branch> [<base>]`
  creates `<main checkout>-wt-<name>` on a new branch, marks it as created by the tool and prints the
  interpreter of the main checkout (`.venv` there) and the `PYTHONPATH=backend` hint. Never put a `.venv`
  symlink or junction inside a worktree: `git worktree remove` can follow the link and delete the real
  environment. `list` shows every worktree with merged, pushed, marker and LINKS columns. `remove <name>`
  refuses a worktree that holds a link, has no marker (`--foreign`), has uncommitted changes, or whose branch is
  neither merged into origin/main nor pushed (`--force`); `--unlink-links` removes only the link entries, then
  scans again. Merged and pushed come from the remote-tracking refs of the last `git fetch`.
- **`scripts/ci_status.py`** gives one verdict for one exact commit: `--sha <40-hex>`, `--pr <n>`
  (`--expect-sha <sha>` exits 1 if the pull request points at another commit) or `--after-merge <n>` (the merge
  commit on `main`). It reads the check runs with `gh api` and the required contexts from the repository
  rulesets. An empty or incomplete list is PENDING, never GREEN. `--wait --deadline <minutes>` polls (30 s
  or more) and ends with `CI: TIMEOUT` (exit 2) at the deadline; `--report <path.json>` saves the result.
  Exit codes: 0 GREEN, 1 RED, SUPERSEDED or sha mismatch, 2 TIMEOUT or ERROR, 3 PENDING without `--wait`, 4 INFRA.
  The last line is always `CI: <VERDICT> <sha>`, with ` (non-required failed: <names>)` after the sha under
  `--required-only`.

  By default a failure of any check, required or not, is RED. `--required-only` computes the verdict from the
  required contexts only: GREEN when all of them are success, skipped or neutral, PENDING while one is missing,
  queued or running, RED only when a required one failed. A non-required failure is still listed, marked
  `[NON-REQUIRED FAILED]`, and named in the last line, for example
  `CI: GREEN <sha> (non-required failed: commit subjects [G1])`; fix it before you merge, because the verdict no
  longer blocks on it. With no required context found, every check counts as required.

  INFRA means a required job was cancelled because GitHub never gave it a runner (the annotation says the job was
  not acquired by a runner, as when hosted macOS capacity runs out). It is infrastructure, not a code failure:
  re-run the job and never read it as RED.

Both have bite tests (`tests/unit/test_worktree_tool.py`, `tests/unit/test_ci_status_tool.py`): a violating
fixture is red and a near-miss legitimate one is green. The junction test runs only on Windows.

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
