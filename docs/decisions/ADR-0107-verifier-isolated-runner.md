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
     controller checkout root, the host home, the docker socket, an SSH agent or a credential store;
     the trusted controller `backend/` is mounted read-only for the expected-id collection step alone,
     never for a step that runs candidate code (review 2026-10-19).
   * **process** (`WEAK`) — the fallback. The environment now redirects *every* per-user location a
     credential-bearing tool discovers through the environment into the run's empty home
     (`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`XDG_*`, the platformdirs `WIN_PD_OVERRIDE_*`
     variables - on Windows platformdirs asks the Known Folder API, not `LOCALAPPDATA` - `GH_CONFIG_DIR`,
     docker/npm/Claude config),
     sets the **null** keyring backend (answers nothing, stores nothing — the `fail` backend raised,
     which invited a `keyring.set_keyring(...)` fallback in code), gives git no system/global config
     and an empty credential helper / no askpass / local-only transport, and sets no SSH agent socket
     or D-Bus session bus. This stops credential discovery *through the environment*; a candidate that
     opens a file by absolute path or calls the OS credential API directly is not stopped, and that
     residual risk is recorded on the report and on Doctor `evidence.verifier`, never presented as
     isolated.
2. **OFFICE, container-produced runs and `verifier_policy.isolated_runner: required` fail closed.** An
   OFFICE verification, a worker run that executed in the container (or VM) sandbox
   (`WorkerRun.sandbox_kind`), or a TaskSpec that requires the isolated runner, is BLOCKED
   (`VERIFIER_ISOLATION_UNAVAILABLE`, not run on the process runner) when the container runner is
   unavailable (§11/§23 OFFICE needs a verified strong boundary). Isolation is never downgraded: code
   produced behind a kernel boundary is never executed on the host because the verifier image is
   missing or the containment report went stale between the worker run and its verification (review
   2026-10-19). Note: the planning compiler does not emit `isolated_runner` yet, so in practice the
   TaskSpec switch applies to TaskSpecs authored outside the compiler.
2a. **The run directory is private and outside the data dir.** Every path the process runner hands to
   candidate code (`HOME`, `TMP`, `AEOS_DATA_DIR`, `PYTHONPATH`, cwd, the snapshot's own `__file__`)
   lives in a fresh `0700` `mkdtemp` directory under the system temp dir. It used to live under
   `<data dir>/vw/`, which let candidate code derive the live DB path from its own environment
   (`Path(HOME).parents[2] / "aeos.sqlite3"`).
3. **Tamper window around candidate code** (`aeos.verification.tamper`, defence in depth for the WEAK
   runner): before the first candidate step the verifier fingerprints the controller checkout **and the
   controller interpreter's site-packages/scripts** (the editable-install finder/`.pth`, startup hooks
   and the platform dist-info are content-hashed; other files by size+mtime) and digests the DB trust
   tables (decisions, policy snapshots, model routes, agents, budgets, users, install manifests,
   TaskSpecs/PlanVersions, acceptance/review/evidence; since the review also server-side sessions, capability
   grants, projects and their integrations, held-out eval suites and the trust-relevant system settings -
   containment proofs, write enablement, flags, bootstrap) and the SQLite schema (tables, indexes and the
   append-only/immutability triggers, so dropping a trigger to rewrite history is caught). The
   controller's own legitimate writes in that
   window are journaled (SQLAlchemy events) so they are not mistaken for tampering. Any other changed
   row, or any changed controller/interpreter file, QUARANTINES the run for the owner's special review
   and raises a HIGH alert (`verification.controller_modified` / `verification.alert`) — never a PASS,
   never an ordinary FAIL-and-repair. A trust table larger than the per-table cap is only partly
   compared, so that window is UNKNOWN (a blocking NOT-PROVEN), never a silent PASS.
4. **Controller integrity now covers the interpreter.** `worker_runtime.integrity.snapshot` adds the
   venv site-packages and scripts, closing the §09 gap that a worker-written `.venv` editable
   finder/`.pth` (or any dependency file) was loaded on the next start undetected.
5. **Migration round-trip** (§16/§31): a migration/schema-impact change with an Alembic tree is verified
   by a real upgrade → downgrade(base) → upgrade and reopen (restore), run as candidate code through the
   runner (the throwaway DB lives in the report dir, the only writable mount of the container runner);
   `migration_rollback` is PASS/FAIL, NOT-PROVEN only when there is no Alembic tree to run. An unproven
   round-trip is a **blocking** NOT-PROVEN (§31 G2 needs a migration/rollback test per migration change),
   unless the human-approved TaskSpec sets `verifier_policy.migration_roundtrip: optional` (like
   `isolated_runner`, the planning compiler does not emit this switch yet).
6. **Impact does not yet widen the trusted test selection.** Impact categories add the checks above and
   the migration round-trip the verifier runs itself; verifier-authored negative/edge/adversarial cases
   per impact (§22) and integration-marked tests remain NOT-PROVEN and are stated as such.

The verifier version becomes `3`, which makes earlier acceptance STALE by design.

### Amendment (review W6-054 / W6-056 / W6-057 / W6-060, verifier version `4`)

7. **Result seal on every pytest step that runs candidate code** (`aeos.verification.seal`). Candidate code
   imported by a test runs inside the test process in *either* runner, so it could rewrite the junit file from
   an `atexit` hook and leave with `os._exit(0)` - a failing trusted suite then read as PASS (the audit's probe).
   The verifier now loads its own plugin `aeos_vseal` with `-p` before collection (first on `PYTHONPATH`, the name
   is refused as a candidate module). At import it reads a per-step random key from a key file in the report dir
   and deletes it, and duplicates the real stdout; while pytest runs it streams every outcome and, at the very end
   of `pytest_sessionfinish`, the exit status, the junit sha256 and pytest's own counts - HMAC-authenticated and
   sequence-numbered. The host requires a sealed session end, the same exit code, an unchanged junit digest, the
   same per-test outcomes and the same terminal summary. A disagreement is a `result` tamper finding
   (QUARANTINED, HIGH alert); no sealed session end FAILS the step. The import preflight and the migration
   round-trip driver seal their result the same way (a mapping or result an imported candidate module prints
   itself is never used; no sealed preflight result is a blocking NOT-PROVEN, a forged seal line is a `result`
   tamper finding). Residual (stated on every
   report, both runners): code that deliberately subverts the plugin inside the interpreter is not excluded, and
   black-box assertions from a separate trusted process are not implemented.
8. **Every process-runner step runs in its own kill group** (`aeos.verification.procgroup`): a Windows Job
   Object with `KILL_ON_JOB_CLOSE` (created suspended, assigned, then resumed) or a POSIX new session, plus an
   inherited environment marker. When the step's main process ends, whatever is still alive in the group is
   listed and killed before the next step and before the tamper window closes, and the verification is
   QUARANTINED (`step_containment`). The verifier no longer waits on the step's pipes, so a descendant holding
   stdout open cannot stall it either; a step whose output pipe stays open after its whole group was killed had
   a process outside the group and counts as a survivor. A step whose kill group fell back to the marker sweep
   (no Job Object) is stated as NOT-PROVEN. Residual: a process created outside the step's tree on the candidate's
   behalf (WMI/Task Scheduler/COM servers, a POSIX daemon that leaves the session and drops the marker,
   launchd/systemd units) is not seen.
9. **Foreign rows are neutralised, not only detected** (`aeos.verification.neutralize`): a foreign approval
   decision is INVALIDATED, an open decision request WITHDRAWN, the task's acceptance INVALIDATED, a session or
   capability grant revoked, a route DISABLED (ineligible until a human re-activates its configuration hash), a
   policy snapshot deactivated and the active one rebuilt, a containment proof removed and write enablement
   switched off - audited as `verification.tamper.neutralize`. Every foreign row is neutralised by its full key,
   not only the first rows the report lists. Rows without a safe inverse (removed rows, schema, TaskSpecs,
   budgets, a modified existing account, a row that cannot be found again, ...) are listed for the owner with the
   remedy. Controller checkout/interpreter files are never restored automatically (the checkout may hold the
   owner's own work); whatever stays in effect - such a file, a manual row, a failed neutralisation - puts the
   control plane on an **integrity hold**: the verifier pauses admission of new worker runs
   (`aeos work pause` state, reason `VERIFIER_TAMPER integrity hold`) until the owner reconciles and runs
   `aeos work resume`.
10. **Container runner proven on a real Docker host** (Docker 29.8.2, `dly-aeos-worker:1.19.3`,
    `dly-aeos-verifier:0.1.0`): `tests/integration/test_verification_container_live.py` records a real PASS
    container containment report, verifies a candidate in the real verifier container (only `/v/s`, `/v/tr`,
    `/v/pytest.ini`, `/v/vp`, `/v/r` mounted, no network, read-only root, non-root) and QUARANTINES the forged-report
    candidate there too. On the owner's host the runner switches to CONTAINER only after `aeos worker containment
    --kind container` PASSes there (the proof is per host and DB).

## Residual risk / owner steps

* Until Docker is installed and `aeos worker containment --kind container` PASSes **and** the verifier
  image is built, the verifier runs on the WEAK process runner. Reads of owner files by absolute path
  or of the OS credential store by direct API call are not prevented there — only the container runner
  prevents them. This is stated on every report and in the Readiness Doctor; it is not hidden.
* The tamper window detects **writes** during a verification, not reads, and not a write that is undone
  before the window closes; the rows of the high-volume append-only logs (`audit_events`, `events`) are
  not digested (only their triggers). The container runner (no data-dir mount) is the actual prevention.
* The container runner and image were first covered by a scripted-docker test only; since amendment 10 a
  real-Docker integration test proves them on a host that has Docker and both pinned images. Each owner host
  still needs its own PASS containment report before the verifier uses the container runner there.
* Candidate code shares its test process with the trusted suite in both runners: the result seal (7) catches
  after-the-fact report/exit-code forgery, not deliberate in-process subversion of the plugin.
