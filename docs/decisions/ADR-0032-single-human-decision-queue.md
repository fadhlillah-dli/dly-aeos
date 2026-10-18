# ADR-0032 — One Human Decision Queue projected from canonical records

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed builder under ADR-0003

## Context

U-12 requires one Control Center and one Human Decision Queue with no competing state, authority or
tracker. §19/§21 let workers and deterministic services stop with a blocker or decision request; §35
requires domain/project scope to be enforced by the backend and Office data never to appear in a
Personal view.

## Decision

1. `aeos.policy.queue.queue_items(session, domain, project_ids)` is a read-only projection over the
   canonical tables: plans waiting for a human (READY_FOR_HUMAN_REVIEW, NEEDS_DECISION,
   AWAITING_MANUAL_CLAUDE, REVIEW_FAILED, PLANNING_FAILED, PUBLICATION_PARTIAL/FAILED), publications in
   PREVIEW, change proposals in SUBMITTED/REVIEWED/PROMOTION_PENDING, task executions BLOCKED with
   NEEDS_DECISION, worker runs in REVIEW that need a human (NEEDS_HUMAN/NEEDS_DECISION or verdict
   BLOCKED/NOT-PROVEN), ACTIVE decisions expiring within 7 days, and open decision requests.
2. Each item states kind, title, exact target (id/version/hash), why it waits, the requested decision,
   the link to act on, and its age. Items are ordered by priority, then age.
3. Decision requests (`decision_requests` table, `aeos.policy.facade.request_decision`) are DATA: they
   grant nothing and are resolved only by a separate human Decision or declined with a rationale.
4. Scope: the active domain only; platform items without a domain belong to the PERSONAL platform
   project (§09); the principal's project scope filters further.

## Consequences

* No module keeps its own "pending approvals" list; the dashboard queue, the decisions page and the
  overview all read the same projection.
* New waiting states in other modules become visible by adding a source here, not a new tracker.
