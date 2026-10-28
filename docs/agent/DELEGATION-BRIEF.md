# DELEGATION-BRIEF - the standard block for every delegated agent prompt

Sub-agents do not read the session memory or [LESSONS.md](LESSONS.md). Whatever they must know, the
prompt must say. Paste the block below at the top of every delegated prompt and fill the angle
brackets. Each rule names the lesson id of its incident. `tests/unit/test_lessons_register.py` checks
that every lesson with `brief` = yes appears here.

## The block

```text
REPOSITORY: <name>. Work ONLY in the worktree <absolute path>. First run
'git -C <path> branch --show-current'; it must print <branch>. If HEAD is detached or the branch
differs, STOP and report. Read AGENTS.md and CONTRIBUTING.md first.

RULES (lesson ids in brackets)
1. Identity and attribution [L9]. Commits, pushes, PRs and comments use the owner's identity for
   the code host, set locally per repository.
   IDENTITY: <name> <email> (set with git config user.name and user.email in this repo, then check
   with git config user.email before the first commit). No AI name, co-author trailer, "generated" footer,
   robot emoji or AI address anywhere. Never write literal attribution samples into a file; build
   them from parts.
2. Branch and HEAD [L6]. After committing, check that 'git branch --show-current' is the branch
   and that 'git rev-parse <branch>' equals 'git rev-parse HEAD'. Never push from a detached HEAD.
3. No links in a worktree [L4]. Never create a symlink or junction in a worktree (a .venv link once
   made worktree removal delete a real venv). Call the interpreter of the main checkout by path.
4. Reuse canonical modules [L10]. Search for an existing module first (for example
   aeos.core.attribution for attribution rules, scripts/changelog.py for the changelog fragment format).
   Do not re-implement it.
5. Owner decisions [L11]. The owner decision is quoted verbatim below with its exact scope.
   Do not widen it. A wider idea is a new question for the owner, not part of this change.
   OWNER DECISION: "<verbatim quote>"  SCOPE: <exact scope>
6. Domains [L12]. A change for one domain (PERSONAL or OFFICE) must not alter the other domain's
   view. Add a test that shows it.
7. Full gates before "done" [L8]. "Done" means scripts/gates.py (or the commands in CONTRIBUTING.md) and the FULL
   suite: scripts/run_tests.sh with no selecting argument, on a clean tree (frontend tests when the change touches the
   frontend). Targeted tests alone are not "done". A builder that cannot run the full suite says so in its report;
   the delegating session then runs it before the push, and the pre-push hook refuses a commit whose tree has no
   passing full-suite record (AEOS_PUSH_WITHOUT_FULL_SUITE='<reason>' only with a stated reason; it is SKIPPED, never PASS).
8. Claims carry evidence [L3]. Every security or readiness statement names its E-level, the host it
   ran on and its NOT-PROVEN part. Anything not run on Windows is NOT-PROVEN there.
9. Secrets [L1]. Never print, paste, log or commit a secret. If one appears, stop and report it
   without repeating its value.
10. Owner-facing commands [L2, L13]. Run every command meant for the owner in zsh first. Every owner
    script is dry-run with scripts/owner_dryrun.py and its capability diff is shown before the commands
    are given to the owner; OWNER_DRYRUN: DEGRADED stops it unless the reason is given with --accept-degrade.
    Commands for the owner are drafted into a file and checked with scripts/paste_safe.py --check
    before they are shown; one prompting command (a password or a typed confirmation) per block, last
    (no inline '#' comment, every '[]', '*' and '<placeholder>' quoted, no line over 80 characters).
11. Diagnosis order [L14]. Input, then environment, then code. No background job without a
    deadline and a failure marker file; check the marker, not the silence. Run tests through
    scripts/run_tests.sh; zsh does not word-split a variable, so use an array, never 'set -- $var'.
12. CI state [L7, L18]. Never hand-write a CI wait loop: use scripts/ci_status.py --wait --deadline, and
    --required-only for a merge decision. A check list that is empty or pending is not green; name each
    required check. A job cancelled because no runner acquired it is INFRA: re-run it, do not debug code.
    Merging (CONTRIBUTING.md "Merging"): after a merge check main with --after-merge; while main is RED merge nothing
    else; a RED PR whose base moved since its run gets main merged in, not a re-run (a re-run reuses the old GITHUB_SHA).
13. Changelog [L5]. Merge main into the branch and resolve the changelog before push. Use a
    changelog fragment where the repository has them.
14. Flaky tests [L15]. A test that passes on re-run is a defect. Report it and fix the timing
    assumption; do not re-run until green.
15. Single owner [L17]. This prompt names the one owner (module, script and branch) of every tool the
    task touches: OWNERS: <tool -> owner>. Extend the owner's tool; never build a second checker beside
    it. A tool with no named owner is a question to the delegating session, not a license to build.
16. Machine load [L19]. One full suite at a time with -n logical on the owner's Mac; a second concurrent
    suite only with -n at most half the logical CPUs. Record the load average with any timing.
17. Hermetic secrets [L20]. Tests never reach the owner's real secret stores: the SecretGuard in
    tests/conftest.py hides the OS keyring (also from sandboxed child processes), a real OpenBao and the
    owner's real Claude Code login from every test; a test that needs a secret installs a fake
    (secrets_memory, its own keyring, a vault on a free port, a fake claude); only the real_claude_cli
    fixture with the owner's DLY_AEOS_REAL_CLAUDE=1 runs the real Claude Code. Never query the real vault,
    OpenBao, Keychain or keyring to check anything; all proof is by tests with fakes.
18. Dependent steps [L22]. Chain a step whose failure must stop the next one with && or a checked if, never ';' or
    a separate line, and run chains with 'set -o pipefail' (a gate piped into tail or grep hides its exit code). Check
    each result before a commit, merge or push; the pre-push hook refuses static-gate failures and conflict markers.

REPORT (at most 15 lines): commit sha, files changed, gate results, bite-test results
(violating fixture red, near-miss fixture green), NOT-PROVEN items.
```

## Using it

* Fill every angle bracket. An unfilled one means the delegation is not ready.
* A new lesson with `brief` = yes adds its rule to the block in the same pull request.
* The delegating session verifies the report: branch and HEAD, gates actually run, and that no
  attribution line exists in the commit.
