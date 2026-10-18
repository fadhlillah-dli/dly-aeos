# ADR-0051 — Worker sandbox requirement and the process-sandbox exception (§11, §25, §50, §51)

* Status: ACCEPTED (requirement); the exception itself is an owner decision recorded per host
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)

## Context

§25 requires non-root, read-only FS except allowlisted paths, no docker socket/host home/SSH agent,
egress default deny via the broker, caps, capability expiry and an external kill controller, and
"Jika boundary yang disyaratkan belum tersedia, pekerjaan tersebut BLOCKED". §51 lists containment,
egress, secret broker, cancel and bypass tests as P1 before workers are trusted. The owner's
machines are Windows and macOS; Docker is not installed on the build host.

## Decision

1. Default requirement for `CAN_RUN_WORKER`: the `container` sandbox, and only after
   `aeos worker containment --kind container` produced a passing report on that host.
   Availability of the docker CLI/daemon alone is not containment.
2. The `process` sandbox is always available but reported as WEAK. It may be used only for the
   PERSONAL domain and only while an ACTIVE, unexpired, human Decision exists with
   `kind=EXCEPTION`, `target_type=worker_sandbox`, `target_id=process`, `domain=PERSONAL`,
   `details.allow_process_sandbox=true`. OFFICE is always BLOCKED.
3. The process sandbox provides (tested, E2 on this host): environment rebuilt from an allow-list
   with every secret-looking name/value removed; HOME/USERPROFILE/APPDATA/LOCALAPPDATA/XDG_*/TEMP
   redirected into the run directory; cwd = candidate worktree; OpenCode permissions deny by
   default (`external_directory`, `bash`, `webfetch`, `task`, `skill`, MCP all denied unless the
   TaskSpec grants them); the upstream key never enters the sandbox (only a run-bound capability
   token for the broker); Windows Job Object with kill-on-close / POSIX process group, psutil tree
   kill and an `AEOS_RUN_ID` marker sweep for orphans.

## Residual risks of the process sandbox (accepted only through the owner decision)

* **No egress enforcement**: a worker process can connect to any host (containment check
  `egress_non_broker` = NOT_ENFORCED). Only OpenCode's tool permissions keep the model from
  fetching URLs; `bash`, if granted, can reach the network.
* **Same OS user**: files readable by the owner (AEOS data-dir DB, the real `~/.ssh`, the OS keyring
  API) are not blocked by the OS (`read_data_db`, `real_ssh_dir`, `keyring_unreachable` =
  NOT_ENFORCED). The tool layer (`external_directory: deny`) blocks OpenCode's own read/edit tools,
  verified against opencode 1.19.3; it is not a kernel boundary.
* **Writes outside the worktree** are not prevented; they are detected only inside the repository
  diff (scope check + quarantine). User-project worktrees share `.git` with the source repository.
* The capability token sits in the per-run config file readable by the worker (run-bound, scoped,
  capped, short TTL, revoked at the end of the run, file deleted afterwards).
* **Shell = same OS user**: with `bash` a worker can run any program as the owner, including OS
  credential helpers (e.g. Git Credential Manager) — a direct push / credential escape is then not
  prevented by this sandbox (§31 enforcement NOT-PROVEN). Therefore a TaskSpec that grants `bash`
  (or `bash:<pattern>`) is admitted under the process sandbox only when the exception decision also
  carries `details.allow_bash=true`; otherwise admission waits with `SANDBOX_NOT_READY` and the
  executor refuses to start (completeness review, ADR-0054).

## Consequences

* With no Docker and no decision, admission waits with `SANDBOX_NOT_READY` and the remedy text.
* The containment report is recorded (SystemSetting + evidence when the collector exists) and shown
  in the dashboard; NOT_ENFORCED is never rendered as pass.
* `sandbox/Dockerfile` defines the hardened worker image; container launch stays NOT_PROVEN until
  the container containment suite is implemented and passes on a host with Docker.
