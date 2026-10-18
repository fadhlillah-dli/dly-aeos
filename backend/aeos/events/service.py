"""Persistent event stream with sequence cursors (§35 "Sumber data dan mekanisme update").

Trusted components emit events tied to run/attempt/lease/version. Events are persisted first
(in the caller's transaction) and only then fanned out to live subscribers after commit, so a
dashboard never sees an event that was rolled back. Clients reconnect with a cursor and dedupe
by ``seq``. No LLM is involved in producing updates.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.redaction import redact
from aeos.models.audit import Event


@dataclass(frozen=True)
class EventView:
    seq: int
    event_id: str
    at: str
    source: str
    kind: str
    entity_type: str
    entity_id: str
    entity_version: str | None
    project_id: str | None
    domain: str | None
    run_id: str | None
    attempt: int | None
    lease_id: str | None
    mode: str
    payload: dict[str, Any]

    @classmethod
    def of(cls, e: Event) -> EventView:
        return cls(
            seq=e.seq,
            event_id=e.event_id,
            at=e.at.isoformat(),
            source=e.source,
            kind=e.kind,
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            entity_version=e.entity_version,
            project_id=e.project_id,
            domain=e.domain,
            run_id=e.run_id,
            attempt=e.attempt,
            lease_id=e.lease_id,
            mode=e.mode,
            payload=e.payload or {},
        )


class EventBroadcaster:
    """In-process fan-out to SSE subscribers. Thread-safe; delivery is best effort (cursor replay
    from the database is the source of truth)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[EventView]]] = []

    def subscribe(self) -> asyncio.Queue[EventView]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[EventView] = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue[EventView]) -> None:
        with self._lock:
            self._subscribers = [(lp, qq) for lp, qq in self._subscribers if qq is not q]

    def publish(self, views: Iterable[EventView]) -> None:
        views = list(views)
        if not views:
            return
        with self._lock:
            subs = list(self._subscribers)
        for loop, q in subs:
            for v in views:
                try:
                    loop.call_soon_threadsafe(_offer, q, v)
                except RuntimeError:  # loop closed
                    self.unsubscribe(q)


def _offer(q: asyncio.Queue[EventView], v: EventView) -> None:
    try:
        q.put_nowait(v)
    except asyncio.QueueFull:
        pass  # client will notice the gap via seq and replay from the cursor


broadcaster = EventBroadcaster()
_PENDING_KEY = "aeos_pending_events"


def emit(
    session: Session,
    *,
    source: str,
    kind: str,
    entity_type: str = "",
    entity_id: str = "",
    entity_version: str | None = None,
    project_id: str | None = None,
    domain: str | None = None,
    run_id: str | None = None,
    attempt: int | None = None,
    lease_id: str | None = None,
    mode: str = "LIVE",
    payload: dict[str, Any] | None = None,
) -> Event:
    ev = Event(
        source=source,
        kind=kind,
        entity_type=entity_type,
        entity_id=str(entity_id),
        entity_version=entity_version,
        project_id=project_id,
        domain=domain,
        run_id=run_id,
        attempt=attempt,
        lease_id=lease_id,
        mode=mode,
        payload=redact(payload or {}),
    )
    session.add(ev)
    session.flush()
    session.info.setdefault(_PENDING_KEY, []).append(EventView.of(ev))
    return ev


@sa_event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop(_PENDING_KEY, None)
    if pending:
        broadcaster.publish(pending)


@sa_event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


def replay(
    session: Session,
    *,
    after_seq: int = 0,
    project_ids: list[str] | None = None,
    domains: list[str] | None = None,
    limit: int = 500,
) -> list[EventView]:
    q = select(Event).where(Event.seq > after_seq).order_by(Event.seq.asc()).limit(min(limit, 2000))
    if project_ids is not None:
        q = q.where((Event.project_id.in_(project_ids)) | (Event.project_id.is_(None)))
    if domains is not None:
        q = q.where((Event.domain.in_(domains)) | (Event.domain.is_(None)))
    return [EventView.of(e) for e in session.scalars(q)]


def latest_seq(session: Session) -> int:
    from sqlalchemy import func

    return int(session.scalar(select(func.max(Event.seq))) or 0)
