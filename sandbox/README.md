# Worker sandbox

| Level | Status on the build host (Windows 11, 2026-10-18) | Evidence |
|---|---|---|
| `process` | Available, **WEAK** — env scrubbed, HOME/APPDATA/XDG/TEMP redirected, cwd = worktree, OpenCode tool permissions, Job Object / process-group kill. No filesystem or egress enforcement. | `aeos worker containment --kind process` (E2 on this host): egress/read/write/keyring = NOT_ENFORCED, env/home/kill = PASS |
| `container` | **NOT_PROVEN** — `Dockerfile` here is a design; Docker was not installed on the build host. | none yet |
| `vm` | Not implemented in V1. | — |

Policy (ADR-0051): `CAN_RUN_WORKER` requires `container` with a passing containment report. The weak
`process` sandbox is allowed only for PERSONAL with an ACTIVE owner EXCEPTION decision
(`target_type=worker_sandbox`, `target_id=process`, `details.allow_process_sandbox=true`). OFFICE is
always BLOCKED until a verified strong boundary exists.
