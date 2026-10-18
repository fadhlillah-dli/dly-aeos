"""SSE event projection stream (§35). One-directional; clients resume with ``?cursor=<seq>``.

Scope is enforced on the backend: only events of the session's active domain (plus global
events with no domain) are delivered, so Office events never reach a Personal session. The scope is
fixed when the stream opens, so the stream re-checks its session on every heartbeat and before every
live event: a logout, an expired/revoked session or a domain switch made elsewhere (another tab)
ends the stream instead of delivering events of the old scope. The client reconnects with its
cursor and gets the new scope (or 401).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from aeos.api.deps import DB, SESSION_COOKIE, CurrentPrincipal
from aeos.core.config import get_settings
from aeos.core.db import session_factory
from aeos.core.errors import AeosError
from aeos.events import service as events
from aeos.events.service import EventView, broadcaster
from aeos.identity import service as identity

router = APIRouter(tags=["events"])
HEARTBEAT_SECONDS = 15


def _scope_unchanged(token: str | None, session_id: str, domains: list[str], project_ids: list[str] | None) -> bool:
    """True while the stream's session is still valid and still has the scope the stream opened with.

    Read-only: an open stream is not user activity, so ``last_seen_at`` is never extended by it (the
    idle timeout keeps working); only resolve_session's own idle-timeout revocation is committed."""
    try:
        with session_factory()() as s:
            p = identity.resolve_session(s, get_settings(), token)
            current = [p.active_domain.value] if p.active_domain else []
            ok = (p.session.id == session_id and current == domains and p.allowed_project_ids == project_ids
                  and all(p.can_access_domain(d) for d in current))
            s.rollback()
            return ok
    except AeosError:
        return False


REPLAY_PAGE = 500


def _replay_all(after: int, domains: list[str], project_ids: list[str] | None, *,
                before: int | None = None) -> Iterator[EventView]:
    """Every visible persisted event with ``after < seq`` (and ``seq < before``), page by page."""
    while True:
        with session_factory()() as s:
            page = events.replay(s, after_seq=after, domains=domains, project_ids=project_ids, limit=REPLAY_PAGE)
        for v in page:
            if before is not None and v.seq >= before:
                return
            after = v.seq
            yield v
        if len(page) < REPLAY_PAGE:
            return


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
    token = request.cookies.get(SESSION_COOKIE)
    session_id = principal.session.id
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def gen() -> AsyncIterator[str]:
        queue = broadcaster.subscribe()
        last = cursor
        try:
            # Replay persisted events after the cursor first (source of truth), then live. Paged, so
            # every event after the cursor is delivered (no silent cut at a page size).
            for v in _replay_all(last, domains, project_ids):
                last = v.seq
                yield _format(v)
            yield f"data: {json.dumps({'type': 'ready', 'cursor': last})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    v = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    if not _scope_unchanged(token, session_id, domains, project_ids):
                        break
                    yield f"data: {json.dumps({'type': 'heartbeat', 'cursor': last})}\n\n"
                    continue
                if v.seq <= last or not _visible(v, domains, project_ids):
                    continue
                if not _scope_unchanged(token, session_id, domains, project_ids):
                    break  # logged out / revoked / domain switched: never deliver the old scope
                if v.seq > last + 1:
                    # gap (other scopes, a full queue): replay from the DB to keep ordering/dedup guarantees
                    for g in _replay_all(last, domains, project_ids, before=v.seq):
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
