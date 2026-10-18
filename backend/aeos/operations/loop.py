"""Periodic SLI evaluation + alert reconciliation (background service, §33 monitoring).

Lifecycle contract used by the app lifespan: ``start(settings)`` / ``stop()``. The loop is a
deterministic projection job (no LLM); it can be disabled with the feature flag
``ops.sli_loop_enabled`` and its interval set with ``ops.sli_loop_interval_seconds``.
Each domain is evaluated separately so alerts keep their domain scope.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow

log = logging.getLogger("aeos.operations.loop")

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_state: dict[str, Any] = {"running": False, "last_run_at": None, "last_error": None, "runs": 0}
MIN_INTERVAL = 30.0


def state() -> dict[str, Any]:
    return dict(_state)


def run_once(settings: Settings) -> dict[str, Any]:
    """One evaluation pass for every domain. Returns per-domain alert reconciliation results."""
    from aeos.core.db import session_scope
    from aeos.operations import flags, sli

    results: dict[str, Any] = {}
    with session_scope() as s:
        if not flags.get_flag(s, "ops.sli_loop_enabled"):
            return {"skipped": "ops.sli_loop_enabled is false"}
    for domain in Domain:
        with session_scope() as s:
            out = sli.evaluate(s, domain=domain, settings=settings)
            results[domain.value] = out["alerts"]
    _state["last_run_at"] = utcnow().isoformat()
    _state["runs"] = int(_state["runs"]) + 1
    return results


def _interval() -> float:
    from aeos.core.db import session_scope
    from aeos.operations import flags

    try:
        with session_scope() as s:
            value = flags.get_flag(s, "ops.sli_loop_interval_seconds")
        return max(MIN_INTERVAL, float(value or 300))
    except Exception:  # database not ready yet -> default interval
        return 300.0


def _main(settings: Settings, first_delay: float) -> None:
    if _stop.wait(first_delay):
        return
    while not _stop.is_set():
        try:
            run_once(settings)
            _state["last_error"] = None
        except Exception as exc:  # logged + visible in the service inventory; never kills the app
            _state["last_error"] = type(exc).__name__
            log.exception("SLI evaluation failed")
        if _stop.wait(_interval()):
            break


def start(settings: Settings, *, first_delay: float = 20.0) -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_main, args=(settings, first_delay), name="aeos-ops-sli", daemon=True)
        _thread.start()
        _state["running"] = True


def stop(timeout: float = 10.0) -> None:
    global _thread
    with _lock:
        _stop.set()
        if _thread is not None:
            _thread.join(timeout=timeout)
        _thread = None
        _state["running"] = False
