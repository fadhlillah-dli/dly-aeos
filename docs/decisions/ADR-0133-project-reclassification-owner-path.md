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
   tasks at once. A lowering never relaxes a compiled task. The broker checks it on every model request for every
   route (decision 13).
7. **In-flight work is never downgraded.** TaskSpecs are immutable (DB trigger) and are never rewritten; after a
   lowering, existing tasks keep their class, and only plans and task DAGs compiled afterwards use the new floor. The
   command prints the existing TaskSpec counts per class and this rule.
8. **Revocation fails closed.** `aeos policy revoke <decision id>` on the lowering decision that still backs the class
   restores the class before its chain of lowerings (audit record with `trigger` `decision_inactive`, event, actor
   `policy`). A lowering that supersedes an earlier ACTIVE lowering carries `restore_to`, the class before the first
   (CONFIDENTIAL -> INTERNAL -> PUBLIC, revoke the newest: CONFIDENTIAL, never the INTERNAL that no ACTIVE decision
   backs any more). A superseded decision, or one a raise already ended, restores nothing.
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
12. **What a task's context may carry** (`knowledge.packs`, review H1). Every context item keeps the class it had when
    it was created: its own label (memory, attachments, evidence, policy files, research, the TaskSpec), else the
    project's class at the record's creation time, read from the append-only audit trail of reclassifications
    (`portfolio.facade.classification_at`; the highest class the project ever had when the creation time is unknown,
    for example metadata search results), never below the current project class. Repository files at the task's
    commit take the project's current class: the lowering decision is a decision about the repository's data (for
    DLY AEOS: it is public). A task-bound pack's ceiling is the task's effective class (decision 6): an item above it
    is left out and listed in `omitted` with "classification X above the task's class Y"; the owner-local MCI source
    is never read into a pack for a PUBLIC task, whatever its status; and an attachment input above the ceiling is
    withheld from the prompt as well (`worker_runtime.executor.resolve_artifacts`; an input known only to the planning
    context, without a class, takes the highest class the project ever had). A pack built before a lowering is
    not reused when its class is above the task's (it is rebuilt).
13. **The broker checks the class on every request, every route** (`broker.model_proxy.task_class_refusal`, review M1):
    before a request counts, the run's task at its effective class must be allowed by the route's
    `allowed_data_classes` and its data terms (`routing.facade.run_class_problem`), else 403 `TASK_CLASS_NOT_ALLOWED`
    and nothing is sent. A grant without a task (a route probe) is not judged there; a task that cannot be read is
    refused.
14. **The executor never starts a run whose context the route may not carry** (`CONTEXT_CLASS_NOT_ALLOWED`, run
    BLOCKED, nothing spawned, workspace removed): the pack's class must be allowed by the route and its terms.
15. **What stays above the new class is reported by the refusal** (superseded in round 3): the earlier report of memory
    records, attachments and evidence above the new class is replaced by decision 17 - a lowering with any content is
    refused and the refusal counts every kind.
16. **An unknown stored label** (for example `Secret`) is read as RESTRICTED, as routing reads it: a lowering from it
    records `from_classification` RESTRICTED with the raw label in `from_label`, a revocation restores RESTRICTED, and
    `--to RESTRICTED` normalises the label. No CHECK constraint is added: SQLite adds one only by rebuilding the
    `projects` table (Alembic batch mode), which would have to drop and re-create its immutable-domain trigger, would
    fail on exactly the unknown labels this handles, and the repository has no CHECK constraint to follow; every write
    path validates instead (create normalises, reclassify normalises, PATCH refuses), and tests pin each one.
