# ADR-0090 — Backups count as ready only after a recorded restore test

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Operations & Reliability module

## Context

Master §33: "Control plane mempunyai backup metadata/artifact dan recovery procedure; restore
benar-benar diuji sebelum mengklaim backup siap" and "OpenBao recovery material, data store backup,
dan integrasi credential recovery dipisah". §50: "Restore backup belum pernah diuji → Tidak
mengklaim recovery siap". §04 keeps Personal and Office evidence/cache in separate partitions. The
control plane is a single SQLite database (WAL) plus files under the OS data directory.

## Decision

1. A backup is a directory `<data>/backups/<id>/` with a SQLite online-backup copy (login sessions
   deleted from the copy), one zip archive per partition (PERSONAL, OFFICE, PLATFORM) of evidence,
   attachments and publications, a `manifest.json` (files, sizes, SHA-256, row counts, schema
   revision, app version, exclusions) and an append-only `results.json`.
2. Excluded by design: OpenBao data and unseal/root/recovery material, OS keyring entries, native
   Claude login state, worker sandboxes, logs, temp files, and any file whose name or content
   matches a credential pattern (listed in the manifest without the value).
3. Readiness states: NOT_RESTORE_TESTED → RESTORE_TESTED only after a restore test restored the
   copy into an isolated temp directory, passed `PRAGMA integrity_check`, matched row counts, read
   every model table and re-hashed every extracted file, **and** recorded a `RESTORE_TEST` evidence
   artifact (origin `restore-test`, E2) through `aeos.verification.facade`. A passed test whose
   evidence could not be recorded is RESTORE_TEST_UNRECORDED (not ready). Corrupt backups never
   produce evidence.
4. Live restore is CLI-only, refuses while the server is running (unless `--force-maintenance`),
   requires a RESTORE_TESTED backup, takes a pre-restore backup (raw copy if the live DB is damaged)
   and never deletes newer files.
5. Retention is an owner policy that only labels backups; nothing deletes backups automatically.
6. A backup is one platform-level unit, but every dashboard view of it is domain-scoped (§35, §50
   "termasuk counts"): readiness stays global (a failed OFFICE archive makes the backup FAILED for
   everyone), while per-partition counts, sizes and problem texts are shown only for the active
   domain. Platform-level records (audit rows, `RESTORE_TEST` evidence) carry no per-domain counts.
   The local CLI shows the full view.

## Consequences

* The dashboard shows recovery as NOT-PROVEN until a restore test passes; an SLI and an alert track
  the age of the newest restore-tested backup.
* Backups are not encrypted by AEOS; off-machine encrypted copies are an owner decision.
* Restoring a backup restores the decision/audit history of that point in time; the pre-restore
  backup preserves the newer history.
