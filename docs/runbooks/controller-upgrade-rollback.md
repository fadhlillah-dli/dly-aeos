# Runbook: Controller upgrade (promotion) and rollback

## Trigger

A change to DLY AEOS itself was merged to `main` by a human (self-build, §09) and should become the
trusted controller; or the active controller release misbehaves and must be rolled back
(§50 "Upgrade controller gagal").

## Authority

Owner only, on the local machine. Promotion and rollback exist only as CLI commands — there is no
HTTP route, and they refuse to run inside a worker/candidate context. Each one records a
`PROMOTION` Decision bound to the exact artifact SHA-256; the `--operator` must be an active
SUPER_ADMIN account.

## Zones (§09)

| Zone | Where | Who changes it |
|---|---|---|
| Trusted released controller | `<data>/releases/<version>-<sha12>.zip` + `ACTIVE.json` | owner via `aeos ops release promote/rollback` |
| Candidate build workspace | branches/worktrees `aeos/*` | workers, within their TaskSpec |
| Accepted release | a commit on `main` merged by a human | owner (merge), then `aeos ops release build` |

## Steps — promotion

1. Make sure the working tree is clean and on the merged `main` commit:
   `git switch main && git pull --ff-only && git status`.
2. **Build**: `aeos ops release build` — refuses dirty trees and commits that are not reachable
   from `main`; writes the zip and `<release>.provenance.json` (commit, tree hash, artifact SHA-256,
   schema head, built_at, builder).
3. **Review** `aeos ops release list` (integrity must be OK).
4. **Promote**: `aeos ops release promote <release_id> --operator <your-username> --reason "..."`
   and type `promote`. The command:
   1. records the PROMOTION decision (fails closed if it cannot be recorded),
   2. checks compatibility of the release's migration head with the current database revision
      (a database newer than the release is refused — downgrades are not supported),
   3. takes a `pre-promote` backup and restore-tests it,
   4. smoke-tests the release in a separate process: imports the app factory from the extracted
      artifact, migrates a **copy** of the database, starts it on a temporary loopback port and
      requires `GET /api/health` → `status: ok`,
   5. runs live migrations only if the schema changed — this requires the server to be stopped
      (or `--force-maintenance`),
   6. updates `ACTIVE.json`, keeping the previous release as `previous`.
   Any failure leaves the old release active and raises a HIGH alert.
5. **Restart** the control plane so the promoted code is what runs.

## Steps — rollback

1. `aeos ops release rollback --operator <your-username> --reason "..."` and type `rollback`.
2. Rollback moves the pointer back to `previous` after the same integrity/compatibility/smoke
   checks. **Code rollback is not data rollback** (§40): if the database was migrated by the newer
   release, rollback is refused and tells you which `pre-promote` backup to restore first:
   `aeos ops backup restore <pre-promote backup id>` (see [backup-restore](backup-restore.md)),
   then run the rollback again.
3. Restart the control plane.

## Verification

* `aeos ops release list` shows the expected `ACTIVE` and `PREVIOUS` releases with integrity OK.
* After restart: `GET /api/health` reports the expected version; `aeos doctor` passes core checks.
* `ACTIVE.json` history contains the promote/rollback entry with decision id and pre-backup id.

## Evidence

* `PROMOTION` Decision rows (target = release id, hash = artifact SHA-256).
* Audit `ops.release.build`, `ops.release.promote`, `ops.release.rollback` with per-step results.
* The pre-promote backup and its `RESTORE_TEST` evidence.

## Known limits

* The promoted artifact is a source archive; the frontend bundle (`frontend/dist`) is built from
  the checkout by the bootstrap script. Serving the UI from the artifact itself is NOT-PROVEN.
* The smoke test proves the app factory starts and serves `/api/health` against a migrated copy of
  the database; it does not prove every feature works (run the test suite / CI for that).
