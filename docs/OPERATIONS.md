# Operations & Reliability (master §33)

How the DLY AEOS control plane is operated, recovered and retired on a single local machine.
Procedures for specific situations are in [runbooks/](runbooks/README.md).

## Principles

* **Recovery is proven, not assumed.** A backup is ready only after a restore test succeeded and
  recorded `RESTORE_TEST` evidence (§33, §50 "Restore backup belum pernah diuji").
* **Humans hold AUTH.** No agent deletes data, production resources or backups. Promotion,
  rollback and live restore are local CLI operations with typed confirmation; promotions are
  `PROMOTION` Decisions bound to the artifact hash.
* **SLIs are computed by code; targets are owner decisions.** No generic availability numbers.
* **Fail closed and say so.** A missing module or failed step is reported as FAILED/UNKNOWN with the
  reason; nothing is claimed done that was not done.

## Data locations

| What | Where |
|---|---|
| Data root | `%LOCALAPPDATA%\DLY-AEOS` · `~/Library/Application Support/DLY-AEOS` · `$XDG_DATA_HOME/DLY-AEOS` (override: `AEOS_DATA_DIR`) |
| Metadata database | `<data>/aeos.sqlite3` (SQLite WAL; audit/events/evidence/plan versions append-only) |
| Domain partitions | `<data>/personal/projects/<key>/{attachments,evidence,publications,task-data}`, `<data>/office/...` |
| Backups | `<data>/backups/<backup_id>/` (+ legacy `pre-upgrade-*.sqlite3` from `aeos db upgrade`) |
| Released controller | `<data>/releases/<version>-<sha12>.zip`, `.provenance.json`, extracted dir, `ACTIVE.json` |
| Scratch | `<data>/tmp/` (restore tests, smoke tests; removed after use) |
| Config (non-secret) | `%LOCALAPPDATA%\DLY-AEOS\aeos.yaml` / `~/Library/Application Support/DLY-AEOS/aeos.yaml` (example: `config/aeos.example.yaml`) |
| Canonical MCI_V2 (owner-supplied) | `<data>/policy/canonical/<file>` (or `mci_source_path` in aeos.yaml) + `<data>/policy/canonical/mci-pack-mapping.yaml`; never in the repository |

Secrets are never in these files: OpenBao + OS credential store only (§26).

