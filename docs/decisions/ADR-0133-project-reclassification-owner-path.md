# ADR-0133 — Project data classification changes through one owner path: a raise is audited, a lowering is a POLICY decision, OFFICE is never lowered (§04, §20, §23, §24)

* Status: ACCEPTED (owner decision R46, 2026-10-29)
* Date: 2026-10-29
* Deciders: owner (Fadhlillah), decision of 2026-10-29 to reclassify the DLY AEOS project from INTERNAL to PUBLIC
  ("Ya, ubah ke PUBLIC", owner request R46, following R45); design by the delegating session. The owner can still
  supersede it as an `ADR` decision (see the register README).
* References: [ADR-0020](ADR-0020-domain-first-project-registry.md) (domain-first registry; the domain stays
  immutable), [ADR-0031](ADR-0031-decision-binding-and-external-write-interlock.md) (decisions bound to exact
  targets), [ADR-0033](ADR-0033-taskspec-compile-defaults.md) (TaskSpec compile defaults),
  [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) (decision C: free OpenRouter WORKER routes serve
  PUBLIC data only), [ADR-0127](ADR-0127-outgoing-model-request-secret-guard.md) (no credential leaves the host in a
  model request, on any route). None of them is edited.

## Context

* The owner, 2026-10-29: the DLY AEOS platform project (`project_key` `dly-aeos`, domain PERSONAL, `is_platform`)
  must become PUBLIC. Its repository is public on GitHub, and the owner wants the best PUBLIC-only routes (DeepSeek
  on OpenCode Go, the free OpenRouter models) to take its tasks (R45: "manfaatkan model terbaik OpenRouter dan
  OpenCode semaksimal mungkin"; R46: "Ya, ubah ke PUBLIC").
* That was impossible. `aeos seed` registers the project INTERNAL (`portfolio/seed.py`); `PATCH /api/projects/{id}`
  edits only name, description, idea and lifecycle (`portfolio.service.EDITABLE_FIELDS`); and the DAG compiler gives
  every task the higher of the project class and the class its plan states (`planning/dag.py`), so a PUBLIC task of
  an INTERNAL project is compiled INTERNAL and never reaches a PUBLIC-only route.
* Master §04: the project record carries `data_classification`; the domain is never changed (a domain move is a
  migration). Nothing in the master fixes the class forever, but §20/§19 make policy changes human AUTH recorded as
  decisions, and §23 makes OFFICE data classes the company's authorisation ("OFFICE default
  external_worker_allowed=false dan approved_worker_routes kosong sampai otorisasi perusahaan", lines 654-656).
* The owner's standing rule of 2026-10-29 (R43, "Saya setujui via chat, Anda jalankan"): the owner approves in chat
  and the maintainer runs the command as the owner's actor; the audit records it.

## Decision

1. **One path.** `aeos project reclassify --project <id|key> --domain <PERSONAL|OFFICE> --to <class> --reason <text>
   --actor <SUPER_ADMIN username>` (`aeos.portfolio.reclassify`) is the only way a project's class changes after
   creation. The domain is explicit and must be the project's domain; a project of the other domain is "not found".
   The class must be PUBLIC, INTERNAL, CONFIDENTIAL or RESTRICTED (`INVALID_CLASSIFICATION` otherwise, any case
   accepted); the reason needs at least 10 characters and names the owner's approval. The same class again records
   nothing.
2. **Raising is always allowed** (it only narrows which routes may see the data), in both domains, for an actor who
   may write: an active SUPER_ADMIN whose domain/project scope covers the project (VIEWER is read-only, §02). It
   writes one audit record `portfolio.project.reclassify` and one event `portfolio.project.reclassified`, both naming
   old class, new class, direction, reason and actor. No decision is needed. A raise ends (INVALIDATED, with the
   actor and reason) the project's ACTIVE lowering decision, which no longer describes the class.
3. **Lowering is a POLICY decision.** The actor must be an active SUPER_ADMIN (`LOWERING_NEEDS_SUPER_ADMIN`), and
   the decision registry re-checks it and its scope. The decision is written by `aeos.policy.reclassification`,
   which the portfolio module reaches only through `aeos.policy.facade` (module boundaries, §10). One transaction
   records:
   * the decision: kind POLICY, target `project_classification/<project id>`, `target_version` = the new class,
     `target_hash` = hash of project id, old and new class, allowed operation `lower_data_classification`,
     rationale = the reason, details `from_classification`, `to_classification`, `actor_username`, the in-flight
     TaskSpec counts and `actor_check` (below); the registry adds its own `decision.record` audit record and
     `decision.recorded` event and refreshes the policy snapshot;
   * the class change on the project;
   * the audit record and the event of decision 2 with `direction` LOWER and the decision id.
   A newer lowering supersedes the previous lowering decision. An ARCHIVED or RETIRED project is refused
   (`PROJECT_READ_ONLY`; reactivate it first).
4. **OFFICE is never lowered here** (`OFFICE_LOWERING_DENIED`, its own message): which OFFICE data classes may be
   processed outside the host is the company's authorisation (§23, `approved_data_classes` of the company
   authorisation), not an owner choice, whoever the actor is. Raising an OFFICE project stays allowed.
