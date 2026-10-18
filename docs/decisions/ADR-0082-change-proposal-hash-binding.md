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
   only (never problem text, attachments or Office data; OFFICE is never sent), (9) required human
   decision with the exact hashes, (10) outcome placeholder.
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
5. **Outcomes** ACCEPTED (requires an ACTIVE PROMOTION decision for this candidate + trusted evidence
   + accepted configuration, and no rollback data), ROLLED_BACK (explicit data-rollback status; closes
   the proposal and invalidates decisions), FAILED, PARTIAL (lists failed parts). History is kept; the
   timeline shows before → after with ArchitectureSnapshot references, and §41 delivery dimensions
   (APPROVED, IMPLEMENTED, ACTIVE, VERIFIED, ARCHITECTURE_SOURCE_UPDATED, DOCS_PUBLISHED) are shown
   separately. Owner decisions sync the linked research entries' lifecycle (REJECTED/DEFERRED/
   REFERENCE_ONLY/ENABLED) with history.

## Consequences

* Any inventory change to a baseline component invalidates pending trial/promotion bindings; the
  owner re-reviews and decides again. This is the intended §50 behaviour.
* Without the policy module the proposal flow fails closed at the decision step.
