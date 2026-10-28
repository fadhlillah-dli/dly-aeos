# LESSONS - mistakes that became rules, and rules that became tools

Purpose: a mistake is paid for once. This file turns it into a rule that names its incident, counts
repeats, and at a threshold turns the rule into a tool that refuses the mistake. A rule without its
incident gets optimised away, so every row names its incident. Read this file before substantial work
(`CLAUDE.md`, step zero). Sub-agents do not read session memory: lessons reach them only through the
brief ([DELEGATION-BRIEF.md](DELEGATION-BRIEF.md)).

## The loop

observe -> record with incident -> count strikes -> threshold -> tool -> bite test -> recall -> prune.

1. **Observe.** A mistake costs time, trust or exposure. Fix the instance first.
2. **Record.** Add a row to table 1 in the same pull request: date, a one-line generic incident, class. The class is
   one of data-loss, secret, public-exposure, process, environment, correctness, quality (the first three are strict).
3. **Count strikes.** The same class of mistake again: raise `strikes` in the same pull request that
   fixes it. Never open a second row for a repeat.
4. **Threshold.** Strike 1: fix it. Strike 2: write the rule here. Strike 3: build a tool. For the
   classes `data-loss`, `secret` and `public-exposure` the tool is due at strike 1.
5. **Tool.** The `tool` column names a repo path, `OWNER-SETTING:<what>` (a setting only the owner can
   change, outside the repo) or `PENDING:<branch>` (built on a branch that is not merged yet). `-` means no tool yet.
6. **Bite test.** Every tool ships a violating fixture that is red and a near-miss legitimate fixture
   that is green, built in a temporary directory. A red fixture must assert its own finding text (the specific
   message), not only a non-zero exit: it must be "red for the right reason".
7. **Recall.** Rows with `brief` = yes are copied into every delegated prompt by id
   (DELEGATION-BRIEF.md). `tests/unit/test_lessons_register.py` keeps both files consistent.
8. **Prune.** A rule that obstructs more than it protects is removed or loosened. Record the date and
   the reason under "Pruned" below; do not delete silently. The `stops applying when` column says in
   advance what makes a rule obsolete.

`level` is the strongest control that exists today: written-rule, memory, process, test, ci-gate,
git-hook, harness-hook or tool-refusal. A `PENDING:` tool raises it once merged.
Evidence: rows describe incidents of 2026-10-26 to 2026-10-28; E1 = static reading, E2 = unit run on macOS arm64.
Nothing here is proven on Windows or macOS Intel unless a CI run says so (NOT-PROVEN).

## Table 1 - Rules with incidents

