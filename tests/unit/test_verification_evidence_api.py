"""Evidence detail access (§35 Live Work contract): full transcript/prompt/IO is not opened by default;
redaction, classification and AUTH gate access. RESTRICTED content is shown to SUPER_ADMIN only."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aeos.core.enums import Domain
from aeos.verification import collector
from tests.conftest import VIEWER_PW, AuthedClient, create_viewer
from tests.unit.test_verification_support import make_world

LOG_TEXT = "worker transcript: prompt=fix the bug; model said: done. owner mail budi" + "@example.com"


def _log(db, world, classification):
    ev = collector.collect_evidence(db, kind="LOG", origin="worker", collector_identity="opencode-worker",
                                    claim="worker event log", content=LOG_TEXT, project_id=world.project.id,
                                    run_id=world.run.id, task_id=world.task.id, classification=classification)
    db.commit()
    return ev


def test_restricted_evidence_content_only_for_super_admin(app, personal_client, db):
    world = make_world(db)
    restricted = _log(db, world, "RESTRICTED")
    internal = _log(db, world, "INTERNAL")
    # list views never carry the content (not opened by default)
    listing = personal_client.get("/api/evidence", params={"run_id": world.run.id}).json()
    assert listing["items"] and all("content" not in i for i in listing["items"])
    admin = personal_client.get(f"/api/evidence/{restricted.id}").json()
    assert admin["content"] and admin["content_note"] is None
    assert "[PII:EMAIL]" in admin["content"] and "@example.com" not in admin["content"]  # redacted at rest
    create_viewer(db)
    with TestClient(app, base_url="http://testserver") as raw:
        viewer = AuthedClient(raw)
        viewer.login("viewer", VIEWER_PW)
        viewer.select_domain(Domain.PERSONAL)
        hidden = viewer.get(f"/api/evidence/{restricted.id}")
        assert hidden.status_code == 200, hidden.text
        body = hidden.json()
        assert body["content"] is None and body["content_note"].startswith("RESTRICTED")
        assert "content_json" not in body
        visible = viewer.get(f"/api/evidence/{internal.id}").json()
        assert visible["content"] and "@example.com" not in visible["content"]
