# ADR-0052 — Self-build zones: candidate workspace, runtime commits and quarantine (§09, §21, §50)

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)

## Context

§09 separates trusted seed/released controller, candidate build workspace and accepted release:
workers may propose controller/policy/CI changes in the candidate but can never load them into the
active controller, lower a grader, change approval policy or raise their own capability. AGENTS.md
lists protected paths whose patches are quarantined. §21: worker output that touches pipeline/IAM/
policy stays a quarantined patch for special review.

## Decision

1. Each run gets `DataLayout.task_data(domain, project_key)/<run_id>/` containing `repo/` (the
   candidate) and `sandbox/` (HOME/TEMP/config, outside the candidate). The workspace is refused if
   it would live inside, or contain, the running controller checkout.
2. Strategy: user projects use `git worktree add -b aeos/<task>-<run8> <dir> <base>`. The platform
   project (DLY AEOS self-build) uses an **isolated clone** (`git clone --no-hardlinks`, remote
   removed) of the trusted controller repository, so the candidate shares no files with the
   controller — not even `.git` hooks, refs or objects. This tightens the task wording ("git worktree
   add") because a shared `.git` would let a worker plant hooks that run in the owner's trusted
   checkout.
3. Runtime git operations run with `core.hooksPath` pointing at an empty directory. The runtime —
   never the worker — stages and commits the candidate on the task branch with the identity
   `AEOS Runtime <aeos-runtime@localhost>`; the diff is computed by the runtime against the base.
4. Scope check on the runtime diff: paths outside `allowed_paths`, matching `forbidden_paths`,
   unsafe paths, `.git` internals or symlinks escaping the worktree are violations (run FAILED,
   patch kept and quarantined). Paths matching the protected list (AGENTS.md for the platform
   project; trusted CI definitions for every project) or added symlinks are quarantined for special
   review (run REVIEW, `quarantined=true`). Forbidden/protected matching is case-insensitive.
5. After every run the executor asserts that no module loaded in the controller process comes from
   the candidate directory.

## Consequences

* A candidate cannot alter the running dashboard/audit/verifier/controller to fake a pass (§50);
  promotion stays a human-controlled release step outside this module.
* Isolated clones cost disk/time for large repositories; acceptable for the pilot.
* User-project worktrees still share `.git` with the user's repository (residual risk, ADR-0051).
