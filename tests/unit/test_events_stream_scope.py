"""SSE stream scope after the stream opened (§35 "Project/domain filter ditegakkan backend", §50).

The stream fixes its domain scope when it opens. A logout, or a domain switch made elsewhere (for
example in another browser tab sharing the session), must END the stream instead of continuing to
deliver events of the old scope. The UI's single shared stream relies on this: it reconnects with
its cursor and then receives only what the new session scope allows.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from sqlalchemy import select

from aeos.core.db import session_factory
from aeos.events import router as events_router
from aeos.events import service as events
from aeos.models.identity import AuthSession

SAFETY_SECONDS = 8.0  # without the fix the stream would never end; stop it so the test fails, not hangs


@pytest.fixture
def fast_stream(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    monkeypatch.setattr(events_router, "HEARTBEAT_SECONDS", 0.05)
    started = {"at": time.monotonic()}

    async def disconnected_after_deadline(self: Any) -> bool:
        return time.monotonic() - started["at"] > SAFETY_SECONDS

    monkeypatch.setattr("starlette.requests.Request.is_disconnected", disconnected_after_deadline)
    return started


def _emit_personal(kind: str) -> None:
    with session_factory()() as s:
        events.emit(s, source="test", kind=kind, domain="PERSONAL")
        s.commit()


def _later(*steps: Any, delay: float = 0.4) -> threading.Thread:
    """Run each step ``delay`` seconds after the previous one, while the stream request is open."""

    def run() -> None:
        for fn in steps:
            time.sleep(delay)
            fn()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _set_selected_domain(domain: str) -> None:
    with session_factory()() as s:
        for auth in s.scalars(select(AuthSession).where(AuthSession.revoked_at.is_(None))):
            auth.selected_domain = domain
        s.commit()
    _emit_personal("test.after_switch")


def _revoke_all() -> None:
    with session_factory()() as s:
        for auth in s.scalars(select(AuthSession).where(AuthSession.revoked_at.is_(None))):
            auth.revoked_at = auth.last_seen_at
            auth.revoked_reason = "logout"
        s.commit()
    _emit_personal("test.after_logout")


def _stream(client: Any, started: dict[str, float]) -> tuple[str, float]:
    latest = client.get("/api/events").json()["latest_seq"]
    started["at"] = time.monotonic()
    r = client.get(f"/api/events/stream?cursor={latest}")
    assert r.status_code == 200, r.text
    return r.text, time.monotonic() - started["at"]


def test_live_event_in_scope_is_delivered(personal_client: Any, fast_stream: dict[str, float]) -> None:
    """Positive control: the same setup delivers a PERSONAL event while the session is unchanged."""
    worker = _later(lambda: _emit_personal("test.in_scope"), _revoke_all)
    body, _ = _stream(personal_client, fast_stream)
    worker.join(5)
    assert '"type": "ready"' in body
    assert "test.in_scope" in body
    assert "test.after_logout" not in body


def test_domain_switch_elsewhere_ends_the_stream(personal_client: Any, fast_stream: dict[str, float]) -> None:
    worker = _later(lambda: _set_selected_domain("OFFICE"))
    body, elapsed = _stream(personal_client, fast_stream)
    worker.join(5)
    assert '"type": "ready"' in body
    assert "test.after_switch" not in body, "PERSONAL event delivered to a session that switched to OFFICE"
    # timing-cap: the 8 s safety stop vs a measured 0.4 s end (20x); without the fix it ends only there
    assert elapsed < SAFETY_SECONDS, "stream kept running after the session scope changed"


def test_logout_ends_the_stream(personal_client: Any, fast_stream: dict[str, float]) -> None:
    worker = _later(_revoke_all)
    body, elapsed = _stream(personal_client, fast_stream)
    worker.join(5)
    assert "test.after_logout" not in body, "event delivered after the session was revoked"
    # timing-cap: the 8 s safety stop vs a measured 0.4 s end (20x); without the fix it ends only there
    assert elapsed < SAFETY_SECONDS, "stream kept running after logout"



def _kinds(body: str) -> list[str]:
    import json

    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            frame = json.loads(line[6:])
            if frame.get("type") == "event":
                out.append(frame["kind"])
    return out


def test_backlog_after_the_cursor_is_delivered_completely(personal_client: Any, fast_stream: dict[str, float],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """The cursor guarantee: every event after the cursor, not only the first page of them."""
    monkeypatch.setattr(events_router, "REPLAY_PAGE", 3)
    latest = personal_client.get("/api/events").json()["latest_seq"]
    with session_factory()() as s:
        for i in range(10):
            events.emit(s, source="test", kind=f"test.backlog.{i}", domain="PERSONAL")
        s.commit()
    worker = _later(_revoke_all)
    fast_stream["at"] = time.monotonic()
    body = personal_client.get(f"/api/events/stream?cursor={latest}").text
    worker.join(5)
    assert [k for k in _kinds(body) if k.startswith("test.backlog.")] == [f"test.backlog.{i}" for i in range(10)]


def test_gap_replay_is_paged(personal_client: Any, fast_stream: dict[str, float], monkeypatch: pytest.MonkeyPatch) -> None:
    """Events the broadcaster dropped (full queue) are replayed from the DB, all of them, in order."""
    from aeos.events import service as svc

    monkeypatch.setattr(events_router, "REPLAY_PAGE", 3)
    real_offer = svc._offer
    monkeypatch.setattr(svc, "_offer", lambda q, v: real_offer(q, v) if v.kind.endswith(".last") else None)

    def burst() -> None:
        with session_factory()() as s:
            for i in range(7):
                events.emit(s, source="test", kind=f"test.gap.{i}", domain="PERSONAL")
            events.emit(s, source="test", kind="test.gap.last", domain="PERSONAL")
            s.commit()

    worker = _later(burst, _revoke_all)
    body, _ = _stream(personal_client, fast_stream)
    worker.join(5)
    assert [k for k in _kinds(body) if k.startswith("test.gap.")] == [f"test.gap.{i}" for i in range(7)] + ["test.gap.last"]
