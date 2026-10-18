# Runbook: Backup, restore test and restore

## Trigger

* Scheduled backup (owner-defined cadence), before any upgrade/migration/bulk change, after
  important approvals — and **always** a restore test after creating a backup you rely on.
* Data loss or corruption in the live data directory; a failed controller upgrade that changed the
  database ([controller-upgrade-rollback](controller-upgrade-rollback.md)).
* The alert "Restore backup belum pernah diuji" (§50).

## Authority

Owner. Live restore is CLI-only, on the local machine, with a typed confirmation. No agent deletes
backups (§33); retention is a listing, deletion is manual.

## What a backup contains (and does not)

| Included | Excluded (separate recovery path, §26/§33) |
|---|---|
| `aeos.sqlite3` consistent online copy (login sessions removed) | OpenBao data, unseal keys, root token, recovery keys |
| `personal.zip` / `office.zip`: evidence, attachments, publications per domain | OS keyring entries, native Claude login |
| `platform.zip`: platform-level evidence (if any) | worker sandboxes, logs, temp files |
| `manifest.json`: files, sizes, SHA-256, row counts, schema revision, app version | files whose name/content looks like a credential (listed, value never recorded) |

## Steps

1. **Create**: `aeos ops backup create --label before-upgrade`
   (or *Riwayat & Pemulihan* → *Backup* → *Buat backup*).
2. **Verify hashes** (fast, not sufficient): `aeos ops backup verify <backup_id>`.
3. **Restore test** (required before the backup counts as ready):
   `aeos ops backup restore-test <backup_id>` — restores into an isolated temp directory under the
   data dir, runs `PRAGMA integrity_check`, compares row counts, opens every table with the
   application models and re-hashes every archived file. Only on success a `RESTORE_TEST` evidence
   artifact is recorded. A corrupt backup records no evidence.
4. **List / retention**: `aeos ops backup list` shows readiness (RESTORE_TESTED, NOT_RESTORE_TESTED,
   RESTORE_TEST_FAILED, RESTORE_TEST_UNRECORDED, INCOMPLETE) and retention status. Record the
   owner's policy with `aeos ops backup retention --keep-last 10 --keep-days 30`; expired backups
   are only *listed* — delete them manually if you decide to.
5. **Copy off-machine** (owner decision): copy the backup directory to encrypted external storage.
   The backup itself is not encrypted; it contains project data of both domains in separate
   archives — keep Office archives on company-approved storage only.
6. **Restore to live** (only a RESTORE_TESTED backup):
   1. Stop the server (`Ctrl+C` in the `aeos serve` terminal). The restore refuses while the port
      is busy or an `aeos serve` process is found, unless you pass `--force-maintenance`.
   2. `aeos ops backup restore <backup_id>` and type the backup id to confirm.
   3. A `pre-restore` backup of the current state is taken first; files newer than the backup are
      kept (nothing deleted); migrations run (`aeos db upgrade` logic).
   4. Start the server; everyone signs in again (sessions are not restored).
7. **Secrets after restore**: credentials are not in the backup. If OpenBao or the keyring was also
   lost, follow [openbao-unseal-recovery](openbao-unseal-recovery.md) and
   [credential-rotation](credential-rotation.md).

## Verification

* `aeos ops backup list` shows the backup as `RESTORE_TESTED`; *Ringkasan* → "Pemulihan" is
  VERIFIED (otherwise NOT-PROVEN).
* After a live restore: `aeos doctor` passes core checks; the project/plan you expected exists; the
  audit trail contains `ops.backup.restore` with the pre-restore backup id.

## Evidence

* `RESTORE_TEST` evidence artifact (E2, origin `restore-test`) bound to the backup id.
* `results.json` in the backup directory (append-only log of verifications and restore tests).
* Audit rows `ops.backup.create`, `ops.backup.verify`, `ops.backup.restore_test`, `ops.backup.restore`.
