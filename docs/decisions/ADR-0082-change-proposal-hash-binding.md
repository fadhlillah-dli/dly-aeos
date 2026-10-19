# ADR-0082 — Change proposals: canonical identity, deterministic review package, trial ≠ promotion, hash-bound decisions

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by module group H (proposals)

## Context

Master §39–§41 let the owner propose ADD/REPLACE/UPGRADE/REMOVE/COMPARE changes from the dashboard
without installing anything, require duplicates to be linked via canonical source identity without
merging Office and Personal contexts, define a 10-item review package, make trial approval and
promotion approval different decisions bound to baseline/candidate hashes, and require outcomes
ACCEPTED / ROLLED_BACK / FAILED / PARTIAL where a rollback is never shown as acceptance. §50 rows:
"Pengguna hanya meminta compare/review URL", "Trial disetujui, promotion belum disetujui",
"Baseline/candidate/target berubah setelah approval", "Change proposal ditolak atau rollback".

## Decision

1. **Identity.** Sources are normalised by `aeos.research.identity.canonical_identity`
   (`github:owner/repo`, `gitlab:…`, `bitbucket:…`, `url:host/path`, `name:slug`; `.git`, case,
   `www.`, sub-paths, SCP form). A rename/redirect recorded by Check Upstream is followed for
   duplicate linking only (never to inherit trust). Duplicates link (`duplicate_of`) to the earliest
   proposal **in the same domain**; a multi-component idea is one parent with explicit, acyclic parts.
2. **Review package** is computed by code: (1) summary/relevance, (2) source identity with dates
   from the registry, (3) baseline from inventory records/relations/observations, (4) alternatives,
   (5) impact dimensions (KNOWN from relations or UNKNOWN with reason), (6) experiment plan plus the
   §40 replacement/rollback plan, (7) costs as actual/estimate/UNKNOWN, (8) optional Claude
   REVIEW/VERIFY/VERDICT via `aeos.claude_bridge.facade.run_structured` with generic public metadata
   only (never problem text, attachments, free-text `name:` identities or Office data; OFFICE is
   never sent; a completed review is reused while the hash of that metadata is unchanged and
   marked STALE once it changes, so no Claude quota is spent on an unchanged input, §41/§42),
   (9) required human decision with the exact hashes, (10) outcome placeholder.
3. **Hashes.** `baseline_hash` = hash of the existing components' desired/config identity (pin,
   manifest hash, admission/enablement/lifecycle, access, DESIGN/CONFIG relations, observed installed/
   configured versions; for ADD the latest ArchitectureSnapshot). `candidate_hash` = hash of kind,
   canonical identities, candidate pin, candidate components, parts and identity-change state.
4. **Decisions** go through `aeos.policy.facade.record_decision` with
   `target_hash = hash(proposal_id, action, baseline_hash, candidate_hash, scope)`; kinds TRIAL,
   PROMOTION, REJECTION (KEEP/REJECT), REVISION_REQUEST (DEFER/REQUEST_MORE_EVIDENCE), APPROVAL
   (REFERENCE_ONLY). Submitted hashes must equal the reviewed package and the live recomputation;
   otherwise `STALE_BINDING` is returned and ACTIVE decisions are invalidated (committed before the
   refusal). TRIAL_APPROVE and PROMOTE require a pinned candidate and a bound scope
   (target/environment/provider/data_class/budget_usd); PROMOTE additionally requires an ACTIVE TRIAL
   decision on the same hashes and trusted LIVE evidence. COMPARE can never be trial-approved,
   promoted or given an outcome. No decision raises a budget, installs, activates or changes routes.
   TRIAL_APPROVE/PROMOTE also pass a **source identity gate**: every candidate identity must be
   stated by the master, configured by the owner, internal or checked live; UNVERIFIED, CHANGED
   (rename/redirect/fork) or NOT_FOUND blocks admission (`SOURCE_IDENTITY_UNVERIFIED`, §50).
   Registry entries added or checked after submission are linked at re-review. Every decision
   except PROMOTE first invalidates the proposal's other ACTIVE decisions, so a proposal never
   holds contradictory active approvals (§39); PROMOTE keeps the trial it builds on.
