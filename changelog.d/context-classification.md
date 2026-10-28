### Security

- **A lowered project's older data never reaches a PUBLIC task or route (ADR-0133 decisions 12-16).** Found by a
  blind review before merge: after INTERNAL -> PUBLIC, a PUBLIC task's context pack floored every item at the current
  project class and nothing enforced the pack's class, so INTERNAL memory, attachments, approved plans, `policy/`
  files and the VERIFIED local MCI source could reach a training-capable PUBLIC-only route (reproduced: INTERNAL
  memory in the worker prompt). Now every context item keeps the class it had when it was created (its own label, else
  the project's class at that time from the reclassification audit trail; the highest class it ever had when unknown),
  a task-bound pack leaves out every item above the task's effective class (listed in `omitted` with a classification
  reason), an attachment input above it is withheld from the prompt too, the MCI source never goes into a PUBLIC
  task's pack, and the executor refuses to start a run whose pack class the route may not carry
  (`CONTEXT_CLASS_NOT_ALLOWED`). The broker checks the run's effective task class on every request for every route
  (403 `TASK_CLASS_NOT_ALLOWED`, nothing sent), so a raise stops a running run's next model call; before, only decision
  C routes were checked. Revoking the newest of two lowerings restores the class before the first (`restore_to`), and
  an unknown stored label is read, recorded and restored as RESTRICTED. A lowering reports how many memory records,
  attachments and evidence items stay above the new class. E2 on macOS arm64: `tests/unit/test_context_classification.py`
  and the reclassify, task-class and executor tests, red before the change with the leak, an HTTP 200 and a started
  run; NOT-PROVEN on Windows and Intel macOS until CI runs there.
