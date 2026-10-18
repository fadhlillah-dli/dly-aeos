"""Tools/repos transparency: inventory, observations and drift (§36-§38). §50 rows:

* 3.1 "Katalog blueprint berisi banyak tool tetapi host belum diprobe" -> PLANNED/CANDIDATE/UNKNOWN only;
* 3.2 "Tool ditemukan dalam README atau lockfile" -> reference/declaration, not an active process;
* 3.3 "Tool installed dan enabled tanpa run yang teramati" -> not IN_USE; last observation shown;
* 3.6 "Binary/model route/config berganti dari approved pin" -> drift visible; readiness invalid;
* 3.10 "Komponen ditemukan di managed scope tanpa registrasi" -> UNREGISTERED observation, never adopted;
* 3.12 "Inventory/stream gagal atau offline" -> last snapshot + STALE, never green;
* 3.13 "Viewer mengakses detail/search/export inventory Office" -> scope enforced, no metadata/counts.

The host is the fake probe boundary (only named binaries exist); the managed scope read by the
reconciler is a temporary platform checkout (pyproject/README/.mcp.json) passed to the real
reconciliation service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aeos.core.enums import Capability, Domain
from aeos.models.inventory import ComponentReference
from tests.acceptance.world import (
    WORKER_ROUTE,
    activate_route,
    caps_by_name,
    doctor_ready,
    fake_opencode,
    manifest_with_pin,
    owner_exception,
    owner_id,
    register_free_route,
    seed_catalog,
)
from tests.conftest import VIEWER_PW, AuthedClient, create_viewer

pytestmark = pytest.mark.acceptance

OFFICE_ONLY = ("jira", "confluence", "bitbucket", "bitbucket-pipelines")


def _ok(r: Any, code: int = 200) -> dict[str, Any]:
    assert r.status_code == code, r.text
    return r.json()


def _all_components(client: AuthedClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        body = _ok(client.get("/api/inventory/components", params={"page": page, "page_size": 200}))
        out += body["items"]
        if len(out) >= body.get("total", len(out)) or not body["items"]:
            return out
        page += 1


def _detail(client: AuthedClient, cid: str) -> dict[str, Any]:
    return _ok(client.get(f"/api/inventory/components/{cid}"))


def _node(client: AuthedClient, cid: str, lens: str = "OBSERVED") -> dict[str, Any]:
    nodes = _ok(client.get("/api/inventory/map", params={"lens": lens}))["nodes"]
    return next(n for n in nodes if n["id"] == cid)


def _scope_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "platform-checkout"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "dly-aeos"\nversion = "0.0.0"\ndependencies = ["temporalio>=1.7", "fastapi>=0.110"]\n',
        encoding="utf-8")
    (repo / "README.md").write_text("Kami memakai LangGraph, Goose dan Serena di laptop pribadi.\n", encoding="utf-8")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"serena": {"command": "uvx"},
                                                               "mystery-mcp": {"command": "npx"}}}), encoding="utf-8")
    return repo


def _reconcile(db, settings, repo: Path | None = None, component_id: str | None = None):
    from aeos.inventory.reconcile import reconcile

    run = reconcile(db, settings, component_id=component_id, actor=owner_id(db), trigger="api", repo=repo)
    db.commit()
    return run


def _enabled_opencode(client: AuthedClient, db, monkeypatch, host, settings) -> None:
    """OpenCode installed on the host and enabled by the owner (agent definition), never run."""
    seed_catalog(db)
    host.install("opencode", "1.19.3\n")
    settings.opencode_binary = None
    fake_opencode(monkeypatch)  # the worker runtime's own view of the same binary
    register_free_route(db, WORKER_ROUTE)
    activate_route(client, WORKER_ROUTE)
    owner_exception(client, target_type="worker_sandbox", target_id="process", details={"allow_process_sandbox": True})
    # after the owner decisions: they mark dependent Doctor results STALE (§08), see world.doctor_ready
    doctor_ready(db, Capability.CAN_RUN_WORKER)
    _ok(client.post("/api/workforce/agents/opencode-implementer/enable", json={"reason": "pilot"}))


# ---------------------------------------------------------------------------------------------- 3.1
def test_unprobed_catalog_is_design_and_unknown_never_installed_or_in_use(personal_client: AuthedClient, db, host):
    seed_catalog(db)
    items = _all_components(personal_client)
    assert len(items) > 50
    for c in items:
        dims = c["dimensions"]
        assert c["design_label"] in ("PLANNED", "SELECTED_DESIGN", "CANDIDATE", "OPTIONAL_CANDIDATE", "WATCHLIST",
                                     "REFERENCE_ONLY", "UNVERIFIED", "PROFILE_DESIGN"), c["component_id"]
        assert dims["installation"]["value"] in ("UNKNOWN", "NOT_APPLICABLE"), c["component_id"]
        assert dims["enablement"]["value"] == "DISABLED" and dims["usage"]["value"] == "NEVER_OBSERVED"
        assert dims["admission"]["value"] == "DISCOVERED"
    observed = _ok(personal_client.get("/api/inventory/map", params={"lens": "OBSERVED"}))
    assert not [n for n in observed["nodes"] if n["present"]], "nothing is observed before a probe"
    assert {n["status"] for n in observed["nodes"]} == {"UNKNOWN"}
    coverage = _ok(personal_client.get("/api/inventory/coverage"))
    codes = {g["code"] for g in coverage["coverage"]["gaps"]} if "coverage" in coverage else {
        g["code"] for g in coverage["gaps"]}
    assert {"NO_RECONCILE", "NEVER_PROBED", "NO_USAGE_INSTRUMENTATION"} <= codes
    assert host.calls == [], "listing the catalog probes nothing"


# ---------------------------------------------------------------------------------------------- 3.2 / 3.10
def test_readme_and_lockfile_mentions_are_declarations_and_unregistered_finds_are_never_adopted(
        personal_client: AuthedClient, db, host, settings, tmp_path):
    seed_catalog(db)
    comps_before = db.scalar(select(func.count()).select_from(ComponentReference))
    _reconcile(db, settings, repo=_scope_repo(tmp_path))
    temporal = _detail(personal_client, "temporal")
    dims = temporal["lifecycle"]
    assert dims["installation"]["value"] in ("UNKNOWN", "NOT_INSTALLED") and dims["usage"]["value"] == "NEVER_OBSERVED"
    assert temporal["versions"]["lockfile_refs"] == ["pyproject.toml"], "a declaration, with its source"
    assert _node(personal_client, "temporal")["status"] == "DECLARED"
    assert _node(personal_client, "temporal", "CONFIG")["present"] is False, "declared is not enabled"
    serena = _detail(personal_client, "serena")
    assert serena["lifecycle"]["usage"]["value"] == "NEVER_OBSERVED"
    assert _node(personal_client, "serena")["status"] == "DECLARED"
    # a README mention is not even a declaration
    for cid in ("langgraph", "goose"):
        assert _node(personal_client, cid)["status"] == "UNKNOWN", cid
    # 3.10: the unregistered MCP server is an observation only
    observed = _ok(personal_client.get("/api/inventory/map", params={"lens": "OBSERVED"}))["nodes"]
    unreg = next(n for n in observed if n["id"] == "unregistered:mcp:mystery-mcp")
    assert unreg["status"] == "UNREGISTERED" and unreg["design_label"] == "UNREGISTERED"
    assert unreg["drift"][0]["drift_class"] == "observed-but-unregistered"
    drift = _ok(personal_client.get("/api/inventory/drift"))
    assert any(i["component_id"] == "unregistered:mcp:mystery-mcp" and i["drift_class"] == "observed-but-unregistered"
               for i in drift["items"])
    assert db.scalar(select(func.count()).select_from(ComponentReference)) == comps_before, "never adopted"
    assert db.get(ComponentReference, "unregistered:mcp:mystery-mcp") is None
    assert personal_client.get("/api/inventory/components/unregistered:mcp:mystery-mcp").status_code == 404
    assert not [c for c in host.calls if "npx" in c or "uvx" in c], "configured MCP commands are never run"


# ---------------------------------------------------------------------------------------------- 3.3
def test_installed_and_enabled_without_a_run_is_not_in_use(personal_client: AuthedClient, db, monkeypatch, host,
                                                           settings):
    _enabled_opencode(personal_client, db, monkeypatch, host, settings)
    _ok(personal_client.post("/api/inventory/refresh", json={"component_id": "opencode"}))
    detail = _detail(personal_client, "opencode")
    dims = detail["lifecycle"]
    assert dims["installation"]["value"] == "INSTALLED" and dims["installation"]["version"] == "1.19.3"
    assert dims["usage"]["value"] == "NEVER_OBSERVED", "installed + enabled is not IN_USE"
    assert detail["config_lens"]["present"] is True
    assert any("agent definition opencode-implementer enabled" in b for b in detail["config_lens"]["basis"])
    assert detail["evidence"]["last_observed_at"], "the last observation is shown with its time"
    node = _node(personal_client, "opencode")
    assert node["status"] == "INSTALLED" and node["at"]
    drift = _ok(personal_client.get("/api/inventory/drift"))["items"]
    assert any(i["component_id"] == "opencode" and i["drift_class"] == "enabled-but-unverified" for i in drift)


# ---------------------------------------------------------------------------------------------- 3.6
def _pinned_opencode(client: AuthedClient, db, host, settings, tmp_path, *, pin: str, installed: str) -> None:
    from aeos.core.paths import repo_root
    from aeos.inventory.manifest import apply_manifest

    seed_catalog(db)
    apply_manifest(db, actor="owner", path=manifest_with_pin(tmp_path, "opencode", pin), root=Path(repo_root()))
    db.commit()
    host.install("opencode", f"{installed}\n")
    settings.opencode_binary = None
    # CAN_RUN_WORKER reads the ADR-0051 sandbox requirement live (not from a stored CheckResult): without
    # Docker only an ACTIVE owner exception for the WEAK process sandbox satisfies it. Recorded before the
    # controlled precondition (world.doctor_ready), whose results it would otherwise mark STALE.
    owner_exception(client, target_type="worker_sandbox", target_id="process", details={"allow_process_sandbox": True})


def test_binary_drift_from_the_approved_pin_is_visible_and_invalidates_readiness(personal_client: AuthedClient, db,
                                                                                 host, settings, tmp_path):
    _pinned_opencode(personal_client, db, host, settings, tmp_path, pin="1.18.0", installed="1.18.0")
    _ok(personal_client.post("/api/inventory/refresh", json={"component_id": "opencode"}))
    versions = _detail(personal_client, "opencode")["versions"]
    assert (versions["approved_pin"], versions["observed_version"], versions["status"]) == ("1.18.0", "1.18.0",
                                                                                          "MATCH")
    doctor_ready(db, Capability.CAN_RUN_WORKER)
    cap = caps_by_name(personal_client)["CAN_RUN_WORKER"]
    assert cap["status"] == "READY", cap.get("reasons")
    # the binary is upgraded in place, away from the approved pin
    host.install("opencode", "1.19.3\n")
    host.replace_binary("opencode", size=4242)
    _ok(personal_client.post("/api/inventory/refresh", json={"component_id": "opencode"}))
    versions = _detail(personal_client, "opencode")["versions"]
    assert (versions["approved_pin"], versions["observed_version"], versions["status"]) == ("1.18.0", "1.19.3",
                                                                                          "MISMATCH")
    drift = _ok(personal_client.get("/api/inventory/drift"))["items"]
    mismatch = [i for i in drift if i["component_id"] == "opencode" and i["drift_class"] == "resolved-version-mismatch"]
    assert mismatch and mismatch[0]["severity"] == "HIGH" and "1.19.3" in mismatch[0]["summary"]
    cap = caps_by_name(personal_client)["CAN_RUN_WORKER"]
    assert cap["status"] != "READY", "readiness is invalid until rechecked"
    checks = {c["check_id"]: c for c in _ok(personal_client.get("/api/doctor/checks"))["items"]}
    assert checks["worker.opencode"]["effective_status"] == "STALE"


BUG_PIN_REDECLARED = (
    "BUG: doctor seam - a re-declared approved pin does not invalidate readiness: apply_manifest only "
    "invalidates checks that declare the 'manifest' invalidator (repo.manifest), worker.opencode accepts any "
    "found version as VERIFIED, and a HIGH resolved-version-mismatch drift finding is not an input of "
    "CAN_RUN_WORKER, so the capability stays READY while the binary deviates from the approved pin (§50 3.6).")


@pytest.mark.xfail(strict=True, reason=BUG_PIN_REDECLARED)
def test_redeclared_pin_that_the_binary_no_longer_matches_invalidates_readiness(personal_client: AuthedClient, db,
                                                                                host, settings, tmp_path):
    from aeos.core.paths import repo_root
    from aeos.inventory.manifest import apply_manifest

    _pinned_opencode(personal_client, db, host, settings, tmp_path, pin="1.19.3", installed="1.19.3")
    _ok(personal_client.post("/api/inventory/refresh", json={"component_id": "opencode"}))
    doctor_ready(db, Capability.CAN_RUN_WORKER)
    assert caps_by_name(personal_client)["CAN_RUN_WORKER"]["status"] == "READY"
    # the owner re-pins opencode to 1.18.0; the host still runs 1.19.3
    apply_manifest(db, actor="owner", path=manifest_with_pin(tmp_path, "opencode", "1.18.0"), root=Path(repo_root()))
    db.commit()
    _ok(personal_client.post("/api/inventory/refresh", json={"component_id": "opencode"}))
    drift = _ok(personal_client.get("/api/inventory/drift"))["items"]
    assert any(i["component_id"] == "opencode" and i["drift_class"] == "resolved-version-mismatch" for i in drift)
    assert caps_by_name(personal_client)["CAN_RUN_WORKER"]["status"] != "READY"


# ---------------------------------------------------------------------------------------------- 3.12
def test_offline_inventory_shows_the_last_snapshot_and_stale_never_green(personal_client: AuthedClient, db,
                                                                         monkeypatch, host, settings):
    from aeos.inventory.observations import expire_component_observations

    _enabled_opencode(personal_client, db, monkeypatch, host, settings)
    _ok(personal_client.post("/api/inventory/refresh", json={"component_id": "opencode"}))
    snap = _ok(personal_client.post("/api/inventory/snapshots", json={"label": "terakhir sehat"}))
    first_seen = _node(personal_client, "opencode")["at"]
    # the observations age out while the reconciler cannot run (TTL elapsed / invalidated)
    expire_component_observations(db, "opencode", reason="reconciler offline; TTL elapsed")
    db.commit()
    node = _node(personal_client, "opencode")
    assert node["status"] == "STALE" and node["stale"] is True and node["present"] is False
    assert node["at"] == first_seen, "the last observed time is kept"
    inst = _detail(personal_client, "opencode")["lifecycle"]["installation"]
    assert inst["stale"] is True and inst["effective"] != "INSTALLED", inst
    # the next reconciliation cannot reach the host: UNKNOWN/FAILED, never green
    def offline(argv):
        raise OSError("host unreachable")

    host.outputs["opencode"] = offline
    run = _ok(personal_client.post("/api/inventory/refresh", json={"component_id": "opencode"}))["run"]
    assert run["status"] in ("FAILED", "PARTIAL") or run["summary"], run
    inst = _detail(personal_client, "opencode")["lifecycle"]["installation"]
    assert inst["effective"] == "UNKNOWN", inst
    assert _node(personal_client, "opencode")["present"] is False
    # the snapshot taken before is still served, with its own time and hash
    old = _ok(personal_client.get(f"/api/inventory/snapshots/{snap['id']}"))
    assert old["snapshot_hash"] == snap["snapshot_hash"] and old["created_at"] == snap["created_at"]
    listed = _ok(personal_client.get("/api/inventory/snapshots"))
    assert listed["mode"] == "DESIGN" and any(i["id"] == snap["id"] for i in listed["items"])


# ---------------------------------------------------------------------------------------------- 3.13
def test_viewer_cannot_reach_office_inventory_detail_search_export_or_counts(personal_client: AuthedClient, db, app,
                                                                             host, vault):
    seed_catalog(db)
    vault.put("office/atlassian", json.dumps({"email": "svc@corp.example", "api_token": "x" * 24}), actor="owner")
    create_viewer(db)
    with TestClient(app, base_url="http://testserver") as raw:
        viewer = AuthedClient(raw)
        viewer.login("viewer", VIEWER_PW)
        viewer.select_domain(Domain.PERSONAL)
        ids = {c["component_id"] for c in _all_components(viewer)}
        assert ids and not ids & set(OFFICE_ONLY)
        for cid in OFFICE_ONLY:
            assert viewer.get(f"/api/inventory/components/{cid}").status_code == 404, cid
            assert viewer.get(f"/api/inventory/components/{cid}/relations").status_code == 404, cid
            assert viewer.get(f"/api/inventory/blast-radius/{cid}").status_code == 404, cid
        found = _ok(viewer.get("/api/inventory/components", params={"q": "jira"}))
        assert found["items"] == [] and found.get("total", 0) == 0
        for q in ("confluence", "jira", "bitbucket"):
            search = _ok(viewer.get("/api/control-center/search", params={"q": q}))
            assert search["items"] == [] and search["scope"]["domain"] == "PERSONAL", search
        export = _ok(viewer.get("/api/ops/export", params={"kind": "inventory"}))
        assert export["scope"]["domain"] == "PERSONAL"
        text = json.dumps(export).lower()
        assert not [cid for cid in ("jira", "confluence", "bitbucket") if f'"{cid}"' in text]
        coverage = _ok(viewer.get("/api/inventory/coverage"))
        coverage = coverage.get("coverage", coverage)
        gap = next(g for g in coverage["gaps"] if g["code"] == "DOMAIN_FILTER")
        assert not any(ch.isdigit() for ch in gap["message"]), "no count of the other domain's components"
        viewer_map = _ok(viewer.get("/api/inventory/map", params={"lens": "DESIGN"}))
        assert not {n["id"] for n in viewer_map["nodes"]} & set(OFFICE_ONLY)
        assert not [g for g in viewer_map["groups"] if set(g["node_ids"]) & set(OFFICE_ONLY)], "no OFFICE counts"
        # events are scoped too: an OFFICE inventory event never reaches the PERSONAL viewer
        from aeos.events import service as events

        events.emit(db, source="inventory", kind="acc.office.inventory", domain="OFFICE",
                    payload={"component_id": "jira", "note": "office-only observation"})
        db.commit()
        listed = _ok(viewer.get("/api/events"))["events"]
        assert all(e.get("domain") != "OFFICE" for e in listed) and "acc.office.inventory" not in json.dumps(listed)
        # credentials: not even the existence of the stored OFFICE credential is visible
        secrets = _ok(viewer.get("/api/secrets/status"))
        assert secrets["office_hidden"] is True
        assert not [i for i in secrets["items"] if str(i.get("ref", "")).startswith("office/")], secrets["items"]
        assert "office/atlassian" not in json.dumps(secrets)
        # a viewer whose scope is PERSONAL cannot switch into OFFICE to read it there
        from aeos.models.identity import User

        user = db.scalar(select(User).where(User.username == "viewer"))
        user.domain_scope = ["PERSONAL"]
        db.commit()
        assert viewer.post("/api/session/context", json={"domain": "OFFICE"}).status_code == 403
        assert viewer.get("/api/inventory/components/jira").status_code == 404
        # the viewer cannot widen the scope by writing either
        assert viewer.post("/api/inventory/refresh", json={}).status_code == 403
    # control: the same records exist and are served inside an OFFICE session of an owner in scope
    personal_client.select_domain(Domain.OFFICE)
    assert "office/atlassian" in json.dumps(_ok(personal_client.get("/api/secrets/status")))
    assert "acc.office.inventory" in json.dumps(_ok(personal_client.get("/api/events"))["events"])
    assert _ok(personal_client.get("/api/inventory/components/jira"))["component_id"] == "jira"
