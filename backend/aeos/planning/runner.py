"""Background execution of the planning pipeline for the API (one thread per plan).

The API returns 202 immediately; progress is visible through persisted events
(``planning.run.updated``, ``plan.state_changed``) and the plan detail. Each thread owns its own DB
session. A plan never has two in-process runs; cross-process duplicates are prevented by the
RUNNING-run guard in ``service.stage_in_progress``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from aeos.core.db import session_factory
from aeos.core.errors import AeosError

log = logging.getLogger("aeos.planning")

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


def is_running(plan_id: str) -> bool:
    with _lock:
        t = _threads.get(plan_id)
        return t is not None and t.is_alive()


def _target(plan_id: str, action: Callable[..., object], actor: str) -> None:
    session = session_factory()()
    try:
        action(session, plan_id, actor=actor)
    except AeosError as exc:  # expected stops (Blocked/Conflict) are already recorded on the plan
        log.info("planning %s stopped: %s %s", plan_id, exc.code, exc.message)
        session.rollback()
    except Exception:  # pragma: no cover - logged; the plan keeps its last committed state
        log.exception("planning pipeline for %s crashed", plan_id)
        session.rollback()
        _mark_crashed(plan_id)
    finally:
        session.close()
        with _lock:
            if _threads.get(plan_id) is threading.current_thread():
                _threads.pop(plan_id, None)


def _mark_crashed(plan_id: str) -> None:  # pragma: no cover - defensive
    from aeos.planning import service

    session = session_factory()()
    try:
        # The plan keeps its stage state; the next run detects the interrupted stage and re-runs it.
        for run in service._runs(session, plan_id):
            if run.status == "RUNNING":
                run.status = "FAILED"
                run.error_code = "PIPELINE_CRASH"
                run.error = "unexpected controller error; see server log"
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def start(plan_id: str, *, actor: str, action: str = "run") -> bool:
    """Start the pipeline (``run``) or a manual import (``import``) in a background thread."""
    from aeos.planning import service

    fn = service.run_pipeline if action == "run" else service.import_manual_result
    with _lock:
        existing = _threads.get(plan_id)
        if existing is not None and existing.is_alive():
            return False
        t = threading.Thread(target=_target, args=(plan_id, fn, actor), name=f"aeos-plan-{plan_id[:8]}", daemon=True)
        _threads[plan_id] = t
        t.start()
    return True


def join_all(timeout: float = 30.0) -> None:
    """Wait for running pipeline threads (tests / graceful shutdown)."""
    with _lock:
        threads = list(_threads.values())
    for t in threads:
        t.join(timeout)
