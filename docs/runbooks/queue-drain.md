# Runbook: Queue drain

## Trigger

Planned maintenance (backup restore, controller upgrade, OS update), quota exhaustion on a free
route (§50 "Free quota habis / 429"), budget near its hard stop, or the SLI `queue_age_seconds` /
`stale_leases` is outside the owner's target.

## Authority

Owner (SUPER_ADMIN). Draining never cancels accepted work and never switches to a paid route.

## Steps

1. **Pause scheduling** so no new run is leased: Control Center → *Pekerjaan Live* → pause
   scheduling (workforce module). If the workforce UI is unavailable, an emergency stop also pauses
   scheduling but additionally cancels active runs — use it only if that is intended.
2. **Let active runs finish** within their caps, or cancel the ones that must stop
   ([cancel-runaway-run](cancel-runaway-run.md)). Watch *Pekerjaan Live* until no run is
   RUNNING/LEASED.
3. **Check the queue**: `aeos ops sli --domain PERSONAL` shows `queue_age_seconds` and
   `stale_leases`. Stale leases must reach 0 (expired leases are reconciled, not reused).
4. **Do the maintenance** (backup, upgrade, ...).
5. **Resume scheduling** explicitly from *Pekerjaan Live* when the reason for draining is resolved.
   Quota-exhausted work waits for the provider reset or an already-approved free fallback; a paid
   fallback needs a separate owner decision (§42).

## Verification

* No run in RUNNING/LEASED before maintenance; `stale_leases` = 0.
* After resume, `queue_age_seconds` decreases and new runs show fresh heartbeats.

## Evidence

* Audit/events for pause and resume (`workforce.*`), SLI snapshots before/after
  (`aeos ops sli --domain <D> --json`).
