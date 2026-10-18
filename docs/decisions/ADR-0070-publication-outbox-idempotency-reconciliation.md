# ADR-0070 — Publication outbox with idempotency keys and reconcile-before-retry

* Status: PROPOSED (implementation decision inside master §14/§29/§41; ACCEPTED when merged under ADR-0003)
* Date: 2026-10-18
* Deciders: seed-build implementer (Claude Code, native Max) for the owner; owner review on merge

## Context

Master §14 and §41: there is no global transaction across git, the AEOS database, Google Docs,
Confluence, Jira and GitHub. Side effects must use an outbox, operation identity, optimistic
concurrency, target/hash binding and reconciliation; exactly-once delivery across APIs is never
claimed. §29: "Jika request create timeout dan hasil tidak diketahui, lakukan rekonsiliasi dahulu
agar retry tidak membuat duplikat." §50 requires: PARTIAL with successes kept, reconcile before
retry after a provider timeout, the approved version kept even when a newer revision appears.

## Decision

1. A `Publication` is created only from the PlanVersion bound by an ACTIVE approval decision
   (`planning.facade.get_approved_version`, never "latest"). The preview stores the complete list of
   concrete operations as `PublicationOperation` rows (the outbox) with targets and payload snapshot.
2. Idempotency key = `content_hash(project, provider, logical_id, operation, approved_hash)`; the
   key is globally unique, so one outbox row exists per side effect forever. A re-preview of the same
   version supersedes an unexecuted preview and *adopts* earlier non-succeeded rows (history kept in
   `response.history`); UNKNOWN rows stay UNKNOWN so they are reconciled first.
3. Per-operation states: PENDING, IN_FLIGHT, SUCCEEDED, FAILED, UNKNOWN, CONFLICT, SKIPPED.
   A timeout/transport error/5xx on a write, or a crash while IN_FLIGHT, is UNKNOWN. UNKNOWN is
   reconciled by marker (Drive `appProperties`, issue-body marker, Jira label, Confluence content
   property) before any retry; if absence cannot be proven the row stays UNKNOWN for a human.
4. Retries are bounded (3 attempts, exponential backoff 30 s → 15 min cap); a human retry grants a
   further bounded budget. Operations run in dependency order (project folder → node folders →
   documents; labels → issues) and never fall back to another parent location.
5. Publication status: PUBLISHED only when every op succeeded/was skipped; otherwise PARTIAL,
   CONFLICT or FAILED — successes are kept and never recreated. Plan state transitions
   PUBLISHING → PUBLISHED / PUBLICATION_PARTIAL / PUBLICATION_FAILED are requested through
   `aeos.planning.states.transition` when present, else applied with a legal-transition table; if the
   plan already moved on (new revision) it is not overwritten.
6. One background executor (`aeos.publisher.worker`) processes RUNNING publications; each publication
   is locked in-process and each op is claimed by a conditional update. Startup marks IN_FLIGHT rows
   UNKNOWN (crash recovery, §14).

## Consequences

* Duplicate external resources are prevented by reconciliation, not by assuming idempotent provider
  APIs. A provider without a marker search cannot be supported without new design work.
* Unchanged documents (same content hash as the last publication) are SKIPPED, so documentation is
  not regenerated/republished on every edit (§28, §41).
* Superseded documents are not deleted externally; deletion would need its own decision flow.
* Real-provider behaviour (eventual consistency, quota) is NOT-PROVEN until exercised against real
  accounts; tests use respx-mocked HTTP.