| id | date | incident | class | strikes | level | tool | brief | stops applying when |
|---|---|---|---|---|---|---|---|---|
| L1 | 2026-10-26 | Owner pasted a vault unseal key into the chat twice | secret | 2 | test | backend/aeos/core/redaction.py + tests/unit/test_redaction_vault_shares.py + OWNER-SETTING:UserPromptSubmit secret blocker | yes | Unseal keys are handled only through a file or hardware token, not typed or pasted |
| L2 | 2026-10-26 | Owner commands with inline '#' comments were rejected by zsh; an unquoted '.[dev]' glob failed; on 2026-10-27 a long command with a quoted path was wrapped by the chat view and the paste put a newline inside the quotes (zsh 'dquote>', twice), and one pasted block held several commands that prompt for a hidden password ('--as <admin>'), so the lines after the first prompt were read as the password | process | 4 | tool-refusal | scripts/paste_safe.py + tests/unit/test_owner_commands_paste_safe.py | yes | Owner commands reach the owner only through a tool that runs the checker before they are shown |
| L3 | 2026-10-26 | A security claim was stated more strongly than proven and corrected later | process | 1 | written-rule | - | yes | Claims are produced from evidence records that carry level, host and NOT-PROVEN part |
| L4 | 2026-10-26 | A .venv junction inside a git worktree made worktree removal delete half a real venv | data-loss | 1 | tool-refusal | scripts/worktree.py | yes | Worktrees are created and removed only by a tool that refuses links |
| L5 | 2026-10-26 | CHANGELOG.md conflicted on almost every merge, each forcing a CI re-run of about 45 minutes | process | 3 | ci-gate | scripts/changelog.py | yes | Changelog entries live in per-change fragment files |
| L6 | 2026-10-26 | A delegated agent left a worktree on a detached HEAD and a push and PR went out on an old commit | public-exposure | 1 | git-hook | scripts/git-hooks/pre-push | yes | A pre-push hook refuses a detached HEAD or a branch ref that differs from HEAD |
| L7 | 2026-10-26 | An ad-hoc CI watcher read an empty or pending check rollup as green; on 2026-10-27 two more hand-written loops failed (a name filter matched 8 of 9 checks so it never finished; another kept polling after it lost its git context) | process | 3 | tool-refusal | scripts/ci_status.py | yes | One status tool is the only way agents read CI state |
| L8 | 2026-10-26 | A delegated agent ran only unit tests; an API-shape versus UI-fixture failure surfaced only on CI after 45 minutes; on 2026-10-27 a second one ran only the unit suite and 33 acceptance tests broke; later that day the delegating session pushed a branch tested only with targeted tests (its brief told the builder not to run the full suite) and CI went red on tests/unit/test_dev_safety_hooks.py | process | 3 | tool-refusal | scripts/run_tests.sh + scripts/full_suite_record.py + scripts/gates.py | yes | CI runs the full suite on every commit before anyone relies on the push, or the full suite runs in a minute |
| L9 | 2026-10-26 | A test file committed literal AI-attribution samples and tripped the repository hygiene scan | public-exposure | 1 | test | tests/unit/test_repository_hygiene.py | yes | Samples come from one shared builder that assembles them from parts |
| L10 | 2026-10-26 | An agent re-implemented attribution regexes instead of reusing aeos.core.attribution; on 2026-10-27 two parallel branches each implemented the changelog fragment format | process | 2 | test | tests/unit/test_repository_hygiene.py | yes | Only one module defines attribution patterns and a test fails on a second copy |
| L11 | 2026-10-26 | An agent widened the scope of an owner decision beyond what the owner said | process | 1 | written-rule | - | yes | Decisions are recorded with an exact scope that tools compare against the change |
| L12 | 2026-10-26 | An inventory change leaked one domain's decision into the other domain's view | process | 1 | written-rule | - | yes | A differential domain test fails on any cross-domain leak |
| L13 | 2026-10-26 | Owner-run scripts failed twice on validation rules that could have been checked first | process | 2 | written-rule | - | yes | A dry-run harness runs every owner script against a temporary database automatically |
| L14 | 2026-10-26 | Background test runs failed on a missing dependency and on zsh not word-splitting a variable, then again in a 'set -- $var' loop, and a third time on 2026-10-27 in the session itself | process | 3 | tool-refusal | scripts/run_tests.sh | yes | One test-runner script owns environment, deadline and failure marker |
| L15 | 2026-10-26 | Verifier runs were QUARANTINED at random under load and were re-run, not fixed; the cause was not timing: psutil raised SystemError reading a dying unrelated process and it escaped the process sweep; on 2026-10-27 a memory-cap test that flaked under load was first 'fixed' as a timeout race, and its own diagnostics then showed macOS compressing the hog's zero-filled memory so the cap never fired | correctness | 5 | test | backend/aeos/verification/procgroup.py | yes | The sweep or psutil changes; re-run the churn tests in tests/unit/test_verification_procs.py |
| L16 | 2026-10-26 | The session scratchpad that held owner scripts can vanish | process | 1 | written-rule | - | no | Owner scripts are generated on demand from the repository |
| L17 | 2026-10-27 | Parallel branches built overlapping checkers; the brief must name the single owner of each tool | process | 1 | written-rule | docs/agent/DELEGATION-BRIEF.md | yes | A test fails on a second implementation of a canonical check |
| L18 | 2026-10-27 | GitHub hosted macOS arm64 capacity cancelled a queued CI job (not acquired by a runner); it is infrastructure, re-run, never read as a code failure | environment | 1 | tool-refusal | scripts/ci_status.py | yes | Required jobs run on runners with reserved capacity |
| L19 | 2026-10-27 | Too many full test suites ran in parallel (6 x pytest -n 4 on a 10-core Mac, load average about 45): agents stalled for 50+ minutes, timing-sensitive tests flaked and before/after speed measurements were invalid; later the same day two full suites with -n logical at once (load average about 25) pushed a memory-cap test past its wall-time bound | environment | 2 | process | - | yes | A test runner limits concurrent full-suite runs and records the load average itself |
| L20 | 2026-10-27 | A test resolved the owner's real GitHub token through the real OpenBao (AppRole login from the macOS Keychain) and called the live GitHub API; CI has no vault, so it stayed green | secret | 1 | test | tests/conftest.py + tests/unit/test_test_isolation.py | yes | Tests run only in a sandbox that has no route to a real keyring or vault |
| L21 | 2026-10-27 | A runbook told the owner to copy the only offline recovery copy of the vault unseal key to a USB volume and then delete it on a separate line, so the delete ran even when the copy failed (caught in review) | data-loss | 1 | test | scripts/paste_safe.py + tests/unit/test_owner_commands_paste_safe.py | no | Owner documents move files only through a command that verifies the copy itself |
| L22 | 2026-10-27 | Twice in one day a step whose failure had to stop the next one was chained with ';': a scripted conflict resolution failed and the commit still ran and committed conflict markers; a scripted rename failed and a merge still ran; a third time that day a gate piped into 'tail' (mypy piped into 'tail -1') hid its exit code and a commit with a type error ran (caught before the push) | process | 3 | git-hook | scripts/gates.py | yes | Agents run multi-step scripts only through a runner that stops at the first failing step |
| L23 | 2026-10-28 | After git pull the server kept serving a Control Center bundle built two days earlier (five frontend commits missing); the Doctor only checked that frontend/dist/index.html existed and stayed VERIFIED | correctness | 1 | test | backend/aeos/operations/freshness.py + tests/unit/test_ui_freshness.py | no | Pulling and serving go through one command that rebuilds and restarts by itself |

