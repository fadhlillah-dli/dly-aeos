# Runbook: Incident containment (emergency stop)

## Trigger

Anything risky is happening and must stop now: a worker writes outside its scope, a credential may
be abused, unexpected spend, data from OFFICE appears in a PERSONAL context, a candidate build tries
to modify policy/verifier/controller, or you simply do not understand what the system is doing.

## Authority

Owner (SUPER_ADMIN). §33: "Incident response menghentikan capability berisiko, menjaga bukti,
menentukan owner dan blast radius, memperbaiki akar masalah, lalu menambah regression/eval case."

## Steps

1. **Stop everything**:
   - Dashboard: *Riwayat & Pemulihan* → *Darurat* → enter the reason, type `HENTIKAN`, confirm.
     The server issues a single-use confirmation token bound to your account (valid 3 minutes).
   - CLI (works when the dashboard does not): `aeos ops emergency-stop --reason "..." --owner "you"
     --blast-radius "what may be affected"` and type `STOP`.
2. **Read the per-step report** — every step is reported separately and nothing is claimed that
   did not happen:
   | Step | What it does | If FAILED |
   |---|---|---|
   | pause_scheduling | no new run starts (verified by reading the state back) | stop the server (`Ctrl+C`) |
   | revoke_capabilities | every active broker grant revoked; remaining count reported | stop the server; rotate upstream credentials |
   | cancel_runs | RUNNING/LEASED runs cancelled (process tree killed) | kill processes manually (Task Manager / `kill`) |
   | preserve_evidence | one INCIDENT_CONTAINMENT evidence artifact per domain partition | export audit/evidence snapshots manually |
   | audit | audit row + CRITICAL alert (statuses only) | note the time and actions by hand |

   The stop acts on every domain, but the dashboard shows counts and run ids of the **active domain
   only** (a step that failed in the other domain still shows FAILED/PARTIAL with a neutral reason).
   Open the other domain's session, or run the CLI, for its details.
3. **Determine owner and blast radius**: which projects/domains, which credentials, which external
   targets (branches, PRs, documents, issues). Use *Riwayat & Pemulihan* → *Audit* filtered by time,
   and export snapshots (*Ekspor*) for the incident record — exports are scoped and redacted.
4. **Contain external effects**: cancel does not undo side effects that already happened. Close
   PRs / revert branches / restore documents deliberately; rotate exposed credentials
   ([credential-rotation](credential-rotation.md)).
5. **Fix the root cause** through the normal change process (proposal → TaskSpec → verification →
   human decision). Add a regression or evaluation case that would have caught it.
6. **Resume** scheduling explicitly only when the cause is understood.

## Verification

* The report status is `COMPLETED`; if `PARTIAL`/`FAILED`, the failed steps were handled manually.
* *Pekerjaan Live* shows no RUNNING/LEASED runs; *Ringkasan* shows the scheduler PAUSED.
* No active capability grant remains (`remaining_active` = 0).

## Evidence

* `ops.emergency_stop` audit row with step statuses, one `INCIDENT_CONTAINMENT` evidence artifact
  per domain (that domain's counts and run ids), CRITICAL alert `emergency_stop`, and
  `ops.emergency.last` (shown on *Ringkasan*).
