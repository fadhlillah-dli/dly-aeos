# ADR-0074 — One human PUBLICATION decision per run, bound to version + hash + preview

* Status: PROPOSED (cross-module contract with aeos.policy; ACCEPTED when merged under ADR-0003)
* Date: 2026-10-18
* Deciders: seed-build implementer (Claude Code, native Max) for the owner; owner review on merge

## Context

U-10: external publication uses a human approval of the concrete version and targets; one approval
may cover the clear operations of one publication/run without re-prompting per API call. §26: the
external-write interlock requires actor AUTH, bootstrap complete, valid provider/domain/target,
write enablement, the approved immutable version/hash and an unexpired capability. §14: approval is
bound to `approved_plan_version_id`, target, manifest hash and operation scope.

## Decision

1. "Approve & Publish" (`POST /api/publications/{id}/execute`, SUPER_ADMIN) requires the human to
   confirm the exact `plan_version_id`, `approved_hash` and `preview_hash` shown in the preview; a
   stale view is refused (409 STALE_PREVIEW) and so is a plan whose approved version changed
   (409 APPROVAL_STALE).
2. It records one Decision via `policy.facade.record_decision`:
   `kind=PUBLICATION`, `target_type=publication`, `target_id=<publication id>`,
   `target_version=<plan_version_id>`, `target_hash=<approved manifest hash>`,
   `allowed_operations=<operation kinds of the run>`, `auth_scope={publication_id, preview_hash,
   project_id, domain, providers, operation_ids, targets}`, `expires_at=now+24h`.
3. `policy.facade.check_external_write` must allow EVERY operation (WriteIntent carries
   `decision_id`, `approved_plan_version_id`, `approved_hash`, `capability_expires_at`,
   `extra.publication_id/operation_id/idempotency_key/preview_hash`). Any denial → nothing runs, the
   decision is invalidated, a DENIED audit event is persisted, the API returns 403 INTERLOCK_DENIED.
4. The executor re-evaluates the interlock (non-interactively) before each write, so revocation or
   expiry stops the run; a human retry after expiry re-confirms and records a new decision.
5. A missing policy/planning implementation fails closed (Blocked), never "allowed".

## Consequences

* The policy module's interlock should validate exactly this binding (kind, target id = publication,
  target_version = approved version, target_hash = approved hash, operation ∈ allowed_operations,
  project/domain, ACTIVE, unexpired, human actor).
* Model/agent output can never create this decision: it is recorded only from an authenticated
  SUPER_ADMIN session with CSRF.
