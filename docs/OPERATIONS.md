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
| `aeos ops release build [--ref HEAD]` | `git archive` of a clean commit reachable from `main` → zip + provenance (commit, tree hash, artifact SHA-256, schema head, built_at, builder) |
| `aeos ops release list` | releases with integrity and ACTIVE/PREVIOUS role |
| `aeos ops release promote <id> --operator <admin>` | decision → compatibility (migration head vs DB) → pre-promote backup + restore test → smoke test in a subprocess on a temp loopback port against a DB copy → live migration only if required (server stopped) → `ACTIVE.json` (previous kept) |
| `aeos ops release rollback --operator <admin>` | pointer back to the previous release after the same checks; refused when the DB is ahead (restore the pre-promote backup first) |

Any failure keeps the previous release active, raises a HIGH alert and is audited
(§50 "Upgrade controller gagal"). Details: [runbook](runbooks/controller-upgrade-rollback.md).

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
* **Database migrations of managed projects:** the verifier does not run upgrade/downgrade/restore
  round-trips (`migration_rollback` NOT-PROVEN). Run them in the project's own pipeline with a
  dedicated migration credential that is separate from the runtime credential, and keep the result
  as CI evidence.
* **Verifier isolation:** every VERIFICATION_REPORT states `verifier_isolation: WEAK` (same OS user,
  no container/egress enforcement) until a sandboxed runner exists.
* **OFFICE delivery:** NOT_APPLICABLE — no Bitbucket push/PR/Pipelines adapter and no company
  authorisation; `prepare-pr` refuses with `OFFICE_DELIVERY_NOT_AVAILABLE`.

## Scenarios tested as normal operations (§33)

| Scenario | Where it is handled |
|---|---|
| Worker crash / controller restart | [restart-recovery](runbooks/restart-recovery.md); lease reconciliation (workforce) |
| Broker outage / expired credential | fail closed (capabilities BLOCKED); [credential-rotation](runbooks/credential-rotation.md) |
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
* The released controller artifact is a source archive; serving the UI from the artifact and
  running the controller directly from the extracted release are NOT-PROVEN.
* Backups are not encrypted by AEOS; encryption at rest/off-machine copies are owner decisions.
