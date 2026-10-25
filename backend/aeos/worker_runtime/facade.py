"""Cross-module contract for sandboxes and worker processes (§11, §25). Owned by ``aeos.worker_runtime``."""

from __future__ import annotations

from typing import Any


def sandbox_capabilities() -> dict[str, Any]:
    """Which isolation levels are available on this host (process/container/vm) with evidence."""
    from aeos.worker_runtime.sandbox import sandbox_capabilities as impl

    return impl()


def containment_report(sandbox_kind: str = "process") -> dict[str, Any]:
    """Run negative containment tests (FS/egress/credential/kill) and report each honestly."""
    from aeos.worker_runtime.containment import containment_report as impl

    return impl(sandbox_kind)


def kill_run_processes(run_id: str) -> dict[str, Any]:
    """Terminate the full process tree / container of a run. Idempotent."""
    from aeos.worker_runtime.processes import kill_run_processes as impl

    return impl(run_id)


def opencode_status() -> dict[str, Any]:
    """Binary path/version of the OpenCode worker engine, or MISSING."""
    from aeos.worker_runtime.opencode import opencode_status as impl

    return impl()


def worker_prompt_revision() -> str:
    """Revision id of the OpenCode worker prompt template; part of a worker route's configuration
    identity (§23 ``ModelRoute.prompt_revision``). Added 2026-10-19; existing signatures unchanged."""
    from aeos.worker_runtime.executor import WORKER_PROMPT_REVISION

    return WORKER_PROMPT_REVISION


def sandbox_readiness(session: Any, *, domain: str) -> dict[str, Any]:
    """Policy requirement for CAN_RUN_WORKER: READY only with a proven container or, for PERSONAL,
    an ACTIVE owner EXCEPTION allowing the weak process sandbox (ADR-0051). Added by module E;
    existing signatures unchanged."""
    from aeos.worker_runtime.sandbox import sandbox_readiness as impl

    return impl(session, domain=domain)


def enforced_run_limits(limits: dict[str, Any] | None, *, sandbox_kind: str | None) -> dict[str, Any]:
    """The per-run compute this runtime ENFORCES for a TaskSpec's limits (§19, §22): ``memory_mb`` is the process
    watchdog's RSS cap (+ Windows job memory limit) or docker ``--memory``; ``cpus`` is docker ``--cpus`` in the
    container sandbox (the process sandbox has no CPU cap: there ``limits.max_cpus`` is a reservation only)."""
    from aeos.worker_runtime import container, processes

    lim = limits or {}
    if sandbox_kind == "container":
        cl = container.limits_for(lim)
        return {"memory_mb": int(str(cl["memory"]).rstrip("m")), "cpus": float(cl["cpus"]), "cpu_enforced": True}
    try:
        cpus = float(lim.get("max_cpus") or 1)
    except (TypeError, ValueError):
        cpus = 1.0
    return {"memory_mb": int(processes.ResourceLimits.from_task(lim).max_memory_mb), "cpus": cpus,
            "cpu_enforced": False}


# --- verifier runner (§20 "Verifier ENF ... isolated runner"; additive 2026-10-19, K2) -----------------------
def verifier_sandbox(session: Any, *, domain: str) -> dict[str, Any]:
    """READY when the verifier may execute candidate code in the container runner (fresh PASS container
    containment report for this host/image/domain + the pinned verifier image), else UNAVAILABLE + reasons."""
    from aeos.worker_runtime.container import verifier_readiness as impl

    return impl(session, domain=domain)


def verifier_container_argv(*, domain: str, verify_id: str, mounts: list[tuple[str, str, bool]],
                            env: dict[str, str], inner_argv: list[str], workdir: str = "/tmp",  # noqa: S108
                            image: str | None = None, user: str | None = None) -> list[str]:
    """Hardened ``docker run`` argv for one verifier step (``--network none``, read-only, non-root, no caps)."""
    from aeos.worker_runtime.container import verifier_argv as impl

    return impl(domain=domain, verify_id=verify_id, mounts=mounts, env=env, inner_argv=inner_argv, workdir=workdir,
                image=image, user=user)


def verifier_container_teardown(verify_id: str) -> dict[str, Any]:
    from aeos.worker_runtime.container import teardown_verifier as impl

    return impl(verify_id)


def docker_cli_env() -> dict[str, str]:
    """Environment for the docker CLI process itself (controller env minus anything secret-looking)."""
    from aeos.worker_runtime.container import docker_cli_env as impl

    return impl()


def controller_integrity_snapshot() -> dict[str, Any]:
    """Fingerprint of the running controller checkout + interpreter site-packages (§09 Zone 1)."""
    from aeos.worker_runtime.integrity import snapshot as impl

    return impl()


def controller_integrity_compare(before: dict[str, Any] | None) -> list[dict[str, str]]:
    """Controller files changed/added/removed since ``before`` (empty = untouched)."""
    from aeos.worker_runtime.integrity import compare as impl

    return impl(before)
