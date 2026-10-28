### Security

- **A project with content is never lowered, and copies keep the class of their source (ADR-0133 decisions 17-18).**
  A delta review reproduced three more leaks through derived copies after a lowering: a compile copied an INTERNAL-era
  PRD section or documentation checkpoint into a PUBLIC task's inputs (labelled with the task's class), a re-render
  dated INTERNAL-era plan content now, and the runtime reused an unrebuilt INTERNAL pack for a PUBLIC task. Since each
  new copy path could leak again, `aeos project reclassify` now refuses a lowering (`LOWERING_BLOCKED_BY_CONTENT`,
  nothing changed) while the project has any plans, plan versions, planning contexts or runs, task specs, worker runs,
  reviews, documents or revisions, memory, attachments, evidence, context packs, decision requests, eval case
  candidates, export snapshots or change proposals; the message counts each kind and says to archive or delete them
  first (plan versions, task specs and evidence are append-only, so such a project keeps its class). Raises stay
  allowed. As defence in depth, the compiler records the class of every input artifact, the pack builder and the
  runtime withhold any artifact above the task's class (in the pack and in the prompt), and a pack above the task's
  class is never handed to a worker. On the owner's Mac the DLY AEOS project holds one evidence record (read-only
  count), so the owner command is refused there until the owner decides. E2 on macOS arm64: each fix disabled in turn
  turns its test red (the lowering went through, the INTERNAL pack was handed over, nothing was withheld, no class was
  recorded); NOT-PROVEN on Windows and Intel macOS until CI runs there.
