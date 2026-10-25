# Container sandbox containment — first real-host run (E3)

Runtime observation of the worker container boundary on the owner's build host (master §11 "Sandbox",
§25 minimum controls, §34 evidence ladder E3). Until this run the container sandbox was implemented
and tested only against a scripted docker CLI (E2) and stated NOT_PROVEN everywhere.

| Field | Value |
|---|---|
| Date (UTC) | 2026-10-25 |
| Host | Windows 11 Pro 10.0.26200 (`sys.platform` win32), Docker Desktop, docker client/server 29.8.2, server linux/amd64 |
| Worker image | `dly-aeos-worker:1.19.3`, id `sha256:919303d210b0446dd55645866d52bd0cead23072eea94eb46a42799387553b21`, built from [sandbox/Dockerfile](../../../sandbox/Dockerfile) unchanged (`docker build --build-arg OPENCODE_VERSION=1.19.3 -t dly-aeos-worker:1.19.3 sandbox/`) |
| Verifier image | `dly-aeos-verifier:0.1.0`, id `sha256:86ac05f81869f2f8b9cee0f8cf446eed512cf0f159af45ae799860051262e22a`, built from [sandbox/verifier.Dockerfile](../../../sandbox/verifier.Dockerfile) |
| Data dir | a throwaway `AEOS_DATA_DIR` (`aeos db upgrade` + `aeos seed`); the owner's real data dir was not used |

## Results

| Run | Result | Raw output |
|---|---|---|
| `aeos worker containment --kind container` (PERSONAL profile) | **overall=PASS, 20/20** | [container-personal-windows.txt](container-personal-windows.txt) |
| `aeos worker containment --kind container --domain OFFICE` (OFFICE profile: user 10002, own network and gateway) | **overall=PASS, 20/20** | [container-office-windows.txt](container-office-windows.txt) |
| `pytest tests/acceptance/test_containment.py` | 10 passed, 1 skipped (`BLOCKED-real-github`: a real GitHub-hosted fork-PR run) — `test_container_sandbox_blocks_filesystem_and_egress` PASSED against the real daemon, no longer skipped | — |

The 20 checks cover non-root user, read-only root and config, allow-listed mounts only, no host files /
controller checkout / AEOS database / `~/.ssh` visible, no secret-looking environment, HOME/XDG redirected,
no egress except the broker gateway, gateway path filter (`/api/session` -> 403), no keyring / D-Bus /
ssh-agent / docker socket / git credential helpers, memory and pid limits, and cancel kills the whole tree.

## Defects this run found (fixed in the same change)

1. **Container name collision.** The first OFFICE run failed (`overall=FAIL`, probe exit 125: *Conflict. The
   container name "/aeos-run-containment-" is already in use*). `container_command` named containers
   `aeos-run-{run_id[:12]}`, so the probe and cancel runs (`containment-probe-…`, `containment-cancel-…`)
   and any two runs sharing a 12-character prefix collided. Names now carry the whole sanitised run id plus
   a random suffix (`sandbox.container_name`); teardown and reaping still go by the `aeos.run_id` label.
   Test: `tests/unit/test_worker_runtime_sandbox.py::test_container_names_never_collide_for_runs_sharing_a_prefix`.
2. **`aeos worker status` understated the proof.** It printed `container strength=NOT_PROVEN` from the
   session-less host probe while admission already said `READY (kind=container)`. It now prints
   `PROVEN for <domain> (containment PASS <checked_at>)` only when that domain's fresh PASS report for the
   current image exists. Test: `::test_worker_status_shows_the_container_proven_only_with_a_fresh_pass`.

## What this does and does not prove

* Proven here (E3, this host, these image ids, valid 7 days per report): the container boundary for both
  profiles. Admission selects the container sandbox only while such a fresh PASS report exists for the
  current image id (ADR-0051 amendment); a rebuilt image or another host needs a new run.
* **Not** proven: a real OpenCode worker run (OpenCode is not installed on the host — `aeos worker status`
  reports `opencode: MISSING`; `aeos worker contract-test` needs it), the verifier container runner on a
  real candidate, macOS/Docker Desktop for Mac, and OFFICE use, which also needs the company
  authorisation (master §23, M5).
