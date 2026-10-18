# ADR-0092 — SLIs from records, SLO targets as owner decisions, deduplicated alerts, emergency stop

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Operations & Reliability module

## Context

Master §33: "SLI/SLO ditentukan sesuai criticality, bukan angka availability generik. Monitoring
mencakup outcome, queue age, stale lease, retry, acceptance failure, budget drift, sync freshness dan
external integration health" and "Incident response menghentikan capability berisiko, menjaga
bukti ...". §35 requires alerts in the Control Center and domain-scoped projections.

## Decision

1. SLIs are computed deterministically from control-plane records per domain (queue age, stale
   leases, retry rate, acceptance failures, accepted outcomes, budget drift, unknown cost entries,
   sync freshness/problems, integration failures) plus the platform-level recovery point age.
2. There are no default targets. A target is recorded by the owner (`aeos ops sli target`, audited)
   in `SystemSetting ops.sli.targets`. Without a target an SLI is `NO_TARGET`; a target without data
   is `UNKNOWN`; only a measured value inside the target is `WITHIN_TARGET`.
3. Alerts are stored once per dedup key in `ops_alerts` and emitted as `alert.raised` /
   `alert.resolved` events carrying their domain. Objective anomalies (expired-but-active leases,
   revision conflicts, failing integrations, no restore-tested backup, failed promotion, emergency
   stop) alert regardless of targets; target breaches alert when a target exists.
4. The emergency stop runs fixed steps (pause scheduling, revoke all capabilities, cancel active
   runs, preserve evidence, audit) through the owning modules' facades, commits and reports each step
   separately, and never reports COMPLETED when a step failed or its module is missing. The API
   requires a single-use, account-bound, 3-minute confirmation token.

## Consequences

* The dashboard never shows a green SLO the owner has not defined.
* Periodic evaluation (`aeos.operations.loop`) needs registration in the app lifespan hooks;
  on-demand evaluation is always available (CLI/API).
