# ADR-0107 — Verifier isolated runner and candidate-code tamper detection (§20, §21, §25)

* Status: PROPOSED
* Date: 2026-10-19
* Deciders: owner (Fadhlillah) — proposed by the K2-isolation re-audit closure

## Context

The independent verifier executes code the worker wrote: the trusted acceptance suite against the
candidate, the candidate's own unit tests and (new here) a schema-migration round-trip. Until now that
code ran only in the process runner — a child process of the same OS user as the controller, with a
scrubbed environment. The re-audit (master §20 "Verifier ENF: read-only suite, isolated runner,
evidence collector"; §21 "workers may not change approval records / policy / routing policy"; §25
"Worker tidak mendapat shell host / credential store") found the gap honestly: there was **no isolated
runner in code**, only env scrubbing with `PYTHON_KEYRING_BACKEND` set to the `fail` backend and a
dead-proxy — a deterrent, not a boundary. Worker-written candidate code could therefore reach the OS
keyring (OpenBao AppRole SecretID), the GitHub CLI `hosts.yml`, Git Credential Manager, the AEOS data
DB and the controller checkout, and could write the controller's `.venv` editable-install finder that
maps `aeos` on the next start — none of which the checkout integrity snapshot covered.

## Decision

1. **Two runners, chosen per verification and stated on every report** (`verifier_isolation` /
   `verifier_runner`), `aeos.verification.runner`:
   * **container** (`CONTAINER`) — selected automatically only when `worker_runtime` reports the
     container boundary is proven on this host now (a fresh PASS **container** containment report for
     the domain profile, the same proof that admits container workers, ADR-0051) **and** the pinned
     verifier image (`sandbox/verifier.Dockerfile`, tagged with the controller version) is built. Each
     step is one `docker run --network none --read-only --cap-drop ALL --security-opt
     no-new-privileges --pull never` as a non-root user, mounting only the run directory (snapshot,
     trusted-suite copy and ini read-only; the report dir writable). Never the data dir/DB, the
     controller checkout, the host home, the docker socket, an SSH agent or a credential store.
   * **process** (`WEAK`) — the fallback. The environment now redirects *every* per-user location a
     credential-bearing tool discovers through the environment into the run's empty home
     (`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`XDG_*`, `GH_CONFIG_DIR`, docker/npm/Claude config),
     sets the **null** keyring backend (answers nothing, stores nothing — the `fail` backend raised,
     which invited a `keyring.set_keyring(...)` fallback in code), gives git no system/global config
     and an empty credential helper / no askpass / local-only transport, and sets no SSH agent socket
     or D-Bus session bus. This stops credential discovery *through the environment*; a candidate that
     opens a file by absolute path or calls the OS credential API directly is not stopped, and that
     residual risk is recorded on the report and on Doctor `evidence.verifier`, never presented as
     isolated.
2. **OFFICE and `verifier_policy.isolated_runner: required` fail closed.** An OFFICE verification, or a
   TaskSpec that requires the isolated runner, is BLOCKED (not run on the process runner) when the
   container runner is unavailable (§11/§23 OFFICE needs a verified strong boundary).
3. **Tamper window around candidate code** (`aeos.verification.tamper`, defence in depth for the WEAK
   runner): before the first candidate step the verifier fingerprints the controller checkout **and the
   controller interpreter's site-packages/scripts** (the editable-install finder/`.pth`, startup hooks
   and the platform dist-info are content-hashed; other files by size+mtime) and digests the DB trust
   tables (decisions, policy snapshots, model routes, agents, budgets, users, install manifests,
   TaskSpecs/PlanVersions, acceptance/review/evidence). The controller's own legitimate writes in that
   window are journaled (SQLAlchemy events) so they are not mistaken for tampering. Any other changed
   row, or any changed controller/interpreter file, QUARANTINES the run for the owner's special review
   and raises a HIGH alert (`verification.controller_modified` / `verification.alert`) — never a PASS,
   never an ordinary FAIL-and-repair.
4. **Controller integrity now covers the interpreter.** `worker_runtime.integrity.snapshot` adds the
   venv site-packages and scripts, closing the §09 gap that a worker-written `.venv` editable
   finder/`.pth` (or any dependency file) was loaded on the next start undetected.
5. **Migration round-trip** (§16/§31): a migration/schema-impact change with an Alembic tree is verified
   by a real upgrade → downgrade(base) → upgrade and reopen (restore), run as candidate code through the
   runner; `migration_rollback` is PASS/FAIL, NOT-PROVEN only when there is no Alembic tree to run.

The verifier version becomes `3`, which makes earlier acceptance STALE by design.

## Residual risk / owner steps

* Until Docker is installed and `aeos worker containment --kind container` PASSes **and** the verifier
  image is built, the verifier runs on the WEAK process runner. Reads of owner files by absolute path
  or of the OS credential store by direct API call are not prevented there — only the container runner
  prevents them. This is stated on every report and in the Readiness Doctor; it is not hidden.
* The tamper window detects **writes** during a verification, not reads, and not a write that is undone
  before the window closes; the container runner (no data-dir mount) is the actual prevention.
* The container runner and image are implemented but NOT_PROVEN on a real Docker host here (Docker was
  not available on the build host); the AEOS side (selection, hardened argv, mounts, path mapping,
  lifecycle) is covered by a scripted-docker test.