The platform repository is public, so MCI content never lives in it (master §06 "MCI tidak dimasukkan
tanpa hak"). `policy/mci-reference.yaml` keeps the reference only: `status: VERIFIED`, `version`,
`content_hash` (`sha256:<lowercase hex>` of the exact bytes of the local file) and `location:
local:<file>`. The source is verified on each machine that has a copy; a machine without it reports
`SOURCE_MISSING` (UNVERIFIED, fail closed), and a source inside the checkout is never read. The per-pack
MCI controls and evidence contracts (§34) are derived MCI content too: they go into the local
`mci-pack-mapping.yaml` (template: `blueprints/mci-pack-mapping.template.yaml`) and a pack is APPROVED
only with an owner POLICY decision on `mci_pack_mapping/<pack>` bound to the hash shown at `/decisions`.
Compliance is never claimed (NOT-PROVEN).

## Backup and restore

| Command | What it does |
|---|---|
| `aeos ops backup create [--label X] [--if-exists]` | consistent SQLite online copy (sessions removed) + per-domain archives of evidence/attachments/publications + manifest (files, sizes, SHA-256, row counts, schema revision, app version); OpenBao data, keyring, Claude login, sandboxes, logs and credential-like files are excluded |
| `aeos ops backup verify <id>` | re-hash every file against the manifest (not a restore test) |
| `aeos ops backup restore-test <id>` | restore into `<data>/tmp`, `PRAGMA integrity_check`, row counts, open all model tables, verify extracted files; `RESTORE_TEST` evidence only on success |
| `aeos ops backup list [--json]` | readiness + retention per backup |
| `aeos ops backup retention --keep-last N --keep-days D` | owner's retention policy (listing only; nothing is deleted automatically) |
| `aeos ops backup restore <id> [--force-maintenance]` | live restore of a restore-tested backup; refuses while the server runs; pre-restore backup first |

API (dashboard *Riwayat & Pemulihan → Backup*): `GET/POST /api/ops/backups`,
`POST /api/ops/backups/{id}/verify`, `POST /api/ops/backups/{id}/restore-test` (SUPER_ADMIN). There is
no HTTP route for live restore. A backup is one platform-level unit holding both domain partitions;
the dashboard shows its overall readiness (a failed OFFICE archive makes the backup FAILED for
everyone) but per-partition file counts, sizes and problem details only for the active domain
(§35 scope). Platform-level records (audit rows, `RESTORE_TEST` evidence) carry no per-domain
counts. The CLI shows the full view. OpenBao has its own recovery path
([runbook](runbooks/openbao-unseal-recovery.md)).

Suggested owner cadence (decide and record it): backup + restore test before every upgrade and at a
fixed interval; keep at least one restore-tested backup off-machine on encrypted storage; keep
Office archives on company-approved storage only.

## Controller releases (self-build zones, §09)

| Command | What it does |
|---|---|
| `aeos ops release build [--ref HEAD]` | `git archive` of a clean commit reachable from `main` **that is accepted** (trusted provider-CI PASS for that exact commit, or an owner EXCEPTION decision) → zip + provenance (commit, tree hash, artifact SHA-256, schema head, acceptance basis, built_at, builder) + pinned UI bundle (`frontend/dist` zipped with its own SHA-256). The UI's commit correspondence is recorded and printed: VERIFIED when the Vite build record (`dist/aeos-build.json`) names a clean frontend tree equal to `git rev-parse <commit>:frontend`; a bundle from another tree refuses the build (RELEASE_UI_MISMATCH); no record or a dirty frontend tree is UNVERIFIED and refuses the build (RELEASE_UI_UNVERIFIED) unless the owner passes `--allow-unverified-ui` (recorded in the provenance and the audit, shown with its reason) |
| `aeos ops release accept --commit <sha> --operator <admin> --reason "..."` | human-only EXCEPTION decision bound to one exact commit, used when trusted CI PASS is not available |
| `aeos ops release list` | releases with integrity, ACTIVE/PREVIOUS role and `ui=VERIFIED/UNVERIFIED/NONE` (the recorded UI commit correspondence, with the reason when not VERIFIED; also in the Control Center, History > Releases) |
| `aeos ops release promote <id> --operator <admin>` | decision (bound to artifact hash, UI hash, runtime-config hash) → compatibility (migration head vs DB) → pre-promote backup + restore test → smoke test in a subprocess on a temp loopback port against a DB copy (health, then functional: temporary admin in the copy, login, explicit PERSONAL, doctor RUNTIME run, capabilities, projects) → live migration only if required (server stopped) → `ACTIVE.json` (previous kept, config hash recorded) |
| `aeos ops release rollback --operator <admin>` | pointer back to the previous release after the same checks; refused when the DB is ahead (restore the pre-promote backup first) |
| `aeos serve [--release auto\|active\|checkout]` | `auto` (default) runs the ACTIVE release when one was promoted: artifact + UI hash verified, extracted tree compared byte-for-byte (planted files refused), `PYTHONPATH` = the release only, byte-code cache outside the release; refuses to start on any integrity failure. `checkout` runs the editable source tree and warns when a release is active. `--dev`/`--reload` always run the checkout and need a separate dev profile (`AEOS_DATA_DIR` + `AEOS_KEYRING_SERVICE`, set by `scripts/dev.*`) unless `--dev-with-real-credentials` |

`GET /api/health` reports `controller.mode` (`release`/`checkout`), `release_id`, `commit`,
`matches_active`; the doctor check `recovery.config_rollback` FAILS with
`CONTROLLER_NOT_ACTIVE_RELEASE` when the running controller is not the ACTIVE release, and with
`CONFIG_CHANGED_SINCE_PROMOTION` when the runtime config / policy snapshot hash differs from the one
recorded at promotion.

Any failure keeps the previous release active, raises a HIGH alert and is audited
(§50 "Upgrade controller gagal"). Details: [runbook](runbooks/controller-upgrade-rollback.md).

**GitHub Releases** (master §06 "GitHub activity ... releases"): AEOS builds and promotes local release
artifacts only; it never creates a GitHub tag or Release. After a successful promotion the owner may
publish one by hand (`gh release create v<version> --target <commit> --notes-file <provenance summary>`),
so release activity on GitHub is an owner action, not something the controller claims.

## SLIs and alerts

`aeos ops sli --domain PERSONAL|OFFICE [--raise-alerts] [--json]` · `GET /api/ops/sli` ·
`POST /api/ops/sli/evaluate` · `GET /api/ops/alerts`.

| SLI | Meaning (computed from) |
|---|---|
| `queue_age_seconds` | oldest QUEUED/READY task (task_executions) |
| `stale_leases` | ACTIVE leases past expiry or without heartbeat for 300 s (leases) |
| `retry_rate` | runs with attempt > 1 / runs, 24 h (worker_runs) |
| `acceptance_failures` | rejected/invalidated acceptances + verifier-failed runs, 24 h |
| `accepted_outcomes` | accepted outcomes, 24 h (the outcome indicator) |
| `budget_drift_usd` | actual recorded cost − committed reservations, 24 h |
| `unknown_cost_entries` | LIVE usage entries with unknown cost, 24 h |
| `sync_freshness_seconds` / `sync_problems` | external references/issue projections age and STALE/CONFLICT/UNKNOWN count |
| `integration_failures` | integrations whose recorded capability status is FAILED/EXPIRED/UNREACHABLE |
| `recovery_point_age_seconds` | age of the newest restore-tested backup (platform-level) |

**Targets**: none by default — every SLI shows `NO_TARGET` until you record one, based on the
criticality you decide: `aeos ops sli target queue_age_seconds --max 1800 --rationale "..."`
(audited). A target with no data shows UNKNOWN, never "within target".

**Per criticality** (§33): `aeos ops sli criticality <project> HIGH|MEDIUM|LOW` records the owner's
criticality of a project, and `aeos ops sli target <key> --criticality HIGH --max ...` sets a target that
applies only to projects of that level (per-project SLIs only); `by_criticality` in the result shows each.

**Error budget** (§46): `aeos ops sli budget-policy --window-days 28 --objective 0.99 --action alert|pause_scheduling`
(owner decision). Every evaluation stores its samples (`ops_sli_samples`); the budget is the share of
measured evaluations allowed to breach over the window (UNKNOWN/NO_TARGET never count as good). When a
budget is exhausted a HIGH `error_budget_exhausted` alert is raised; with `pause_scheduling` new work is
paused. The scheduler pause is global, so it is lifted only when the budget of every domain that caused it
has recovered, and never when the current pause was set by someone else (emergency stop, a human).

**Alerts** are deduplicated (`ops_alerts`) and emitted once as `alert.raised` events (resolved:
`alert.resolved`), carrying the domain so Office alerts never reach a Personal session. Objective
anomalies alert without targets: stale leases, sync conflicts, failing integrations, no restore-tested
backup, failed promotions, emergency stops. A background evaluation loop (`aeos.operations.loop`,
feature flags `ops.sli_loop_enabled` / `ops.sli_loop_interval_seconds`) runs when registered in the
app lifespan.

## Emergency stop (incident containment)

Dashboard *Riwayat & Pemulihan → Darurat* (single-use confirmation token, typed `HENTIKAN`) or
`aeos ops emergency-stop --reason "..."`. Steps, each reported separately: pause scheduling, revoke
all active capabilities, cancel RUNNING/LEASED runs, preserve evidence, audit + CRITICAL alert.
[Runbook](runbooks/incident-containment.md).

The stop itself is platform-wide (an OFFICE run is cancelled from a PERSONAL session too), but what
is *shown* is scoped (§35, §50 "termasuk counts"): the dashboard report carries every step status
(a failure in another domain still shows as FAILED/PARTIAL with a neutral reason) plus counts and run
ids of the active domain only; with no domain selected it shows statuses only. The audit row, the
CRITICAL alert and `ops.emergency.last` hold statuses only. One `INCIDENT_CONTAINMENT` evidence
artifact is stored per domain partition with that domain's counts. The local CLI shows the full
report.

## Feature flags

`GET /api/ops/flags`, `PUT /api/ops/flags/{key}` (SUPER_ADMIN, audited, emits `ops.flag.changed`,
invalidates dependent doctor checks `flag:<key>`). Only registered flags with a named consumer can be
set; flags only narrow behaviour — they never grant AUTH, enable paid routes or open OFFICE access.

## Export snapshots (§41)

`GET /api/ops/export?kind=inventory|architecture|evidence|audit|decisions[&project_id=][&download=true]`
returns a JSON **snapshot** — `snapshot_id`, `content_hash`, `generated_at`, scope (active domain +
platform rows, user project scope), `mode: SNAPSHOT` — redacted again before leaving the server
(number of redacted fields reported). Exports are registered (`GET /api/ops/exports`) and audited. A
snapshot is never a live status.

## Service inventory, runbooks, retirement

* `GET /api/ops/services` — control plane, database, background services, OpenBao probe
  (CONNECTED ≠ VERIFIED), Vite in dev mode.
* `GET /api/ops/runbooks` — index of `docs/runbooks/*.md`.
* `GET /api/ops/retirement/{project_id}` — read-only retirement checklist (consumers, jobs,
  credentials, data retention, legal hold, backups, communication, final owner decision). Nothing is
  deleted or revoked by it (§33).
* `PUT /api/ops/retirement/{project_id}/legal-hold` and `.../steps/{key}` record the tracked workflow
  (`ops_retirements`; a step record does not change the checklist hash, a legal-hold change does).
  `POST /api/ops/retirement/{project_id}/decision` records the owner's final verification: a `RETIREMENT`
  decision bound to the shown `checklist_hash` (409 `RETIREMENT_BLOCKING_ITEMS` /
  `RETIREMENT_CHECKLIST_STALE` / `RETIREMENT_REQUIRES_RETIRING`); lifecycle `RETIRED` is refused
  (`RETIREMENT_NOT_VERIFIED`) unless that decision still matches the current checklist.

## Dependency lockfile

`requirements.lock` is a **universal** lock (Windows, macOS, Linux; markers such as
`sys_platform == 'win32'`) of `pyproject.toml` including the `dev` extra. The bootstrap scripts
install it with `pip install -r requirements.lock` and then the project with `--no-deps`.

Regenerate after changing `[project].dependencies` or the `dev` extra:

```bash
python -m pip install uv            # inside the repository venv
python -m uv pip compile pyproject.toml --universal --extra dev -o requirements.lock
python -m pip install -r requirements.lock && python -m pip install --no-deps -e . && python -m pip check
```

Review the diff (new packages are supply-chain changes) and commit it with the dependency change.
Hash pinning (`--generate-hashes`) is a possible hardening step recorded in ADR-0093.

A candidate that changes a manifest or lockfile is **high impact**: the verifier records a
`DEPENDENCY_DIFF` evidence item (added / removed / upgraded packages; licences `UNKNOWN` = NOT-PROVEN)
and — because the trusted suite runs with the controller's installed packages (recorded as
`dependency_set_hash` on every verification) — a changed Python dependency set is NOT-PROVEN in
isolation (§09 Zone 2) until it is verified in a separate environment (ADR-0106).

