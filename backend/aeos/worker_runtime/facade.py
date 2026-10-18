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
