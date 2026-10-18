"""SSE event projection stream (§35). One-directional; clients resume with ``?cursor=<seq>``.

Scope is enforced on the backend: only events of the session's active domain (plus global
events with no domain) are delivered, so Office events never reach a Personal session.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from aeos.api.deps import DB, CurrentPrincipal
from aeos.core.db import session_factory
from aeos.events import service as events
from aeos.events.service import EventView, broadcaster

router = APIRouter(tags=["events"])
HEARTBEAT_SECONDS = 15


def _visible(view: EventView, domains: list[str], project_ids: list[str] | None) -> bool:
    if view.domain is not None and view.domain not in domains:
        return False
    return not (project_ids is not None and view.project_id is not None and view.project_id not in project_ids)


@router.get("/api/events")
def list_events(principal: CurrentPrincipal, db: DB, after: int = Query(0, ge=0), limit: int = Query(200, le=2000)) -> dict[str, Any]:
    domains = [principal.active_domain.value] if principal.active_domain else []
    views = events.replay(db, after_seq=after, domains=domains, project_ids=principal.allowed_project_ids,
                          limit=limit)
    return {"events": [asdict(v) for v in views], "latest_seq": events.latest_seq(db)}


@router.get("/api/events/stream")
async def stream(request: Request, principal: CurrentPrincipal, cursor: int = Query(0, ge=0)) -> StreamingResponse:
    domains = [principal.active_domain.value] if principal.active_domain else []
    project_ids = principal.allowed_project_ids
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def gen() -> AsyncIterator[str]:
        queue = broadcaster.subscribe()
        last = cursor
        try:
            # Replay persisted events after the cursor first (source of truth), then live.
            with session_factory()() as s:
                backlog = events.replay(s, after_seq=last, domains=domains, project_ids=project_ids, limit=2000)
            for v in backlog:
                last = v.seq
                yield _format(v)
            yield f"data: {json.dumps({'type': 'ready', 'cursor': last})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    v = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'cursor': last})}\n\n"
                    continue
                if v.seq <= last or not _visible(v, domains, project_ids):
                    continue
                if v.seq > last + 1:
                    # gap: replay from the DB to keep ordering/dedup guarantees
                    with session_factory()() as s:
                        for g in events.replay(s, after_seq=last, domains=domains, project_ids=project_ids):
                            if g.seq < v.seq:
                                last = g.seq
                                yield _format(g)
                last = v.seq
                yield _format(v)
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _format(v: EventView) -> str:
    # Default SSE "message" frames; the kind travels inside the payload.
    return f"id: {v.seq}\ndata: {json.dumps({'type': 'event', **asdict(v)}, default=str)}\n\n"
