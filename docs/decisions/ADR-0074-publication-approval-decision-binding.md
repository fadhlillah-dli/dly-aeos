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

## Amendment (completeness review, 2026-10-18)

6. A human retry always re-checks that the publication's version is still the approved one
   (APPROVAL_STALE otherwise); with an expired run decision it also re-confirms version, approved
   hash and preview hash and records a new PUBLICATION decision for exactly those values.
7. An interlock denial before a single write is persisted per operation (FAILED,
   `INTERLOCK_DENIED`) and audited with result DENIED.

## Amendment 2026-10-18 (integration)

Integration of planning, policy and publisher (seam W1). The planning APPROVAL, the publisher's per-run
PUBLICATION decision and `check_external_write` disagreed: the publisher handed the interlock a
PUBLICATION decision, the interlock accepted only an APPROVAL, approved targets used free-form keys the
publisher's writes never carried, and several outbox operations (`ensure_label`, `upload_attachment`,
`create_doc` on Confluence) were unknown to the interlock. Reconciled into ONE model (§05 step 9-10, §14,
§26, U-10):

8. **Plan approval = version + hash + targets + operation kinds.** "Approve & Publish" on the plan
   (`APPROVAL`, `auth_scope.action = APPROVE_AND_PUBLISH`) binds the exact PlanVersion id + manifest hash,
   the explicit targets as `{provider, root_id}` (the identity stored in `ProjectIntegration.root_id`,
   one provider vocabulary, ADR-0020 amendment) and the publication operation kinds of those providers.
   An `APPROVE` (execution only) never authorises a publication (`PUBLICATION_NOT_APPROVED`).
9. **Run decision = one preview.** Execute records exactly one human `PUBLICATION` decision for the
   previewed run: publication id, preview hash, the concrete `operation_ids`, the exact
   `{provider, root_id}` targets, the operation kinds, 24 h expiry and `approval_decision_id` (the plan
   approval it executes). No per-API re-prompt: every write of the run references this one decision.
10. **The interlock checks both.** For a publication write (`catalog.is_publication_write`) the interlock
    requires the PUBLICATION decision (human, ACTIVE, unexpired, this publication, this preview hash, this
    operation id, target inside the run) AND the referenced APPROVAL (human, ACTIVE, same version/hash,
    APPROVE_AND_PUBLISH, operation kind and target inside the approval, still the plan's approved version).
    A plan approval alone is refused (`PUBLICATION_DECISION_REQUIRED`). Delivery writes (push/PR) keep
    using the APPROVAL directly.
11. **The publisher writes only where configuration and approval agree.** Preview plans only the purposes
    whose provider the approval names; a configured target outside the approved scope is BLOCKED
    (`TARGET_NOT_APPROVED`) and execute refuses (`TARGET_NOT_READY`). Execute/retry also refuse a preview
    whose approval was re-recorded after the preview (`APPROVAL_STALE`).
12. **Consequences proved end to end** (`tests/e2e/test_e2e_planning_publication.py`,
    `tests/unit/test_publication_binding.py`): stale hash/version refused; a newer version invalidates
    the approval so a running publication is denied per operation and the newer version is never
    published until approved; revoking either decision stops the run.
