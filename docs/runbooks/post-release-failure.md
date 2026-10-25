# Runbook: Post-release failure of a product project (G8)

## Trigger

`aeos delivery observe-target <project_id> --environment production --sha <merge commit>` (or
`POST /api/delivery/targets/observe`) recorded a **FAIL** observation of the registered production target: the
target reports the merge commit but its health, a smoke / critical-flow check, an owner SLI threshold or the
required artifact digest failed. AEOS then raised the HIGH alert `post_release_failure`, opened (or linked) an
incident and filed a decision request in the Human Decision Queue.

## Authority

Owner (SUPER_ADMIN) only. §40: production, destructive or privileged actions follow human AUTH; AEOS never
deploys and never rolls back. The incident owner is the human who ran the observation.

## Steps

1. **Read the evidence**: *Riwayat & Pemulihan* → *SLI, alert & flag* → alert `post_release_failure`; open its
   incident (*Riwayat* → *Insiden*). The timeline names the RUNTIME_OBSERVATION evidence, one SMOKE_TEST per
   failing check and the recommendation:
   * `ROLLBACK_RECOMMENDED` — your own rollback criteria (`rollback_on` of the production target) are met;
   * `CRITERIA_NOT_MET` — a failure was seen, but not one you listed as a rollback condition;
   * `UNDECIDED` — you have not recorded rollback criteria yet (see step 5).
2. **Decide** in *Keputusan* (`/decisions`), on the request "Pasca-rilis gagal: ...":
   * **Rollback**: roll the deployment back yourself on the project's own platform (its deploy tool, release
     page or previous image). Then decline the request with a note that names what you rolled back to.
   * **Forward-fix**: file the fix through the normal change process (plan → TaskSpec → verification → your
     merge). Decline the request with a note that names the follow-up.
   * **Accept the risk**: record a `RISK_ACCEPTANCE` decision with an expiry for this request
     (`POST /api/decisions` with `request_id`).
3. **Observe again** after your action: `aeos delivery observe-target <project_id> --environment production --sha
   <commit now served>`. A PASS of the same commit resolves the alert; the incident stays open for you.
4. **Close the incident**: evidenced root cause, corrective action and a regression test or eval case
   (`/api/ops/incidents/{id}/...`); see [incident-containment](incident-containment.md) when anything must stop
   right now.
5. **Record rollback criteria** for next time (owner values, never defaults): `PUT /api/delivery/targets/
   {integration_id}/checks` with `rollback_on` (any of `health_fail`, `smoke_fail`, `critical_flow_fail`,
   `sli_breach`, `artifact_digest_mismatch`), or `aeos delivery target-checks <project_id> --environment
   production --file checks.json`.

## Verification

* The decision request is RESOLVED (risk accepted) or DECLINED with your note (rollback / forward-fix).
* A new production observation of the commit the target now serves is PASS; gate G8 shows it.
* Code rollback does not undo data changes (§31): if a migration ran, check the data state explicitly.

## Evidence

* `RUNTIME_OBSERVATION` (aggregate) and one `SMOKE_TEST` / `PROBE` per check, HIGH alert
  `post_release_failure` (dedup per project + commit), the incident timeline with the recommendation, the
  decision request and your decision / decline note, and the audit rows `delivery.target.observe`,
  `ops.incident.open`, `decision.request.create`.
