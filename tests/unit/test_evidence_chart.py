"""§29 "Evidence chart dari data test/metric yang tercatat; bukan ilustrasi untuk menyatakan PASS": every plotted
point is a recorded SLI sample (id + time), a missing value is an UNKNOWN gap (never 0), nothing is drawn without
data, and another domain's samples never appear."""

from __future__ import annotations

from datetime import timedelta

from aeos.core.timeutil import utcnow
from aeos.docs_engine import visuals as vis
from aeos.models.operations_ext import OpsSliSample
from tests.conftest import AuthedClient


def test_chart_is_drawn_only_from_bound_recorded_points():
    pts = [{"ref": "s1", "at": "2026-10-19T08:00:00+00:00", "value": 2.0},
           {"ref": "s2", "at": "2026-10-19T09:00:00+00:00", "value": None},
           {"ref": None, "at": "2026-10-19T10:00:00+00:00", "value": 9.0},
           {"ref": "s4", "at": "2026-10-19T11:00:00+00:00", "value": 0.0}]
    c = vis.evidence_chart(title="Lease usang", unit="count", source="ops_sli_samples", points=pts)
    assert c["status"] == "EVIDENCE_BOUND" and c["refs"] == ["s1", "s4"]
    assert c["unknown_points"] == 1 and c["unbound_points_dropped"] == 1
    assert "line [2, 0]" in c["mermaid"] and "9" not in c["mermaid"].split("line")[1]
    assert "PASS" not in c["mermaid"] and c["evidence_role"] == "RECORDED_DATA_NOT_A_VERDICT"
    empty = vis.evidence_chart(title="x", unit="count", source="s", points=[{"ref": "a", "at": "t", "value": None}])
    assert empty["status"] == "NO_DATA" and empty["mermaid"] is None


def test_sli_chart_endpoint_uses_this_domains_samples(personal_client: AuthedClient, db):
    now = utcnow()
    db.add_all([OpsSliSample(at=now - timedelta(hours=2), sli_key="stale_leases", scope="PERSONAL", value=1.0,
                             status="NO_TARGET"),
                OpsSliSample(at=now - timedelta(hours=1), sli_key="stale_leases", scope="PERSONAL", value=3.0,
                             status="NO_TARGET"),
                OpsSliSample(at=now, sli_key="stale_leases", scope="OFFICE", value=99.0, status="NO_TARGET")])
    db.commit()
    body = personal_client.get("/api/ops/sli/stale_leases/chart").json()
    assert body["status"] == "EVIDENCE_BOUND" and len(body["refs"]) == 2
    assert "line [1, 3]" in body["mermaid"] and "99" not in body["mermaid"]
    assert body["source"] == "ops_sli_samples" and body["checked_at"]
    assert personal_client.get("/api/ops/sli/queue_age_seconds/chart").json()["status"] == "NO_DATA"
    assert personal_client.get("/api/ops/sli/nope/chart").status_code == 404
