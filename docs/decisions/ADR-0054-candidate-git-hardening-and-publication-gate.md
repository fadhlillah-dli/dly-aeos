# ADR-0054 — Candidate git hardening, quarantine release and the publication gate (§09, §14, §21, §31, §50)

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); completeness review of module group E

## Context

The candidate workspace (ADR-0052) is edited by an untrusted worker, but the trusted runtime and
broker later run `git` on it (stage, diff, commit, fetch for the push mirror). Git executes
commands named in repository configuration: a worker that writes `core.fsmonitor`, a filter/diff
driver or a hook into a git directory it can reach would get that command executed by the
runtime, in the controller's context and with the controller's environment (verified on this host:
`git status`/`git add` run a planted `core.fsmonitor`). The worker can also rewrite the `.git`
pointer file of a linked/separate checkout to redirect runtime git to another repository.

Separately, the review found three open ends: (1) a quarantined patch (§21 "patch karantina yang
melalui review khusus sebelum dipromosikan") could still be accepted or published; (2) a push/PR
whose outcome was lost (crash/timeout) could not be reconciled, while §14 requires reading
"publication records dan status eksternal sebelum retry"; (3) the broker could publish a run that
was still running, failed or cancelled.

## Decision

1. **Trusted git directory.** The isolated clone (platform/self-build) keeps its git directory in
   `<run>/git` (`--separate-git-dir`), outside the tree the worker edits; `repo/.git` is only a
   pointer the runtime never follows. Every runtime git command on a candidate passes the git dir
   and work tree resolved at creation (`--git-dir/--work-tree`), `core.hooksPath=<empty dir>`,
   `core.fsmonitor=false`, `core.untrackedCache=false`, `--no-ext-diff --no-textconv`, and an
   environment without `GIT_*` overrides or secret-looking variables. `WorkerRun.git_dir_ref`
   records the trusted git dir; the broker fetches the task branch from it, not from the
   worker-editable tree.
2. **Control-file guard.** At creation the runtime fingerprints (and backs up) the git control
   files (`config`, `config.worktree` of the git dir and common dir, `info/attributes`); config is
   compared entry-wise ignoring `branch.*` so owner actions such as `push -u` do not trip it. The
   runtime hooks directory must stay empty. Before any runtime git command the guard re-checks; on
   any change it refuses to run git on that candidate (sticky for the run), restores the original
   control files, removes planted hooks, records an audit event
   `worker_runtime.git_control_tampered`, and the executor fails + quarantines the run
   (`git-control-modified`). Nothing is committed from a tampered candidate.
3. **Quarantine release is a human decision.** A quarantined patch can be accepted
   (`mark_accepted`) or published (push/PR) only with an ACTIVE human EXCEPTION Decision
   `target_type=run_quarantine`, `target_id=<run>`, `target_hash=<runtime head commit>`,
   recorded by the owner after the special review (`POST /api/runs/{id}/quarantine-release`,
   through the policy facade). A different head needs a new decision. The decision never accepts
   the task by itself: verifier + review + AcceptanceRecord are still required.
4. **Publication gate.** The broker pushes/opens a PR only for a run in REVIEW or DONE with a
   runtime-recorded head commit, pushes exactly that commit (`expected_head` pinned to it), and
   refuses quarantined runs without the release decision.
5. **Outbox reconciliation.** A push that times out during the push stage is recorded `UNKNOWN`
   (it may have landed), not `FAILED`. `reconcile_side_effect` (admin
   `POST /api/broker/side-effects/{id}/reconcile`) reads the external state — `git ls-remote` of the
   task branch against the expected commit, or the GitHub PR list for head/base — and records
   SUCCEEDED/FAILED; a read failure keeps UNKNOWN. It never pushes, creates or merges anything.
   Admission (and readiness) keep a task waiting with `SIDE_EFFECT_UNRECONCILED` while an earlier
   attempt has a PENDING/UNKNOWN side effect, and with `LEASE_LOST_PROCESS_ALIVE` while an earlier
   worker process is alive.
6. **Versioned configuration.** Each WorkerRun records the revision hash of the RoleSkillPack and
   AgentDefinition it ran with (`role_revision_hash`, `agent_revision_hash`), so a changed role or
   agent definition never rewrites the history of earlier attempts (§50 "Role/model/mode diganti").
7. **Verifier availability.** Admission waits with `VERIFIER_UNAVAILABLE` when the verification
   module (acceptance path) is absent: no worker is started whose result could never be accepted
   (§19 "siapa verifier dan penerima hasilnya").

## Consequences

* A worker cannot use git configuration, hooks or the `.git` pointer to execute code through the
  runtime or to redirect runtime git; attempts are detected, reverted, audited and quarantined.
* Residual risk (process sandbox, ADR-0051): the worker runs as the same OS user, so it can still
  reach files outside the worktree by absolute path; the guard covers git-mediated execution only.
* Residual risk (worktree strategy for user projects): the common git directory is the owner's
  repository. A change made by one run is restored when that run finalizes, but a second run
  created on the same repository in between snapshots the already-changed config. Concurrency is
  1 by default (2 only with an owner decision); projects that need stronger isolation can set
  `RepositoryReference.details.isolated_clone=true`.
* Quarantined work stays visible as a candidate until a human records the special-review release;
  there is no automatic or model-made release.
