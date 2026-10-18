# ADR-0060 — Independent verifier runs the controller's trusted suite against an exact head snapshot

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed build (ADR-0003)

## Context

Master §09/§19/§20 require a verifier outside the candidate trust context: the implementer must not
be able to lower the grader, and "worker exit 0 / all tests pass" must not count (§50). The platform
builds itself, so the candidate contains its *own* copy of `tests/acceptance` and of the verifier.
On a single developer machine there is not yet a proven sandbox (worker_runtime containment is a
separate, NOT-PROVEN capability).

## Decision

1. The verifier (`aeos.verification.verifier`) verifies a WorkerRun in REVIEW whose candidate worktree
   lives in the project's managed `task-data` directory and does not overlap the controller checkout.
2. It materialises **exactly the head commit's tree** with git plumbing (`ls-tree` + `cat-file
   --batch`): no hooks, no checkout filters, no `.gitattributes`, no textconv, no fsmonitor. Uncommitted
   worker edits and export-ignore tricks cannot change what is tested. Symlinks/submodules are not
   materialised and are reported as NOT-PROVEN items.
3. The **trusted acceptance suite** is copied from the trusted controller checkout
   (`repo_root()/tests/acceptance` + `tests/__init__.py` + `tests/conftest.py`), hashed (suite hash),
   and run in a child process with `PYTHONPATH=<snapshot>/backend`, a fresh temp cwd, a verifier-owned
   pytest ini (`-c`, `--rootdir`, `--confcutdir`, plugin autoload disabled), a scrubbed allow-list
   environment (no provider keys, `HOME`/temp redirected, keyring disabled, dead proxy as egress
   deterrent), a timeout and process-tree kill (psutil).
4. Expected test ids are first collected with the **trusted controller code**; a candidate run that
   drops, deselects or errors trusted tests FAILs; a skipped trusted test makes the result NOT_PROVEN.
   Suite files are re-hashed after the run (tamper → FAIL). A preflight confirms the candidate's
   packages import from the snapshot (not from an editable install).
5. Additional lower-trust signals: the candidate's own unit tests (failing → FAIL; passing proves
   nothing alone) and ruff with the **trusted** configuration. Static scans of added lines: credential
   shapes (FAIL, value never persisted) and product code reaching into the test runner (FAIL).
6. Scope: changed files from `git diff base..head` must match TaskSpec `allowed_paths` (empty list =
   fail closed), not match `forbidden_paths`; protected paths (AGENTS.md list + generic grader/pipeline
   paths, case-insensitive) → FAIL when outside the contract, QUARANTINED (task BLOCKED for special
   review) when the TaskSpec explicitly allows them.
7. Status PASS only when the trusted suite passed completely and no scope/secret/tamper violation
   exists. Evidence: SCOPE_CHECK, DIFF, SECRET_SCAN, LINT_REPORT (E1), TEST_REPORT (E2) and a
   VERIFICATION_REPORT (E2) whose environment records head/base/tree, artifact hash (tree + diff),
   config hash (suite hash, ruff config, verifier version, interpreter, TaskSpec hash, policy) and the
   verifier identity. The run's `verifier_status` is updated through `aeos.workforce.facade`; FAIL
   requests a bounded repair attempt; absence of the workforce module degrades closed (UNAVAILABLE).
8. Projects other than the platform have no trusted suite registered outside the candidate yet →
   BLOCKED (`NO_TRUSTED_SUITE`), never PASS.

## Consequences

* Residual risk (recorded in every report): candidate code runs as the same OS user in a child
  process; a deliberately malicious candidate could still tamper with its own test process. Strong
  isolation is NOT-PROVEN until worker_runtime provides a real sandbox.
* Paths are kept short (`<data>/vw/<8 hex>/…`, `--basetemp`) because nested pytest temp dirs hit the
  Windows 260-character limit.
* Trusted tests must not parametrize over candidate-dependent data (collection ids must match).
