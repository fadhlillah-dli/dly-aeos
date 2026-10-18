"""Background executor for RUNNING publications (started by the app lifespan, §35 one control plane).

* ``start(settings)`` performs crash recovery (IN_FLIGHT → UNKNOWN, reconcile before retry, §14) and
  starts one daemon thread; ``stop()`` stops it. No-op when nothing is pending.
* ``wake()`` lets the API trigger an immediate pass after an execute/retry.
* There is exactly one in-process executor; ``jobs.process_publication`` also guards each
  publication with a lock and claims each operation with a conditional update.
"""

from __future__ import annotations

import logging
import threading

from aeos.core.config import Settings
from aeos.core.db import session_factory

log = logging.getLogger("aeos.publisher.worker")

POLL_SECONDS = 10.0
_thread: threading.Thread | None = None
_stop = threading.Event()
_wake = threading.Event()


def run_once() -> int:
    """Process every RUNNING publication once; returns how many were visited."""
    from aeos.publisher import jobs

    session = session_factory()()
    try:
        ids = jobs.due_publications(session)
        for pub_id in ids:
            try:
                jobs.process_publication(session, pub_id)
            except Exception:  # logged; the op states stay durable and visible
                session.rollback()
                log.exception("publication %s processing failed", pub_id)
        return len(ids)
    finally:
        session.close()


def _loop() -> None:
    while not _stop.is_set():
        try:
            run_once()
        except Exception:  # pragma: no cover - keep the loop alive; visible in logs/doctor
            log.exception("publisher worker pass failed")
        _wake.wait(POLL_SECONDS)
        _wake.clear()


def start(settings: Settings) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    from aeos.publisher import jobs

    session = session_factory()()
    try:
        n = jobs.recover_in_flight(session)
        session.commit()
        if n:
            log.warning("publisher recovery: %d in-flight operation(s) marked UNKNOWN for reconciliation", n)
    finally:
        session.close()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="aeos-publisher", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    _wake.set()
    if _thread is not None:
        _thread.join(timeout=15)
    _thread = None


def wake() -> None:
    _wake.set()


def running() -> bool:
    return _thread is not None and _thread.is_alive()
