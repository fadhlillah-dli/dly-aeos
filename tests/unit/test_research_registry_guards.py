"""Research registry guards added in the module-group-H completeness review (master §45 "DLY AEOS
... source ID/URL UNVERIFIED", §47 "sumber lama ditandai stale", §50 "Viewer mengakses ... Office ->
Backend scope diterapkan termasuk counts/cache/events", "Source repo rename/redirect -> tidak
mewarisi trust otomatis")."""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.errors import ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import Event
from aeos.models.research import ResearchEntry
from aeos.research import catalog
from aeos.research import service as research
from tests.conftest import AuthedClient

API = "https://api.github.com"


def _platform(db) -> ResearchEntry:
    return db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == research.PLATFORM_ENTRY_KEY))


def test_platform_repo_without_configured_slug_is_unverified_without_url(db):
    """§45: 'Repository pengguna belum diberikan; source ID/URL UNVERIFIED' - never a guessed URL."""
    research.seed_registry(db, platform_repo_slug=None)
    db.commit()
    e = _platform(db)
    assert e.identity_status == catalog.UNV and e.urls == [] and e.identity.startswith("name:")
    assert research.entry_view(e)["checkable"] is False


def test_platform_repo_follows_owner_configuration_until_checked(db):
    research.seed_registry(db, platform_repo_slug="fadhlillah-dli/dly-aeos")
    db.commit()
    e = _platform(db)
    assert e.identity_status == "CONFIGURED" and e.urls == ["https://github.com/fadhlillah-dli/dly-aeos"]
    assert e.identity == "github:fadhlillah-dli/dly-aeos"
    research.seed_registry(db, platform_repo_slug="fadhlillah-dli/dly-aeos-next")  # owner changed config
    db.commit()
    assert _platform(db).identity == "github:fadhlillah-dli/dly-aeos-next"
    e = _platform(db)
    e.upstream_status = "OK"  # after a live check the recorded identity is no longer rewritten by config
    db.commit()
    research.seed_registry(db, platform_repo_slug="someone/else")
    db.commit()
    assert _platform(db).identity == "github:fadhlillah-dli/dly-aeos-next"


def test_history_cap_never_drops_identity_or_lifecycle_events():
    history = [{"at": "t0", "event": "IDENTITY_CHANGED", "from": "github:a/b", "to": "github:c/d"},
               {"at": "t1", "event": "LIFECYCLE_RESET", "from": "SOURCE_VERIFIED", "to": "DISCOVERED"}]
    history += [{"at": f"c{i}", "event": "UPSTREAM_CHECK", "status": "OK"} for i in range(80)]
    capped = research._cap_history(history)
    assert capped[0]["event"] == "IDENTITY_CHANGED" and capped[1]["event"] == "LIFECYCLE_RESET"
    checks = [h for h in capped if h["event"] == "UPSTREAM_CHECK"]
    assert len(checks) == research.HISTORY_CHECK_EVENTS_KEPT and checks[-1]["at"] == "c79"


@respx.mock(assert_all_called=False)
def test_non_json_upstream_response_is_an_honest_error(respx_mock, db):
    research.seed_registry(db)
    db.commit()
    e = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:opencode"))
    respx_mock.get(f"{API}/repos/anomalyco/opencode").mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    out = research.check_upstream(db, e, actor_type="user", actor_id="owner", domain="PERSONAL")
    db.commit()
    assert out["check_status"] == "ERROR" and out["touched"] is False
    assert out["checked_at"] is None and out["identity_status"] == "STATED_BY_MASTER"  # nothing claimed as checked


@respx.mock(assert_all_called=False)
def test_upstream_events_carry_the_entry_domain(respx_mock, db):
    entry, _dup = research.add_by_url(db, url="https://github.com/acme/widget", name=None, category=None, notes=None,
                                      actor_user_id="owner", domain="PERSONAL")
    db.commit()
    respx_mock.get(f"{API}/repos/acme/widget").mock(return_value=httpx.Response(
        200, json={"full_name": "acme/widget", "archived": False, "fork": False}))
    respx_mock.get(f"{API}/repos/acme/widget/releases/latest").mock(return_value=httpx.Response(404, json={}))
    respx_mock.get(f"{API}/repos/acme/widget/tags?per_page=1").mock(return_value=httpx.Response(200, json=[]))
    research.check_upstream(db, entry, actor_type="user", actor_id="owner", domain="PERSONAL")
    db.commit()
    ev = db.scalar(select(Event).where(Event.kind == "research.upstream.checked", Event.entity_id == entry.id))
    assert ev is not None and ev.domain == "PERSONAL"


def test_manual_entries_cannot_pose_as_master_references(db):
    with pytest.raises(ValidationFailed):
        research.add_by_url(db, url="https://github.com/acme/fake-ref", name="S99", category=research.REFERENCE_CATEGORY,
                            notes=None, actor_user_id="owner", domain="PERSONAL")


def test_list_freshness_never_uses_other_domain_entries(personal_client: AuthedClient, db):
    """checked_at of an OFFICE-submitted entry must not surface as freshness in a PERSONAL session."""
    research.seed_registry(db)
    db.add(ResearchEntry(name="office-only tool", identity="github:corp/internal", category="candidate",
                         submitted_domain="OFFICE", checked_at=utcnow(), lifecycle="DISCOVERED"))
    db.commit()
    body = personal_client.get("/api/research").json()
    assert body["checked_at"] is None
    assert all(i["identity"] != "github:corp/internal" for i in body["items"])
