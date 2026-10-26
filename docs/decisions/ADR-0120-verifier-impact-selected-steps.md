# ADR-0120 — Verifier v4: impact-selected executed checks, per-candidate environments, UI/a11y and project baseline (§05, §09, §16, §22, §31, §34)

* Status: PROPOSED
* Date: 2026-10-26
* Deciders: owner (Fadhlillah) — proposed by the wave 7 E1 audit closure (W6-013, -031, -043, -044, -045, -046,
  -063, -104, -105)

## Context

After ADR-0107 the verifier ran candidate code in an isolated (or honestly WEAK) runner, but impact still added
almost nothing to what it *executed*: only the schema-migration round-trip. API/auth changes were checked by a
regex over the implementer's added lines (a comment satisfied it), contract changes added only a review argument,
integration-marked tests never ran, a dependency change could never be verified (it stayed a blocking NOT-PROVEN),
frontend changes ran no test at all, the A11Y reviewer had no evidence to cite, the DB/Data Engineer's credential
split was documentation only, and an existing project's inventory never had a test baseline.

## Decision

1. **One map from impact category to executed checks** (`aeos.verification.impact`); every VERIFICATION_REPORT
   carries `impact_checks` (selected checks, status, which executed). A category with nothing executed is stated
   on the report, never satisfied by the always-run trusted suite.
2. **Negative tests are executed, not grepped** (`controlled`): refusal tests are found in the syntax tree of the
   changed test files (`pytest.raises`/`assertRaises`, an asserted 4xx status, an asserted refusal code) inside
   functions the change touched, then run against the head; PASS only when they ran and passed.
3. **Verifier-owned adversarial pack** (`adversarial`, `packs/probe_api_boundary.py`): for `api`/`auth` impact on
   the DLY AEOS layout the verifier runs its own probes over every operation of the candidate app's OpenAPI
   document (unauthenticated, cross-origin write, non-loopback host, VIEWER write, malformed JSON, traversal ids,
   hostile logins) through the runner; trusted acceptance modules are selected per category and their outcome
   recorded. A failing probe FAILs; a probe that could not run blocks PASS. Projects whose registered suite has no
   pack get an explicit NOT-PROVEN line.
4. **Contract diff** (`contracts`): facade signatures (syntax tree) and OpenAPI operations (generated from the
   base and head apps through the runner, or static documents) against the base. A breaking change is a blocking
   NOT-PROVEN unless the human-approved TaskSpec says `verifier_policy.contract_breaking: approved` (§16 "kontrak
   berdampak tinggi masuk keputusan/review"). Trusted tests referencing a changed facade are reported as its
   contract tests.
5. **Integration-marked tests are opt-in per TaskSpec** (`verifier_policy.integration_tests: run|required`): run
   with `-m "integration and not network"` on the trusted suite and the candidate's `tests/integration`; a tool-
   absent skip is NOT-PROVEN (blocking for `required`, api impact, Backend/API and Integration Engineer roles).
6. **Per-candidate Python environment** (`envs`): a dependency change is verified again in a venv built from the
   candidate's hash-pinned pip lockfile (`--require-hashes --only-binary=:all: --no-deps`, pip options refused),
   with the controller's test harness first on `PYTHONPATH`; its `dependency_set_hash` is recorded. Process runner
   only; otherwise the §09 Zone 2 NOT-PROVEN stays, with the reason.
7. **Frontend step and UI/a11y evidence** (`frontend`, `packs/ui_driver.py`): `npm ci --ignore-scripts` from the
   candidate lockfile (project npm config not materialised), vitest with a verifier-owned config, `tsc -b
   --noEmit`, `vite build`, then per-state renders (loading/error/unauthenticated/empty x desktop/mobile) in
   headless Chromium with screenshots and a verifier a11y probe (WCAG subset; **not axe-core** - contrast, focus
   order and screen readers stay NOT-PROVEN). For the DLY AEOS layout the loading/error states render for a
   synthetic signed-in session and the empty state is answered by the candidate's own app on an empty throwaway
   database (real response shapes). A render with an uncaught script error is never evidence of its state (FAIL
   under the error/401 envelopes or the candidate app, NOT-PROVEN under generic answers). The A11Y review package
   gets a `ui_evidence` section. axe-core is not
   added: it would change the frontend lockfile, which is the owner's/frontend cluster's decision.
8. **DB/Data Engineer**: the migration driver (`packs/migration_roundtrip.py`) seeds, backs up, restores and
   compares, and rolls back one revision with data present; `db_credentials` checks the owner-named
   `migration_credential_ref` / `runtime_credential_ref` on deployment targets (settable through the integration
   API, validated like `credential_ref`) and scans runtime config for the migration reference (NOT_APPLICABLE without a deployment target, never an invented value).
9. **Role evidence that names a verifier check is machine-checked at acceptance** (`(ui_a11y)`,
   `(negative_tests)`, `(migration_rollback)`, `(db_credentials)` in `planning.dag.ROLE_EVIDENCE`); the UX/UI line is
   no longer satisfied by any SUPPORTED claim.
10. **Project test baseline** (`project_baseline`): the repository's own tests at `inventory_commit`, through the
    same runner selection and tamper window, recorded as a TEST_REPORT the inventory's `tests.baseline` (and so
    `PlanningContext.repo_inventory`) cites for exactly that commit.

