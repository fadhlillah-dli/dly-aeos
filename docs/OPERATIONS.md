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

Secrets are never in these files: OpenBao + OS credential store only (§26).

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
| `aeos ops release build [--ref HEAD]` | `git archive` of a clean commit reachable from `main` **that is accepted** (trusted provider-CI PASS for that exact commit, or an owner EXCEPTION decision) → zip + provenance (commit, tree hash, artifact SHA-256, schema head, acceptance basis, built_at, builder) + pinned UI bundle (`frontend/dist` zipped with its own SHA-256; its correspondence to the commit is labelled UNVERIFIED) |
| `aeos ops release accept --commit <sha> --operator <admin> --reason "..."` | human-only EXCEPTION decision bound to one exact commit, used when trusted CI PASS is not available |
| `aeos ops release list` | releases with integrity and ACTIVE/PREVIOUS role |
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
  `config.health_path`, default `/health`). AEOS never deploys; `aeos delivery observe-target
  <project> --environment staging --sha <commit>` performs one GET and records a RUNTIME_OBSERVATION
  that is PASS only when the target reports the exact commit and healthy.
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
  migration credential separate from the runtime credential.
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
  containment proof removed, write enablement off; audit `verification.tamper.neutralize`); the alert lists what
  still needs your review (removed rows, schema, a modified existing account, ...) with the remedy. If a listed
  decision was your own (recorded from another AEOS process during the verification), record it again.
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
  (1) a company-authorisation POLICY decision on `office_policy` listing `bitbucket` whose
  `office_policy.atlassian_deployment` is `"CLOUD"` (the interlock needs it too), (2) the project
  integration `bitbucket/code` with `root_id=<workspace>/<repo_slug>` and `config={"enabled": true}`, (3) the
  registered Bitbucket repository VERIFIED (`POST
  /api/repositories/<id>/verify`), (4) `office/bitbucket` stored: `{"access_token": "..."}` (repository access
  token) or `{"email": "...", "api_token": "..."}` (Atlassian API token; optional `git_username`), and (5)
  `CAN_USE_OFFICE_INTEGRATIONS` READY. Data Center (`atlassian_deployment: "DATA_CENTER"`) is `NOT_SUPPORTED`. The broker then pushes the accepted head
  sha-pinned to `aeos/*` and opens a draft PR; nothing merges, declines or approves. Pipelines/commit-status CI
  becomes provider-ci evidence (pipeline reference on `bitbucket-pipelines/ci`: step names per gate). Without
  Bitbucket Premium's "enforce merge checks" the merge gate is NOT_ENFORCEABLE. G7 release approvers for OFFICE:
  `auth_scope={"bitbucket_accounts": ["<Atlassian account id>"]}`. Live tenant behaviour is NOT-PROVEN (M5).
* **Route feasibility (ARENA/COMPARATOR/LOCAL, ADR-0111):** activate the free route as TRIAL (owner decision),
  then `aeos broker probe-route <route_key>` / `POST /api/broker/routes/<key>/feasibility` sends one synthetic
  PUBLIC prompt and records PROBE evidence; the route never serves worker tasks and its status is unchanged.

## Worker runtime (sandbox, engine contract, attribution)

| Command / setting | What it does |
|---|---|
| `aeos worker status [--domain OFFICE]` | sandbox levels, the policy requirement for the domain, residual risks, OpenCode status |
| `aeos worker containment --kind process` | negative containment tests in a real child process (WEAK: NOT_ENFORCED items are residual risks) |
| `aeos worker containment --kind container [--domain OFFICE]` | the 20-check suite INSIDE the worker image (build it first: see `sandbox/README.md`); a fresh PASS for the current image is what lets admission select the container sandbox |
| `aeos worker contract-test` | real OpenCode against a scripted loopback model with a hostile repository `opencode.json`: request contract, output cap, tool list, project config ignored, outside-file denied, patch-only, watchdog, kill. Records `WORKER_CONTRACT_TEST` evidence (Doctor `worker.contract`); a real route's model behaviour stays NOT_PROVEN |
| `aeos worker reap` | remove worker containers whose run is no longer active (after a controller crash) |
| `commit_author_name` / `commit_author_email` (`aeos.yaml` in the OS config dir, see `config/aeos.example.yaml`, or `AEOS_COMMIT_AUTHOR_NAME` / `AEOS_COMMIT_AUTHOR_EMAIL`) | §06 attribution of candidate commits: set the owner's GitHub noreply address (`<id>+<user>@users.noreply.github.com`). Unset, commits use `AEOS Runtime <aeos-runtime@localhost>` and the run records `commit_identity.source = runtime-default (not linked to an account)` (the run record keeps the address minimised; it is in the commit only). Every candidate commit carries `AEOS-Run-Id`, `AEOS-Task`, `AEOS-Agent`, `AEOS-Route`, `AEOS-Engine` and `AEOS-Prompt-Revision` trailers |

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
* Provider entitlements for human-only merge enforcement (e.g. branch protection on GitHub Free
  private repositories) are not proven on the real account.
* Worker containment, egress control, secret broker, verifier independence, cancel and bypass tests
  must pass on this machine before workers are trusted.
* The released controller artifact is a source archive run with the checkout's virtual environment
  (third-party packages are pinned by `requirements.lock`, not bundled). The UI bundle is pinned by
  hash at build time, but that it was built from the released commit is UNVERIFIED. Releases built
  before acceptance gating show acceptance UNKNOWN at promotion.
* Backups are not encrypted by AEOS; encryption at rest/off-machine copies are owner decisions.
