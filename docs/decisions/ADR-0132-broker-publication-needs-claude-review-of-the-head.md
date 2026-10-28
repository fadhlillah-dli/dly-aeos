# ADR-0132 — Broker publication needs a Claude review PASS of the exact head (owner rule R44; §15, §19, §31)

* Status: ACCEPTED (owner rule R44, 2026-10-29)
* Date: 2026-10-29
* Deciders: owner (Fadhlillah), rule R44 of 2026-10-29; design by the delegating session. The owner can still
  supersede it as an `ADR` decision (see the register README).
* References: [ADR-0053](ADR-0053-capability-broker.md) (trusted capability broker),
  [ADR-0054](ADR-0054-candidate-git-hardening-and-publication-gate.md) (runtime-recorded head, publication gate,
  quarantine release),
  [ADR-0111](ADR-0111-office-code-delivery-and-route-feasibility.md) (Bitbucket path),
  [ADR-0121](ADR-0121-code-host-identity-and-no-ai-attribution.md) (commit identity gate). None of them is edited.

## Context

* The owner, 2026-10-29 (R44, paraphrased from Indonesian): all OpenCode and OpenRouter work must pass a Claude
  review; a worker has no right to commit, push, let alone merge, as the architecture says. The verbatim sentence is
  in the owner requests register: its wording (work "done by" a named AI tool) is what the repository's attribution
  guard refuses as an AI-production line, so the public repository carries the paraphrase. Scope of this ADR: worker output (OpenCode on OpenRouter, OpenCode Zen/Go or any other worker route)
  reaches a code host only after a Claude review, and a worker itself never commits to the real repository, pushes or
  merges.
* Master §31: "worker tidak mempunyai credential remote; hanya broker tepercaya dapat menulis branch yang diizinkan dan
  membuat PR; owner credential untuk main/production tidak berada di runtime agent". §15/§19: the Claude checkpoint
  review (`review.checkpoint`, official Claude Code bridge) is the read-only reviewer of every worker change.
* The gap: the broker's publication check (`git_broker._require_publishable`) accepted any run in REVIEW or DONE with a
  runtime-recorded head. A REVIEW run is one whose worker exited; its review may not have run yet. Delivery
  (`prepare-pr`) requires an ACCEPTED acceptance (verifier PASS + review PASS), but the broker facade is the boundary
  every caller crosses, and it did not require a review: a worker candidate could be pushed to an `aeos/*` branch and
  a PR opened before any Claude verdict existed.
* What already records the binding: `review_run` stores the checkpoint package as trusted `REVIEW_PACKAGE` evidence
  (collector origin, append-only) whose `environment` names the head commit the reviewer saw, the verified artifact
  hash and the package hash; the `ReviewRecord` keeps `package_evidence_id`, `package_hash` and `target_hash` (the
  artifact hash). The manual-package fallback copies all three. The review record itself has no head column.

## Decision

1. **Where.** `_require_publishable` (shared by the GitHub and Bitbucket push and PR creation) ends with
   `_require_claude_review`, after the run state, the runtime head and the quarantine release, and before the commit
   identity check, any grant consumption, interlock call, credential read or request. The rule is in
   `aeos.verification.publication_review`, reached through the additive facade function
   `aeos.verification.facade.publication_review`.
2. **What counts.** The run's latest checkpoint review (the non-specialist kind; specialist reviews stay part of
   acceptance) must be all of: by the Claude checkpoint reviewer (`reviewer_kind` CLAUDE); COMPLETED; not MOCK; bound
   to exactly the head being published (the runtime-recorded head, the only commit the broker pushes or opens a PR
   for) through its own checkpoint package evidence: `REVIEW_PACKAGE`, trusted, of this run, whose `environment`
   carries this review's `package_hash` and `target_hash` and names that head as `commit`; a PASS verdict (`PASS-STATIC`
   or better) that its own HIGH/CRITICAL findings do not contradict (`clamp_verdict`, the rule the review was recorded
   with); answered by a proven Claude model of the family the review policy requested (`_model_policy_problem`, the
   check acceptance makes: UNKNOWN, another family or a non-Claude model never counts; `allowed_models` applies).
   The latest review decides: a FAIL after a PASS of the same head refuses.
3. **Refusal.** `PolicyDenied` code `REVIEW_REQUIRED`, message `no Claude review PASS for head <sha12>: <why>`, details
   `reason` (NO_HEAD, NO_REVIEW, NOT_CLAUDE, NOT_COMPLETED, MOCK, UNBOUND, OTHER_HEAD, NOT_PASS, FINDINGS, MODEL,
   REVIEW_GATE_UNAVAILABLE), `head_commit`, `review_id`, `reviewed_head`, `verdict`. An audit record
   `broker.<operation>` DENIED carries the same. Nothing is pushed, no grant is consumed, no credential is read. The
   verification module missing refuses too (fail closed). Delivery keeps the code (`OWNER_RULE_CODES`), so
   `prepare-pr` answers `REVIEW_REQUIRED` instead of a generic broker failure, and the audit row persists.
4. **Unchanged.** The quarantine/special-review rule (an owner EXCEPTION bound to the head) is still checked first and
   still required; every other broker check stays. The gate approves nothing: it reads recorded evidence. Merge stays
   outside the runtime: the broker has no merge operation (tested), and merge to main is the owner's credential or a
   delegated maintainer's (U-09, §31).
5. **A worker cannot publish itself.** Proven at the seams (`tests/unit/test_worker_cannot_publish.py`): git in the
   worker's sandbox environment obtains no code-host credential even when the owner's own home holds one (HOME
   redirected, empty global config, system config ignored, prompts off); the isolated candidate has no remote; the
   broker's worker-facing surface is the model proxy only, a push/PR capability is not a model-proxy capability, and
   `prepare-pr` needs the owner's session. Existing tests cover the rest: no shell in the process sandbox, `bash`
   denied by default, the container's internal broker-only network (integration, needs Docker). The runtime commits
   the candidate only inside the run's own workspace (`workspace.commit_candidate`, a task branch of an isolated clone
   or a worktree); the executor tells the worker not to run git commit/push, and nothing it commits leaves without
   the broker.

## Consequences

* A run is published only after its own Claude PASS. A worker commit added after the review (a new head) needs a new
  review; so does a review that was waiting for quota, a manual result or a re-request.
* Tests that publish record a review the way `review_run` does (`tests/unit/review_gate_support.py`); a test also
  checks that the real native and manual review paths record the binding the gate reads.
* Residual: the gate trusts the control-plane database. A process that can write `review_records` and
  `evidence_artifacts` can forge a review; the WEAK process sandbox detects such writes after a run (trust-table
  window), the container prevents them. Specialist reviews (security, a11y, privacy, SRE) are required by acceptance,
  not by this gate. Evidence: E2 unit runs on macOS arm64; NOT-PROVEN on Windows and macOS Intel until CI runs, and
  against live GitHub/Bitbucket (local bare remotes and mocked HTTP).