`VERIFIER_VERSION` becomes 4, so earlier acceptance is STALE (re-verification).

## Consequences / not decided here

* The planner's TaskSpec contract (schema v1, strict) does not emit `integration_tests` or `contract_breaking`;
  planner-compiled tasks keep the fail-closed defaults until the planning contract is versioned.
* The container runner has no node runtime and no per-candidate venv: frontend and Zone 2 steps stay NOT-PROVEN
  there (stated).
* npm/pip installs reach the package registry/index; on the process runner that egress is not enforced.
* Provider CI check-runs are not imported into the project baseline.

## Integration note (wave 7 merge with ADR-0107 amendments 7-10)

This ADR and the verifier-isolation amendments of ADR-0107 (result seal, per-step kill groups, neutralisation)
landed together as `VERIFIER_VERSION` 4. In the merged verifier the impact-selected pytest steps that run candidate
code - negative tests, integration-marked tests (trusted and candidate), the adversarial probes and the trusted
suite in the per-candidate environment - run under the same result seal as the trusted suite (a forged or
inconsistent result FAILs the step and QUARANTINES the run), the migration pack driver seals its result, and the
per-candidate interpreter's steps are recorded in the verifier runner's kill-group list.

The gaps this note first stated (OpenAPI dump, UI driver and distribution listing unsealed; npm/vitest/tsc/vite and
the pip install outside a kill group; the project baseline outside both) were closed in wave 7b (2026-10-26).
These changes landed as `VERIFIER_VERSION` 5: `dependency_set_hash` now means something else (candidate-only
distributions, read from metadata), and the venv and pip steps run isolated. The version is part of every
verification's config hash, so a version 4 report is never taken as a version 5 result: acceptance recorded under
version 4 is STALE and a version 4 report cannot be accepted (re-verify). What was closed:

