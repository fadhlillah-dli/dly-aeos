"""Background scheduler loop (lifecycle hook ``aeos.workforce.loop``).

Every tick: reconcile expired leases (always — safety, even when paused), refresh readiness of
plan versions with open tasks, and — only when ``scheduler.enabled`` is true and scheduling is not
paused — admit READY tasks up to the concurrency cap and run each in its own executor thread.

Safe to start when other modules are missing: every cross-module call fails closed (the task
waits with a reason) and errors are logged; nothing is ever pretended. No LLM is used.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from aeos.core.config import Settings
from aeos.core.db import session_factory

log = logging.getLogger("aeos.workforce.loop")
DEFAULT_INTERVAL = 5.0


class SchedulerLoop:
    def __init__(self, settings: Settings, *, interval: float = DEFAULT_INTERVAL,
                 executor: Callable[..., dict[str, Any]] | None = None) -> None:
        self.settings = settings
        self.interval = interval
        self.stop_event = threading.Event()
        self.shutdown = threading.Event()  # tells running executors to stop their workers
        self.thread: threading.Thread | None = None
        self.executors: dict[str, threading.Thread] = {}
        self._executor = executor
        self.ticks = 0
        self.last_error: str | None = None

    def _execute(self, run_id: str) -> None:
        fn = self._executor
        if fn is None:
            from aeos.worker_runtime.executor import execute_run as fn
        try:
            fn(self.settings, run_id, shutdown=self.shutdown)
        except Exception:  # logged; the lease expires and reconciliation takes over
            log.exception("executor failed for run %s", run_id)

    def tick(self, *, admit: bool = True, start_threads: bool = True) -> dict[str, Any]:
        from aeos.workforce import scheduler

        info: dict[str, Any] = {"reconcile": None, "refreshed": 0, "leased": []}
        with session_factory()() as s:
            info["reconcile"] = scheduler.reconcile(s)
            s.commit()
            for pv in scheduler.active_plan_versions(s):
                try:
                    scheduler.refresh_readiness(s, pv)
                    s.commit()
                    info["refreshed"] += 1
                except Exception as exc:
                    s.rollback()
                    log.warning("refresh_readiness(%s) failed: %s", pv, exc)
            enabled = scheduler.scheduler_enabled(s)
            paused = scheduler.scheduling_paused(s)
            info.update(enabled=enabled, paused=paused)
            if admit and enabled and not paused:
                self.executors = {k: t for k, t in self.executors.items() if t.is_alive()}
                try:
                    info["leased"] = scheduler.admit_ready_tasks(s)
                    s.commit()
                except Exception as exc:
                    s.rollback()
                    log.warning("admission failed: %s", exc)
                    info["leased"] = []
        if start_threads:
            for run_id in info["leased"]:
                t = threading.Thread(target=self._execute, args=(run_id,), daemon=True, name=f"aeos-run-{run_id[:8]}")
                self.executors[run_id] = t
                t.start()
        self.ticks += 1
        return info

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as exc:  # never kill the loop; surfaced via summary/doctor
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("scheduler tick failed")
            self.stop_event.wait(self.interval)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="aeos-scheduler")
        self.thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self.stop_event.set()
        self.shutdown.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        for t in list(self.executors.values()):
            t.join(timeout=timeout)


_lock = threading.Lock()
_loop: SchedulerLoop | None = None


def start(settings: Settings, *, interval: float = DEFAULT_INTERVAL) -> None:
    global _loop
    with _lock:
        if _loop is not None and _loop.thread is not None and _loop.thread.is_alive():
            return
        _loop = SchedulerLoop(settings, interval=interval)
        _loop.start()
        log.info("scheduler loop started (interval %.1fs)", interval)


def stop() -> None:
    global _loop
    with _lock:
        loop, _loop = _loop, None
    if loop is not None:
        loop.stop()
        log.info("scheduler loop stopped")


def is_running() -> bool:
    return _loop is not None and _loop.thread is not None and _loop.thread.is_alive()


def current() -> SchedulerLoop | None:
    return _loop


def run_once(settings: Settings, *, execute: bool = True,
             executor: Callable[..., dict[str, Any]] | None = None, force_admit: bool = False) -> dict[str, Any]:
    """One synchronous tick (CLI ``aeos work run-once``): leased runs execute in the foreground."""
    from aeos.workforce import scheduler

    loop = SchedulerLoop(settings, executor=executor)
    if force_admit:
        with session_factory()() as s:
            if scheduler.scheduling_paused(s):
                return {"paused": True, "leased": [], "note": "scheduling is paused"}
    info = loop.tick(start_threads=False, admit=True) if not force_admit else _forced_tick(loop)
    results = []
    if execute:
        for run_id in info["leased"]:
            fn = executor
            if fn is None:
                from aeos.worker_runtime.executor import execute_run as fn
            results.append(fn(settings, run_id))
    info["results"] = results
    return info


def _forced_tick(loop: SchedulerLoop) -> dict[str, Any]:
    from aeos.workforce import scheduler

    info = loop.tick(start_threads=False, admit=False)
    with session_factory()() as s:
        info["leased"] = scheduler.admit_ready_tasks(s)
        s.commit()
    return info
