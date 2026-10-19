# Runbooks (master §33)

Operational procedures for the DLY AEOS control plane. Every runbook has the same shape:
**Trigger** (when to use it), **Authority** (who may run it), **Steps**, **Verification** (how you
know it worked) and **Evidence** (what is recorded). The Control Center lists these files through
`GET /api/ops/runbooks`.

| Runbook | Use when |
|---|---|
| [restart-recovery](restart-recovery.md) | The controller crashed, the machine rebooted, or the server will not start |
| [credential-rotation](credential-rotation.md) | A token/key expires, leaks, or is rotated on schedule |
| [queue-drain](queue-drain.md) | You need to stop new work cleanly (maintenance, upgrade, quota) |
| [cancel-runaway-run](cancel-runaway-run.md) | A worker run loops, overspends, or ignores its caps |
| [publication-reconciliation](publication-reconciliation.md) | A publication is PARTIAL/CONFLICT/UNKNOWN or a provider timed out |
| [backup-restore](backup-restore.md) | Taking, testing or restoring backups |
| [model-rollback](model-rollback.md) | A model route/alias/provider changed or a new route misbehaves |
| [incident-containment](incident-containment.md) | Anything risky is happening and must stop now (emergency stop) |
| [openbao-unseal-recovery](openbao-unseal-recovery.md) | OpenBao is sealed, lost its token, or must be recovered |
| [controller-upgrade-rollback](controller-upgrade-rollback.md) | Promoting a new controller release or rolling it back |
| [domain-migration](domain-migration.md) | A project must move between PERSONAL and OFFICE (migration with an owner decision; the domain is never edited) |

Rules that apply to every runbook:

* Humans hold AUTH. No agent deletes data, production resources or backups autonomously (§33).
* Commands are run from the repository root: `.\aeos.ps1 <cmd>` on Windows, `./aeos <cmd>` on
  macOS/Linux. The examples below write `aeos <cmd>`.
* Never paste secrets into commands, tickets, chat or logs. Use `aeos secrets set ... --stdin`.
* UNKNOWN / NOT-PROVEN is not a pass. If a verification step cannot be performed, record it as
  NOT-PROVEN and keep the capability blocked.
