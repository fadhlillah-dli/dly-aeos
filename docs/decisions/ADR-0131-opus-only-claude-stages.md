# ADR-0131 — Opus at every Claude stage; the Claude Fable family is never requested (§02, §12, §15, §17)

* Status: ACCEPTED (owner decision 2026-10-28, owner requests R38 and R42)
* Date: 2026-10-28
* Deciders: owner (Fadhlillah), decision of 2026-10-28; built by the delegating session. The owner can still
  supersede it as an `ADR` decision (see the register README).
* References: [ADR-0030](ADR-0030-planning-pipeline-and-manual-fallback.md) (four-stage pipeline, Opus → Fable →
  Opus → Fable baseline), [ADR-0040](ADR-0040-claude-native-bridge-invocation.md) (bridge invocation and the
  model-policy check), [ADR-0062](ADR-0062-review-acceptance-binding.md) (checkpoint review, quota scope per family).
  None of them is edited; this ADR amends them where they name Fable.

## Context

* The owner, 2026-10-28 (verbatim, R38): "jangan pernah gunakan Fable 5.1". Asked what replaces Fable in the
  planning pipeline, the owner answered the same evening (verbatim, R42): "Ganti ke Opus 5.5".
* Exact scope: DLY AEOS never requests a Claude Fable model through the Claude Code bridge, and every stage that
  used Fable switches to Opus. Nothing else changes: the number of stages, the review gates, the manual fallback,
  the quota handling and the human decisions stay as they are.
* Reason given: the owner moved to a Claude Max x5 plan; Fable uses more of the plan's quota; the owner rates
  Opus 5.5 and Fable 5.1 as one tier, so the switch is not treated as a quality reduction.
* Before this ADR the master (§02, §15, §17) and the code used Opus for the Primary Planner and the Synthesizer and
  Fable for the Planning Challenger, the Final Planning Reviewer, the checkpoint review (`DEFAULT_REVIEW_MODEL`) and
  the proposal review (`REVIEW_MODEL`). A second model family was one source of the reviewers' independence.

## Decision

1. **Every Claude stage requests Opus.** The four planning stages (`aeos.planning.states.STAGES`), the bridge's
   display policy (`SUPERVISOR_POLICY`: `planning.primary`, `planning.challenge`, `planning.synthesis`,
   `planning.final`, `review.checkpoint`), the checkpoint review default (`verification.review.DEFAULT_REVIEW_MODEL`)
   and the proposal review (`proposals.review.REVIEW_MODEL`) request the alias `opus`, and the resolved model must
   be in the `opus` family. The planning baseline recorded on every plan is renamed `opus-only-4-stage`; its
   `source` cites master §02, §15 and §17 as amended by this ADR. All four stages still run and none is skippable:
   the §02 rule that reducing or skipping a stage needs an evaluated POLICY decision is untouched, because no stage
   is removed. A stage-model pin (POLICY decision on `planning_baseline/stage_models`) must name an exact Opus id;
   a Fable id is rejected as "not an exact model id of the stage's policy family".
2. **Independence now comes from a fresh context per stage, not from a second model family.** Checked in the code
   before this ADR was written (no new mechanism was needed):
   * each stage is its own `run_structured` call, so its own `claude -p` process with the prompt on stdin;
   * every request gets a new supervisor directory (`workspace.prepare_workspace` refuses to reuse one);
   * `--no-session-persistence` is a required flag, and `--resume`, `--continue`, `-c` and `-r` are forbidden flags
     (`bridge.REQUIRED_FLAGS`, `bridge.FORBIDDEN_FLAGS`, checked by `assert_safe_argv` before any process starts);
   * the Challenger and the Final Reviewer receive only the package under review, never the planner's conversation
     (`test_challenger_and_reviewer_receive_only_the_package`);
   * a reviewer whose answer carries the session id of the stage that authored the plan fails with
     `REVIEWER_NOT_INDEPENDENT` (`planning.service._complete_stage`);
   * the manual fallback writes a separate package (its own folder) per stage, and the command it gives carries the
     same flags.
   `test_each_stage_is_a_separate_invocation_in_a_fresh_context` (planning bridge seam, real bridge and a scripted
   binary) asserts one process per stage, four distinct supervisor directories, `--no-session-persistence` and no
   resume or session flag on every call, and four distinct sessions.
