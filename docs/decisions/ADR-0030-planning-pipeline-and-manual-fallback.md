# ADR-0030 — Four-stage planning pipeline as bounded activities with manual fallback

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed builder under ADR-0003

## Context

Master §02/§15/§17 keep the Opus → Fable → Opus → Fable planning baseline (Primary Planner,
Planning Challenger, Planning Synthesizer, Final Planning Reviewer). §12 forbids the Claude API: only
the official Claude Code binary with the owner's Max login may be used, and when the bridge is not
proven the platform must hand a package to the user instead of silently switching to an API. §14
requires an explicit planning state machine separate from runtime/publication/acceptance state, and
immutable PlanVersions. §50 requires that an exhausted Fable quota makes review wait instead of
skipping a gate, and that a changed model alias is not accepted as the old baseline.

## Decision

1. `aeos.planning.states` holds every §14 planning state and an explicit transition table; any other
   transition is a `Conflict` (`ILLEGAL_PLANNING_TRANSITION`). Only `Plan.state` is touched.
2. The pipeline runs the four stages in order as bounded activities through
   `aeos.claude_bridge.facade.run_structured` with JSON Schemas from `contracts/planning/` (plain
   draft 2020-12, `additionalProperties: false`, no `$ref`, no numeric/length constraints), no tools,
   and a 900 s timeout. The baseline is recorded in `Plan.policy_baseline` with `skippable: false`;
   no code path skips a stage (changing the baseline needs a separate evaluated POLICY decision).
3. Each stage writes a `PlanningRun` (model requested/resolved, engine version, input/output hash,
   usage, trace ref, session presence) and a `PlanningArtifact`. A resolved model whose identifier
   does not contain the policy family (`opus`/`fable`) as a whole token — including an UNKNOWN model
   — fails the stage (`PLANNING_FAILED`/`REVIEW_FAILED`); the output is not accepted.
4. The Challenger and Final Reviewer receive only the package under review (fixed context, requested
   outcome, plan content, findings) — never the planner prompt, attachments or worker instructions.
   The challenger must not continue the primary planner's session, and the final reviewer must not
   continue the planner's or the synthesizer's session (`REVIEWER_NOT_INDEPENDENT`). Claude session
   ids stay server-side.
5. The Primary Planner's output and the Synthesizer's output each become a new immutable PlanVersion
   (`manifest_hash = content_hash(canonical content)`); every new version invalidates approvals of
   superseded versions. Provenance is kept: MOCK/DEMO output yields a MOCK/DEMO version, and a
   synthesis or human edit of MOCK/DEMO content stays MOCK/DEMO (never becomes LIVE).
6. `Blocked`/unavailable bridge → `write_manual_package` and `AWAITING_MANUAL_CLAUDE`; the imported
   manual result goes through the same schema/model-family validation. If the package cannot be
   written the plan goes to `NEEDS_DECISION`. Quota exhausted → checkpoint + `NEEDS_DECISION`; a later
   run resumes at the same stage. There is no API fallback anywhere.
7. OFFICE planning requires `CAN_PLAN_OFFICE` from the Doctor and a company authorisation that allows
   Claude Max for office data; otherwise the plan is `NEEDS_DECISION` before any model call or manual
   package (§23, §51 P1).
8. The final verdict can at most be PASS-STATIC (a plan review runs no tests); a PASS-STATIC with
   blocking findings is downgraded to NOT-PROVEN by code, never upgraded. No verdict is an approval.

## Consequences

* Native automation stays NOT-PROVEN until the bridge module passes its capability test; the manual
  path keeps planning usable in the meantime.
* Attachments are untrusted DATA: they are fenced with a nonce, flagged for instruction-like text and
  cannot change domain, goal, policy, permissions or routing. Only attachments whose intake scan is
  CLEAN/FLAGGED are read; PENDING or unknown scan states are not used (fail closed).
* Authority-shaped instructions in the human request text itself (domain switch, "ignore previous",
  approval claims) are recorded as an `UNTRUSTED_INPUT` constraint; the human-selected domain stays.
* The final verdict is tied to the version it judged (`verdict_version_id`); a later human edit is
  shown as not reviewed, and an approval records which version was last reviewed.
* The API runs the pipeline in a background thread (202 + `plan.state_changed` /
  `planning.run.updated` events); CLI/tests run it synchronously. A RUNNING run younger than the
  timeout blocks duplicate runs across processes; an older one is treated as interrupted and the
  stage is re-run (not skipped).
