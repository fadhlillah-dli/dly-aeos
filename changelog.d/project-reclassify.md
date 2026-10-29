### Added

- **`aeos project reclassify`: one audited owner path changes a project's data classification (ADR-0133).** The
  owner decided on 2026-10-28 that the DLY AEOS project (seeded INTERNAL) becomes PUBLIC, so that PUBLIC-only routes
  (DeepSeek on OpenCode Go, the free OpenRouter models) can take its tasks; until now no path existed (PATCH refuses
  the field, and the DAG compiler made a task with no stated class INTERNAL). A raise is allowed for an active
  SUPER_ADMIN with an audit record and an event. A lowering is a POLICY decision on
  `project_classification/<project id>` plus the audit record and the event, all naming old class, new class, reason,
  actor and the owner's approval (`--approval-ref`); it needs an active SUPER_ADMIN, and an OFFICE project is never
  lowered (company authorisation, master §23). The command asks no password: that is allowed only because the owner
  approves in chat and the maintainer runs it as the owner's actor (owner rule R43), and the decision and audit record
  state it. Existing TaskSpecs are never rewritten; revoking the lowering decision restores the previous class.
  `PATCH /api/projects/{id}` still refuses `data_classification`, now naming the command, and `aeos policy decide`
  refuses the target. Owner step: `docs/OWNER_ACTIONS.md` 2.13.