## Delivery targets, gates and verifier honesty (ADR-0106)

* **Release approvers (G7):** record the GitHub logins allowed to merge/release a project as a human
  POLICY decision: `POST /api/decisions` with `kind=POLICY`, `target_type=release_approvers`,
  `target_id=<project id>`, `auth_scope={"github_logins": ["<login>"]}`. Without it a human merge
  is shown NOT_PROVEN; a merge by any other account is FAIL.
* **Staging / production targets (G6/G8):** register a PERSONAL integration with
  `provider=deployment-target`, `purpose=staging|production`, `root_id=https://<base url>` (optional
  `config.health_path`, default `/health`; an absolute path only - `@host/...`, `//host/...`, query or
  fragment are refused). AEOS never deploys, releases or rolls back; `aeos delivery observe-target
  <project> --environment staging --sha <commit>` performs read-only GETs and records one aggregate
  RUNTIME_OBSERVATION that is PASS only when the target reports the exact commit and healthy and every
  owner check passed. DLY AEOS' own `/api/health` (set `health_path=/api/health`, e.g. Windows
  `.\aeos.ps1 delivery target-checks <project> --environment production --health-path /api/health`, macOS
  `./aeos delivery target-checks ...`) proves its commit only while `controller` runs the ACTIVE clean release.
  * **Checks (owner values, never defaults):** `PUT /api/delivery/targets/{integration_id}/checks` or
    `aeos delivery target-checks <project> --environment <env> --file checks.json` with
    `smoke_checks` (`name`, `path`, `expect_status` (default 200), `json_keys`, `kind` `smoke|critical_flow`;
    each one recorded as SMOKE_TEST evidence), `sli_checks` (`name`, `path`, `json_key`, `max`/`min` owner
    thresholds - without one an SLI is `NO_TARGET`, report only), `rollback_on` (any of `health_fail`,
    `smoke_fail`, `critical_flow_fail`, `sli_breach`, `artifact_digest_mismatch`; without it the rollback
    recommendation is `UNDECIDED`) and `require_artifact_digest` (true/false); `health_path` can be set the
    same way. A generic integration save whose config does not name these keys keeps them (an explicit value,
    even empty, replaces them); changing them makes earlier observations STALE.
  * **Staging gate (§08):** a staging observation and a staging screenshot run only while
    `CAN_VALIDATE_STAGING` is READY for the project (verifier, restore test, and per project an
    observable `deployment-target`; a `local-target` is recorded but not observable). Refusals are audited.
  * **Owner decisions (POST /api/decisions kind POLICY, or `aeos policy decide`):**
    `target_type=staging_environment`, `target_id=<project id>`, `auth_scope={"staging": "AUTHORISED",
    "target": "<exact base url>"}` authorises the (human) staging deploys to that target - G6 PASS needs it;
    `auth_scope={"staging": "NONE"}` (or `production_environment` + `{"production": "NONE"}`) states the
    project has no such environment - the only way G6/G8 become NOT_APPLICABLE.
  * **Post-release failure (G8):** a FAIL production observation raises the HIGH alert
    `post_release_failure`, opens/links an incident with the rollback recommendation and files a decision
    request (rollback / forward-fix / accept) - see [post-release-failure](runbooks/post-release-failure.md).
  * **Build once (§31):** `aeos delivery artifacts <project> --sha <merge commit>` records the provider CI
    artifact digests (ARTIFACT_DIGEST evidence; attestation presence shown, signature not verified); a target
    that reports `artifact_digest`/`digest` is compared with it. The gate response's `promotion` is PASS
    only with G5 and G7 PASS and every registered target serving the recorded digest.
