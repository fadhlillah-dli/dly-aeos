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
execution is blocked. The `pytest` line above writes no full-suite record, so the pre-push hook refuses the push
afterwards: run the suite with `scripts/run_tests.sh` (on Windows with Git Bash, see "Run the tests with a
preflight"). mypy must report no errors for `--platform darwin` and `--platform win32`;
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
- **`conflict-markers`.** When `origin/main` exists, `git diff --check <merge base with origin/main>..HEAD` must report
  no "leftover conflict marker" (whitespace complaints are ignored). A FAIL lists each `file:line`. A file whose only
  finding is a lone `=======` line passes: git reports a markdown setext heading underline as a conflict marker too,
  so only a file that also has a `<<<<<<<`, `>>>>>>>` or `|||||||` line is reported. SKIPPED with the reason when
  `origin/main` is missing (LESSONS L22: a failed scripted conflict resolution chained with `;` once committed its
  markers).
- **Full-suite record (informational).** A local run prints whether HEAD's tree has a passing full-suite record from
  `scripts/run_tests.sh` (see "Run the tests with a preflight"); the pre-push hook refuses a push without one.
- **`NOT RUN: <job> (<reason>)`.** One line for every job of every workflow that runs on `pull_request` or `push` that this run does not
  execute (the job ids of `ci.yml` and the job names of the other workflows are read from the files, so a new job shows
  up by itself): the backend shards, the verifier, bootstrap, browser e2e, the supply-chain report, the aggregators
  and `pr title [G1]` (checked only in CI). `static-checks` counts as run only when ruff, both mypy
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

Before any gate the hook refuses these classes of wrong push (incident 2026-10-26: a delegated agent left a worktree
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
- **Full suite** (LESSONS L8, strike 3: a branch tested only with targeted tests was pushed and CI went red). The tree
  of every pushed commit (deletions are not checked) must have a passing full-suite record, written by
  `scripts/run_tests.sh`. Without one the push stops with `full suite not run for tree <short> (<sha>): run
  scripts/run_tests.sh on a clean tree, or push with AEOS_PUSH_WITHOUT_FULL_SUITE='<reason>'`. With that variable set
  to a non-empty reason the `full-suite` guard is SKIPPED (never PASS) and the reason is printed. Set it for one push
  only. `AEOS_PUSH_WITHOUT_FULL_SUITE='docs only' git push` is sh syntax (macOS, Git Bash). In cmd use
  `cmd /c "set "AEOS_PUSH_WITHOUT_FULL_SUITE=docs only" && git push"`: the child cmd ends with the push, whereas a
  bare `set` in the open window keeps the reason for every later push from that window. In PowerShell use
  `try { $env:AEOS_PUSH_WITHOUT_FULL_SUITE = 'docs only'; git push } finally { Remove-Item Env:AEOS_PUSH_WITHOUT_FULL_SUITE }`
  (an unset variable only makes the hook refuse). Records live in the
  common git directory, so every worktree of the clone shares them, and a tree is content-addressed: a merge tested in
  one worktree counts for the same tree pushed from another. A commit that changes content (a merge of `origin/main`
  that brings changes, an edit) has a new tree and needs a new full run; a message-only amend keeps the tree.

The hook also runs the `conflict-markers` gate on the pushed tip.

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
tail of the log. macOS arm64 is proven; Windows and Intel macOS are NOT-PROVEN. On Windows run the same script
with Git Bash; from cmd name its bash, `"%ProgramFiles%\Git\bin\bash.exe" scripts/run_tests.sh` (a `bash` found
first on `PATH` can be the WSL one); it picks `.venv\Scripts\python.exe`. A suite run with `python -m pytest`
directly writes no full-suite record, so the pre-push hook refuses its push. CI lints the shell scripts with `shellcheck scripts/*.sh scripts/git-hooks/pre-push scripts/git-hooks/post-merge`.

After a PASS the runner records the run for the pre-push hook (`scripts/full_suite_record.py`, the one owner of the
record format) when the run was FULL and the tree CLEAN; otherwise it prints one line
`run_tests: full-suite record: not recorded: <why>`. Recording never fails a run.

- **Full** means no pytest argument that can select or deselect tests: only `-q`, `-qq`, `-v`, `-vv`, `-x`,
  `--exitfirst`, `-n N`, `--numprocesses N`, `--durations N`, `--tb=...`, `-r...` and `--color=...` (and `--deadline`,
  which is the runner's own) are allowed. A path, `-k`, `-m`, `--lf`, `--deselect`, `-p` or anything else makes the run
  partial.
- **Clean** means no staged, unstaged or untracked file that git does not ignore, both before pytest starts and after
  it passes, and HEAD unchanged in between: then HEAD's tree is exactly what was tested.
- The record is one line `<HEAD tree sha> <HEAD sha> <UTC time> <load average>` appended to
  `<git common dir>/aeos-full-suite-runs` (`git rev-parse --git-common-dir`), shared by every worktree of the clone.

A delegated builder that cannot run the full suite says so; the delegating session runs it before the push
(`docs/agent/DELEGATION-BRIEF.md` rule 7).

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
  `scripts/git-hooks/pre-push`, `scripts/git-hooks/post-merge`) and `actionlint` over the workflows once for all host
  OSes, on pull requests and on
  `main`. The actionlint release is pinned by version and SHA-256, and a bite control lints a known-bad and a
  near-miss fixture on every run. The three required `backend [G1,G2] (<os>)` aggregators need the job, so a failing
  static check fails them all.
- **Commit subjects.** `commit subjects [G1]` runs only on pull requests (`ubuntu-latest`, full history) and
  checks the subjects and the no-attribution rule (message, author and committer e-mail, merge commits
  included) of the pull request's commits with `scripts/commit_subject.py`. It is the one job that differs between
  a pull request and a push to `main` (`tests/unit/test_ci_gates.py` lists it as the only job or step whose `if:`
  reads the `github` context: event name, event payload, ref, head or base ref). A new check with such a condition
  fails the test, so it cannot silently skip `main` that way; a condition that reaches the event by another route,
  for example through a `needs` output or an `env` value, is not caught and needs a human look.
- **Pull request title.** `.github/workflows/pr-title.yml` (job `pr title [G1]`, `ubuntu-latest`, read-only token) checks
  the title with `python scripts/commit_subject.py --subject "<title>"`: the title becomes the merge-commit body on
  `main` (the repository merges with a merge commit only), so it follows the same Conventional Commits and
  no-attribution rules.
  The title reaches the script through an environment variable, never through an expression inside `run:`. The check
  is a required check of the ruleset on `main` (see "Merging"). The workflow also runs on a push to `main`, where the job
  is skipped: a skipped run counts as OK and gives `ci_status.py --after-merge` the context it waits for.
- **Verifier.** `backend verifier [G2] (macos-latest)` runs the real trusted-suite verifier test.
- **Release evidence.** `.github/workflows/release.yml` (job `release evidence [NO-GATE]`, `ubuntu-latest`) runs only when
  a tag `vX.Y.Z` is pushed, never on pull requests or on `main`. It builds the evidence of that one commit and uploads it;
  it never creates a GitHub Release ([ADR-0122](docs/decisions/ADR-0122-release-provenance-in-ci.md), see "Releasing").
  It is the one job with write scopes, and only `id-token` and `attestations` for the provenance attestation
  (`tests/unit/test_delivery_ci_secrets.py` holds the allow-list).
- **Required names are aggregators.** The ruleset on `main` requires `backend [G1,G2] (<os>)` for each
  OS. These are small jobs on `ubuntu-latest` that wait for the static job and every backend job of their OS. They pass only
  when each of those jobs succeeded: a failed, skipped or cancelled job fails them. The other required
  checks are real jobs: `bootstrap [G2] (<os>)`, `frontend [G1,G3]`,
  `browser e2e + a11y smoke [G3]`, `commit subjects [G1]` and `pr title [G1]`. The supply-chain report, `supply-chain report [NO-GATE] (SBOM, licences, vulnerabilities, SAST)`, is `[NO-GATE]`. Keep these names when you
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
python scripts/ci_durations.py \
    --os windows-latest w1.xml w2.xml w3.xml \
    --os macos-latest a1.xml a2.xml \
    --os macos-26-intel i1.xml i2.xml --source "CI run <id>"
```

Commit the rewritten `tests/ci_durations.json`. The weights only move files between shards: no test
is added or dropped.

### Developer tools

Standalone helpers in `scripts/` (stdlib only, ASCII output, last line is the verdict):

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

- **`scripts/github_settings.py`** keeps the repository settings in the repository: `.github/repository-settings.json`
  (merge methods, merge commit title and message, delete branch on merge) and `.github/rulesets/*.json` (the ruleset
  on `main` and the release tag ruleset). `--check` reads the live settings with `gh api` (read-only), prints each
  difference by setting name or check context and ends with `GITHUB_SETTINGS: OK`, `DRIFT` or `ERROR` (exit 0, 1, 2).
  `--apply` prints the changes it would send and sends nothing without `--yes`; with `--yes` it updates the repository
  (`PATCH`), the existing rulesets (`PUT`, matched by name) and creates a missing one (`POST`), then checks again. It
  never deletes a ruleset and never runs in CI; `--repo owner/name` names another repository. GitHub can serve a stale
  read for a while after a write (2026-10-27: a `--check` seconds after `--apply --yes` read DRIFT, minutes later OK),
  so when a read shows DRIFT, `--check` and the check after `--apply --yes` wait `--settle SECONDS` (default 10, `0`
  disables) and read once more; the verdict is the second read, and the output says that it waited.
- **`scripts/action_pins.py`** is the monthly report of pinned versions, because a pin never moves by itself and
  ADR-0115 allows no automatic Dependabot pull requests. `python scripts/action_pins.py --report` lists every
  `uses: owner/repo@<40-hex sha> # vX` of `.github/workflows` and the pinned release binary of `ci.yml`
  (`ACTIONLINT_VERSION` with `ACTIONLINT_SHA256`), asks the GitHub CLI (`gh`) for the newest release published at
  least 14 days ago and the commit of its tag, and prints one row per pin: name, pinned tag, pinned sha (the SHA-256 for
  a binary), newest eligible tag, the full commit SHA of that tag (an action update only; also `newest_sha` in `--json`) and `CURRENT`, `UPDATE <tag>` or `UNKNOWN (reason)`. `UNKNOWN` covers a pin without a
  version comment, a tag that does not point at the pinned sha and a failed lookup. It always exits 0; the last line is
  `ACTION_PINS: <n> pins, <k> updates available`. `--json` prints JSON only (the last line then goes to stderr),
  `--today YYYY-MM-DD` replaces the date of the 14-day rule and `--workflows DIR` the workflow directory. **Run it
  monthly.** An update is a normal pull request: the new full SHA of the release tag (the report prints it, so nothing is resolved by hand) and the new version comment, and for a
  binary also the new SHA-256 from that release's checksums file (bump the version and the checksum together).
  `tests/unit/test_delivery_ci_secrets.py` fails when a pin has no version comment.

- **`scripts/paste_safe.py`** checks that shell commands meant for the owner survive a paste into zsh. Draft the
  commands into a file first and check it before you show them, in chat or in a document:

  ```bash
  python scripts/paste_safe.py --check docs/OWNER_ACTIONS.md
  python scripts/paste_safe.py --check "$TMPDIR/owner-steps.sh"
  python scripts/paste_safe.py --check --text './aeos doctor -g SECRET'
  ```

  A `.md` file (or `-` for standard input) is read as Markdown and every bash, sh, zsh, shell or console block is
  checked; a `.sh`, `.zsh`, `.bash` or `.txt` file, `--text` and `--shell` are plain shell text. It refuses an inline
  `#` comment, an unquoted `[ ] ? *` or `<placeholder>` (zsh reads `<` as a redirection), a line over 80 characters
  (a chat view or a narrow terminal wraps it and the paste breaks it: split it with a trailing backslash or after a
  pipe), a command that reads an answer from the terminal that is not the last line of its block (a password or
  key: any `aeos` command with `--as`, `admin create`, `secrets set`, `vault import-unseal-key`, `vault unseal`; a
  typed confirmation: `ops backup restore`, `ops emergency-stop`, `ops release accept`/`promote`/`rollback`,
  `secrets delete` and `vault install` without `--yes`, and `vault init`), two such commands in one block,
  and an `rm` after a `cp` outside `cp SRC DST && cmp SRC DST && rm SRC`. Each finding is
  `path:line: problem and fix: line`. Exit codes: 0 OK, 1 findings, 2 usage or read error; the last line is
  `PASTE_SAFE: OK` or `PASTE_SAFE: FAIL`. A block that shows file contents opts out with
  `<!-- paste-safe: file-content -->` directly above it. `tests/unit/test_owner_commands_paste_safe.py` runs it
  over every Markdown file of the repository and fails when a CLI command gains a prompt that the checker's lists
  lack. A trailing `\` continues a line only in zsh and bash: on Windows it becomes `^` in cmd.exe and a backtick
  in PowerShell (`docs/OWNER_ACTIONS.md` section 5).

- **`scripts/owner_dryrun.py`** dry-runs an owner script before the owner runs it (LESSONS L13). An owner script
  lives outside the repository (for example in `~/Documents/dly-aeos-owner-scripts`) and exposes
  `run(client, username, password, ask)`; a script without it is refused. Run it with the project interpreter from
  the checkout the server runs (it imports the repository's own packages, not only the stdlib):

  ```bash
  .venv/bin/python scripts/owner_dryrun.py \
    ~/Documents/dly-aeos-owner-scripts/example.py \
    --answer SEMUA --answer TIDAK
  ```

  It copies the live database with sqlite3's online backup from a read-only connection into a new temporary data
  dir (with a copy of `aeos.yaml`), reads which secret references exist from the real `aeos secrets status` (the
  PERSONAL view without `--include-office`: names and expiry, never a value) and gives the dry run an in-memory store
  that reports exactly those as present, with dummy values; `office/*` is not mirrored (NO_ACCESS, as for the
  controller). A real store that cannot be read (OpenBao sealed or stopped, or a PERSONAL reference UNKNOWN) is
  `OWNER_DRYRUN: FAIL` before anything is copied: start or unseal it and run again. The dry-run process has the null
  keyring, OpenBao at a closed loopback port, and refuses every TCP connect to a non-loopback address and to the
  loopback ports of the real OpenBao and Control Center. It creates a temporary SUPER_ADMIN in the copy (`--user
  NAME`, default `dryrun-admin`), runs the script against the real app through a TestClient with the `--answer` values
  in order (`ask`, `input()` and `getpass.getpass()` all take them, a getpass answer is not echoed; a prompt with no
  answer left fails the run with `no --answer left for prompt: ...`), and runs the Doctor without `--functional`
  before and after. While the Doctor runs, never while the script runs, the loopback port of the real Control Center
  is open to it, so `host.port` sees a running server as `aeos doctor` does (CONNECTED), not as a port it cannot bind.
  It prints the capability table (before -> after) and every check whose status changed. A capability that got worse
  (READY < DEGRADED < UNKNOWN < BLOCKED), or any newly failing gating check (critical, required or project tier, or a
  FAILED guard) whatever the status did (`BLOCKED -> BLOCKED`, or `BLOCKED -> DEGRADED` because another check
  recovered), ends with `OWNER_DRYRUN: DEGRADED` (exit 1); `--accept-degrade 'reason'` accepts it with a stated
  reason (exit 0, the last line says `OWNER_DRYRUN: DEGRADED-ACCEPTED`, never OK). Otherwise `OWNER_DRYRUN: OK` (exit
  0); a harness or script failure, or a copy that is not at this checkout's migration head, is `OWNER_DRYRUN: FAIL`
  (exit 2). The temporary dir is removed in every case. Show the capability diff together with the commands before
  they reach the owner (`docs/agent/DELEGATION-BRIEF.md`, rule 10). Another owner command that writes state is
  dry-run in-process against a temporary database, or wrapped in a tiny owner script and run through this harness.
  NOT-PROVEN: child processes of the Doctor probes (the same ones `aeos doctor` runs) are not network-isolated, and
  only the database and `aeos.yaml` of the data dir are copied.

The tools have bite tests (`tests/unit/test_worktree_tool.py`, `tests/unit/test_ci_status_tool.py`,
`tests/unit/test_github_settings.py`, `tests/unit/test_action_pins_tool.py`,
`tests/unit/test_owner_commands_paste_safe.py`, `tests/unit/test_owner_dryrun.py`): a violating fixture is red and a
near-miss legitimate one is green. The junction
test runs only on Windows.

#### Claude Code hooks

The committed `.claude/settings.json` registers two Claude Code hooks for anyone who works on this repository with
Claude Code (hooks reference: <https://code.claude.com/docs/en/hooks>). Both are fail-open: whatever goes wrong inside
a hook (a missing `python3`, bad input, an exception, a timeout), the session or the prompt goes on unchanged.

- **SessionStart** runs `scripts/claude_hooks/session_facts.py`, which runs `scripts/preflight.py --facts` with the
  interpreter that `scripts/run_tests.sh` would use (`$AEOS_GATES_PYTHON`, the `.venv` of this checkout, the `.venv`
  of the main checkout, else the launching `python3`), and puts the environment facts (interpreter, venv, pytest,
  xdist, node, git, `core.hooksPath`, `gh` login status) into the session context, so nobody guesses them. The hook
  itself starts with the system `python3`; running `preflight.py` directly under it would describe that interpreter
  and say the project venv and xdist are missing.
- **UserPromptSubmit** runs `scripts/claude_hooks/secret_prompt_guard.py` (stdlib only, about 30 ms). A prompt that
  carries a high-confidence secret shape (a provider token with a fixed prefix, a Vault/OpenBao token, an unseal key
  line, a PEM private key header; the patterns are the ones of `aeos.core.redaction`) is blocked before it reaches the
  model. The message, in Indonesian and English, names only the kind of secret, never the value, and says: do not paste
  secrets into chat, store them with `./aeos secrets set`, and rotate anything already sent (AGENTS.md invariant 12:
  a secret seen in chat is burned). A block keeps the secret from the model; Claude Code may still keep the typed text
  in its local prompt history, which is another reason to rotate. Ordinary prose, git SHAs, UUIDs, digests and
  placeholders such as `<your-token>` pass.

To switch them off for yourself, put `"disableAllHooks": true` in `.claude/settings.local.json` (not committed): it
turns off every hook of the session, not one of them. For a single run use
`claude --settings '{"disableAllHooks": true}'`. There is no per-hook switch: hook entries from user, project and local
settings merge, so a `.claude/settings.local.json` entry cannot replace or cancel the committed one, and `/hooks` is a
read-only browser that only lists them. Do not edit the committed file for a personal preference. A copy of the guard
in your user-level Claude Code settings, to protect your other repositories too, is optional: point the user-level
`UserPromptSubmit` hook at the absolute path of this script (it needs the `backend/` folder of this checkout next to
it, so it works only from a checkout).
`tests/unit/test_secret_prompt_guard.py` holds the bite tests (every secret kind blocked, also under a bare
`python3 -S -I` without any installed package; near misses allowed; bad input silent) and checks that the hook
commands in `.claude/settings.json` name scripts that exist.

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

### Merging

One written procedure, backed by `scripts/ci_status.py` (flags are described under "Developer tools" above). The decision
to merge stays the owner's (see "Rules for every change"); this is how it is carried out.

The only merge method is a merge commit: `gh pr merge <n> --merge`. Squash and rebase are switched off in the
repository and in the ruleset (ADR-0115, amendment 2026-10-27), because `ci_status.py --after-merge` and the release
tag check look for the merge commit by its SHA. The head branch is deleted on merge. The required checks are the eight
backend, bootstrap, frontend and browser checks plus `commit subjects [G1]` and `pr title [G1]`; a branch need not be
up to date with `main` to merge (step 2 and 3 cover the merged result).

1. **Merge only on a GREEN verdict for the exact head.** Run
   `python scripts/ci_status.py --pr <n> --expect-sha <head sha> --required-only --wait --deadline 25`. Merge only if
   the last line is `CI: GREEN <sha>` (exit 0). Read the `non-required failed` names in that line: the verdict does
   not block on them, so fix them first. A pull request whose head moved is SUPERSEDED or MISMATCH, never a merge.
2. **Check main after the merge.** Run
   `python scripts/ci_status.py --after-merge <n> --wait --deadline 25`. Runs on `main` are never cancelled, so the
   merge commit always gets its own verdict.
3. **While `main` is RED, merge nothing else.** The next merge waits until a fix pull request has made `main` GREEN
   again; otherwise two failures hide behind one red.
4. **Re-run only for INFRA.** For an [INFRA verdict](#developer-tools) (defined there) re-run that job
   (`gh run rerun <run id> --failed`). A RED verdict is a code or test failure: do not re-run until green
   (LESSONS L15).
5. **RED and `main` moved since the run: update the branch, do not re-run.** Merge `main` into the pull request branch
   and push; the new head gets a new run. GitHub's documentation says that a re-run "will also use the same
   `GITHUB_SHA` (commit SHA) and `GITHUB_REF` (git ref) of the original event that triggered the workflow run"
   ([Re-running workflows and jobs](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/re-running-workflows-and-jobs)),
   and for `pull_request` the documented `GITHUB_SHA` is the "last merge commit on the `GITHUB_REF` branch" and
   `GITHUB_REF` is `refs/pull/<n>/merge` ([Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)).
   So a re-run measures the old base again. NOT-PROVEN: that the checkout step of this workflow then really
   builds the old test merge commit was not run against the live service. When a RED pull request exists and the base
   branch got commits after the original workflow run was created, `ci_status.py --pr` prints
   `HINT: main moved since this run (approximate <old>..<new>); update the branch, do not re-run.` before the verdict.
   The window starts at the workflow run's `created_at`, which a re-run does not change (the start of the latest
   attempt is later and would miss a base commit that landed between the first run and the re-run); only when the run
   object is unavailable the earliest check run start is used, and the line says so. The range is approximate: the
   workflow run object's `pull_requests[]` was empty for runs of closed pull requests, and for an open pull request it
   is unmeasured here (NOT-PROVEN), so `base.sha` is not used and the time heuristic stands in; a base commit that
   landed between the trigger and the run creation is not seen.
6. **One concern per pull request, merged the same day it is green.** Every open pull request costs a full run. At
   the time of writing one run starts about 16 jobs, 7 of them on macOS, and GitHub Free allows 20 concurrent jobs
   and 5 concurrent macOS jobs ([Actions limits](https://docs.github.com/en/actions/reference/limits)), so a
   second open run queues behind the first. Recount against `ci.yml` when the layout changes.
7. **Settings change through the files.** The merge policy lives in `.github/repository-settings.json` and
   `.github/rulesets/*.json`, reviewed like code. After a pull request that changes them is merged, run
   `python scripts/github_settings.py --apply --yes` and then `python scripts/github_settings.py --check`; the last
   line must be `GITHUB_SETTINGS: OK`. A required check added to the ruleset must exist in a workflow
   (`tests/unit/test_ci_gates.py` keeps `REQUIRED_CHECKS` equal to the ruleset file), or every merge waits for ever.

### Releasing

1. Set the new version in `pyproject.toml`, `frontend/package.json` (and the root entry of
   `frontend/package-lock.json`) and `CITATION.cff`, together with `date-released`.
2. Run `python scripts/changelog.py --assemble` (add `--dry-run` first to see it) to move the fragments
   into `[Unreleased]` of `CHANGELOG.md`. Then move the `[Unreleased]` entries under a new version heading,
   and add its compare/tag link.
3. Write the release notes in `docs/releases/vX.Y.Z.md`.
4. After the release pull request is merged (see "Merging"), tag the merge commit `vX.Y.Z`, run
   `git fetch origin` and then `python scripts/release_check.py --tag vX.Y.Z`. It reads the tagged commit and
   checks that the tag equals the version in every version source (`pyproject.toml`, `frontend/package.json`,
   the root entry of `frontend/package-lock.json`, `CITATION.cff`, `backend/aeos/__init__.py`), that `CHANGELOG.md`
   has `## [X.Y.Z]`, that `changelog.d/` holds only its README, that `docs/releases/vX.Y.Z.md` exists and that the
   tagged commit is an ancestor of `origin/main`. Its last line is `RELEASE_CHECK: PASS` or `RELEASE_CHECK: FAIL`;
   fix every FAIL line (delete the tag first if the commit is wrong) before step 5.
5. Confirm the tagged commit is already GREEN on `main` (the release run waits only 15 minutes, and a `main` run takes
   about 15 to 25):
   `python scripts/ci_status.py --sha "$(git rev-list -n 1 vX.Y.Z)" --required-only --wait --deadline 30`, then
   push the tag: `git push origin vX.Y.Z`. Release tags are protected by the v* tag ruleset. The push starts
   `release evidence [NO-GATE]` ([.github/workflows/release.yml](.github/workflows/release.yml),
   [ADR-0122](docs/decisions/ADR-0122-release-provenance-in-ci.md)). It runs `release_check.py --tag` again on the tagged
   commit, then requires CI to be GREEN for that commit (`scripts/ci_status.py --required-only --wait`; PENDING, RED,
   INFRA or TIMEOUT stop it and there is no override). Only then it builds `dist/dly-aeos-src-X.Y.Z.zip` (`git archive`,
   prefix `dly-aeos-X.Y.Z/`), `dist/sbom.cdx.json` (the supply-chain job's SBOM command), `dist/SHA256SUMS` and a
   build-provenance attestation of each file, and uploads them as the workflow artifact `release-evidence-vX.Y.Z`
   (kept 90 days). CI never creates or publishes a GitHub Release.
6. Wait for that run (the tagged commit is `git rev-list -n 1 vX.Y.Z`) with
   `python scripts/ci_status.py --sha <tagged commit> --required-only --require "release evidence [NO-GATE]" --wait --deadline 25`
   (last line `CI: GREEN <sha>`), then download and verify the evidence:
   `gh run download --name release-evidence-vX.Y.Z --dir dist` and, for each file,
   `gh attestation verify dist/dly-aeos-src-X.Y.Z.zip --repo fadhlillah-dli/dly-aeos`.
7. `aeos ops release` stays the release authority ([docs/OPERATIONS.md](docs/OPERATIONS.md), "Controller releases").
   After `aeos ops release promote`, the owner (or the delegating session on the owner's explicit instruction) creates
   the GitHub Release as a draft from the verified files:
   `gh release create vX.Y.Z --draft --notes-file docs/releases/vX.Y.Z.md dist/dly-aeos-src-X.Y.Z.zip dist/sbom.cdx.json dist/SHA256SUMS`,
   and publishes it after reading it.
