# ADR-0133 — Project data classification changes through one owner path: a raise is audited and applies to open work at once, a lowering is a POLICY decision, OFFICE is never lowered (§04, §20, §23, §24)

* Status: ACCEPTED (owner decision R46, 2026-10-28)
* Date: 2026-10-28
* Deciders: owner (Fadhlillah), decision of 2026-10-28 (late evening WIB) to reclassify the DLY AEOS project from
  INTERNAL to PUBLIC ("Ya, ubah ke PUBLIC", owner request R46, following R45); design by the delegating session. The
  owner can still supersede it as an `ADR` decision (see the register README).
* References: [ADR-0020](ADR-0020-domain-first-project-registry.md) (domain-first registry; the domain stays
  immutable), [ADR-0031](ADR-0031-decision-binding-and-external-write-interlock.md) (decisions bound to exact
  targets), [ADR-0033](ADR-0033-taskspec-compile-defaults.md) (TaskSpec compile defaults; amended here only for the
  data class of a task), [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) (decision C: free OpenRouter
  WORKER routes serve PUBLIC data only), [ADR-0127](ADR-0127-outgoing-model-request-secret-guard.md) (no credential
  leaves the host in a model request, on any route). None of the files is edited.

## Context

* The owner, 2026-10-28: the DLY AEOS platform project (`project_key` `dly-aeos`, domain PERSONAL, `is_platform`)
  must become PUBLIC. Its repository is public on GitHub, and the owner wants the best PUBLIC-only routes (DeepSeek
  on OpenCode Go, the free OpenRouter models) to take its tasks (R45: "manfaatkan model terbaik OpenRouter dan
  OpenCode semaksimal mungkin"; R46: "Ya, ubah ke PUBLIC").
* That was impossible. `aeos seed` registers the project INTERNAL (`portfolio/seed.py`); `PATCH /api/projects/{id}`
  edits only name, description, idea and lifecycle (`portfolio.service.EDITABLE_FIELDS`); and the DAG compiler gave
  every task the higher of the project class and the class its plan states, with INTERNAL when the plan stated none
  (`planning/dag.py`), so even a PUBLIC project would have produced INTERNAL tasks.
* Routing read only the task's compiled class, so raising a project would not have reached its open tasks.
* Master §04: the project record carries `data_classification`; the domain is never changed (a domain move is a
  migration). Nothing in the master fixes the class forever, but §20/§19 make policy changes human AUTH recorded as
  decisions, and §23 makes OFFICE data classes the company's authorisation ("OFFICE default
  external_worker_allowed=false dan approved_worker_routes kosong sampai otorisasi perusahaan", lines 654-656).
* The owner's standing rule of 2026-10-28 (R43, "Saya setujui via chat, Anda jalankan"): the owner approves in chat
  and the maintainer runs the command as the owner's actor; the audit records it.

## Decision

1. **One path.** `aeos project reclassify --project <id|key> --domain <PERSONAL|OFFICE> --to <class> --reason <text>
   --actor <SUPER_ADMIN username> [--approval-ref <text>]` (`aeos.portfolio.reclassify`) is the only way a project's
   class changes after creation. The domain is explicit and must be the project's domain; a project of the other
   domain is "not found". The class must be PUBLIC, INTERNAL, CONFIDENTIAL or RESTRICTED (`INVALID_CLASSIFICATION`
   otherwise, any case accepted); the reason needs at least 10 characters. The same class again records nothing.
2. **Raising is always allowed** (it only narrows which routes may see the data), in both domains, for an actor who
   may write: an active SUPER_ADMIN whose domain/project scope covers the project (VIEWER is read-only, §02). It
   writes one audit record `portfolio.project.reclassify` and one event `portfolio.project.reclassified`, both naming
   old class, new class, direction, reason, actor and approval reference. No decision is needed. A raise ends
   (INVALIDATED, with the actor and reason) the project's ACTIVE lowering decision, which no longer describes the
   class. It applies to open work at once (decision 6).
3. **Lowering is a POLICY decision.** The actor must be an active SUPER_ADMIN (`LOWERING_NEEDS_SUPER_ADMIN`), and
   the decision registry re-checks it and its scope. The decision is written by `aeos.policy.reclassification`,
   which the portfolio module reaches only through `aeos.policy.facade` (module boundaries, §10). One transaction
   records:
   * the decision: kind POLICY, target `project_classification/<project id>`, `target_version` = the new class,
     `target_hash` = hash of project id, old and new class, allowed operation `lower_data_classification`,
     rationale = the reason, details `from_classification`, `to_classification`, `actor_username`, `approval_ref`,
     `actor_check` (decision 9) and the in-flight TaskSpec counts; the registry adds its own `decision.record` audit
     record and `decision.recorded` event and refreshes the policy snapshot;
   * the class change on the project;
   * the audit record and the event of decision 2 with `direction` LOWER and the decision id.
   A newer lowering supersedes the previous lowering decision. An ARCHIVED or RETIRED project is refused
   (`PROJECT_READ_ONLY`; reactivate it first).
4. **OFFICE is never lowered here** (`OFFICE_LOWERING_DENIED`, its own message): which OFFICE data classes may be
   processed outside the host is the company's authorisation (§23, `approved_data_classes` of the company
   authorisation), not an owner choice, whoever the actor is. Raising an OFFICE project stays allowed.
