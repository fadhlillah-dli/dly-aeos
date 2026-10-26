# LESSONS - mistakes that became rules, and rules that became tools

Purpose: a mistake is paid for once. This file turns it into a rule that names its incident, counts
repeats, and at a threshold turns the rule into a tool that refuses the mistake. A rule without its
incident gets optimised away, so every row names its incident. Read this file before substantial work
(`CLAUDE.md`, step zero). Sub-agents do not read session memory: lessons reach them only through the
brief ([DELEGATION-BRIEF.md](DELEGATION-BRIEF.md)).

## The loop

observe -> record with incident -> count strikes -> threshold -> tool -> bite test -> recall -> prune.

1. **Observe.** A mistake costs time, trust or exposure. Fix the instance first.
2. **Record.** Add a row to table 1 in the same pull request: date, a one-line generic incident, class.
3. **Count strikes.** The same class of mistake again: raise `strikes` in the same pull request that
   fixes it. Never open a second row for a repeat.
4. **Threshold.** Strike 1: fix it. Strike 2: write the rule here. Strike 3: build a tool. For the
   classes `data-loss`, `secret` and `public-exposure` the tool is due at strike 1.
5. **Tool.** The `tool` column names a repo path, `OWNER-SETTING:<what>` (a setting only the owner can
   change, outside the repo) or `PENDING:<branch>` (built, not yet merged). `-` means no tool yet.
6. **Bite test.** Every tool ships a violating fixture that is red and a near-miss legitimate fixture
   that is green, built in a temporary directory.
7. **Recall.** Rows with `brief` = yes are copied into every delegated prompt by id
   (DELEGATION-BRIEF.md). `tests/unit/test_lessons_register.py` keeps both files consistent.
8. **Prune.** A rule that obstructs more than it protects is removed or loosened. Record the date and
   the reason under "Pruned" below; do not delete silently. The `stops applying when` column says in
   advance what makes a rule obsolete.

`level` is the strongest control that exists today: written-rule, memory, process, test, ci-gate,
git-hook, harness-hook or tool-refusal. A `PENDING:` tool raises it once merged.
Evidence: rows describe incidents of 2026-10-26; E1 = static reading, E2 = unit run on macOS arm64.
Nothing here is proven on Windows or macOS Intel unless a CI run says so (NOT-PROVEN).

## Table 1 - Rules with incidents

| id | date | incident | class | strikes | level | tool | brief | stops applying when |
|---|---|---|---|---|---|---|---|---|
| L1 | 2026-10-26 | Owner pasted a vault unseal key into the chat twice | secret | 2 | written-rule | PENDING:fix/paste-safe-owner-commands detection + OWNER-SETTING:UserPromptSubmit secret blocker | yes | Unseal keys are handled only through a file or hardware token, not typed or pasted |
| L2 | 2026-10-26 | Owner commands with inline '#' comments were rejected by zsh; an unquoted '.[dev]' glob failed | process | 2 | written-rule | PENDING:fix/paste-safe-owner-commands | yes | A checker rejects non-paste-safe owner commands before they are shown |
| L3 | 2026-10-26 | A security claim was stated more strongly than proven and corrected later | process | 1 | written-rule | - | yes | Claims are produced from evidence records that carry level, host and NOT-PROVEN part |
| L4 | 2026-10-26 | A .venv junction inside a git worktree made worktree removal delete half a real venv | data-loss | 1 | written-rule | PENDING:feat/dev-ci-worktree-tools scripts/worktree.py | yes | Worktrees are created and removed only by a tool that refuses links |
| L5 | 2026-10-26 | CHANGELOG.md conflicted on almost every merge, each forcing a CI re-run of about 45 minutes | process | 3 | written-rule | PENDING:feat/changelog-fragments | yes | Changelog entries live in per-change fragment files |
| L6 | 2026-10-26 | A delegated agent left a worktree on a detached HEAD and a push and PR went out on an old commit | public-exposure | 1 | written-rule | PENDING:feat/dev-safety-hooks | yes | A pre-push hook refuses a detached HEAD or a branch ref that differs from HEAD |
| L7 | 2026-10-26 | An ad-hoc CI watcher read an empty or pending check rollup as green | process | 1 | written-rule | PENDING:feat/dev-ci-worktree-tools scripts/ci_status.py | yes | One status tool is the only way agents read CI state |
| L8 | 2026-10-26 | A delegated agent ran only unit tests; an API-shape versus UI-fixture failure surfaced only on CI after 45 minutes | process | 1 | written-rule | PENDING:feat/dev-gates scripts/gates.py | yes | The gates tool runs the full set and a hook refuses a push without its result |
| L9 | 2026-10-26 | A test file committed literal AI-attribution samples and tripped the repository hygiene scan | public-exposure | 1 | test | PENDING:refactor/single-attribution | yes | Samples come from one shared builder that assembles them from parts |
| L10 | 2026-10-26 | An agent re-implemented attribution regexes instead of reusing aeos.core.attribution | process | 1 | written-rule | PENDING:refactor/single-attribution | yes | Only one module defines attribution patterns and a test fails on a second copy |
| L11 | 2026-10-26 | An agent widened the scope of an owner decision beyond what the owner said | process | 1 | written-rule | - | yes | Decisions are recorded with an exact scope that tools compare against the change |
| L12 | 2026-10-26 | An inventory change leaked one domain's decision into the other domain's view | process | 1 | written-rule | - | yes | A differential domain test fails on any cross-domain leak |
| L13 | 2026-10-26 | Owner-run scripts failed twice on validation rules that could have been checked first | process | 2 | written-rule | - | yes | A dry-run harness runs every owner script against a temporary database automatically |
| L14 | 2026-10-26 | Background test runs failed on a missing dependency and on zsh not word-splitting a variable, then again in a 'set -- $var' loop | process | 3 | written-rule | PENDING:feat/dev-safety-hooks scripts/run_tests.sh | yes | One test-runner script owns environment, deadline and failure marker |
| L15 | 2026-10-26 | A flaky timing-sensitive verification test was re-run instead of fixed | process | 1 | written-rule | - | yes | The test no longer depends on wall-clock timing |
| L16 | 2026-10-26 | The session scratchpad that held owner scripts can vanish | process | 1 | written-rule | - | no | Owner scripts are generated on demand from the repository |

## Rules (one line each; the incident is in table 1)

* L1 A secret is never pasted into chat, a file in the repo, a command line or a log. Rotate on exposure.
* L2 Owner commands are paste-safe in zsh: no inline `#` comments, quote every `[]` and `*`.
* L3 A security or readiness claim carries its E-level, the host it ran on and its NOT-PROVEN part.
* L4 A worktree never contains a symlink or junction. Remove worktrees only with a tool that refuses links.
* L5 Merge main into the branch and resolve the changelog before push, not after CI.
* L6 After committing, `git branch --show-current` is the branch and the branch ref equals HEAD.
* L7 CI is green only when the check list is non-empty and every required check succeeded.
* L8 "Done" means the full gate run, plus integration and frontend tests when touched.
* L9 Never commit literal attribution samples: build them from parts.
* L10 Reuse the canonical module instead of re-implementing it.
* L11 Quote the owner decision verbatim with its exact scope in the brief and the PR. Widening is a new question.
* L12 A change for one domain is proven not to alter the other domain's view.
* L13 Dry-run every owner command or script in-process against a temporary database and paste the result first.
* L14 Diagnose input, then environment, then code. No background job without a deadline and a failure marker.
* L15 A flaky test is a defect: record it and fix its timing assumption. A re-run is not a fix.
* L16 Owner scripts live in a persistent private folder outside the repository.

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
