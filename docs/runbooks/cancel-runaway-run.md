# Runbook: Cancel a runaway run

## Trigger

A worker run keeps looping, its heartbeat continues without observed progress, it approaches its
token/cost/time cap, it touches paths outside its TaskSpec, or the verifier reports repeated
failures that the worker ignores.

## Authority

Owner (SUPER_ADMIN). Cancel stops processes and revokes access; it does **not** undo external side
effects that already happened (§35) — those need reconciliation.

## Steps

1. **Inspect first**: *Pekerjaan Live* → open the run (*Run Detail*): observed actions, touched
   paths, caps, usage, verifier status. Do not rely on the worker's own narrative.
2. **Cancel the run** from *Run Detail* (workforce module). Cancellation revokes the run's broker
   capability, kills the full process tree/sandbox and releases its budget reservation.
3. **If cancel fails or several runs misbehave**, use the emergency stop
   ([incident-containment](incident-containment.md)): it pauses scheduling, revokes every active
   capability and cancels all RUNNING/LEASED runs, reporting each step separately.
4. **Reconcile side effects**: branches pushed by the broker (`aeos/*` only), PRs opened, documents
   or issues created. Close or keep them deliberately; nothing is merged automatically.
5. **Record the cause**: add a regression/eval case or adjust caps in the TaskSpec/route policy
   through the normal decision process (§33 incident loop).

## Verification

* The run shows CANCELLED; its capability grant is revoked; no process of the run remains
  (Windows: Task Manager / `Get-Process`; macOS: `ps -ax | grep opencode`).
* `aeos ops sli --domain <D>`: `stale_leases` = 0.

## Evidence

* Audit + events for the cancel and grant revocation; the run's trace and evidence are preserved
  (evidence is append-only, never deleted).
