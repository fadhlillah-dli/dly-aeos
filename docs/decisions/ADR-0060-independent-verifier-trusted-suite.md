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
9. Hardening added by the completeness review (2026-10-18):
   * a candidate `backend/` top-level module that would shadow the test harness, the stdlib or any
     module of the controller environment (e.g. `backend/pytest.py`, `backend/tests/`, `backend/json.py`)
     FAILs before the trusted suite runs (`harness_shadow`) — `PYTHONPATH` precedes site-packages;
   * changed **binary** files (git prints only "Binary files differ") are scanned for credential
     shapes from the snapshot; files that cannot be scanned keep the result NOT_PROVEN;
   * the diff parser consumes hunk bodies by their declared line counts, so an added line that looks
     like a `+++ b/<path>` header cannot re-attribute later lines to an allow-listed file;
   * tree entries with Windows device names / trailing dots or spaces are refused on **every** OS
     (same verdict everywhere; nothing is written to a device);
   * git plumbing runs with an allow-list environment (no credentials, no `GIT_*` overrides,
     `protocol.allow=never`, no credential helper, no lazy fetch);
   * a head that does not descend from the recorded base is NOT_PROVEN (base..head is then not the
     worker's change); when the runtime recorded no head, the run's task branch is used, never the
     worker-controlled checkout `HEAD`;
   * status keys stored in evidence avoid words the redactor masks (`leak_scan_status`, not
     `secret_scan_status`), otherwise a failing scan would be stored as `[REDACTED]` and be invisible
     to gates and the PR bundle;
   * acceptance is valid only while nothing it is bound to has drifted (run/PR head, PR base, grader /
     verifier configuration, PlanVersion) — `acceptance_is_valid` fails closed without a recheck, and a
     verification made under another configuration is not accepted.

## Consequences

* Residual risk (recorded in every report): candidate code runs as the same OS user in a child
  process; a deliberately malicious candidate could still tamper with its own test process. Strong
  isolation is NOT-PROVEN until worker_runtime provides a real sandbox.
* Paths are kept short (`<data>/vw/<8 hex>/…`, `--basetemp`) because nested pytest temp dirs hit the
  Windows 260-character limit.
* Trusted tests must not parametrize over candidate-dependent data (collection ids must match).

## Amendment 2026-10-18 (integration)

1. **Trusted suite for managed (non-platform) projects.** Decision 8 is refined: a project's grader can
   now be registered outside every candidate. The owner calls `POST /api/projects/{id}/trusted-suite`
   (SUPER_ADMIN + CSRF) naming a reviewed commit/branch of the project's own registered repository; the
   controller exports exactly that tree with git plumbing and copies `tests/acceptance/**` (+ shared
   `tests/__init__.py`, `tests/conftest.py`, the `pyproject.toml` lint configuration) to
   `<data>/trusted-suites/<project_id>/<id>/`, then records a human POLICY decision
   (`target_type=trusted_suite`, `target_id=<project_id>`, `target_hash=<suite hash>`). The verifier uses
   that copy only while its files still hash to the decision (`TRUSTED_SUITE_TAMPERED` otherwise);
   without a registration the result stays BLOCKED `NO_TRUSTED_SUITE`. The expected test ids are
   collected with the human-reviewed **base commit** of the run (the project's trusted code), never with
   the candidate head; the platform keeps using the controller checkout. `current_config_hash` resolves
   the same suite, so a re-registered grader makes earlier acceptance STALE; a project without a
   registration keeps the previous controller-suite identity (it can never be verified PASS, and that
   hash never equals a registered suite's), a registered copy that is missing/tampered gives UNKNOWN.
2. The deterministic loop verifies REVIEW runs of the platform and of projects with a registration.
3. `update_run(verifier_status=)` now receives the verifier's own vocabulary (accepted by the workforce
   module); QUARANTINED moves the task to BLOCKED with reason code `QUARANTINED` (waits for the owner).
4. The verifier's wall-clock seconds are recorded as §42 units through
   `aeos.cost.facade.record_unit_usage(category="COMPUTE", unit="seconds")`, cash cost UNKNOWN.
5. **Binary leak scan fix.** The redaction patterns gained a "not after an alphanumeric" guard against
   prose false positives (security fix c07370d); in binary content that guard let a token glued to other
   bytes (`PK<token>`) through. The binary scan uses the patterns without the guard.