3. **Fail closed at the bridge.** `fable` is removed from the families a request may name
   (`parsing.ALLOWED_REQUEST_FAMILIES` is `opus`, `sonnet`; `parsing.REFUSED_REQUEST_FAMILIES` is `fable`). One
   function identifies the family, `parsing.refused_family` (the bridge facade exports it): the name is NFKC-normalised
   (full-width letters become ASCII), invisible characters are removed (zero-width space and joiner, BOM, soft
   hyphen, controls) and it is split into lower-case alphanumeric tokens; a `fable` token is the family. So the alias,
   a pinned id such as `claude-fable-5-1`, `fable[1m]`, `Fable 5.1`, any case and a zero-width character inside the
   name are all refused by `bridge.validate_request` before any process starts, with their own code
   `MODEL_FAMILY_REFUSED` and the message "model '...' is refused: DLY AEOS never requests the Claude fable family
   (owner decision 2026-10-28, ADR-0131); planning and review stages use opus". A word that only contains it
   (`fableish`, `notfable`) is not the family. A name the tokens cannot identify, for example one written with
   letters of another script that only look Latin, names no family AEOS knows: the generic check refuses it as
   `REQUEST_INVALID`, also before any process starts. That is the safe answer, because calling it Fable would be a
   guess and nothing is sent either way. The manual package has the same preflight, so no Fable package is written.
   `bridge.capability_test` checks the family first: a refused family returns status `REFUSED` without probing the
   binary or the login and without persisting a capability-test row or evidence. `aeos bridge test` accepts only
   `opus`, and so does the Control Center capability test (`POST /api/claude/capability-test`, schema
   `model: "opus"`).
4. **A Fable answer is never accepted, whatever was requested.** The parser keeps `fable` in `parsing.FAMILIES`, so
   an answer produced by a Fable model is named as such. `parsing.resolve_model` refuses every model that produced
   output and belongs to a refused family, and a request that named one, even when the request itself said `fable`
   (a package or a record written before this ADR): `MODEL_POLICY_MISMATCH` with the message "answer produced by ...
   (family fable): the Claude fable family is never accepted, whatever was requested (owner decision 2026-10-28,
   ADR-0131)". That is a hard stop at every planning stage (checkpoint and NEEDS_DECISION, no other model, nothing
   skipped) and a refused proposal review. The checkpoint review's identity check
   (`verification.review.refused_review_model`, the first step of `_model_policy_problem`) refuses a review answered
   by, or requested from, Fable with its own message, whatever its stored `model_requested` says.
5. **Records written before this ADR.** None is rewritten; none can make AEOS request or accept Fable.
   * Plans: a plan's `policy_baseline` is history. When such a plan runs its next stage,
     `planning.service.stage_model` ignores a recorded model outside the stage family and requests `opus`.
   * Planning manual packages written for Fable (`package.json` `model_requested`): `read_manual_result` refuses them
     with `MODEL_FAMILY_REFUSED` (whether or not a result exists yet); `import_manual_result` refuses before anything
     changes (`PolicyDenied`, `MODEL_FAMILY_REFUSED`) and tells the owner to run the plan again; the plan's manual
     instructions no longer point to the package (they say not to run it and to run the plan again) and
     `allowed.import_manual` is false. Running the plan again marks the manual run ABANDONED and runs the same stage
     with opus, writing a new manual package if the bridge is still not proven.
   * A checkpoint review waiting for a manual result that was requested from Fable: `collect_manual_review` refuses
     the import (`PolicyDenied`, `MODEL_FAMILY_REFUSED`) and tells the owner to request a new review
     (`aeos verify review <run>`), which uses opus.
   * A review completed by a Fable model (or requested from Fable) before this ADR is not counted for any new
     acceptance: the acceptance check names it "not counted for acceptance: the review was answered by ... (Claude
     fable family), which DLY AEOS never accepts (owner decision 2026-10-28, ADR-0131); request a new review, it
     uses opus". Acceptance records already written are history and stay as they are.
6. **The route catalog keeps the Fable row as history.** `claude-native/fable` (SUPERVISOR pool, CANDIDATE) is not
   deleted: it records the 2026-10-18 probe, and on the owner's machine it holds the observations of earlier Fable
   calls (resolved version, Claude Code version). The seed has no status
   field (rows are created CANDIDATE), and moving the row to another pool would change its configuration identity
   and put it in a worker pool (WATCHLIST is one), which is worse. It is not selectable: no stage requests it, the
   bridge refuses its alias, and a SUPERVISOR route can never be activated as a worker route (`activate_route`
   refuses it). Its `notes` (descriptive, not part of the configuration identity, refreshed by `aeos seed` on an
   untouched CANDIDATE row) now start with "NOT USED" and cite this ADR; its configuration hash is unchanged, so no
   decision is voided. Disabling the row (`aeos routes disable claude-native/fable`) stays the owner's option; it is
   not needed for the refusal and it marks the Doctor checks that depend on the supervisor routes STALE until they
   are re-run.
