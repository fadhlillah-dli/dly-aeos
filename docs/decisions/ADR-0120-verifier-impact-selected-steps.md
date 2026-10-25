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
   order and screen readers stay NOT-PROVEN). The A11Y review package gets a `ui_evidence` section. axe-core is not
   added: it would change the frontend lockfile, which is the owner's/frontend cluster's decision.
8. **DB/Data Engineer**: the migration driver (`packs/migration_roundtrip.py`) seeds, backs up, restores and
   compares, and rolls back one revision with data present; `db_credentials` checks the owner-named
   `migration_credential_ref` / `runtime_credential_ref` on deployment targets and scans runtime config for the
   migration reference (NOT_APPLICABLE without a deployment target, never an invented value).
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