5. **A task's class at compile time** (`planning.dag.task_classification`, amends the ADR-0033 default): a task whose
   plan states no class takes its project's class (a PUBLIC project's task is PUBLIC); a stated class can only raise
   it (the higher of the two); an unknown or invalid label, of the task or of the project, becomes the strictest
   class, RESTRICTED, and the compile result lists it under `classification_findings` (`TASK_CLASS_UNKNOWN` /
   `PROJECT_CLASS_UNKNOWN`, with the task key, the label and "compiled as RESTRICTED (the strictest class)"), as does
   the `plan.dag.compile` audit record.
6. **The class routing applies** (`routing.task_class.effective_task_class`, the one helper every routing reader
   uses: selection, fallback, an evaluation-bound selection, the capacity/PUBLIC-share check, the per-request check of
   a decision C route in the broker, the paid escalation and admission of ADR-0128 and the evaluation executor's route
   check; a test fails on any other routing reader of `task.data_classification`) is the higher of the task's compiled
   class and its project's current class. So a raise stops a less restrictive route from taking the project's open
   tasks at once, also at the next model call of a running decision C run. A lowering never relaxes a compiled task.
7. **In-flight work is never downgraded.** TaskSpecs are immutable (DB trigger) and are never rewritten; after a
   lowering, existing tasks keep their class, and only plans and task DAGs compiled afterwards use the new floor. The
   command prints the existing TaskSpec counts per class and this rule.
8. **Revocation fails closed.** `aeos policy revoke <decision id>` on the lowering decision that still backs the class
   restores the class it lowered from (audit record with `trigger` `decision_inactive`, event, actor `policy`). A
   superseded decision, or one a raise already ended, restores nothing.
9. **No password, because the owner approves in chat.** Lowering without a password prompt is allowed only because
   the owner approves the change in chat and the maintainer runs the command as the owner's actor (owner rule R43,
   2026-10-28). The records show it: `actor_check` in the decision and the audit record states "named active
   SUPER_ADMIN, no password prompt (CLI): allowed only because the owner approves in chat and the maintainer runs it
   as the owner's actor (owner rule R43); approval_ref names that approval", and `--approval-ref` (for example
   "chat 2026-10-28, owner rule R43") is stored in the decision, the audit record and the event. Without it the
   records say `approval_ref` null and the command prints "not given"; nothing is invented. No password prompt is
   added.
10. **No other writer.** `PATCH /api/projects/{id}` keeps refusing `data_classification` (`FIELD_NOT_EDITABLE`, now
    with a message that names this command): a field edit would skip decisions 3, 4 and 9 and create a second path
    that could drift from the first (one control plane, U-12). The generic decision path (`aeos policy decide`,
    `POST /api/decisions`) refuses the target (`DEDICATED_ENDPOINT_REQUIRED`), so a lowering decision never exists
    without the change it describes.
11. **Each refusal is durable.** A policy refusal (OFFICE, actor role, actor scope, unknown actor) rolls back the
    unit of work and commits a DENIED audit record with its code; validation errors (class, reason, domain) record
    nothing.

## Consequences

* After the owner's command, the DLY AEOS project is PUBLIC; plans compiled afterwards produce PUBLIC tasks unless
  the plan states a higher class for a task, and DeepSeek on OpenCode Go and the free OpenRouter WORKER routes may
  take them (ADR-0041 decision C, ADR-0124 ordering). Tasks already compiled stay INTERNAL. The outgoing request
  guard (ADR-0127) still refuses any request carrying a credential, whatever the class.
* Residual risk of decision 9: any local process that can run the CLI and names the owner's username can lower a
  PERSONAL project's class; `aeos policy decide` and `aeos routes activate` ask for the password. The decision, the
  audit record and the event make such a change visible with the approval it claims, it is limited to PERSONAL, and
  revoking it restores the class.
* Decision 6 costs one primary-key read of the project per routing check. The PUBLIC-share accounting of runs that
  already ran (`capacity._public_runs`) still counts by the class a run's task was compiled with: it describes what
  ran, not what may run.
* A test fixture that put a PUBLIC TaskSpec into an INTERNAL project (which the compiler never produces) now sees
  that task routed as INTERNAL; the broker proxy fixture uses a PUBLIC project.
* Evidence: E2 on macOS arm64. `tests/unit/test_portfolio_reclassify.py` (each refusal red with its own message next
  to an allowed near miss; all red without the change, and the revocation test red without the registry hook alone;
  the owner command of OWNER_ACTIONS 2.13 runs as written against a temporary database seeded like a real one) and
  `tests/unit/test_task_classification.py` (compile rule and routing floor; the behaviour tests red without the
  change, the near misses green). Windows and Intel macOS: NOT-PROVEN until CI runs there. The owner's real database
  is not touched by the tests.
