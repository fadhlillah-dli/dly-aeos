# ADR-0031 — Human decisions bound to exact targets, and a fail-closed external-write interlock

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed builder under ADR-0003

## Context

Master §13/§14/§20/§26 and AGENTS.md invariant 3: approvals and other AUTH are human decisions bound
to an exact target, version/hash and operation scope; agent/model output is DATA. §26 lists the
conditions of the external-write interlock. §20 states SUPER_ADMIN is not automatically AUTH over
office resources. U-09 keeps merge and production human-only. §50 requires that an old approval
cannot be used after the hash changes and that a revision appearing during publish never makes the
job "query latest".

## Decision

1. `Decision` rows are created only for an active SUPER_ADMIN user whose domain/project scope covers
   the decision, or for an owner decision recorded from an ACCEPTED ADR that records an explicit
   owner instruction (`owner:ADR-NNNN`, `actor_kind = HUMAN_RECORDED`; allow-list
   `aeos.policy.catalog.OWNER_RECORDED_ADRS` = {ADR-0003}, in a protected path). Design ADRs are
   documentation, not owner AUTH, so editing or adding an ADR file can never mint an owner actor.
   An owner-recorded actor may only hold `EXCEPTION` decisions (`OWNER_RECORDED_KINDS`; ADR-0003 §3
   does not delegate approvals, publication, risk, budget, policy or write enablement) and can never
   revoke a decision. Anything else is `PolicyDenied`. Human-only operations
   (merge/approve PR, force push, default-branch push, branch deletion, production) cannot be
   delegated by any decision except an owner-recorded EXCEPTION, which the interlock still never
   honours for AEOS components.
2. Binding columns stay immutable (DB trigger); lifecycle is ACTIVE → EXPIRED / INVALIDATED /
   SUPERSEDED. Renewal is a new decision. Every record and lifecycle change is audited and emitted.
3. Plan approval (`APPROVE_AND_PUBLISH` / `APPROVE`) must reference the current version and its exact
   manifest hash (stale → 409 with the latest version). It binds `plan_version` id + hash, the
   explicit targets and the derived operations (publication operations only for APPROVE_AND_PUBLISH;
   broker delivery operations only for repository targets). `get_approved_version` returns the version
   bound by the ACTIVE approval and never the latest version.
4. Write enablement per provider = `SystemSetting write_enabled.<provider> == true` AND an ACTIVE human
   `WRITE_ENABLEMENT` decision; revoking or expiring the decision turns the flag off.
5. `check_external_write` evaluates every condition and returns all failing reasons (stable
   `CODE: text` strings): bootstrap, actor role/scope, project/domain, provider per domain, known
   non-human-only operation, OFFICE company authorisation covering the provider and a verified
   Atlassian deployment kind, write enablement, approved immutable version/hash/decision/operation/
   target scope, an approval made by a human (`DECISION_ACTOR_NOT_HUMAN` otherwise), a target that
   names no other provider than the write (`TARGET_PROVIDER_MISMATCH`), a LIVE version (MOCK/DEMO
   content is never published, `VERSION_NOT_LIVE`) whose stored content still matches its hash
   (`VERSION_INTEGRITY`), and an unexpired capability (a missing expiry is a denial).
6. OFFICE policy defaults come from `policy/office-defaults.yaml` (missing file → deny-all) and can be
   relaxed only by ACTIVE human POLICY decisions on the `office_policy:OFFICE` target while an ACTIVE
   company authorisation (same target, `office_authorization` + a `company_reference` + explicit
   providers) exists; configuration flags cannot relax them. The generic decision endpoint refuses
   policy-bearing keys (`office_authorization`, `office_policy`, `enabled`, ...) in free-form
   `details` (`RESERVED_DETAIL_KEYS`), so they can only come from the validated fields.
7. The active PolicySnapshot hashes `policy/*.yaml` plus ACTIVE POLICY/EXCEPTION decisions. The MCI
   reference is UNVERIFIED unless the referenced file exists with a matching hash; compliance is
   always NOT-PROVEN; risk classes R0–R4 carry no invented thresholds. Projections of the snapshot
   list only the bound decisions of the viewer's active domain (+ global ones); the OFFICE policy
   projection hides OFFICE decision ids outside OFFICE sessions (no ids, targets or counts leak).
8. Decision details/scope are stored with string-pattern redaction (`aeos.policy.textsafe.scrub`)
   instead of key-name masking, so fields such as `office_authorization` keep their meaning while
   secret-shaped values are still removed.

## Consequences

* Publishers/brokers must pass the approved version id/hash, the approval decision id, the exact
  target and a capability expiry; otherwise the interlock denies the write.
* Approvals created before a new PlanVersion are invalidated automatically; a running publication is
  then denied by the interlock and stops (it does not switch to the new version).
* Other modules move a plan only through `aeos.planning.facade.transition_plan`, which is limited to
  the publication states (PUBLISHING/PUBLISHED/PUBLICATION_PARTIAL/PUBLICATION_FAILED, and
  NEEDS_DECISION from a publication state); APPROVED/REJECTED/REVISION_REQUESTED are reachable only
  through a human decision (`TRANSITION_NOT_DELEGATED`).
* The ADR-0003 delegation is visible as a decision but grants nothing inside AEOS. Once the owner
  revokes it, the seed never re-records it — not even after the ADR file changes.