5. **Outcomes** ACCEPTED (requires an ACTIVE PROMOTION decision for this candidate + trusted evidence
   + accepted configuration, and no rollback data), ROLLED_BACK (explicit data-rollback status; closes
   the proposal and invalidates decisions), FAILED, PARTIAL (lists failed parts). History is kept; the
   timeline shows before → after with ArchitectureSnapshot references, and §41 delivery dimensions
   (APPROVED, IMPLEMENTED, ACTIVE, VERIFIED, ARCHITECTURE_SOURCE_UPDATED, DOCS_PUBLISHED) are shown
   separately. Owner decisions sync the linked research entries' lifecycle (REJECTED/DEFERRED/
   REFERENCE_ONLY/ENABLED) with history — shared registry rows only from non-OFFICE proposals, so
   Office decisions never surface in Personal views.

## Consequences

* Any inventory change to a baseline component invalidates pending trial/promotion bindings; the
  owner re-reviews and decides again. This is the intended §50 behaviour.
* Without the policy module the proposal flow fails closed at the decision step.

## Amendment 2026-10-19 (audit gap closure, master §39–§41, §47, §49 M3)

1. **Domain-scoped baselines.** The ADD/COMPARE baseline uses the latest ArchitectureSnapshot taken in
   the proposal's own domain; outcome before/after snapshots must belong to that domain and the "after"
   snapshot must postdate the PROMOTION decision. Another domain's snapshot never changes a baseline hash
   or appears in a package.
2. **Owner plan bound to the candidate.** `ChangeProposal.owner_plan` holds the owner's measurable
   acceptance (metric + threshold + baseline), the operations owner, the REPLACE/UPGRADE plan (cutover
   order, retirement point, rollback/forward recovery, compatibility, restore-tested backup, coexistence)
   and an optional canary plan. These keys are part of `candidate_hash` (editing them re-reviews and
   re-binds); REMOVE closure progress and canary evidence are recorded outside the hash.
3. **Trial admission.** TRIAL_APPROVE binds `target/environment/provider/data_class/budget_usd` plus
   `max_minutes`, `max_attempts`, `stop_conditions` and `rollback_scope`; the environment is
   sandbox/staging (canary only with a declared canary plan, never for unsafe coexistence); RESTRICTED
   data is refused; the provider must have a WORKER/LOCAL route allowed for the domain and data class
   (ELIGIBLE or NOT_ACTIVATED; UNKNOWN_PROVIDER/INELIGIBLE refuse); the Security/Privacy assessment by
   code must not be BLOCKED; problem, benefit and a measurable criterion are required
   (`BENEFIT_NOT_TESTABLE`). The decision enumerates its operations (`trial.sandbox`, `trial.task.run`,
   `trial.eval.run`, `trial.verifier.collect`, `trial.stop`; promotion: `promote.candidate`,
   `inventory.config.apply`, `release.promote.candidate`). Linked duplicates of one canonical identity
   never hold ACTIVE decisions at the same time (`DUPLICATE_DECISION_CONFLICT`).
4. **Trial execution.** `POST /api/proposals/{id}/trial/launch` (owner) re-verifies the TRIAL binding
   and creates, in the proposal's project (platform project for PLATFORM scope), a Plan + immutable
   PlanVersion (`source=CHANGE_PROPOSAL`) whose single TaskSpec carries `change_proposal:<id>`, the
   decision id, candidate pin/hash, caps, stop conditions and operations; the same owner action records
   the plan APPROVAL bound to that exact version hash with the TRIAL decision as authority (§40 "one
   authorization covers the series of trial operations"), and the workforce scheduler enqueues it (or the
   trial waits with the readiness reasons). The scheduler re-verifies the binding before every attempt
   and while running (stale hashes, stopped trial, other provider, operation outside the decision, caps)
   and stops the attempt otherwise. `trial/stop` cancels the trial tasks (capabilities revoked, process
   tree killed, evidence kept).