7. **Master text.** The master is a protected path and is not edited. Where it names Fable for a planning or review
   stage, this ADR amends it: §02 (the Opus → Fable → Opus → Fable baseline, now Opus at all four stages), §03 (the
   diagram's "review Fable"), §12 (Fable 5.1 as supervisor candidate; a Fable limit), §15 (the Challenger's "Fable
   tertinggi yang tersedia" and the Final Reviewer's "Fable sesuai policy", now the Opus family per baseline), §17
   (the four Opus/Fable planning stages) and §23 (the pool table's "Pengawas Max: Fable 5.1"). The §50 row "Fable Max
   quota habis" now applies to the Opus stages with the same behaviour: the review waits and no gate is skipped.
   Neutral facts stay: the §52 source S02 (Fable on Max), the research catalog entry for it, the parser's family
   list, the Doctor's note that the Max weekly limit includes Fable, and the release history.

## What changed

* Backend: `planning/states.py`, `planning/service.py` (baseline name and source, docstring, the refused manual
  package), `planning/cli.py`, `claude_bridge/parsing.py` (`refused_family`, the refusal in `resolve_model`),
  `claude_bridge/bridge.py` (request and capability-test refusal), `claude_bridge/facade.py`, `claude_bridge/manual.py`,
  `claude_bridge/cli.py`, `claude_bridge/router.py`, `verification/review.py` (review default, `refused_review_model`,
  the manual import refusal), `verification/acceptance.py`, `proposals/review.py`, `routing/seed.py` (notes only), the Doctor capability text of `CAN_PLAN_PERSONAL` (and the
  generated `docs/OWNER_TARGET_CAPABILITY_MATRIX.md`), the workforce engine hints of the Planning Challenger and the
  Final Planning Reviewer (their role revision changes once at the next `aeos seed`).
* Control Center: the stage labels ("Review penantang (Opus)", "Review akhir (Opus)"), the plan page heading, the
  stage-model pin form (all four stages in the Opus family) and the capability test model choice (`opus` only).
* Tests: the stage-model, quota-scope and resolved-model assertions that named Fable, in the unit, e2e and trusted
  acceptance suites (acceptance 1.30 and 1.31 keep their meaning: the review waits on an observed limit, now scoped
  `opus`; an answer from outside the policy family is never accepted, now a Fable answer to an Opus request), and
  `tests/unit/test_opus_only_claude_stages.py` with the guards of this ADR (no stage names Fable; a Fable request
  is refused in every spelling and the capability test starts and stores nothing; a Fable answer fails every stage
  and both reviews whatever was requested; records written before this ADR are refused), each red with its own
  message and each with an Opus near miss.

## Amendment 2026-10-28: review before merge

A blind review of the first build found that a record or package written before this ADR could still let a Fable
answer through, because the checks compared the answer only with what had been requested: a waiting checkpoint
review whose stored `model_requested` was `fable` plus a Fable result envelope became COMPLETED with a pass verdict;
`acceptance` counted such a review; a planning package written for Fable could be imported and its instructions
still said `--model fable`. It also found that `capability_test("fable")` probed the binary and the login and
stored a FAILED row before refusing, that `fable[1m]`, `Fable 5.1` or a zero-width character inside the name got the
generic `REQUEST_INVALID` instead of the dedicated refusal, and that the decision date was wrong (the owner decided on
2026-10-28, WIB). Decisions 3, 4 and 5 above now state the fixed behaviour; each fix has a bite test that is red with
its own message on the first build and an Opus near miss that completes.

## Consequences

* All Claude stages draw on one family's quota. An observed limit is recorded with scope `opus` (ADR-0062), so the
  whole pipeline waits on that one limit, where before a Fable limit stopped only the review stages.
* The reviewers no longer bring a second model family's view. Their independence rests on the separate context of
  decision 2; blind spots that one model family shares are a residual risk the owner accepted with this decision.
* An old plan or an old POLICY pin that names Fable cannot make AEOS request Fable (decisions 1, 3 and 5).
* Evidence: E2, unit, seam and e2e tests on macOS arm64 (stage models, the refusal of every Fable spelling with
  no process started, a Fable answer rejected at every stage and in both reviews whatever was requested, records
  and packages written before this ADR refused, one fresh invocation per stage).
  NOT-PROVEN: a live Opus-only planning run through the owner's Max login (the last live run, 2026-10-19, used
  Fable for the review stages), and Windows and macOS Intel until CI runs.
