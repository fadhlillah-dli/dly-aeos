# Runbook: Controller restart and recovery

## Trigger

The control plane crashed, the machine rebooted, `aeos serve` refuses to start (port in use,
database error), or the dashboard shows the event stream as disconnected/STALE for a long time.

## Authority

Local operator (the owner on this machine). No external writes are performed by this runbook.

## Steps

1. **Check for a still-running instance.** `aeos serve` refuses to start when the port is taken and
   never binds another interface (§06):
   - Windows: `Get-NetTCPConnection -LocalPort 8787 -State Listen | Select-Object OwningProcess`
   - macOS: `lsof -nP -iTCP:8787 -sTCP:LISTEN`
   Stop the old process if it is a stale AEOS server, or start on another port with
   `aeos serve --port 8790`.
2. **Run the bootstrap doctor** (no server or LLM needed): `aeos doctor`. Fix MISSING/FAILED core
   items first (Python dependencies, database path permissions, disk space).
3. **Database health.** `aeos db status` shows current vs head revision. If the database is behind,
   `aeos db upgrade` takes a pre-upgrade backup automatically before migrating.
4. **Start the server**: `aeos serve` and sign in at http://127.0.0.1:8787.
5. **Reconcile work state before anything new starts.** Worker runs that were RUNNING/LEASED when
   the controller died are not restarted blindly (§50 "Controller crash dan worker lama masih
   aktif"): open *Pekerjaan Live*, check runs with a stale heartbeat, and let the scheduler's lease
   reconciliation mark them; cancel anything you do not recognise
   ([cancel-runaway-run](cancel-runaway-run.md)).
6. **Reconcile external side effects.** Publications that were IN_FLIGHT may have created
   resources: follow [publication-reconciliation](publication-reconciliation.md) before retrying.
7. **Evaluate SLIs and alerts**: `aeos ops sli --domain PERSONAL --raise-alerts` (and OFFICE if
   used). Stale leases and integration failures show up as alerts.

## Verification

* `GET /api/health` returns `{"status": "ok"}` and the header shows "Stream live".
* *Ringkasan* shows no new blockers caused by the restart; stale leases = 0 after reconciliation.
* No run is shown as RUNNING without a fresh heartbeat.

## Evidence

* Audit rows: `identity.login`, scheduler/lease reconciliation events, `ops.*` actions taken.
* SLI evaluation result (alerts raised/resolved are `alert.raised` / `alert.resolved` events).