17. **A lowering needs a project without content** (review round 3: per-source labels kept leaking through new copy
    paths - a compile after the lowering copies an INTERNAL-era PRD section or documentation checkpoint into a PUBLIC
    task's inputs, a re-render dates INTERNAL-era plan content now, an unrebuilt pack is reused). The lowering is refused
    (`LOWERING_BLOCKED_BY_CONTENT`, nothing changed, DENIED audit record) while the project has any row in the content
    tables (`portfolio.reclassify.CONTENT_TABLES`: plans, plan versions, planning contexts, planning runs, task specs,
    worker runs, reviews, documents, document revisions and checkpoints, memory records, attachments, evidence, context
    packs, decision requests, eval case candidates, export snapshots, change proposals); the message gives the count
    per kind and says to archive or delete them first. No archive tool is built. Plan versions, task specs and
    evidence are append-only, so a project that has any of them can never be lowered: the message says so, and work
    that may be PUBLIC needs a new project created at PUBLIC. Records about the system or references only (audit trail,
    events, decisions, grants, broker and usage records, repositories, integrations, pull requests, executions,
    acceptance hashes, publications, ops records) are not content; a test fails when a new project table is neither
    in the list nor named as not content. Raises stay always allowed. Decisions 12-15 stay as defence in depth for a
    project lowered before this rule or by a direct database edit.
18. **Copies carry the class of their source** (review round 3, F1 and F3, defence in depth). The task compiler
    records a `classification` on every compiled input artifact (a PRD section: the project's class when its plan
    version was created; an attachment: its label, else the project's highest class; a documentation checkpoint: the
    class when docs_engine recorded it). The pack builder and `resolve_artifacts` use that recorded class for every
    artifact but a plain label (none recorded: the project's highest class) and withhold any artifact above the task's
    effective class, in the pack and in the prompt. `_context_pack` checks the newest task pack against the task's
    effective class when it is used - a pack above it is never given to the worker, also when it was not rebuilt. The
    ratchet of decision 6 also scans `planning/dag.py` and `docs_engine/`.
19. **Platform probe evidence is not content** (maintainer decision 2026-10-29 within the owner's R46). The content
    check of decision 17 does not count an evidence row that is a platform probe: `run_id` and `task_id` both NULL and
    its kind on an explicit allow-list, `portfolio.reclassify.EXEMPT_PROBE_KINDS`, which holds `PROVIDER_CAPABILITY`
    only - the code host's merge-gate capability probe (`aeos delivery capabilities --record`, origin collector),
    platform metadata read by the Doctor and the PR bundle. It is safe because it never reaches a worker: a task pack
    selects only that task's own evidence; the documentation engine selects `SIGNAL_EVIDENCE_KINDS`, which exclude
    it; planning and the Claude bridge read no evidence; and its one copy, the PR bundle evidence, is stored INTERNAL,
    which the pack ceiling withholds from a lower-class task. `PROBE` was considered and left out: several collectors
    write it (route feasibility, the Claude bridge test, containment), so it is not one kind of platform metadata.
    Everything else still blocks - an allow-listed kind with a run or a task, and any other kind. The refusal and the
    success output state how many probe records were not counted, and the decision and audit record carry
    `exempt_probe_evidence`. A test builds a task pack and the worker prompt for a project holding such a probe and
    finds no trace of it, and a ratchet fails when a pack or prompt builder (knowledge, worker runtime, documentation
    engine, planning, Claude bridge) names an exempt kind or the task-pack runtime collector stops selecting only the
    task's own evidence.

## Consequences

* On this Mac the DLY AEOS project holds no content and one `PROVIDER_CAPABILITY` probe without a run or task
  (read-only count, 2026-10-29), so with decision 19 the owner's command goes through; once the project has plans or
  tasks a lowering is refused. A project without content that is lowered becomes PUBLIC; plans compiled afterwards produce PUBLIC tasks unless the plan states a
  higher class for a task, and DeepSeek on OpenCode Go and the free OpenRouter WORKER routes may take them (ADR-0041
  decision C, ADR-0124 ordering). The outgoing request guard (ADR-0127) still refuses any request carrying a
  credential, whatever the class.
* Residual risk of decision 9: any local process that can run the CLI and names the owner's username can lower a
  PERSONAL project's class; `aeos policy decide` and `aeos routes activate` ask for the password. The decision, the
  audit record and the event make such a change visible with the approval it claims, it is limited to PERSONAL, and
  revoking it restores the class.
* Decision 6 costs one primary-key read of the project per routing check. The PUBLIC-share accounting of runs that
  already ran (`capacity._public_runs`) still counts by the class a run's task was compiled with: it describes what
  ran, not what may run.
* A test fixture that put a PUBLIC TaskSpec into an INTERNAL project (which the compiler never produces) now sees
  that task routed as INTERNAL; the broker proxy fixture uses a PUBLIC project. The shared test route allows PUBLIC and
  INTERNAL unless a test says otherwise (a route that allows no class can never be selected).
* What a PUBLIC task of a lowered project no longer receives (decision 12): the `policy/` files and research entries
  (labelled INTERNAL), records created while the project was INTERNAL, metadata search results (creation time
  unknown) and evidence (labelled INTERNAL unless its collector states another class). It still receives the repository at its commit, the
  TaskSpec and everything created as PUBLIC afterwards. Labelling the committed `policy/` files PUBLIC (they are in the
  public repository) would be a separate decision.
* Evidence: E2 on macOS arm64. `tests/unit/test_portfolio_reclassify.py` (each refusal red with its own message next
  to an allowed near miss; all red without the change, and the revocation test red without the registry hook alone;
  the owner command of OWNER_ACTIONS 2.13 runs as written against a temporary database seeded like a real one) and
  `tests/unit/test_task_classification.py` (compile rule and routing floor; the behaviour tests red without the
  change, the near misses green; a ratchet refuses any unmarked reader of `task.data_classification` in routing,
  knowledge and the worker runtime, red on the earlier pack builder) and `tests/unit/test_context_classification.py`
  (after INTERNAL -> PUBLIC, INTERNAL memory, an INTERNAL attachment, an INTERNAL plan and the MCI source are absent
  from a PUBLIC task's prompt - red before: the INTERNAL memory reached it - while PUBLIC ones are present; a raise
  refuses the running run's next request - red before: HTTP 200); the executor refusal in
  `tests/unit/test_worker_runtime_executor.py` (red before: the run reached REVIEW). Round 3, each disabled in turn and
  red: the content refusal (one parametrized test over all 17 content kinds plus the P1/P2 test - with the check off
  the lowering went through), the pack check at use (F3 - the INTERNAL pack was handed over), the artifact ceiling
  (F1 - nothing was withheld) and the compile-time class (F1 - no class recorded). Windows and Intel macOS:
  NOT-PROVEN until CI runs there. The tests never open the owner's real database; the live count above was a separate
  read-only query (`mode=ro&immutable=1`).
