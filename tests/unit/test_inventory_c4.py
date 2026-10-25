"""Tools & Repos / System Map gaps closed by the C4 cluster (§07, §08, §25, §34-§38, §41, §51).

Each test names the audit item it guards; every assertion would fail on the pre-C4 code.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.errors import Conflict, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.inventory import dependencies as deps
from aeos.inventory import drift as drift_mod
from aeos.inventory import snapshots, views
from aeos.inventory.observations import record_observation
from aeos.inventory.probes import clear_digest_cache, file_digest, path_fingerprint, set_runner_override
from aeos.inventory.reconcile import reconcile, reconcile_guard
from aeos.inventory.seed import run as seed_run
from aeos.models.inventory import CheckResult, ComponentReference
from tests.conftest import create_admin
from tests.unit.test_inventory_fakes import base_runner, ok

PY_LOCK = """# uv pip compile
alembic==1.20.0
    # via dly-aeos (pyproject.toml)
mako==1.3.0
    # via alembic
colorama==0.4.6 ; sys_platform == 'win32'
    # via
    #   click
    #   dly-aeos (pyproject.toml)
"""
NPM_LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {"dependencies": {"react": "^19.0.0"}, "devDependencies": {"vitest": "^3.0.0"}},
        "node_modules/react": {"version": "19.1.0"},
        "node_modules/vitest": {"version": "3.2.0", "dev": True, "dependencies": {"tinypool": "^1"}},
        "node_modules/tinypool": {"version": "1.0.2", "dev": True},
        "node_modules/vitest/node_modules/chai": {"version": "5.1.0", "dev": True},
    },
}


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


@pytest.fixture
def lock_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "frontend").mkdir(parents=True)
    (repo / "requirements.lock").write_text(PY_LOCK, encoding="utf-8")
    (repo / "frontend" / "package-lock.json").write_text(json.dumps(NPM_LOCK), encoding="utf-8")
    return repo


# ------------------------------------------------------------------ §34 evidence level per claim


def test_config_only_check_is_not_e3_and_binary_probe_is(seeded):
    from aeos.models.doctor_ext import DoctorRun

    now = utcnow()
    run = DoctorRun(started_at=now, finished_at=now, trigger="test", actor="t", groups=[], functional=False)
    frun = DoctorRun(started_at=now, finished_at=now, trigger="test", actor="t", groups=[], functional=True)
    seeded.add_all([run, frun])
    seeded.flush()
    for cid, comp, rid in (("repo.lockfiles", "dly-aeos-platform-repo", run.id), ("claude.binary", "claude-code", run.id),
                           ("adapter.github_token", "github", frun.id)):
        seeded.add(CheckResult(check_id=cid, title=cid, group="X", component_id=comp, scope={}, status="VERIFIED",
                               last_checked_at=now, run_id=rid))
    seeded.commit()
    levels = {c["claim"]: c["level"] for c in views.component_detail(seeded, "dly-aeos-platform-repo",
                                                                     domain="PERSONAL")["evidence"]["claims"]}
    assert levels["Doctor repo.lockfiles"] == "E1"
    claude = {c["claim"]: c["level"] for c in views.component_detail(seeded, "claude-code",
                                                                     domain="PERSONAL")["evidence"]["claims"]}
    assert claude["Doctor claude.binary"] == "E3"
    gh = {c["claim"]: c["level"] for c in views.component_detail(seeded, "github", domain="PERSONAL")["evidence"]["claims"]}
    assert gh["Doctor adapter.github_token"] == "E3"  # run with --functional: remote read-only probe


def test_file_stat_observation_is_e1(seeded):
    record_observation(seeded, component_id="install-manifest", dimension="installation", status="INSTALLED",
                       observer="reconciler", detail={"operation_ref": "stat"})
    seeded.commit()
    inst = next(c for c in views.component_detail(seeded, "install-manifest", domain="PERSONAL")["evidence"]["claims"]
                if c["claim"].startswith("Instalasi"))
    assert inst["level"] == "E1", "a file stat of the manifest is static evidence, not runtime"


# ------------------------------------------------------------------ §35 next action / reason before id


def test_rows_carry_reason_and_next_action(seeded):
    rows = {r["component_id"]: r for r in views.list_components(seeded, domain="PERSONAL", page_size=200)["items"]}
    assert rows["opencode"]["why_selected"] and rows["opencode"]["function"]
    assert rows["opencode"]["next_action"]["code"] == "RUN_RECONCILE"
    assert rows["hermes"]["next_action"]["code"] == "REVIEW_CANDIDATE"
    assert rows["paperclip"]["next_action"]["code"] in ("NONE_REFERENCE", "REVIEW_CANDIDATE")


# ------------------------------------------------------------------ §37 dependency view / SBOM


def test_lockfiles_are_itemised_with_direct_transitive_and_parent(seeded, lock_repo):
    py = views.component_dependencies(seeded, "dly-aeos-control-plane", domain="PERSONAL", repo=lock_repo)
    by = {p["name"]: p for p in py["items"]}
    assert by["alembic"]["relation"] == "direct" and by["mako"]["relation"] == "transitive"
    assert by["mako"]["required_by"] == ["alembic"] and by["colorama"]["direct"] is True
    assert by["colorama"]["marker"] == "sys_platform == 'win32'"
    assert by["alembic"]["used_by_build"]["label"].startswith("digunakan oleh build")
    npm = views.component_dependencies(seeded, "control-center-frontend", domain="PERSONAL", repo=lock_repo,
                                       relation="transitive")
    names = {p["name"]: p for p in npm["items"]}
    assert set(names) == {"tinypool", "chai"} and names["chai"]["parent_path"] == ["vitest"]
    assert names["tinypool"]["required_by"] == ["vitest"]
    page = views.component_dependencies(seeded, "control-center-frontend", domain="PERSONAL", repo=lock_repo,
                                        page=2, page_size=2)
    assert page["total"] == 4 and len(page["items"]) == 2 and page["counts"]["direct"] == 2
    assert "Image container" in page["coverage"]["images"]
    na = views.component_dependencies(seeded, "git", domain="PERSONAL", repo=lock_repo)
    assert na["status"] == "NOT_APPLICABLE"


def test_cyclonedx_sbom_from_lockfiles(lock_repo):
    bom = deps.cyclonedx(lock_repo, version="test")
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5"
    purls = {c["purl"] for c in bom["components"]}
    assert {"pkg:pypi/alembic@1.20.0", "pkg:npm/react@19.1.0", "pkg:npm/chai@5.1.0"} <= purls
    assert bom == {**bom, "serialNumber": deps.cyclonedx(lock_repo)["serialNumber"]}  # deterministic serial
    (lock_repo / "sbom").mkdir()
    (lock_repo / "sbom" / "aeos.cdx.json").write_text(json.dumps(bom), encoding="utf-8")
    reads, comps = deps.read_sbom_files(lock_repo)
    assert reads[0].status == "READ" and len(comps) == len(bom["components"])


def test_reconcile_records_lockfile_hash_and_coverage_gaps(seeded, settings, lock_repo):
    reconcile(seeded, settings, runner=base_runner(), repo=lock_repo)
    seeded.commit()
    codes = {g["code"] for g in views.coverage(seeded, domain="PERSONAL")["gaps"]}
    assert {"SBOM_NOT_COMMITTED", "IMAGES_NOT_MANAGED"} <= codes
    page = views.component_dependencies(seeded, "control-center-frontend", domain="PERSONAL", repo=lock_repo)
    assert page["lockfile"]["matches_last_reconcile"] is True
    (lock_repo / "frontend" / "package-lock.json").write_text(json.dumps({**NPM_LOCK, "x": 1}), encoding="utf-8")
    page = views.component_dependencies(seeded, "control-center-frontend", domain="PERSONAL", repo=lock_repo)
    assert page["lockfile"]["matches_last_reconcile"] is False


def test_sbom_api_is_downloadable(personal_client, db):
    seed_run(db)
    db.commit()
    r = personal_client.get("/api/inventory/sbom")
    assert r.status_code == 200 and r.json()["bomFormat"] == "CycloneDX"
    assert "attachment" in r.headers["content-disposition"]
    d = personal_client.get("/api/inventory/components/control-center-frontend/dependencies", params={"page_size": 5})
    assert d.status_code == 200 and d.json()["page_size"] == 5


# ------------------------------------------------------------------ §07/§25 structured identity fields


def _manifest_data():
    from aeos.inventory.manifest import load_manifest, master_path

    load = load_manifest()
    return copy.deepcopy(load.data), master_path().read_text(encoding="utf-8")


def test_manifest_records_digest_ports_config_refs_and_compatibility(seeded):
    ob = seeded.get(ComponentReference, "openbao")
    assert ob.details["ports"][0] == {"port": 8200, "host": "127.0.0.1", "setting": "openbao_addr",
                                      "purpose": "API vault (loopback saja)"}
    assert "settings:openbao_addr" in ob.details["config_refs"]
    assert ob.details["signature"]["result"] == "NOT_VERIFIED" and ob.details["signature"]["method"] == "gpg"
    oc = views.component_detail(seeded, "opencode", domain="PERSONAL")
    assert oc["versions"]["distribution"]["kind"] == "release_asset"
    assert oc["versions"]["digest_status"] == "NO_APPROVED_DIGEST"
    assert oc["compatibility"]["compatibility"]["os"] == ["windows", "macos", "linux"]


#: Probe types that observe DLY AEOS's own code, files or data - nothing installed from a third-party
#: distribution, so there is no artifact identity to record (they are versioned by the repository itself).
NOT_INSTALLED_PROBES = frozenset({"platform_module", "repo_file", "data_dir", "git_repo", "none"})


def test_every_installable_binary_component_records_its_identity_fields():
    """§07: digest, port, configuration references and compatibility per component - every REQUIRED/OPTIONAL
    component the bootstrap probes as an installed artifact (binaries such as git/vscode/docker, PyPI modules such
    as pytest/ruff/mypy/keyring, npm packages such as vitest/typescript, the OS credential store, the embedded
    SQLite) states them, nothing guessed (W6-019: the guard used to cover probe type 'binary' only)."""
    from aeos.inventory.manifest import load_manifest

    fields = ("approved_digest", "distribution", "signature", "ports", "config_refs", "compatibility")
    missing = {}
    checked: set[str] = set()
    for c in load_manifest().data["components"]:
        ptype = (c.get("probe") or {}).get("type") or "none"
        if ptype not in NOT_INSTALLED_PROBES and c.get("requirement") in ("REQUIRED", "OPTIONAL"):
            checked.add(ptype)
            gaps = [f for f in fields if f not in c]
            if gaps:
                missing[c["component_id"]] = gaps
            assert c.get("approved_digest") is None or c.get("approved_pin"), c["component_id"]
    assert not missing, missing
    assert checked >= {"binary", "python_module", "npm_package", "keyring_backend", "sqlite", "python_runtime",
                       "python_package"}, checked
    by_id = {c["component_id"]: c for c in load_manifest().data["components"]}
    assert by_id["pytest"]["distribution"]["kind"] == "pypi" and "file:requirements.lock" in by_id["pytest"]["config_refs"]
    assert by_id["vitest"]["distribution"]["kind"] == "npm"
    assert "file:frontend/package-lock.json" in by_id["typescript"]["config_refs"]
    assert by_id["os-credential-store"]["distribution"]["kind"] == "os_component"
    for cid in ("pytest", "ruff", "mypy", "keyring-backend", "vitest", "typescript", "os-credential-store"):
        assert by_id[cid]["approved_digest"] is None and by_id[cid]["signature"]["result"] == "UNKNOWN", cid
        assert by_id[cid]["ports"] == [], cid
    by = {c["component_id"]: c for c in load_manifest().data["components"]}
    assert set(by["git"]["compatibility"]["os"]) == {"windows", "macos", "linux"}
    assert "settings:commit_author_email" in by["git"]["config_refs"]
    assert by["mermaid-cli"]["config_refs"] == ["settings:mmdc_binary"] and by["docker"]["ports"] == []


def test_manifest_rejects_guessed_or_invalid_identity_fields():
    from aeos.inventory.manifest import validate_manifest

    data, master = _manifest_data()
    comp = next(c for c in data["components"] if c["component_id"] == "openbao")
    comp["config_refs"] = ["settings:not_a_setting", "file:../outside"]
    comp["ports"] = [{"port": 1, "host": "127.0.0.1", "setting": "nope"}]
    comp["distribution"] = {"kind": "release_asset", "url": "https://example.invalid/guessed.zip"}
    comp["signature"] = {"method": "gpg", "result": "VERIFIED", "verified": True}
    comp["approved_digest"] = "sha256:" + "a" * 64
    errors, _ = validate_manifest(data, master_text=master)
    joined = "\n".join(errors)
    for needle in ("not a Settings field", "repo-relative", "port setting", "does not appear in the master",
                   "verified_at", "approved_digest needs"):
        assert needle in joined, needle
    comp["ports"] = [{"port": 8200, "host": "0.0.0.0"}]
    errors, _ = validate_manifest(data, master_text=master)
    assert any("ports" in e for e in errors), "non-loopback ports are rejected by the schema"


def test_binary_digest_is_observed_and_compared_with_the_approved_digest(seeded, settings, tmp_path):
    exe = tmp_path / "opencode.exe"
    exe.write_bytes(b"opencode build A")
    runner = base_runner()
    runner.binaries["opencode"] = str(exe)
    runner.outputs["opencode"] = ok("1.19.3\n")
    reconcile(seeded, settings, component_id="opencode", runner=runner)
    seeded.commit()
    d = views.component_detail(seeded, "opencode", domain="PERSONAL")["versions"]
    digest_a = file_digest(exe, use_cache=False)
    assert d["observed_digest"] == digest_a
    comp = seeded.get(ComponentReference, "opencode")
    comp.approved_pin = "1.19.3"
    comp.details = {**comp.details, "approved_digest": "sha256:" + "0" * 64}
    seeded.commit()
    items = drift_mod.compute_drift(seeded, evaluated={"opencode"})
    assert any(i.drift_class == "resolved-version-mismatch" and i.key == "digest" for i in items)
    assert views.component_detail(seeded, "opencode", domain="PERSONAL")["versions"]["digest_status"] == "MISMATCH"


def test_tool_schema_change_is_drift_against_the_admitted_schema(seeded):
    comp = seeded.get(ComponentReference, "serena")
    comp.details = {**comp.details, "tool_schema_hash": "sha256:" + "1" * 64}
    seeded.commit()
    record_observation(seeded, component_id="serena", dimension="configuration", status="CONFIGURED",
                       observer="reconciler", detail={"tool_schema_hash": "sha256:" + "2" * 64})
    seeded.commit()
    items = drift_mod.compute_drift(seeded, evaluated={"serena"})
    hit = next(i for i in items if i.key == "tool_schema")
    assert hit.drift_class == "resolved-version-mismatch" and hit.severity == "HIGH"
    assert views.component_detail(seeded, "serena", domain="PERSONAL")["versions"]["tool_schema_hash"]


def test_admission_records_capability_test_results_and_evidence(seeded):
    d = views.component_detail(seeded, "claude-code", domain="PERSONAL")["admission"]
    assert d["capability_tests"][0]["id"] == "claude.capability_test"
    assert d["capability_tests"][0]["result"] == "NOT_TESTED" and d["capability_tests"][0]["evidence_id"] is None
    record_observation(seeded, component_id="claude-code", dimension="functional", status="VERIFIED",
                       observer="claude_bridge", evidence_id="ev-123", ttl_seconds=3600)
    seeded.commit()
    d = views.component_detail(seeded, "claude-code", domain="PERSONAL")["admission"]
    assert d["capability_tests"][0]["result"] == "VERIFIED" and d["capability_tests"][0]["evidence_id"] == "ev-123"


def test_same_size_same_mtime_replacement_changes_the_fingerprint(tmp_path):
    from aeos.inventory.probes import SystemProbeRunner

    f = tmp_path / "tool.exe"
    f.write_bytes(b"AAAA")
    st = f.stat()
    clear_digest_cache()
    before = path_fingerprint(SystemProbeRunner(), str(f))
    f.write_bytes(b"BBBB")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size + same mtime
    clear_digest_cache()
    after = path_fingerprint(SystemProbeRunner(), str(f))
    assert before["size"] == after["size"] and before["mtime_ns"] == after["mtime_ns"]
    assert before["digest"] != after["digest"], "content digest exposes an in-place replacement"


# ------------------------------------------------------------------ §36 drift severity from policy


def test_drift_severity_follows_the_policy_file(seeded, tmp_path, monkeypatch):
    policy = tmp_path / "inventory-drift.yaml"
    policy.write_text("severity:\n  resolved-version-mismatch:\n    version: LOW\n    digest: NOPE\n"
                      "  made-up-class:\n    default: HIGH\n", encoding="utf-8")
    monkeypatch.setattr(drift_mod, "policy_path", lambda root=None: policy)
    p = drift_mod.drift_policy()
    assert p["source"] == "policy/inventory-drift.yaml"
    assert p["severity"]["resolved-version-mismatch"]["version"] == "LOW"
    assert p["severity"]["resolved-version-mismatch"]["digest"] == "HIGH"  # invalid value ignored
    assert any("made-up-class" in w for w in p["warnings"])
    record_observation(seeded, component_id="opencode", dimension="installation", status="INSTALLED",
                       observer="reconciler", observed_version="1.19.3")
    seeded.get(ComponentReference, "opencode").approved_pin = "1.18.0"
    seeded.commit()
    item = next(i for i in drift_mod.compute_drift(seeded, evaluated={"opencode"}) if i.key == "version")
    assert item.severity == "LOW"


def test_shipped_policy_matches_the_defaults():
    import yaml

    from aeos.core.paths import repo_root

    raw = yaml.safe_load((Path(repo_root()) / "policy" / "inventory-drift.yaml").read_text(encoding="utf-8"))
    assert raw["severity"] == drift_mod.DEFAULT_POLICY["severity"]


# ------------------------------------------------------------------ §08 one set of component references


def test_observation_for_an_unregistered_component_id_is_refused(db):
    record_observation(db, component_id="anything-before-seed", dimension="usage", status="IDLE", observer="t")
    seed_run(db)
    db.commit()
    with pytest.raises(ValidationFailed) as exc:
        record_observation(db, component_id="no-such-component", dimension="usage", status="IDLE", observer="t")
    assert exc.value.code == "OBSERVATION_INVALID"
    record_observation(db, component_id="unregistered:mcp:x", dimension="discovery", status="UNREGISTERED",
                       observer="reconciler")


def test_every_doctor_check_names_a_manifest_component():
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.inventory.manifest import load_manifest

    load_all()
    ids = {c["component_id"] for c in load_manifest().data["components"]}
    missing = {cid: s.component_id for cid, s in REGISTRY.items() if s.component_id and s.component_id not in ids}
    assert missing == {}


# ------------------------------------------------------------------ §36 edges: provenance/scope/time + tests


def test_design_edges_store_provenance_scope_and_time(seeded):
    m = views.system_map(seeded, lens="DESIGN", domain="PERSONAL")
    assert m["edges"]
    for e in m["edges"]:
        assert e["provenance"].startswith("install-manifest:"), e
        assert set(e["scope"]["domains"]) <= {"PERSONAL", "PLATFORM"}, e
        assert e["created_at"] is not None


def test_edge_card_shows_the_latest_check_results(seeded):
    seeded.add(CheckResult(check_id="adapter.google_oauth", title="g", group="PROJECT_ADAPTERS",
                           component_id="google-drive", scope={"domain": "PERSONAL"}, status="CONFIGURED",
                           last_checked_at=utcnow()))
    seeded.commit()
    m = views.system_map(seeded, lens="DESIGN", domain="PERSONAL")
    edge = next(e for e in m["edges"] if (e["source"], e["target"]) == ("publisher", "google-drive"))
    assert edge["tests"] and edge["tests"][0]["check_id"] == "adapter.google_oauth"
    office = views.system_map(seeded, lens="DESIGN", domain="OFFICE")
    assert all(t["check_id"] != "adapter.google_oauth" for e in office["edges"] for t in e["tests"])


# ------------------------------------------------------------------ §36/§37 records as nodes; project filter


def _project(db, domain="PERSONAL", key="p1"):
    from aeos.models.portfolio import Project

    p = Project(project_key=key, name=key.upper(), domain=domain)
    db.add(p)
    db.flush()
    return p


def test_routes_roles_and_project_repositories_are_nodes(seeded):
    from aeos.models.portfolio import RepositoryReference
    from aeos.models.routing import ModelRoute
    from aeos.models.workforce import AgentDefinition, RoleSkillPack

    p = _project(seeded)
    office = _project(seeded, "OFFICE", "o1")
    seeded.add_all([
        RepositoryReference(project_id=p.id, provider="GITHUB", full_name="me/app", provider_repo_id="42",
                            verification_status="VERIFIED", last_verified_at=utcnow()),
        RepositoryReference(project_id=office.id, provider="BITBUCKET", full_name="corp/secret"),
        ModelRoute(route_key="or-free", pool="WORKER", provider="openrouter", model_id="x:free", status="APPROVED",
                   eligible_domains=["PERSONAL"]),
        RoleSkillPack(role_key="implementer", name="Implementer", group="build"),
        AgentDefinition(agent_key="impl", role_key="implementer", harness="opencode", enabled=True),
    ])
    seeded.commit()
    cfg = views.system_map(seeded, lens="CONFIG", domain="PERSONAL")
    ids = {n["id"]: n for n in cfg["nodes"]}
    assert "route:or-free" in ids and "role:implementer" in ids
    repo = next(n for n in cfg["nodes"] if n["kind"] == "repository" and n.get("record"))
    assert "me/app" in repo["label"]
    edges = {(e["source"], e["target"], e["relation"]) for e in cfg["edges"]}
    assert ("route:or-free", "openrouter", "calls") in edges and ("role:implementer", "opencode", "runs_with") in edges
    assert not [n for n in cfg["nodes"] if "corp/secret" in n["label"]], "OFFICE repo never in a PERSONAL map"
    listed = views.list_components(seeded, domain="PERSONAL")
    assert [r["full_name"] for r in listed["project_repositories"]] == ["me/app"]
    assert listed["project_repositories"][0]["identity"] == "github:42"
    obs = views.system_map(seeded, lens="OBSERVED", domain="PERSONAL")
    obs_repo = next(n for n in obs["nodes"] if n["kind"] == "repository" and n.get("record"))
    assert obs_repo["status"] == "VERIFIED" and obs_repo["at"]


def test_project_services_come_from_the_approved_plan_version(seeded):
    from aeos.models.planning import Plan, PlanVersion
    from aeos.policy import facade as policy

    owner = create_admin(seeded)
    p = _project(seeded)
    plan = Plan(project_id=p.id, domain="PERSONAL", title="plan")
    seeded.add(plan)
    seeded.flush()
    content = {"architecture": {"overview": {"component_architecture": ["API service (FastAPI)", "Worker job"]}}}
    v = PlanVersion(plan_id=plan.id, project_id=p.id, domain="PERSONAL", version=1, content=content,
                    manifest_hash="sha256:" + "e" * 64)
    seeded.add(v)
    seeded.flush()
    assert views.list_components(seeded, domain="PERSONAL")["project_services"] == [], "unapproved: nothing declared"
    policy.record_decision(seeded, kind="APPROVAL", actor_user_id=owner.id, target_type="plan_version", target_id=v.id,
                           target_hash=v.manifest_hash, project_id=p.id, rationale="owner approves the plan",
                           allowed_operations=["plan.execute"])
    seeded.commit()
    services = views.list_components(seeded, domain="PERSONAL")["project_services"]
    assert {s["name"] for s in services} == {"API service (FastAPI)", "Worker job"}
    assert all(s["status"] == "DECLARED" and s["provenance"] == f"plan_version:{v.manifest_hash}" for s in services)
    m = views.system_map(seeded, lens="DESIGN", domain="PERSONAL", project_id=p.id)
    assert {n["name"] for n in m["nodes"] if n["kind"] == "project_service"} >= {"API service (FastAPI) (P1)"}
    assert views.list_components(seeded, domain="OFFICE")["project_services"] == []
    with pytest.raises(Exception):  # another domain's project is not found
        views.system_map(seeded, lens="DESIGN", domain="OFFICE", project_id=p.id)


def test_environment_filter_and_snapshot_render(seeded):
    assert views.system_map(seeded, lens="DESIGN", domain="PERSONAL", environment="staging")["nodes"] == []
    snap = snapshots.create_snapshot(seeded, label="v1", domain="PERSONAL", actor="t")
    seeded.commit()
    m = views.system_map(seeded, lens="DESIGN", domain="PERSONAL", snapshot_id=snap.id)
    assert m["mode"].startswith("SNAPSHOT") and m["snapshot"]["id"] == snap.id and m["nodes"] and m["edges"]


def test_snapshot_approval_is_a_bound_human_decision(seeded):
    owner = create_admin(seeded)
    a = snapshots.create_snapshot(seeded, label="a", domain="PERSONAL", actor="t")
    b = snapshots.create_snapshot(seeded, label="b", domain="PERSONAL", actor="t")
    seeded.commit()
    diff = snapshots.diff_snapshots(seeded, a.id, b.id, domain="PERSONAL")
    assert diff["both_approved"] is False and diff["approval_note"]
    for s in (a, b):
        meta = snapshots.approve_snapshot(seeded, s.id, domain="PERSONAL", actor_user_id=owner.id,
                                          rationale="owner approves this architecture")
        assert meta["approved"] is True
    seeded.commit()
    assert snapshots.diff_snapshots(seeded, a.id, b.id, domain="PERSONAL")["both_approved"] is True
    assert {m["id"] for m in snapshots.list_snapshots(seeded, domain="PERSONAL", approved_only=True)} == {a.id, b.id}
    c = snapshots.create_snapshot(seeded, label="c", domain="PERSONAL", actor="t")
    seeded.commit()
    assert c.id not in {m["id"] for m in snapshots.list_snapshots(seeded, domain="PERSONAL", approved_only=True)}


# ------------------------------------------------------------------ §38 API pagination / concurrency / rate limit


def test_drift_is_paginated(seeded, settings):
    reconcile(seeded, settings, runner=base_runner())
    seeded.commit()
    full = views.drift_list(seeded, domain="PERSONAL")
    assert full["total"] >= 2
    page = views.drift_list(seeded, domain="PERSONAL", page=2, page_size=1)
    assert len(page["items"]) == 1 and page["total"] == full["total"] and page["severity_policy"]["source"]


def test_concurrent_reconcile_of_the_same_scope_is_refused(seeded, settings):
    with reconcile_guard("git"):
        with pytest.raises(Conflict) as exc:
            reconcile(seeded, settings, component_id="git", runner=base_runner())
        assert exc.value.code == "RECONCILE_IN_PROGRESS"
        reconcile(seeded, settings, component_id="python-runtime", runner=base_runner())  # other scope runs


def test_full_refresh_is_rate_limited(personal_client, db):
    seed_run(db)
    db.commit()
    set_runner_override(base_runner())
    try:
        assert personal_client.post("/api/inventory/refresh", json={}).status_code == 200
        r = personal_client.post("/api/inventory/refresh", json={})
        assert r.status_code == 409 and r.json()["error"]["code"] == "REFRESH_TOO_SOON"
        assert r.json()["error"]["details"]["retry_after_seconds"] >= 1
        assert personal_client.post("/api/inventory/refresh", json={"component_id": "git"}).status_code == 200
    finally:
        set_runner_override(None)


def test_snapshots_list_is_paginated_via_api(personal_client, db):
    seed_run(db)
    db.commit()
    for label in ("a", "b", "c"):
        personal_client.post("/api/inventory/snapshots", json={"label": label})
    body = personal_client.get("/api/inventory/snapshots", params={"page_size": 2}).json()
    assert body["total"] == 3 and len(body["items"]) == 2 and body["page"] == 1


def test_relations_list_is_paginated_via_api(personal_client, db):
    seed_run(db)
    db.commit()
    full = personal_client.get("/api/inventory/components/opencode/relations").json()
    assert full["total"] >= 2 and full["total"] == full["total_outgoing"] + full["total_incoming"]
    assert len(full["items"]) == full["total"]
    pages = [personal_client.get("/api/inventory/components/opencode/relations",
                                 params={"page": n, "page_size": 1}).json() for n in range(1, full["total"] + 1)]
    assert all(len(p["items"]) == 1 and p["total"] == full["total"] for p in pages)
    assert [p["items"][0]["id"] for p in pages] == [r["id"] for r in full["items"]]
    assert personal_client.get("/api/inventory/components/opencode/relations",
                               params={"page_size": 501}).status_code == 422


# ------------------------------------------------------------------ §38 configuration with hash/revision and target


def test_reconcile_observes_configuration_hashes_with_targets(seeded, settings, tmp_path):
    from aeos.models.portfolio import ProjectIntegration
    from aeos.models.workforce import AgentDefinition

    p = _project(seeded)
    seeded.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="google-drive", purpose="docs",
                                  root_id="folder-1"))
    seeded.commit()
    reconcile(seeded, settings, runner=base_runner(), repo=tmp_path)
    seeded.commit()
    oc = views.component_detail(seeded, "opencode", domain="PERSONAL")
    conf = oc["lifecycle"]["configuration"]
    assert conf["value"] == "CONFIGURED" and conf["version"]
    assert oc["versions"]["config_revision"]["status"] == "OBSERVED"
    routing = views.component_detail(seeded, "model-routing", domain="PERSONAL")["lifecycle"]["configuration"]
    assert routing["value"] == "MISSING"
    drive = views.component_detail(seeded, "google-drive", domain="PERSONAL")["lifecycle"]["configuration"]
    assert drive["value"] == "CONFIGURED" and drive["version"]
    seeded.add(AgentDefinition(agent_key="impl", role_key="implementer", harness="opencode", enabled=True,
                               revision_hash="sha256:" + "1" * 64))
    seeded.commit()
    rev = views.component_detail(seeded, "opencode", domain="PERSONAL")["versions"]["config_revision"]
    assert rev["status"] == "RECORDED" and rev["value"][0]["agent_revision_hash"] == "sha256:" + "1" * 64


# ------------------------------------------------------------------ §37 scope / access / cost field groups


def test_scope_access_and_cost_groups_use_real_records(seeded, secrets_memory):
    from aeos.models.cost import Budget
    from aeos.models.portfolio import ProjectIntegration
    from aeos.models.routing import ModelRoute

    p = _project(seeded)
    seeded.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="google-drive", purpose="docs",
                                  root_id="folder-9"))
    seeded.add(ModelRoute(route_key="or-free", pool="WORKER", provider="openrouter", model_id="x:free",
                          status="APPROVED", eligible_domains=["PERSONAL"], free_until=utcnow() + timedelta(days=10)))
    seeded.add(Budget(scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=5, status="ACTIVE"))
    seeded.commit()
    drive = views.component_detail(seeded, "google-drive", domain="PERSONAL")
    assert drive["scope"]["authorization_scope"]["status"] == "EFFECTIVE"
    assert drive["scope"]["authorization_scope"]["integrations"][0]["root_id"] == "folder-9"
    orr = views.component_detail(seeded, "openrouter", domain="PERSONAL")
    assert orr["cost"]["free_expiry"]["status"] == "RECORDED"
    assert orr["cost"]["budget_source"]["items"][0]["scope_type"] == "GLOBAL"
    comp_id = next(c.component_id for c in seeded.scalars(select(ComponentReference))
                   if (c.access or {}).get("identity_ref") == "personal/github")
    exp = views.component_detail(seeded, comp_id, domain="PERSONAL")["access"]["expiry"]
    assert exp["status"] == "MISSING"
    secrets_memory.put("personal/github", "t" * 40, actor="test")
    exp = views.component_detail(seeded, comp_id, domain="PERSONAL")["access"]["expiry"]
    assert exp["status"] in ("RECORDED", "NO_EXPIRY_RECORDED") and exp["ref"] == "personal/github"
    na = views.component_detail(seeded, "git", domain="PERSONAL")["access"]["expiry"]
    assert na["status"] == "NOT_APPLICABLE"


# ------------------------------------------------------------------ §38 optional polling (off by default)


def test_polling_is_off_by_default_and_refreshes_only_expired_components(seeded, settings, monkeypatch):
    from aeos.inventory import loop

    runner = base_runner()
    set_runner_override(runner)
    try:
        assert loop.tick(settings) == []
        due = loop.due_components(seeded)
        assert 0 < len(due) <= loop.MAX_PER_TICK
        loop.set_poll_enabled(seeded, True, actor="local-operator", reason="test polling")
        seeded.commit()
        refreshed = loop.tick(settings)
        assert refreshed and len(refreshed) <= loop.MAX_PER_TICK
        seeded.expire_all()
        assert not set(refreshed) & set(loop.due_components(seeded)), "fresh components are not polled again"
    finally:
        set_runner_override(None)
