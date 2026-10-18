"""Every Operations write endpoint is SUPER_ADMIN + CSRF only (master §26, §35, §50 "VIEWER memanggil
write endpoint langsung -> Ditolak backend"); human-only operations have no HTTP route at all (§09).
"""

from __future__ import annotations

import pytest

from aeos.core.enums import Domain
from tests.conftest import ADMIN_PW, VIEWER_PW, create_admin, create_viewer

WRITES = [
    ("post", "/api/ops/backups", {"label": "x"}),
    ("post", "/api/ops/backups/some-id/verify", None),
    ("post", "/api/ops/backups/some-id/restore-test", None),
    ("post", "/api/ops/sli/evaluate", None),
    ("post", "/api/ops/emergency-stop/prepare", None),
    ("post", "/api/ops/emergency-stop", {"token": "x" * 20, "reason": "viewer try"}),
    ("put", "/api/ops/flags/ops.sli_loop_enabled", {"value": False}),
]


@pytest.mark.parametrize(("method", "url", "body"), WRITES)
def test_viewer_is_refused_on_every_ops_write(client, db, method, url, body):
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(url, **kwargs)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "READ_ONLY_ROLE"


@pytest.mark.parametrize(("method", "url", "body"), WRITES)
def test_admin_write_without_csrf_is_refused(client, db, method, url, body):
    create_admin(db)
    client.login("owner", ADMIN_PW)
    client.select_domain(Domain.PERSONAL)
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client.client, method)(url, **kwargs)  # raw TestClient: no CSRF header
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


def test_unauthenticated_reads_are_refused(client):
    for url in ("/api/ops/backups", "/api/ops/releases", "/api/ops/sli", "/api/ops/alerts", "/api/ops/flags",
                "/api/ops/export?kind=audit", "/api/ops/services", "/api/control-center/overview"):
        assert client.get(url).status_code == 401, url


def test_no_http_route_for_human_only_operations(app):
    """Live restore, promotion and rollback are CLI-only (no route a worker/candidate could call)."""
    paths = {(m, getattr(r, "path", "")) for r in app.routes for m in (getattr(r, "methods", None) or set())}
    for _method, path in paths:
        assert not path.endswith("/restore"), path
        assert "promote" not in path and "rollback" not in path, path