* **Kill groups for the tool steps.** The frontend's `npm ci`, vitest, `tsc -b --noEmit` and `vite build`
  (`frontend_install`, `frontend_vitest`, `frontend_typecheck`, `frontend_build`) and the per-candidate venv
  creation and pip install (`dependency_env_venv`, `dependency_env_install`) run in their own kill groups and are
  recorded in the runner's step list (`ProcessRunner.tool`): a process left behind is a survivor and the run is
  QUARANTINED. The venv creation runs only the controller's stdlib and is a trusted-only step. It and the pip
  install run the controller interpreter in isolated mode (`-I`: no `PYTHONPATH`, no working directory on
  `sys.path`). Without it, a `venv.py` that an earlier candidate step left in the run directory replaced the stdlib
  `venv` inside that trusted-only step (reproduced in review). `npm ci
  --ignore-scripts` and the hash-pinned wheel install run no candidate code, but they install candidate-chosen
  packages, so they are not trusted-only (fail closed). Honest runs are not quarantined: every member keeps the
  2 s grace it already had. Only the allow-listed build-tool helper `esbuild` (esbuild's service process ends when its
  parent's pipe closes), together with a Windows console host serving it, gets 10 s more, and only while nothing
  else is left. Helpers that exit in that time are listed on the step (`helper_exits`). Anything still alive
  after it is a survivor. A grace never weakens the tamper window, because a process that ends inside it acted
  while the window was still open. Measured on Windows 11 (Job Object): `npm ci`, the platform's whole vitest
  suite, `tsc -b` and `vite build` left no process behind at all.
  `tests/integration/test_verification_frontend_real.py` (real `npm ci` from the registry, the real
  toolchain, Chromium) passes with no survivor. On macOS (process group plus marker sweep) only the unit tests
  with a scripted node run. An honest real-toolchain run there is NOT-PROVEN.
* **Sealed drivers.** The OpenAPI dump (base and head) and the UI state driver import the seal plugin before
  anything else and seal their result at their very end, with the sha256 of every file they wrote: the document,
  and each screenshot. The host re-checks those digests (`seal.output_problems`), and a file that changed after the
  seal is result tampering (QUARANTINED). Without a sealed end, the OpenAPI diff is NOT-PROVEN (or breaking, when
  only the head fails) and the UI states are NOT_RUN. A screenshot without a sealed digest is never evidence.
* **Dependency listing.** The listing is no longer a process of the candidate environment, so there is no output
  left to forge: the verifier reads the installed distributions' metadata from the venv's `site-packages` itself,
  right after the install and before anything in the environment has run. `dependency_set_hash` now covers
  the candidate's installed distributions only. The trusted harness is recorded separately (`harness`).
  `.pth` start-up hooks are listed, and the ones with `import` lines are stated: they run when the
  candidate interpreter starts, before the seal plugin reads its key, so the seal of the candidate-environment suite
  does not cover them (residual). An honest lockfile can carry one too (coverage ships `a1_coverage.pth`), so this
  is stated, not blocking.
* **Project baseline** (`verification/project_baseline.py`) follows the same discipline. Its pytest step is
  sealed: a forged or inconsistent result makes the baseline TAMPERED, and a missing sealed end leaves its Python
  part NOT_RUN, so there is no trusted measurement. Every step (pytest, `npm ci`, vitest) runs in a kill group: a
  survivor makes the baseline TAMPERED (audited as `verification.controller_modified`, with the survivors), and a
  degraded kill group leaves the tamper status UNKNOWN. A project whose import path ships a top-level module that
  would shadow the test runner, the seal plugin or the standard library is not run (NOT_RUN, stated), the same
  rule the verifier applies to candidates. Not applied by the baseline: the verifier's automatic neutralisation of
  foreign trust-table rows and its admission integrity hold. The baseline reports and audits such rows only, and
  says so on every report (`stated`). The summary the inventory cites carries the tamper window's verdict beside
  the measurement (`integrity`), so a PASS baseline whose integrity is UNKNOWN is shown as UNKNOWN, not as a pass.

Still not covered (stated):

* vitest, `tsc` and `vite` come from the candidate's (or project's) own lockfile, so their reports and exit codes
  are the candidate toolchain's own. A verifier plugin cannot seal them (trust LOW, as the evidence says).
* On the process runner the run directory is writable by candidate code. A later step reads what an earlier
  candidate step wrote there, and that includes trusted-only steps. Examples are a module in the shared step
  working directory, which `python -c` / `python -m` put first on `sys.path`, the seal plugin file itself, or the
  base-commit export. For steps that import candidate code this reaches no further than the stated in-interpreter
  subversion of the seal. For trusted-only steps it is an additional gap: only the trusted-suite copy is
  hash-checked before and after. The venv creation is the exception, because isolated mode imports nothing from
  the run directory. The container runner mounts these read-only and gives each step its own tmpfs working
  directory.
