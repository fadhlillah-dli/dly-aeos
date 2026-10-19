# Runbook: Model route rollback

## Trigger

A model alias or provider route changed (§50 "Model alias/provider route berubah"), a newly
activated route produces failed verifications or scope escapes, a free route expired or started
charging, or a provider's terms/data handling changed.

## Authority

Owner. Route activation and deactivation are human decisions (`ROUTE_ACTIVATION` Decision rows);
workers never select their own model, and there is no silent paid fallback (§23, §42).

## Steps

1. **Contain**: if runs on the route are misbehaving, pause scheduling ([queue-drain](queue-drain.md))
   or cancel the affected runs ([cancel-runaway-run](cancel-runaway-run.md)).
2. **Identify the last accepted configuration**: *Model & Rute* shows each route's status,
   resolved model version (UNKNOWN when an alias hides it), evaluation results and the decision
   that activated it. *Riwayat & Pemulihan* → *Keputusan* shows the activation history. Every APPROVED
   activation records its `rollback_target` (the previous approved configuration hash with a config
   snapshot in that decision, or "disable the route" for a first approval) — shown as *Rollback* in the
   route drawer.
3. **Disable the bad route** and re-activate the previous approved route through the routing
   module (`aeos routes --help`; UI *Model & Rute*). Record the rationale; the change is a new
   decision, history is not rewritten.
4. **Invalidate dependent readiness**: the doctor marks route checks STALE on configuration change;
   re-run `aeos doctor` before resuming.
5. **Re-evaluate**: results produced under the changed route are recorded with that configuration
   and do not satisfy the old baseline automatically (§50). Re-run the relevant evaluation suite
   (`aeos eval start|bind|collect`) before trusting the route again: an APPROVED re-activation needs
   `--eval-run` ids of completed, gate-passing runs of exactly the new configuration; until then only
   TRIAL is possible.
6. **Resume scheduling**.

## Verification

* New runs show the restored route key and resolved model in *Run Detail*.
* Verifier pass rate returns to the previous baseline on the same suite; no paid usage appears in
  *Biaya & Kuota* unless an explicit budget decision exists.

## Evidence

* Decision rows for deactivation/activation, doctor results, evaluation run ids and the usage
  ledger for the affected period.