* **Provider enforcement test (M4):** after your own bypass attempt (or the opt-in live test
  `tests/acceptance/test_delivery.py::test_provider_enforces_human_only_merge_on_the_real_repository`
  with `DLY_AEOS_LIVE_BYPASS_REPO`, `DLY_AEOS_LIVE_BYPASS_BRANCH=aeos-probe/<name>`,
  `DLY_AEOS_LIVE_BYPASS_TOKEN_REF`), `aeos delivery enforcement --repo <owner/name> --branch <branch>` reads
  GitHub's rule suites (token needs `Administration: read`) and records PROVIDER_ENFORCEMENT_TEST evidence only
  for a change refused by an ACTIVE ruleset; `aeos delivery capabilities --record` then shows enforcement
  VERIFIED for that ruleset until it changes (STALE). `--attest "<what was refused>"` stores an untrusted
  owner claim (CLAIMED, never VERIFIED).
* **Pipeline reference (§31):** set `config.gate_G1 ... gate_G5` on the project's `ci` integration
  to exact CI job names (template: `blueprints/pipeline-reference.yaml`); otherwise CI jobs are
  classified by name heuristics and the evidence says `NAME_HEURISTIC`.
* **Database migrations:** when a change touches a migration/schema path and the candidate has an
  Alembic tree (`env.py` + `versions/`), the verifier now runs an upgrade -> downgrade(base) ->
  upgrade round-trip and reopens the database (restore) as candidate code through its runner, and
  `migration_rollback` is PASS/FAIL (not merely NOT-PROVEN). A candidate without an Alembic tree, or
  a managed project whose migrations run only in its own pipeline, is a **blocking** NOT-PROVEN (G2
  needs a migration/rollback test per migration change) unless the TaskSpec sets
  `verifier_policy.migration_roundtrip: optional`; run those in the project's pipeline with a dedicated
  migration credential separate from the runtime credential. Since verifier v4 the round-trip also
  seeds one generated row per table, backs the database up with the SQLite backup API, deletes and
  restores it and compares the seeded rows, then rolls one revision back and forward **with the data
  present** (a downgrade that only breaks on populated tables fails, leg `data_rollback`).
