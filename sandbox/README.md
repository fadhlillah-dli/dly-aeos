# Worker sandbox

| Level | Status on the build host (Windows 11, 2026-10-19) | Evidence |
|---|---|---|
| `process` | Available, **WEAK** — env scrubbed, HOME/APPDATA/XDG/TEMP redirected, cwd = worktree, OpenCode tool permissions (deny by default, inline config, project config refused), Job Object / process-group kill, memory/process watchdog, controller-checkout integrity check. No filesystem or egress enforcement by the OS. Never for shell or egress TaskSpecs, R3 only with explicit owner coverage, never R4, never elevated. | `aeos worker containment --kind process` (E2 on this host): egress/read/write/keyring = NOT_ENFORCED, env/home/kill/non_root = PASS |
| `container` | **Implemented; proven only where the owner's own report says PASS** — `aeos.worker_runtime.container`: this `Dockerfile` (pinned OpenCode), internal network + broker gateway, hardened `docker run`, teardown by label, 20-check containment suite run inside the image. | 2026-10-26, Windows 11 + Docker 29.8.2 with `dly-aeos-worker:1.19.3` built: `tests/acceptance/test_containment.py::test_container_sandbox_blocks_filesystem_and_egress` PASSED (real daemon, temp data dir). Admission still needs the owner's recorded report: `aeos worker containment --kind container` |
| `vm` | Not implemented in V1 (R4 work is BLOCKED). | — |

Granted shell (container only, wave 7): the per-run config dir (mounted read-only at `/aeos`) carries the AEOS
runner `aeos-run.js`; OpenCode's bash permission allows only `node /aeos/aeos-run.js <program> [args...]`. The runner
starts exactly one program with its arguments (never through a shell, so a pattern grant is not widened), returns a
bounded head/tail and keeps the full output under `.aeos/cmdlog/`, which the runtime collects as evidence and removes
from the candidate. No image rebuild is needed for it (`tests/unit/test_worker_runtime_shell_runner.py` runs it
inside the real image). It is a compaction and log-retention convention, not a security boundary: the container is.

## Enabling the container sandbox on a host (owner steps)

```
docker build --build-arg OPENCODE_VERSION=1.19.3 -t dly-aeos-worker:1.19.3 sandbox/
aeos worker containment --kind container              # PERSONAL profile
aeos worker containment --kind container --domain OFFICE   # OFFICE profile (still needs company authorisation)
aeos worker status
```

The report must say `overall=PASS`; it is valid for 7 days and only for the image id it was recorded with.
Then `aeos worker contract-test` proves the engine contract (tool list, repository config ignored, caps).

Policy (ADR-0051 and its 2026-10-19 amendment): `CAN_RUN_WORKER` requires `container` with a passing
containment report. The weak `process` sandbox is allowed only for PERSONAL with an ACTIVE owner EXCEPTION
decision (`target_type=worker_sandbox`, `target_id=process`, `details.allow_process_sandbox=true`). OFFICE
is always BLOCKED until its own container profile is proven.