## Rules (one line each; the incident is in table 1)

* L1 A secret is never pasted into chat, a file in the repo, a command line or a log. Rotate on exposure.
* L2 Commands for the owner are drafted into a file and checked with `scripts/paste_safe.py --check` before they are shown; one prompting command (a password or a typed confirmation) per block, last. The checker (and the docs test) refuses an inline `#` comment, an unquoted `[]`, `?`, `*` or `<placeholder>`, a line over 80 characters and `rm` after an unproven `cp`.
* L3 A security or readiness claim carries its E-level, the host it ran on and its NOT-PROVEN part.
* L4 A worktree never contains a symlink or junction. Remove worktrees only with a tool that refuses links.
* L5 Merge main into the branch and resolve the changelog before push, not after CI.
* L6 After committing, `git branch --show-current` is the branch and the branch ref equals HEAD.
* L7 CI is green only when the check list is non-empty and every required check succeeded. Never hand-write a CI wait loop: use `scripts/ci_status.py --wait --deadline` (`--required-only` for a merge decision).
* L8 "Done" means the full gate run and the full suite (`scripts/run_tests.sh` with no selecting argument, on a clean tree; frontend tests when touched). Targeted tests alone are not done. The pre-push hook refuses a commit whose tree has no passing full-suite record unless `AEOS_PUSH_WITHOUT_FULL_SUITE='<reason>'` is set (SKIPPED, never PASS).
* L9 Never commit literal attribution samples: build them from parts.
* L10 Reuse the canonical module or script instead of re-implementing it (the changelog fragment format lives only in scripts/changelog.py).
* L11 Quote the owner decision verbatim with its exact scope in the brief and the PR. Widening is a new question.
* L12 A change for one domain is proven not to alter the other domain's view.
* L13 Dry-run every owner command or script in-process against a temporary database and paste the result first.
* L14 Diagnose input, then environment, then code. No background job without a deadline and a failure marker. zsh does not word-split a variable: use an array.
* L15 A flaky test is a defect: record it and fix its timing assumption. A re-run is not a fix.
* L16 Owner scripts live in a persistent private folder outside the repository.
* L17 Every delegated brief names the single owner of each tool it touches; a second checker is not built.
* L18 A job cancelled because no runner acquired it is infrastructure: re-run it (`ci_status.py` says INFRA), do not debug code.
* L19 One full suite at a time with -n logical on the owner's Mac; a second concurrent suite only with -n at most half the logical CPUs. Record the load average with any timing.
* L20 Tests never reach the owner's real secret stores: the SecretGuard in tests/conftest.py hides the OS keyring (also from sandboxed child processes), a real OpenBao and the owner's real Claude Code login from every test; a test that needs a secret installs a fake (secrets_memory, its own keyring, a vault on a free port, a fake claude), and only the real_claude_cli fixture with the owner's DLY_AEOS_REAL_CLAUDE=1 runs the real Claude Code. Never query the real vault or keyring to check anything.
* L21 An owner command that copies and then deletes runs the delete only in one chain after a compare: `cp SRC DST && cmp SRC DST && rm SRC`, paths quoted; never a separate `rm` line.
* L22 Chain a step whose failure must stop the next one with `&&` or a checked `if`, never `;` or a separate line, and run chains with `set -o pipefail` (a gate piped into `tail` or `grep` otherwise reports the pipe's status, not its own); check the result before a commit, merge or push. Tool: the pre-push hook (scripts/gates.py --hook) runs the static gates and the conflict-markers gate on the pushed tree, so a commit that slipped through never reaches the remote.
* L23 After a `git pull` that changed `frontend/` or `backend/`, restart `aeos serve`; it rebuilds a stale UI bundle once when that is safe. The Doctor (`runtime.frontend_dist`, `runtime.code_drift`) and `/api/health` `ui_build` report what is still stale.

## Table 2 - Proven methods

| method | re-runnable proof | failure it prevents | retire when |
|---|---|---|---|
| Blind verifier: give a second reader the claim, not the evidence | Re-run the verifier on the same claim and compare verdicts | A claim confirmed only because the reader saw the author's reasoning | A tool derives the verdict from the evidence record |
| Adversarial review, then delta-only re-review | Diff of the fixes against the findings list | Endless full re-reviews, or fixes nobody re-checked | Findings are tracked by a tool that re-checks each fix |
| In-process dry-run of owner scripts against a temporary database | Run the script with a temp data dir and compare the printed result | Owner-run scripts failing on validation that was checkable up front | The harness dry-runs every owner script automatically |
| Merge main into a branch and resolve the changelog before push | The merge commit exists and the branch has no conflict with main | A conflict found only after a long CI run | Changelog fragments make the conflict impossible |
| Violating fixture red plus near-miss fixture green for every gate | The bite tests of the gate in a temporary directory | A gate that never fails, or one that rejects legitimate input | The gate is replaced by a stronger check with its own bite test |

## Pruned

None yet. Format: `Lnn - date - reason - who decided`.