* **Migration/runtime credential split (`db_credentials`, §16 DB/Data Engineer):** name both references
  on each `deployment-target` integration: `config.migration_credential_ref` and
  `config.runtime_credential_ref` (default: the integration's `credential_ref`), set through
  `PUT /api/projects/{id}/integrations` and validated like `credential_ref` (secret-store reference in the
  project's domain namespace). References only, never values. The verifier FAILs when both are the same reference, when an entry looks like a secret value,
  or when a runtime configuration file of the candidate (env files, `config/`, settings, compose/k8s/helm)
  names the migration reference. With no deployment target the check is NOT_APPLICABLE (local runtime);
  a target that names only one reference is NOT_PROVEN until you add the other.
* **Verifier v5 (wave 7b):** the per-candidate environment's `dependency_set_hash` covers only the
  candidate's installed packages, and the venv and pip steps run isolated in their own kill groups. A
  verification made by v4 is never reused as a v5 result: acceptance recorded under v4 shows STALE and a v4
  report cannot be accepted, so re-run the verification (ADR-0120 integration note).
* **Impact-selected verifier checks (§22, §31, verifier v4):** every VERIFICATION_REPORT carries
  `impact_checks` - per impact category the checks it selected (`aeos.verification.impact`) and which
  executed. API/auth changes execute the change's own refusal tests (found in the test syntax tree, never
  in comments) and, for the DLY AEOS layout, the verifier-owned adversarial probes over every operation of
  the candidate app (unauthenticated, cross-origin, non-loopback host, VIEWER write, malformed JSON,
  traversal ids, hostile logins). Facade/OpenAPI changes get a contract diff against the base; a breaking
  change is a blocking NOT-PROVEN unless the approved TaskSpec sets `verifier_policy.contract_breaking:
  approved`. Integration-marked tests run only when the TaskSpec sets `verifier_policy.integration_tests:
  run|required` (a tool-absent skip is NOT-PROVEN; blocking for `required`, api impact and the Backend/API
  and Integration Engineer roles). The planner's TaskSpec contract (schema v1) does not emit these
  opt-ins yet; planner-compiled tasks keep the fail-closed defaults.
* **Per-candidate environments (§09 Zone 2):** a candidate that changes its Python dependency set is
  verified again in a virtual environment built in the run directory from its own hash-pinned pip
  lockfile (`pip install --require-hashes --only-binary=:all: --no-deps`; the trusted test harness is
  the controller's). Lockfiles without `--hash` pins, pip options in the file, non-pip lock formats and
  the container runner leave the change a blocking NOT-PROVEN with the reason. Generate hashes with
  `uv pip compile --generate-hashes`. The install step reaches the package index (not an enforced egress
  boundary on the process runner).
* **Frontend verification:** a frontend change runs `npm ci --ignore-scripts` from the candidate's
  `package-lock.json` (project `.npmrc` is not used), vitest with the verifier's own config, `tsc -b
  --noEmit` and `vite build`, then renders the built bundle in headless Chromium per UI state (loading,
  error, unauthenticated, empty; desktop and mobile) with screenshots and the verifier's accessibility
  probe (a WCAG subset, not axe-core). For the DLY AEOS layout the loading/error states render for a
  signed-in (synthetic, MOCK) session and the empty state is answered by the candidate's own app on an
  empty throwaway database, so every endpoint has its real shape. A render that raises an uncaught script
  error is never evidence of its state: under the error/401 envelopes or the candidate's own app it FAILs
  `ui_a11y`, under the verifier's generic answers that state is NOT-PROVEN. Needs node/npm and Playwright's Chromium on the host and the
  process runner; otherwise `frontend_tests` stays a visible, non-blocking NOT-PROVEN (G3 provider CI).
* **Project test baseline (§05 step 6):** `aeos verify project-baseline <project id>` (or
  `POST /api/projects/{id}/test-baseline`) runs the repository's own tests at the inventoried commit
  through the verifier runner and tamper window and records a TEST_REPORT (commit, suite identity,
  passing/failing ids). The inventory's `tests.baseline` (and so planning's repository inventory) cites
  it for exactly that commit; a newer commit is NOT_RUN until the baseline runs again.
* **Verifier isolation (§20 isolated runner):** the verifier executes candidate code (the trusted
  suite against the candidate, the candidate's own tests, the migration round-trip) through one of two
  runners, stated on every VERIFICATION_REPORT (`verifier_isolation` / `verifier_runner`) and shown in
  the Gate panel next to the verifier status, and surfaced by Doctor `evidence.verifier`:
  * `CONTAINER` - selected automatically when this host has a fresh PASS **container** containment
    report for the domain profile (the same proof that admits container workers, `aeos worker
    containment --kind container`) **and** the pinned verifier image is built (`docker build -f
    sandbox/verifier.Dockerfile -t dly-aeos-verifier:<controller version> .`). Each step runs
    `docker run --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pull
    never` as a non-root user with only the run directory mounted (the trusted controller `backend/`
    read-only for the expected-id collection step alone); it cannot reach the data DB, the
    OS keyring, `gh`/git credentials, the host home, an SSH agent or the docker socket.
  * `WEAK` (process) - the fallback when the container boundary is not proven here. Candidate code
    runs as the controller's OS user with a scrubbed, **redirected** environment
    (`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`XDG_*`/platformdirs `WIN_PD_OVERRIDE_*`/
    `GH_CONFIG_DIR`/docker/npm/claude config into the empty run home, null keyring backend, no git system/global config or credential helper,
    no SSH agent socket, no D-Bus session bus). That stops credential discovery **through the
    environment** only; a deliberately malicious candidate could still read a file by absolute path or
    call the OS credential API directly. The residual risk is recorded on the report (`residual_risks`)
    and on Doctor `evidence.verifier`; it is never presented as isolated. OFFICE verifications, a
    worker run that executed in the container sandbox (isolation is never downgraded) and a TaskSpec
    `verifier_policy.isolated_runner: required` are BLOCKED rather than run on this runner. The run
    directory is a private temp directory outside the data dir, so no candidate-visible path leads to
    the live DB.
  Either way, writes made by candidate code during a verification to the controller checkout, the
  controller interpreter's site-packages (the editable-install finder/`.pth`) or the DB trust tables
  (decisions, policy, routes, budgets, TaskSpecs, acceptance/evidence, sessions, capability grants,
  projects/integrations, eval suites, containment proofs/write enablement/flags) and the SQLite schema (tables,
  indexes, append-only/immutability triggers) are detected and the run is
  QUARANTINED for the owner's special review with a HIGH alert. Foreign approval/session/grant/route/policy rows
  are neutralised at once (decision INVALIDATED, request WITHDRAWN, acceptance INVALIDATED, session/grant
  revoked, route DISABLED until you re-activate its configuration hash, policy snapshot rebuilt, a forged
  containment proof removed, write enablement off, a forged or modified account deactivated; audit
  `verification.tamper.neutralize`); the alert lists what still needs your review (removed rows, schema, a
  modified account's role and scope, ...) with the remedy. A deactivated account is re-activated locally with a
  one-time password: Windows `.\aeos.ps1 admin recover <username>`, macOS `./aeos admin recover <username>`. If a
  listed decision was your own (recorded from another AEOS process during the verification), record it again.
  When something stays in effect (a changed controller file such as `policy/**`, a row without a safe inverse,
  a failed neutralisation) the verifier also pauses admission of new worker runs (**integrity hold**, reason
  `VERIFIER_TAMPER integrity hold`; the alert lists each remedy). Reconcile, then resume: Windows
  `.\aeos.ps1 work resume --reason "VERIFIER_TAMPER reconciled"`, macOS `./aeos work resume --reason ...`.
  Two more guards apply to both runners: every pytest step that runs candidate code is **sealed** by the
  verifier's own plugin (a junit report or exit code changed after the fact is QUARANTINED; candidate code that
  subverts the plugin inside its interpreter is a stated residual risk), and every process-runner step runs in
  its own kill group (Windows Job Object / POSIX session): a process that outlives its step is killed and the
  run QUARANTINED.
* **Specialist reviews:** a UI change (or a frontend/UX role) needs the A11Y reviewer, data/AI roles or
  CONFIDENTIAL/RESTRICTED data the PRIVACY reviewer, CI/IaC or operations roles the SRE reviewer. Request
  them with "Review wajib (spesialis)" on the run list, `POST /api/runs/<run>/reviews/batch`, or
  `aeos verify review <run> --kind A11Y|PRIVACY|SRE|SECURITY`. A run has a review budget
  (`reviewer_policy.max_reviews_per_run`, default 12; quota waits do not count).
* **OFFICE delivery (Bitbucket Cloud, ADR-0111):** DISABLED by default. It becomes AVAILABLE only with
  (1) a company-authorisation POLICY decision on `office_policy` listing `bitbucket`, naming the repository or
  its workspace as an approved target and carrying the override `atlassian_deployment: "CLOUD"` (the interlock
  needs it too) - Control Center: switch the header domain to OFFICE, **Keputusan** (`/decisions`) -> POLICY ->
  tick "Ini otorisasi perusahaan untuk OFFICE"; CLI: `aeos policy decide --kind POLICY --domain OFFICE
  --office-authorization --company-reference "<ref>" --allowed-provider bitbucket --approved-target
  bitbucket:workspace=<workspace> --office-policy atlassian_deployment=CLOUD --as <admin> --rationale "<why>"`;
  (2) the project integration `bitbucket/code` with `root_id=<workspace>/<repo_slug>` and `enabled` - Control
  Center: **Portofolio** -> project -> **Integrasi** -> "Kode — Bitbucket", tick "Aktifkan adapter" (no
  deployment field: the deployment comes from the override above); (3) the registered Bitbucket repository
  VERIFIED (Repositori -> "Verifikasi identitas", or `POST /api/repositories/<id>/verify`); (4)
  `office/bitbucket` stored: `{"access_token": "..."}` (repository access token) or `{"email": "...",
  "api_token": "..."}` (Atlassian API token; optional `git_username`) - the Doctor builds the same header as the
  broker/publisher (Bearer / Basic); and (5) `CAN_USE_OFFICE_INTEGRATIONS` READY. Data Center
  (`atlassian_deployment: "DATA_CENTER"`) is `NOT_SUPPORTED`. The broker then pushes the accepted head
  sha-pinned to `aeos/*` and opens a draft PR; nothing merges, declines or approves. Pipelines/commit-status CI
  becomes provider-ci evidence (pipeline reference: **Integrasi** -> "CI — Bitbucket Pipelines", exact step or
  commit-status names per gate G1/G2/G3/G5). Without Bitbucket Premium's "enforce merge checks" the merge gate is
  NOT_ENFORCEABLE. G7 release approvers for OFFICE: `auth_scope={"bitbucket_accounts": ["<Atlassian account
  id>"]}`. Live tenant behaviour is NOT-PROVEN (M5).
* **Company-authorisation scope (§01 U-03, §07 step 10, §51 P1):** the authorisation also records what the
  company approved - `approved_targets` (`confluence:tenant=<site>.atlassian.net[,space_id=<numeric id>]
  [,root_id=<parent id>]`, `jira:tenant=<site>[,root_id=<KEY>]`, `bitbucket:workspace=<ws>[,root_id=<ws>/<repo>]`),
  `approved_data_classes` (PUBLIC/INTERNAL/CONFIDENTIAL/RESTRICTED) and, optionally, `approved_identities`
  (`office/atlassian=<account id or email>`, `office/bitbucket=<uuid or account id>`; a nickname is not unique
  and is never compared). Absent = UNDECIDED and fail closed: no OFFICE target is written, read, verified or
  registered outside the list; no OFFICE data of an unapproved class reaches a worker route or Claude Max (an
  OFFICE request without a project has no classification and is refused). Control Center: the three fields under "Ini otorisasi perusahaan untuk
  OFFICE" on **Keputusan**; CLI: repeatable `--approved-target`, `--approved-data-class` and
  `--approved-identity` on `aeos policy decide --office-authorization`, plus `--office-policy KEY=VALUE` for the
  overrides. Single-quote every value that contains a comma (`--approved-target
  'confluence:tenant=<site>.atlassian.net,space_id=<id>'`): Windows PowerShell splits an unquoted comma into
  separate arguments. `aeos doctor -c CAN_USE_OFFICE_INTEGRATIONS --functional` compares the stored credential's
  account with the approved identity (`secret.office_identity`).
* **OFFICE worker route eligibility (§23):** after the authorisation lists the route in
  `approved_worker_routes`: Control Center **Keputusan** -> "Kebijakan OFFICE" -> "Jadikan route worker layak
  OFFICE", or `aeos policy office-route <route_key> --as <admin> --credential-ref office/<provider> --rationale
  "<why>"` (password-confirmed). The route becomes OFFICE-only; activation stays `aeos routes activate ...`.
* **Deployment targets (PERSONAL, observe-only):** **Portofolio** -> project -> **Integrasi** -> "Staging —
  Deployment target" / "Produksi — Deployment target": the https base URL and an optional health path (validated
  with the observer's rule). AEOS only GETs it (G6/G8, `aeos delivery observe-target`, `aeos docs screenshot`);
  deploying stays a human action. The GitHub Actions pipeline reference is "CI — GitHub Actions" on the same page.
* **Route feasibility (ARENA/COMPARATOR/LOCAL, ADR-0111):** activate the free route as TRIAL (owner decision),
  then `aeos broker probe-route <route_key>` / `POST /api/broker/routes/<key>/feasibility` sends one synthetic
  PUBLIC prompt and records PROBE evidence; the route never serves worker tasks and its status is unchanged.
  The probe sends the route's own request shape (`<endpoint>/chat/completions`, bare upstream model id; ADR-0041
  amendment 2026-10-25): OpenCode Zen `https://opencode.ai/inference/openai/v1`, Gemini
  `https://generativelanguage.googleapis.com/v1beta/openai`. After upgrading, run `aeos seed` once: it corrects
  untouched seeded CANDIDATE routes (new config hash); a route activated on the superseded endpoint is listed in
  `request_shape_pending` and refused by the broker (`ROUTE_ENDPOINT_SUPERSEDED`): run `aeos routes disable <key>`,
  then `aeos seed` again (a disabled seeded route gets the corrected request shape and stays DISABLED), then
  activate the corrected configuration.
* **Free OpenCode Zen routes (S07 client rule):** S07 says "Free-tier chat models require OpenCode; use paid models
  or Go from other clients". The broker is its own client, so a free Zen route is not activated or proxied until
  you decide: `aeos routes activate <key> ... --accept-client-terms --client-terms-note "<what you read>"` (recorded
  on the decision and in the config hash; whether the provider accepts the call stays NOT-PROVEN until a probe),
  or use OpenCode Go / a paid route with a BUDGET decision instead. `aeos routes list` flags undecided routes with
  `client-terms=OWNER_DECISION_REQUIRED`.
* **LOCAL route endpoint:** start an OpenAI-compatible server on this host, then `aeos routes set-endpoint
  local/qwen3.8-flash-next --endpoint http://127.0.0.1:<port>/v1 --as <admin> --rationale "..." [--model <served
  name>] [--quantization ...] [--backend ...] [--hardware ...]` (or the Models page form). Loopback `http` only;
  it is a new configuration, so activate a new TRIAL, then probe. A loopback LOCAL route needs no BUDGET decision.

## Worker runtime (sandbox, engine contract, attribution)

| Command / setting | What it does |
|---|---|
| `aeos worker status [--domain OFFICE]` | sandbox levels, the policy requirement for the domain, residual risks, OpenCode status |
| `aeos worker containment --kind process` | negative containment tests in a real child process (WEAK: NOT_ENFORCED items are residual risks) |
| `aeos worker containment --kind container [--domain OFFICE]` | the 20-check suite INSIDE the worker image (build it first: see `sandbox/README.md`); a fresh PASS for the current image is what lets admission select the container sandbox |
| `aeos worker contract-test` | real OpenCode against a scripted loopback model with a hostile repository `opencode.json`: request contract, output cap, tool list, project config ignored, outside-file denied, patch-only, watchdog, kill. Records `WORKER_CONTRACT_TEST` evidence (Doctor `worker.contract`); a real route's model behaviour stays NOT_PROVEN |
| `aeos worker reap` | remove worker containers whose run is no longer active (after a controller crash) |
| `commit_identities` (`aeos.yaml` in the OS config dir, see `config/aeos.example.yaml`, or `AEOS_COMMIT_IDENTITIES` as JSON) | §06 + owner rule (AGENTS.md invariant 11): the identity of each code host - `github` (your GitHub identity, e.g. `<id>+<user>@users.noreply.github.com`) and `bitbucket` (your company identity). Candidates of a project are committed with the identity of its code host (PERSONAL -> github, OFFICE or a registered Bitbucket repository -> bitbucket) as author AND committer. Unset (or invalid, or an AI/bot identity) for that host: the commit uses `AEOS Runtime <aeos-runtime@localhost>`, the run records `commit_identity.pushable = false` with the reason, and the broker refuses the push. The run record keeps the address minimised; it is in the commit only. See "Commit identity and no AI attribution" below |
| `commit_author_name` / `commit_author_email` (`AEOS_COMMIT_AUTHOR_NAME` / `AEOS_COMMIT_AUTHOR_EMAIL`) | backward-compatible fallback for the **github** identity only; `commit_identities.github` wins when both are set. Bitbucket never falls back |

Worker rules enforced by the runtime (ADR-0051 amendment 2026-10-19): no shell and no egress TaskSpec under
the process sandbox; R3 under the process sandbox only when the owner's exception lists
`run_worker_process_sandbox_r3`; R4 always BLOCKED (no VM sandbox); no process sandbox while AEOS runs
elevated; a repository carrying `opencode.json` / `.opencode/` is refused (`WORKTREE_AGENT_CONFIG`); an
out-of-scope edit stops the run immediately; memory/process caps (`limits.max_memory_mb`,
`limits.max_processes`) stop it with `RESOURCE_LIMIT`; a change to the running controller checkout fails and
quarantines the run (HIGH alert); a worker blocker (`.aeos/report.json`, schema
`contracts/worker/aeos-report.schema.json`) becomes an AGENT decision request and the task waits BLOCKED
(`WORKER_BLOCKER`) for the owner. A worker prompt template change changes `ModelRoute.prompt_revision`, which
invalidates route approvals bound to the old configuration hash (`PROMPT_REVISION_CHANGED`).

Wave 7 additions:

* **Tamper checks around every run** (§09 Zone 1): besides the checkout and interpreter, the bytecode the next
  start would load (`__pycache__` of the checkout and site-packages, and in release mode the
  `PYTHONPYCACHEPREFIX` tree under the data dir) and the owner config file (`AEOS_CONFIG` / `aeos.yaml`) are
  fingerprinted. A new/changed `.pyc` is accepted only when its code equals a fresh compile of its source
  (checked in a separate `python -I -S -B` process); anything else is a finding and the file is deleted. A changed
  `aeos.yaml` is restored to its pre-run bytes. Under the WEAK process sandbox a DB trust-table tamper window
  (decisions, budgets, routes, agents, policy snapshots, grants, ...) wraps the run: a row changed by another
  process fails and quarantines the run (`db-trust-table-modified`, HIGH alert `DB_TRUST_TAMPER`). The
  bookkeeping the server process does for a run when the executor runs in the CLI (`aeos work run-once`) is
  tolerated: the run's grant usage growing, a grant/session revoked, a session's `last_seen_at` moving forward.
  Anything else written by another AEOS process during the run (for example a decision recorded from the CLI, or
  a new sign-in) is reported as foreign: fail closed, review and reconcile. On `capability_grants` even this
  process's own broker writes mask nothing: a forged cap, scope or grant row is foreign in every case. If the
  executor itself fails after the worker started, the worker is stopped and the same checks still run.
* **Integrity hold**: any controller/config/bytecode/DB finding (from a worker run or a verification) sets the
  host-wide hold `worker_runtime.integrity_hold`: admission, the executor's start check, running attempts and
  `CAN_RUN_WORKER` are BLOCKED (`INTEGRITY_HOLD`) until the owner reviews the findings (`aeos worker status`),
  restores the checkout (`git status` / `git checkout -- <path>`) and records the reconciliation for exactly that
  hold: `aeos policy decide --kind RISK_ACCEPTANCE --target-type worker_integrity_hold --target-id <hold id> --as
  <SUPER_ADMIN> --domain <PERSONAL|OFFICE> --days 1 --rationale "<what you checked>"` (your explicit choice of
  domain; the hold is host-wide). A later finding needs a new one. A domain's views show the hold, but another
  domain's run id and findings only as a count (`aeos worker status --domain <that domain>` shows them). In the
  Control Center the hold is a red panel on **Pekerjaan Live** (and a banner on **Ringkasan**) with the findings,
  the command above and, for a SUPER_ADMIN, **Catat rekonsiliasi** - the same RISK_ACCEPTANCE through
  `POST /api/decisions`, bound to the hold id, in the domain of the session (two explicit steps, 1-day expiry).
* **CPU cap**: `limits.max_cpus` is a Windows Job Object CPU-rate hard cap in the process sandbox (`run.caps.cpu_cap`
  records the rate in force); on macOS/Linux it stays a reservation only. `limits.max_processes` is part of every
  TaskSpec (256), the same cap for both sandboxes.
* **Granted shell (container only)**: commands run only through the AEOS runner
  `node /aeos/aeos-run.js <program> [args...]` (bash permission patterns). The runner starts exactly one program
  with its arguments and never re-parses them with a shell, so a pattern grant such as `bash:npm test*` allows
  nothing more than before. The engine sees the first 40 and last 80 lines; the full output is collected as
  untrusted evidence (`worker-command-logs.json`) and removed from the candidate. Only runs that had the runner
  get this; in any other run a worker-written `.aeos/cmdlog/` is an ordinary (scope-checked) change.
* **Context**: a run without a CURRENT task-scoped ContextPack gets one built at start (knowledge facade, system
  principal bound to the TaskSpec, the TaskSpec's input artifacts as DATA items under the pack's token budget).
* **Doctor `worker.route_behaviour`**: per APPROVED/TRIAL worker route, the §43 measure `tool_schema_failures` of a
  trusted evaluation on the route's current configuration (+ verified runs). UNKNOWN until such runs exist, STALE
  after a configuration/engine change; FAILED only above the owner's threshold - a POLICY decision with target
  `worker_route_behaviour:tool_schema_failures` and `details.max_failure_rate` (0..1, `POST /api/decisions`; in
  the Control Center: Kesiapan -> the check -> **Tetapkan ambang pemilik**, PERSONAL session, entered as a percentage).
  Without it, measured failures are reported, never judged.

## Commit identity and no AI attribution (owner rule, AGENTS.md invariant 11)

The owner's standing rule for DLY AEOS and every project it creates or manages: every GitHub activity (commits as
author AND committer, pushes, PRs, issues, comments) uses the owner's GitHub identity; every Bitbucket activity
uses the owner's company identity. No AI appears on GitHub, Bitbucket or the company's Jira/Confluence - not as
author, committer, co-author or contributor, and not in any attribution line. Provenance (run id, agent, route,
model) stays in DLY AEOS records (run rows, EvidenceArtifact); it is not written to the code host.

