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

## Amendment 2026-10-18 (integration)

* The process-sandbox exception is recordable through `POST /api/decisions` (`kind=EXCEPTION`,
  `exception=worker_sandbox_process`, optional `allow_bash`), the Decision page preset and `aeos policy
  decide ... --exception worker_sandbox_process [--allow-bash]`. They produce exactly
  `worker_sandbox/process` with `details.allow_process_sandbox = true` and an explicit boolean
  `allow_bash`, PERSONAL only, with an expiry; a renewal supersedes the previous decision, so dropping
  `allow_bash` takes effect immediately. `sandbox_readiness` and `process_shell_allowed` read these
  decisions unchanged (tested: record -> READY/process, revoke -> BLOCKED).

## Amendment 2026-10-19 (audit: master §03, §09, §11, §20, §21, §25 "jangan sampai ada yang terlewat")

The master's minimum controls are hard rules, not risk the exception can buy away. Changes:

1. **No host shell, ever** (§25 "Worker tidak mendapat shell host, vault root ..."). A TaskSpec that grants
   `bash` or `bash:<pattern>` is admitted only in the `container` sandbox with a passing containment
   report. `details.allow_bash` is still recorded but no longer admits a shell under the process sandbox
   (admission/executor code `SHELL_NEEDS_CONTAINER`). Even `bash:pytest *` runs worker-written code as the
   owner's OS user (keyring, Git Credential Manager, data DB), so no pattern list is a boundary.
2. **No egress tasks** (§25 "egress default deny dengan broker"): a TaskSpec with `webfetch` + `egress_allowed`
   waits with `EGRESS_NOT_ENFORCEABLE` (process: no OS egress filter; container: only the broker is reachable;
   a per-host allow-list proxy is not implemented).
3. **Risk tiers** (§25 "VM/microVM/sandbox untuk risiko lebih tinggi"): R0-R2 may use the process sandbox
   under this exception; R3 (privileged / protected-path work - the compiler raises such tasks to R3) needs the
   container, unless the owner's exception lists the operation `run_worker_process_sandbox_r3` in its
   `allowed_operations` (an explicit, recorded acceptance; the field is part of the immutable binding);
   R4 needs a VM/microVM, which V1 does not implement, so R4 is always BLOCKED (`VM_SANDBOX_REQUIRED`).
4. **Non-root** (§25): the process sandbox is refused while the controller runs as root / an elevated
   Administrator (the worker would inherit it); the process containment report has a `non_root` check.
5. **Container sandbox implemented** (`aeos.worker_runtime.container`): pinned image
   `dly-aeos-worker:<opencode pin>` built by the owner from `sandbox/Dockerfile`; an `--internal` docker
   network per domain profile whose only reachable endpoint is a fixed broker gateway forwarding
   `/broker/v1/*` (everything else 403); hardened `docker run` (user 10001/10002, read-only root, tmpfs
   /tmp and HOME, cap-drop ALL, no-new-privileges, memory/cpu/pids limits, only the worktree rw and the
   per-run config ro); teardown by the `aeos.run_id` label in the kill controller; orphan reaping
   (`aeos worker reap`). `aeos worker containment --kind container [--domain OFFICE]` runs 20 negative
   checks INSIDE the image; `sandbox_readiness` selects the container only for a PASS report that is fresh
   (7 days), for this platform, this domain profile and the current image id. **Status: NOT_PROVEN on the
   build host** (no Docker); the code paths are tested against a scripted docker CLI, which proves the AEOS
   side only.
6. **OFFICE profile** (§11): separate network, gateway, container user and per-domain data partition, and
   its own containment report. OFFICE stays BLOCKED until that report passes on the host - and the
   scheduler still requires the company authorisation (`OFFICE_DENIED`).
7. **Controller integrity** (§09 Zone 1): every run fingerprints the trust-relevant files of the running
   controller checkout (the protected-path list) before the worker starts and compares them afterwards; a
   change fails + quarantines the run and raises a HIGH alert. Writes to the data-dir DB are not detectable
   this way; they stay a stated residual risk of the process sandbox (the container never mounts the data
   dir). Combined with (1), OpenCode's own tools are the only file access a process-sandbox worker has.
8. **Repository agent config** (§25): the executor refuses a worktree whose base commit carries
   `opencode.json`, `opencode.jsonc` or `.opencode/` (`WORKTREE_AGENT_CONFIG`), delivers the security
   config inline via `OPENCODE_CONFIG_CONTENT` (without the token) and sets
   `OPENCODE_DISABLE_PROJECT_CONFIG=1`; these files plus `AGENTS.md`/`CLAUDE.md` are protected paths.
   Whether the pinned binary honours (2)/(3) of that list is checked by `aeos worker contract-test`
   (`project_config_ignored`) on a host with OpenCode - NOT_PROVEN here.

Residual risk accepted by the owner under the process exception after this amendment: same OS user for
OpenCode's own tools (bounded by `external_directory: deny`), no OS egress filter for the engine process
itself (it talks only to the broker), and undetected direct writes to the data-dir DB.
