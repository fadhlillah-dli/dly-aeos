### Changed

- **A task's data class follows its project, and routing applies the project's current class.** At DAG compile a
  task whose plan states no class takes its project's class (it was always INTERNAL), a stated class can only raise
  it, and an unknown label of the task or the project becomes RESTRICTED with a `classification_findings` entry in
  the compile result and its audit record (it was treated as INTERNAL). Routing (selection, fallback, the
  evaluation-bound selection, the capacity check, the per-request check of a decision C route and the evaluation
  executor's route check) uses the higher of the task's compiled class and its project's current class through one
  helper, so raising a project stops less restrictive routes from taking its open tasks at once; a lowering never
  relaxes a compiled task. E2: `tests/unit/test_task_classification.py` and `tests/unit/test_portfolio_reclassify.py`
  on macOS arm64 (behaviour tests red without the change, near misses green; the documented owner command runs as
  written against a temporary database); NOT-PROVEN on Windows and Intel macOS until CI runs there.