**Owner step (required before any push, PR, issue or page goes out):**

1. Open your local `aeos.yaml` (OS config dir: `%LOCALAPPDATA%\DLY-AEOS\aeos.yaml` on Windows,
   `~/Library/Application Support/DLY-AEOS/aeos.yaml` on macOS, or the file `AEOS_CONFIG` names) and set both
   identities. The real addresses live only there. The repository is public and never contains them, so never
   copy this file into the checkout (`/aeos.yaml` and `config/aeos.yaml` are gitignored as a guard). Replace the
   placeholders; an address left as `<...>` is refused:

   ```yaml
   commit_identities:
     github:
       name: Your Name
       email: <id>+<username>@users.noreply.github.com   # your GitHub identity (noreply address recommended)
     bitbucket:
       name: Your Name
       email: you@company.example.com                    # placeholder: your company identity (a different address)
   ```

2. Run `aeos doctor` (or open Kesiapan in the Control Center). `git.commit_identity.github` must show
   **CONFIGURED**, and so must `git.commit_identity.bitbucket` once a Bitbucket repository is registered (until
   then it is NOT_APPLICABLE). MISSING means the key is absent (the remedy names the key, never an address);
   FAILED means the entry is invalid, including an AI or bot identity.
3. Until `git.commit_identity.github` is CONFIGURED, `CAN_PREPARE_PR` is **BLOCKED** and the broker refuses every
   push with `COMMIT_IDENTITY_MISSING`. OFFICE delivery readiness lists a missing or invalid company identity as a
   reason, and the Bitbucket broker refuses the push the same way.

