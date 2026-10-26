"""TechnologyInventoryView / SystemMapView projections (§35-§38) and ArchitectureSnapshot (§13).

§50 rows covered:
* "Viewer mengakses detail/search/export inventory Office" -> PERSONAL scope never shows OFFICE items.
* "Snapshot export dibuka setelah runtime berubah" -> scope/time/hash; diff, not live inventory.
* "Mock diagram ... ditampilkan" -> DESIGN lens labelled DESIGN; uninstrumented edges UNKNOWN.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from aeos.core.errors import NotFound, ValidationFailed
from aeos.inventory import snapshots, views
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run
from aeos.models.decisions import ArchitectureSnapshot
from aeos.models.inventory import ComponentReference
from tests.unit.test_inventory_fakes import base_runner

OFFICE_ONLY = {"bitbucket", "bitbucket-pipelines", "jira", "confluence"}
PERSONAL_ONLY = {"github", "github-issues", "github-actions", "google-drive", "google-docs"}


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


def test_design_lens_shows_logical_map_and_separates_domains(seeded):
    personal = views.system_map(seeded, lens="DESIGN", domain="PERSONAL")
    office = views.system_map(seeded, lens="design", domain="OFFICE")
    p_ids = {n["id"] for n in personal["nodes"]}
    o_ids = {n["id"] for n in office["nodes"]}
    assert not (p_ids & OFFICE_ONLY) and p_ids >= PERSONAL_ONLY
    assert not (o_ids & PERSONAL_ONLY) and o_ids >= OFFICE_ONLY
    for m, ids in ((personal, p_ids), (office, o_ids)):
        assert all(e["source"] in ids and e["target"] in ids for e in m["edges"])
        assert all(e["status"] == "DESIGN" for e in m["edges"])
    edges = {(e["source"], e["target"], e["relation"]) for e in personal["edges"]}
    # §36 logical map: UI -> control plane -> Claude native / broker / publisher; state; OpenBao
    assert ("control-center-frontend", "dly-aeos-control-plane", "calls") in edges
    assert ("dly-aeos-control-plane", "claude-native-bridge", "calls") in edges
    assert ("claude-native-bridge", "claude-code", "runs_with") in edges
    assert ("worker-runtime", "opencode", "runs_with") in edges
    assert ("publisher", "google-docs", "publishes_to") in edges
    assert ("secret-provider", "openbao", "reads") in edges
    assert ("publisher", "jira", "publishes_to") not in edges  # OFFICE edge hidden from PERSONAL
    assert personal["mode"] == "DESIGN" and personal["lens_label"] == "Rancangan disetujui"
    groups = {g["id"] for g in personal["groups"]}
    assert groups == {"experience/intake", "planning/governance", "workforce", "tools/context", "code/docs/CI",
                      "data/secrets", "operations/evaluation"}


def test_observed_lens_marks_uninstrumented_as_unknown(seeded, settings):
    empty = views.system_map(seeded, lens="OBSERVED", domain="PERSONAL")
    assert all(n["status"] == "UNKNOWN" and not n["present"] for n in empty["nodes"])
    assert empty["edges"] == []
    runner = base_runner()
    runner.add_binary("node", "v24.15.0")
    reconcile(seeded, settings, runner=runner)
    seeded.commit()
    m = views.system_map(seeded, lens="OBSERVED", domain="PERSONAL")
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes["nodejs-runtime"]["present"] and nodes["nodejs-runtime"]["status"] == "INSTALLED"
    assert nodes["hermes"]["status"] == "UNKNOWN"  # candidate never probed
    assert all(e["status"] in ("UNKNOWN", "OBSERVED", "STALE") for e in m["edges"])
    assert any(e["status"] == "UNKNOWN" and "tidak diinstrumentasi" in e["evidence"] for e in m["edges"])


def test_config_lens_needs_explicit_basis(seeded, settings):
    m = views.system_map(seeded, lens="CONFIG", domain="PERSONAL")
    nodes = {n["id"]: n for n in m["nodes"]}
    assert not nodes["openbao"]["present"]  # a default setting is not enablement
    assert not nodes["claude-code"]["present"]
    seeded.get(ComponentReference, "claude-code").enablement = "ENABLED"
    seeded.commit()
    m = views.system_map(seeded, lens="CONFIG", domain="PERSONAL")
    node = next(n for n in m["nodes"] if n["id"] == "claude-code")
    assert node["present"] and node["status"] == "ENABLED" and node["basis"]


def test_config_lens_counts_an_activated_go_route_for_the_opencode_console(seeded):
    """An OpenCode Go route (ADR-0041 amendment 2026-10-26) runs on the OpenCode Console component once activated;
    a CANDIDATE row is no basis."""
    from aeos.models.routing import ModelRoute

    route = ModelRoute(route_key="opencode-go/glm-5.3-flash", pool="WORKER", provider="opencode-go",
                       model_id="opencode-go/glm-5.3-flash", status="CANDIDATE", eligible_domains=["PERSONAL"])
    seeded.add(route)
    seeded.commit()
    node = next(n for n in views.system_map(seeded, lens="CONFIG", domain="PERSONAL")["nodes"]
                if n["id"] == "opencode-console")
    assert not node["present"]
    route.status = "TRIAL"
    seeded.commit()
    node = next(n for n in views.system_map(seeded, lens="CONFIG", domain="PERSONAL")["nodes"]
                if n["id"] == "opencode-console")
    assert node["present"] and any("opencode-go/glm-5.3-flash" in b for b in node["basis"])


def test_invalid_lens_rejected(seeded):
    with pytest.raises(ValidationFailed):
        views.system_map(seeded, lens="LIVE", domain="PERSONAL")


def test_blast_radius_and_path_search(seeded):
    br = views.blast_radius(seeded, "sqlite-metadata-store", domain="PERSONAL")
    affected = {i["component_id"] for i in br["items"]}
    assert "dly-aeos-control-plane" in affected and "control-center-frontend" in affected
    assert all(i["path"][0] == "sqlite-metadata-store" for i in br["items"])
    deps = views.blast_radius(seeded, "dly-aeos-control-plane", domain="PERSONAL", direction="dependencies")
    assert "python-runtime" in {i["component_id"] for i in deps["items"]}
    office_br = views.blast_radius(seeded, "dly-aeos-control-plane", domain="PERSONAL", direction="dependencies")
    assert not ({i["component_id"] for i in office_br["items"]} & OFFICE_ONLY)
    path = views.find_path(seeded, "control-center-frontend", "claude-code", domain="PERSONAL")
    assert path["found"] and path["path"][0] == "control-center-frontend" and path["path"][-1] == "claude-code"
    with pytest.raises(NotFound):
        views.find_path(seeded, "control-center-frontend", "jira", domain="PERSONAL")
    with pytest.raises(NotFound):
        views.blast_radius(seeded, "jira", domain="PERSONAL")


def test_component_detail_has_every_section_37_field_group(seeded):
    d = views.component_detail(seeded, "claude-code", domain="PERSONAL")
    for group in ("identity", "scope", "source", "versions", "usage_role", "lifecycle", "access", "relations",
                  "evidence", "cost", "operations", "changes"):
        assert group in d, group
    dims = d["lifecycle"]
    assert set(dims) == {"admission", "installation", "configuration", "connectivity", "functional", "enablement",
                         "usage", "lifecycle_final"}
    for v in dims.values():
        assert {"value", "at", "source", "reason"} <= set(v)
    assert d["source"]["license"]["value"] is None and d["source"]["license"]["status"] == "UNKNOWN"
    assert d["source"]["license"]["reason"]
    assert d["versions"]["approved_pin"] is None and d["versions"]["status"] == "NO_PIN"
    assert d["scope"]["maintainer"]["status"] == "UNKNOWN"
    assert d["changes"]["propose_url"] == "/proposals?component=claude-code"
    assert d["cost"]["design"]["model"] == "SUBSCRIPTION"
    unverified = views.component_detail(seeded, "ddorchestrator", domain="PERSONAL")
    assert unverified["source"]["source_url"] is None and unverified["source"]["unresolved_identity"] is True
    assert "install" not in unverified["operations"]


def test_component_identity_group_has_kind_category_upstream_and_logical_instances(seeded, settings):
    """§37 Identity: stable ID, kind, friendly name, function, category, upstream identity, instances."""
    before = views.component_detail(seeded, "nodejs-runtime", domain="PERSONAL")["identity"]
    assert before["instances"] is None and before["instances_note"]
    runner = base_runner()
    runner.add_binary("node", "v24.15.0")
    reconcile(seeded, settings, runner=runner)
    seeded.commit()
    ident = views.component_detail(seeded, "nodejs-runtime", domain="PERSONAL")["identity"]
    assert ident["component_id"] == "nodejs-runtime"
    assert ident["kind"] and ident["category"] and ident["friendly_name"] and ident["function"]
    assert ident["category_label"]
    assert ident["instances"], "an observed installation becomes a component instance"
    blob = json.dumps(ident["instances"], default=str)
    assert str(Path.home()) not in blob and not re.search(r"[A-Za-z]:\\\\", blob), "instances are logical"
    unverified = views.component_detail(seeded, "ddorchestrator", domain="PERSONAL")["identity"]
    assert unverified["upstream_identity_status"] == "UNVERIFIED"


def test_detail_and_lists_never_leak_local_paths(seeded, settings):
    runner = base_runner()
    runner.add_binary("node", "v24.15.0")
    reconcile(seeded, settings, runner=runner)
    seeded.commit()
    blob = json.dumps(views.component_detail(seeded, "nodejs-runtime", domain="PERSONAL"), default=str)
    blob += json.dumps(views.list_components(seeded, domain="PERSONAL", page_size=200), default=str)
    assert str(Path.home()) not in blob and str(settings.data_dir) not in blob
    assert not re.search(r"[A-Za-z]:\\\\Users", blob)


def test_domain_scope_for_detail_list_and_drift(seeded, settings):
    with pytest.raises(NotFound):
        views.component_detail(seeded, "jira", domain="PERSONAL")
    with pytest.raises(NotFound):
        views.component_detail(seeded, "github", domain="OFFICE")
    personal = views.list_components(seeded, domain="PERSONAL", page_size=200)
    assert not ({i["component_id"] for i in personal["items"]} & OFFICE_ONLY)
    office = views.list_components(seeded, domain="OFFICE", page_size=200)
    assert not ({i["component_id"] for i in office["items"]} & PERSONAL_ONLY)
    rel = views.component_relations(seeded, "publisher", domain="PERSONAL")
    assert not ({r["target"] for r in rel["outgoing"]} & OFFICE_ONLY)


def test_list_filters_pagination_and_advanced_view(seeded):
    page = views.list_components(seeded, domain="PERSONAL", requirement="CANDIDATE", page_size=5)
    assert page["total"] > 5 and len(page["items"]) == 5
    assert all(i["requirement"] == "CANDIDATE" for i in page["items"])
    q = views.list_components(seeded, domain="PERSONAL", q="opencode")
    assert {"opencode", "opencode-console"} <= {i["component_id"] for i in q["items"]}
    adv = views.list_components(seeded, domain="PERSONAL", view="advanced", category="workforce")
    assert all("source_refs" in i and "relations" in i for i in adv["items"])
    assert set(adv["facets"]) == {"kinds", "requirements", "categories", "design_labels"}


def test_coverage_reports_gaps_honestly(seeded, settings, tmp_path):
    cov = views.coverage(seeded, domain="PERSONAL")
    assert any(g["code"] == "NO_RECONCILE" for g in cov["gaps"])
    reconcile(seeded, settings, runner=base_runner(), repo=tmp_path)  # empty repo: no lockfiles read
    seeded.commit()
    cov = views.coverage(seeded, domain="PERSONAL")
    codes = {g["code"] for g in cov["gaps"]}
    assert {"NPM_LOCK_NOT_READ", "PY_LOCK_MISSING", "CANDIDATES_NOT_PROBED", "EXTERNAL_NOT_PROBED",
            "SCOPE_LIMIT"} <= codes
    msg = next(g["message"] for g in cov["gaps"] if g["code"] == "NPM_LOCK_NOT_READ")
    assert "lockfile frontend belum dibaca" in msg.lower()


def test_snapshot_is_immutable_and_diff_lists_changes(seeded):
    a = snapshots.create_snapshot(seeded, label="sebelum", domain="PERSONAL", actor="tester")
    seeded.commit()
    b0 = snapshots.create_snapshot(seeded, label="sama", domain="PERSONAL", actor="tester")
    seeded.commit()
    assert snapshots.diff_snapshots(seeded, a.id, b0.id, domain="PERSONAL")["same_hash"] is True
    comp = seeded.get(ComponentReference, "git")
    comp.approved_pin = "2.45.1"
    seeded.get(ComponentReference, "ralph").tombstoned_at = comp.updated_at
    seeded.commit()
    b = snapshots.create_snapshot(seeded, label="sesudah", domain="PERSONAL", actor="tester")
    seeded.commit()
    diff = snapshots.diff_snapshots(seeded, a.id, b.id, domain="PERSONAL")
    assert diff["same_hash"] is False
    assert {n["id"] for n in diff["nodes"]["removed"]} == {"ralph"}
    changed = {c["key"]: c["fields"] for c in diff["nodes"]["changed"]}
    assert changed["git"]["approved_pin"] == [None, "2.45.1"]
    assert any(e["source"] == "ralph" for e in diff["edges"]["removed"])
    assert a.refs["manifest"]["content_hash"] and a.refs["master"]["sha256"].startswith("sha256:")
    assert a.refs["scope"]["domain"] == "PERSONAL"
    # immutability: the append-only trigger refuses updates and deletes
    with pytest.raises(DBAPIError):
        seeded.execute(ArchitectureSnapshot.__table__.update().values(label="x"))
    seeded.rollback()
    with pytest.raises(DBAPIError):
        seeded.execute(ArchitectureSnapshot.__table__.delete())
    seeded.rollback()
    # domain separation for snapshots
    assert not snapshots.list_snapshots(seeded, domain="OFFICE")
    with pytest.raises(NotFound):
        snapshots.get_snapshot(seeded, a.id, domain="OFFICE")