5. **In-flight work keeps its class.** TaskSpecs are immutable (DB trigger) and carry the class they were compiled
   with; routing, review and context packs read the task's class. No existing TaskSpec is rewritten, so planned or
   running work is never silently downgraded. Only plans and task DAGs compiled after the change use the new floor,
   and a task there is PUBLIC only when its plan states PUBLIC. The command prints the existing TaskSpec counts per
   class and this rule. After a raise, open tasks below the new class keep their class for routing until they end;
   the delivery gate G0 refuses a task below the project floor, and the command lists those tasks so the owner can
   cancel and re-plan them.
6. **Revocation fails closed.** `aeos policy revoke <decision id>` on the lowering decision that still backs the class
   restores the class it lowered from (audit record with `trigger` `decision_inactive`, event, actor `policy`). A
   superseded decision, or one a raise already ended, restores nothing.
7. **No other writer.** `PATCH /api/projects/{id}` keeps refusing `data_classification` (`FIELD_NOT_EDITABLE`, now
   with a message that names this command): a field edit would skip decisions 3 and 4 and create a second path that
   could drift from the first (one control plane, U-12). The generic decision path (`aeos policy decide`,
   `POST /api/decisions`) refuses the target (`DEDICATED_ENDPOINT_REQUIRED`), so a lowering decision never exists
   without the change it describes.
8. **The actor is named, not authenticated by password.** Like `aeos plan`, the command names an active
   SUPER_ADMIN and asks no password, because the owner's rule is that the maintainer runs it as the owner's actor
   after the owner approves in chat. The decision says so (`details.actor_check` = "named active SUPER_ADMIN (CLI,
   no password prompt)"), and the reason names the approval.
9. **Each refusal is durable.** A policy refusal (OFFICE, actor role, actor scope, unknown actor) rolls back the
   unit of work and commits a DENIED audit record with its code; validation errors (class, reason, domain) record
   nothing.

## Consequences

* After the owner's command, the DLY AEOS project is PUBLIC; plans created afterwards can produce PUBLIC tasks,
  which DeepSeek on OpenCode Go and the free OpenRouter WORKER routes may take (ADR-0041 decision C, ADR-0124
  ordering). Tasks already compiled stay INTERNAL. The outgoing request guard (ADR-0127) still refuses any request
  carrying a credential, whatever the class.
* Residual risk of decision 8: any local process that can run the CLI and names the owner's username can lower a
  PERSONAL project's class; `aeos policy decide` and `aeos routes activate` ask for the password. The decision, the
  audit record and the event make such a change visible, it is limited to PERSONAL, and revoking it restores the
  class. A password (or a typed confirmation) can be added later without changing the records.
* Residual risk of decision 5: after a raise, a task that is already open keeps routes allowed for its lower class
  until it ends; G0 refuses its delivery. Re-evaluating open tasks against the current project class at dispatch is
  a routing change outside this ADR.
* The DAG compiler is unchanged: a task whose plan states no class still defaults to INTERNAL (never below the
  floor), and an unknown task class is treated as INTERNAL there.
* Evidence: E2, `tests/unit/test_portfolio_reclassify.py` on macOS arm64 (each refusal red with its own message next
  to an allowed near miss; all 14 tests red without the change, and the revocation test red without the registry
  hook alone; the owner command of OWNER_ACTIONS 2.13 runs as written against a temporary database seeded like a
  real one). Windows and Intel macOS: NOT-PROVEN until CI runs
  there. The owner's real database is not touched by the tests.