CONFIGURED is not VERIFIED: reading `aeos.yaml` does not prove the address is linked to the account. Candidates
committed before this rule still carry the old `AEOS-Agent`/`AEOS-Route`/`AEOS-Engine` trailers and are refused at
push (`AI_ATTRIBUTION_IN_COMMIT`); re-run those tasks. The decision is recorded in
[ADR-0121](decisions/ADR-0121-code-host-identity-and-no-ai-attribution.md).

What enforces it:

| Where | Rule | Refusal code |
|---|---|---|
| Worker candidate commit (`worker_runtime`) | author = committer = the identity of the project's code host; the commit message names no agent, route, engine, model or prompt revision (no `AEOS-*` provenance trailers; the subject keeps `run <id8>`). Without a valid identity the runtime identity commits locally and `caps.commit_identity.pushable = false` (with the reason); a worker-made commit with another identity or AI attribution also marks it not pushable | - (recorded on the run) |
| Broker push (`git_broker` GitHub, `bitbucket_broker` Bitbucket) | before any push the broker reads the remote's branches into its own mirror (same credential as the push) and checks every commit the push would publish - every commit of the head that no remote branch reaches yet, so unpushed history below the run's base counts too, while history already on the remote (colleagues' commits) does not: author e-mail = committer e-mail = the configured identity, no AI/bot identity, no AI attribution in the message. More than 1000 such commits is refused (push the existing history yourself first). Refusals are audited (`DENIED`), the outbox row is FAILED, nothing reaches the remote. A remote whose branches cannot be read fails the push (FAILED, nothing sent) | `COMMIT_IDENTITY_MISSING`, `COMMIT_IDENTITY_MISMATCH`, `AI_ATTRIBUTION_IN_COMMIT`, `COMMIT_RANGE_UNBOUNDED` |
| PR title/body (delivery + broker, GitHub and Bitbucket) | checked before the branch is pushed and again before any credential is read | `AI_ATTRIBUTION_IN_TEXT` |
| Publisher GitHub Issues/labels, Jira issues, Confluence pages/folders | checked before the request; the operation is FAILED with the reason | `AI_ATTRIBUTION_IN_TEXT` |

The guard (`aeos.core.attribution`) refuses, it never strips: remove the attribution and send it again. It looks for
attribution, not for mentions: `Co-Authored-By:`/`Assisted-By:`/`Signed-off-by:`... naming an AI or a bot, `AEOS-Agent:`
/`AEOS-Route:`/`AEOS-Engine:`/`AEOS-Model:` trailers, `Model:`/`Route:`/`Reviewer:` trailers naming an AI, "Generated
with/by", "written with the help of", "AI-generated", the robot emoji, AI vendor and bot noreply addresses. A commit
subject such as `fix(claude_bridge): ...` or "route deepseek requests through the broker" is ordinary text. A
trailer naming a human called Claude is refused too (fail closed; reword it), and so is a co-author named with generic
AI wording ("AI Assistant", "an LLM"). Invisible characters, fullwidth forms and look-alike Cyrillic/Greek letters are
folded before matching, so an AI name split by a zero-width space is still found. The PR evidence bundle renders neutral
roles ("supervisor review", "worker run", "independent verifier"); the reviewer tool and model stay in the review
record and the bundle data.

## Scenarios tested as normal operations (§33)

| Scenario | Where it is handled |
|---|---|
| Worker crash / controller restart | [restart-recovery](runbooks/restart-recovery.md); lease reconciliation (workforce) |
| Broker outage / expired credential | fail closed (capabilities BLOCKED); executor: no grant -> `BROKER_UNAVAILABLE` before start, broker unreachable mid-run -> attempt FAILED (never DONE, no fallback); [credential-rotation](runbooks/credential-rotation.md) |
| Quota exhaustion | queue/backoff, no paid fallback; [queue-drain](runbooks/queue-drain.md) |
| Partial publication / provider outage | [publication-reconciliation](runbooks/publication-reconciliation.md) |
| Runaway run | [cancel-runaway-run](runbooks/cancel-runaway-run.md), emergency stop |
| Data loss / failed upgrade | [backup-restore](runbooks/backup-restore.md), [controller-upgrade-rollback](runbooks/controller-upgrade-rollback.md) |
| Model route change | [model-rollback](runbooks/model-rollback.md) |

## Open risks and unknowns (§51)

* MCI content is not readable — canonical compliance/READY that depends on it is not claimed.
* OFFICE use of personal Claude Max, external routes, data classes and trace destinations is not
  authorised.
* Provider enforcement of human-only merge: the platform repository is public on GitHub Free with a
  ruleset on `main` (ADR-0115), but a real bypass attempt against it has not been run, so enforcement
  is CONFIGURED, not proven. Private repositories of managed products on GitHub Free stay NOT_ENFORCEABLE.
* Worker containment, egress control, secret broker, verifier independence, cancel and bypass tests
  must pass on this machine before workers are trusted.
* The released controller artifact is a source archive run with the checkout's virtual environment
  (third-party packages are pinned by `requirements.lock`, not bundled). The UI bundle is pinned by
  hash at build time; that it was built from the released commit is VERIFIED only through the Vite
  build record of its clean source tree; an UNVERIFIED bundle is pinned only by the owner's explicit
  `--allow-unverified-ui` (shown per release). No release
  has been built or promoted on the build host yet (NOT-PROVEN). Releases built
  before acceptance gating show acceptance UNKNOWN at promotion.
* Backups are not encrypted by AEOS; encryption at rest/off-machine copies are owner decisions.
