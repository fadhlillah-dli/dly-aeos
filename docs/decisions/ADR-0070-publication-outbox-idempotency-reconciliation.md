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

## Amendment (completeness review, 2026-10-18)

7. **One run per project at a time.** Execute and retry are refused (409 PUBLICATION_RUNNING) while
   another publication of the same project is RUNNING; human retry/reconcile never interleave with
   the executor pass of the same publication (409 PUBLICATION_BUSY).
8. **Unknown outcomes across runs/versions.** The idempotency key contains the approved hash, so a
   newer version gets new keys. A new preview is therefore refused (409 UNRESOLVED_OPERATIONS, with
   the publication ids to reconcile) when an earlier run's create/update on the same logical id is
   still UNKNOWN/IN_FLIGHT. The human reconciles that run first (read-only, by marker); the newer
   version then plans an update under revision control. An older run's resource is never adopted as
   if it carried the newer content, and never re-created.
9. **Preview drift.** Execute is refused (409 TARGETS_CHANGED) when a target the preview planned to
   create now has an ExternalReference, or an update's baseline revision changed. As defence in
   depth the executor never runs a create whose target appeared meanwhile (same content → SKIPPED,
   other content → CONFLICT; a new preview plans the update).
10. **Interlock denial mid-run** marks the op FAILED with `blocked.code=INTERLOCK_DENIED` and a DENIED
    audit entry (no silent PENDING); a human retry re-checks it.
11. **Bounded reconciliation.** A blocked or failing reconciliation backs off exponentially and counts
    toward `MAX_RECONCILE_FAILURES`; afterwards the op stays UNKNOWN for a human and the publication
    finalises (PARTIAL/FAILED) instead of polling forever. IN_FLIGHT/UNKNOWN ops keep a run open
    regardless of their attempt count. A request that cannot even be prepared (e.g. DOCX snapshot)
    is FAILED with "nothing was sent", not UNKNOWN.
12. **Crash recovery grace.** Startup recovery turns IN_FLIGHT into UNKNOWN but defers the first
    reconciliation until `RECOVERY_GRACE` after the last attempt; an IN_FLIGHT op younger than
    `IN_FLIGHT_STALE_AFTER` is never reconciled as absent (it may still run in `aeos publish run`).
13. **Retry needs the current approval.** A retry is a new human action: it is refused
    (409 APPROVAL_STALE) when the publication's version is no longer the approved one, even while the
    run's PUBLICATION decision has not expired.
14. **Reject.** `POST /api/publications/{id}/reject` (SUPER_ADMIN) cancels a PREVIEW (§05 step 9
    "Reject"); nothing is written externally and its outbox rows are adopted by a later preview.
15. Only files inside the AEOS data directory are ever embedded/uploaded from an outbox row.