5. **Scope vs what ran.** Every binding check compares the decision scope with the trial records: task
   domain/data class, run provider, attempts and ledger spend; any drift invalidates the decision.
6. **Evidence about the candidate.** Gate evidence must be trusted LIVE evidence produced for a TaskSpec/
   run linked to the proposal, an eval run linked to it, or verifier evidence naming the proposal and its
   candidate hash, and created after the TRIAL decision (`EVIDENCE_NOT_FOR_CANDIDATE`).
   `attach_evidence` and the ACCEPTED outcome re-verify the bindings (`STALE_BINDING`, proposal back to
   UNDER_REVIEW). After PROMOTE only the promoted change may move the baseline (pin/manifest/observed
   version of the upgraded component; retirement fields of a replaced/removed one).
7. **Claude checkpoint before PROMOTE.** `POST /api/proposals/{id}/checkpoint` records a review of the
   bound evidence package (hash over candidate hash + acceptance + evidence) through the native bridge, a
   manual Claude Code result, or — only after the bridge could not run for that package, or for OFFICE —
   an explicit owner NOT-PROVEN record. PROMOTE needs a current record that is not FAIL/BLOCKED/waiting,
   a linked TaskSpec, the named operations owner, and for REPLACE/UPGRADE the cutover and rollback plan
   (plus a restore-tested backup when the component holds state/data); the PROMOTE decision records the
   authorised cutover.
8. **Outcomes do what they claim.** ACCEPTED requires the lawful configuration change (manifest pin =
   accepted pin AND enabled by decision or observed installed/configured at that version after the
   promotion), an observation after activation, the pre-declared acceptance met per criterion with bound
   evidence (or an ACTIVE RISK_ACCEPTANCE waiver), an ADR link for ADD/REPLACE/REMOVE, every REMOVE
   closure item CLOSED/WAIVED and canary evidence when a canary was run. ROLLED_BACK must reference the
   successful rollback/restore operation that ran after the promotion (`ops.release.rollback`,
   `ops.backup.restore` or `inventory.manifest.load` restoring the previous pin; restore-test evidence for
   `data_rollback=DONE`) and records a `ROLLBACK` Decision bound to the proposal and candidate. ACCEPTED/
   ROLLED_BACK/PARTIAL trigger a scoped inventory reconciliation and compute the inventory/ADR/docs/
   ownership finalization from records (`inventory_docs_update` names what is still missing).
9. **Links and dimensions.** `POST /api/proposals/{id}/links` links TaskSpecs, eval runs, publications,
   controller releases, ADRs and observations after validating each record in the proposal's domain;
   `aeos ops release promote --proposal <id>` promotes a controller release only while that proposal's
   PROMOTION binding holds and records the release. DOCS_PUBLISHED follows the linked publications
   (PARTIAL/CONFLICT stay visible); ACTIVE is ENABLED_IN_CONFIG/OBSERVED_IN_CONFIG from inventory only.
10. **Review package additions.** UPGRADE delta (version/publisher/license/terms/schema), REMOVE closure
    plan, ADD overlap with existing components, recorded shortcomings (registry limitations, open drift),
    impact dimensions for schema/API/tools, hooks/prompts/memory, identity and resource/hardware, CI/docs
    derived from related inventory records, rollback UNKNOWN unless a tested rollback/restore reference
    exists (old pins are targets, not proof), candidate-specific plain language with unmeasured
    percentages masked, ACTUAL (30-day ledger, domain-scoped) and ESTIMATE (route pricing) costs, and the
    Security/Privacy assessment. Same-domain possible duplicates by goal/component are suggested, never
    merged. Worker/Hermes/reviewer suggestions enter as untrusted SUBMITTED proposals
    (`POST /api/proposals/suggestions`, `proposals.facade.file_suggestion`).
