### Added

- **`aeos project reclassify`: one audited owner path changes a project's data classification (ADR-0133).** The
  owner decided on 2026-10-29 that the DLY AEOS project (seeded INTERNAL) becomes PUBLIC, so that PUBLIC-only routes
  (DeepSeek on OpenCode Go, the free OpenRouter models) can take its tasks; until now no path existed (PATCH refuses
  the field, and the DAG compiler lifts every task to the project class). A raise is allowed for an active
  SUPER_ADMIN with an audit record and an event. A lowering is a POLICY decision on
  `project_classification/<project id>` plus the audit record and the event, all naming old class, new class, reason
  and actor; it needs an active SUPER_ADMIN, and an OFFICE project is never lowered (company authorisation, master
  §23). Existing TaskSpecs keep their class; only plans and task DAGs compiled afterwards use the new floor, and the
  command prints the counts that stay. Revoking the lowering decision restores the previous class; a raise ends it.
  `PATCH /api/projects/{id}` still refuses `data_classification`, now naming the command, and `aeos policy decide`
  refuses the target. The command names the actor without a password prompt (the owner approves in chat and the
  maintainer runs it, as for `aeos plan`); the decision states that. Owner step: `docs/OWNER_ACTIONS.md` 2.13. E2:
  `tests/unit/test_portfolio_reclassify.py` (14 tests, each refusal red with its own message next to an allowed near
  miss, all red without the change; the documented owner command runs as written against a temporary database) on
  macOS arm64; NOT-PROVEN on Windows and Intel macOS until CI runs there.
