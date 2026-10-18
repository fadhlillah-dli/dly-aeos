"""InstallManifest + seed (§07, §37, §45, §50 transparency rows).

§50 rows covered:
* "Katalog blueprint berisi banyak tool tetapi host belum diprobe" -> PLANNED/CANDIDATE/UNKNOWN, never
  INSTALLED/ENABLED/IN_USE.
* "Nama repo ambigu atau sumber tidak terverifikasi" -> UNVERIFIED, no guessed URL/license/version,
  no install action.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from aeos.core.errors import ValidationFailed
from aeos.core.paths import repo_root
from aeos.inventory import manifest as mf
from aeos.inventory.seed import run as seed_run
from aeos.models.inventory import (
    ComponentObservation,
    ComponentReference,
    ComponentRelation,
    InstallManifestRecord,
)

# Every §45 row, split into one component per tool, plus the runtime pieces this repo uses.
SECTION_45_IDS = {
    "dly-aeos-platform-repo", "dly-aeos-control-plane", "vscode", "claude-code", "git", "github", "github-issues",
    "github-actions", "bitbucket", "bitbucket-pipelines", "jira", "confluence", "google-drive", "google-docs",
    "openbao", "os-credential-store", "sops-age", "infisical", "opencode", "goose", "pi", "hermes", "openhands",
    "deep-agents", "litellm", "openrouter", "opencode-console", "gemini-api", "deepseek-api", "9router",
    "toolhive", "docker-mcp-gateway", "serena", "context7", "codebase-memory-mcp", "rtk", "ccusage", "langfuse",
    "opentelemetry", "promptfoo", "temporal", "langgraph", "copier", "spec-kit", "openspec", "bmad-method", "gsd",
    "agent-orchestrator", "paperclip", "ddorchestrator", "claude-native-subagents", "agency-agents", "gstack",
    "superpowers", "metagpt", "chatdev", "crewai", "openai-symphony", "ralph", "gas-town", "ruflo",
    "research-technology-registry", "readiness-doctor", "install-manifest", "trusted-self-build-seed",
    "security-test-tools", "pytest", "ruff", "secret-scan",
}
RUNTIME_IDS = {
    "control-center-frontend", "sqlite-metadata-store", "python-runtime", "nodejs-runtime", "docker",
    "mermaid-cli", "keyring-backend",
}
REQUIRED_NAMES = {
    "OpenCode", "Hermes Agent", "Paperclip", "9Router", "ToolHive", "Serena", "Context7", "RTK", "ccusage",
    "Spec Kit", "OpenSpec", "BMAD-METHOD", "MetaGPT", "ChatDev", "CrewAI", "Gas Town", "Ruflo", "Temporal",
    "LangGraph", "LiteLLM", "OpenRouter", "Infisical", "DDorchestrator", "codebase-memory-mcp",
}


@pytest.fixture(scope="module")
def manifest() -> mf.ManifestLoad:
    return mf.load_manifest()


def _components(m: mf.ManifestLoad) -> dict[str, dict]:
    return {c["component_id"]: c for c in m.data["components"]}


def test_manifest_validates_and_contains_every_section_45_row(manifest):
    comps = _components(manifest)
    missing = (SECTION_45_IDS | RUNTIME_IDS) - set(comps)
    assert not missing, f"missing §45/runtime components: {sorted(missing)}"
    names = {c["name"] for c in comps.values()}
    assert names >= REQUIRED_NAMES
    assert manifest.content_hash.startswith("sha256:")
    assert manifest.data["status"] == "DESIGN"


def test_unverified_components_have_null_url_and_no_install(manifest):
    comps = _components(manifest)
    for cid in ("ddorchestrator", "codebase-memory-mcp"):
        assert comps[cid]["identity_verified"] == "UNVERIFIED"
        assert comps[cid]["design_label"] == "UNVERIFIED"
        assert comps[cid]["source_url"] is None
    for c in comps.values():
        if c["identity_verified"] == "UNVERIFIED":
            assert "install" not in (c["operations"] or {}), c["component_id"]
        if c["source_url"] is None:
            assert c["identity_verified"] == "UNVERIFIED", c["component_id"]
        if c["requirement"] in ("CANDIDATE", "REFERENCE_ONLY"):
            assert "install" not in (c["operations"] or {}), c["component_id"]


def test_every_source_url_appears_verbatim_in_master_and_no_guessed_license(manifest):
    master = (repo_root() / mf.MASTER_REL).read_text(encoding="utf-8")
    for c in manifest.data["components"]:
        if c["source_url"]:
            assert c["source_url"] in master, c["component_id"]
        assert c["license"] is None, c["component_id"]  # the master never states a license
        assert c["approved_pin"] is None or "latest" not in c["approved_pin"].lower()
        for sid in c["source_refs"]:
            assert f"| {sid} |" in master


def test_categories_domains_and_bootstrap_orders(manifest):
    comps = _components(manifest)
    assert manifest.data["categories"] == list(mf.CATEGORIES)
    assert {c["category"] for c in comps.values()} <= set(mf.CATEGORIES)
    for cid in ("bitbucket", "jira", "confluence", "bitbucket-pipelines"):
        assert comps[cid]["domains"] == ["OFFICE"]
    for cid in ("github", "github-issues", "github-actions", "google-drive", "google-docs"):
        assert comps[cid]["domains"] == ["PERSONAL"]
    assert comps["claude-code"]["bootstrap_order"] == 2
    assert comps["openbao"]["bootstrap_order"] == 3
    assert comps["opencode"]["bootstrap_order"] == 5
    assert comps["opencode"]["friendly_name"].startswith("OpenCode")
    assert "pengawas" in comps["claude-code"]["friendly_name"]


def test_health_probes_are_loopback_settings_only(manifest):
    for c in manifest.data["components"]:
        health = (c.get("probe") or {}).get("health")
        if health:
            assert health["settings_url"] == "openbao_addr"


def _validate(data):
    master = (repo_root() / mf.MASTER_REL).read_text(encoding="utf-8")
    errors, _ = mf.validate_manifest(data, master_text=master)
    return errors


def test_validation_rejects_guessed_url_latest_pin_install_and_unknown_target(manifest):
    data = copy.deepcopy(manifest.data)
    comps = {c["component_id"]: c for c in data["components"]}
    comps["ddorchestrator"]["source_url"] = "https://github.com/example/ddorchestrator"
    comps["git"]["approved_pin"] = "latest"
    comps["infisical"]["operations"]["install"] = {"kind": "manual", "note": "x"}
    comps["goose"]["relations"].append({"target": "does-not-exist", "relation": "calls", "data_description": "x"})
    comps["serena"]["license"] = "MIT"
    errors = "\n".join(_validate(data))
    assert "ddorchestrator" in errors or "components/" in errors
    assert "does not appear in the master" in errors
    assert "approved_pin" in errors or "latest" in errors
    assert "install" in errors
    assert "does-not-exist" in errors
    assert "license" in errors


def test_validation_rejects_bad_relation_type_and_duplicate_ids(manifest):
    data = copy.deepcopy(manifest.data)
    data["components"][0]["relations"].append({"target": "git", "relation": "owns", "data_description": "x"})
    data["components"].append(copy.deepcopy(data["components"][1]))
    errors = "\n".join(_validate(data))
    assert "duplicate component_id" in errors
    assert "owns" in errors


def test_seed_is_idempotent_and_never_sets_runtime_state(db):
    """§50: blueprint catalog with many tools, host not probed -> never INSTALLED/ENABLED/IN_USE."""
    first = seed_run(db)
    db.commit()
    second = seed_run(db)
    db.commit()
    assert first["created"] >= 100 and first["stored_new_record"] is True
    assert second["created"] == 0 and second["stored_new_record"] is False
    assert second["manifest_hash"] == first["manifest_hash"]
    assert db.scalar(select(func.count()).select_from(InstallManifestRecord)) == 1
    rows = list(db.scalars(select(ComponentReference)))
    assert all(r.mode == "DESIGN" for r in rows)
    assert all(r.enablement == "DISABLED" for r in rows)
    assert all(r.admission_status == "DISCOVERED" for r in rows)
    assert db.scalar(select(func.count()).select_from(ComponentObservation)) == 0
    from aeos.inventory.views import list_components

    data = list_components(db, domain="PERSONAL", page_size=200)
    for it in data["items"]:
        dims = it["dimensions"]
        assert dims["installation"]["value"] not in ("INSTALLED",)
        assert dims["enablement"]["value"] == "DISABLED"
        assert dims["usage"]["value"] == "NEVER_OBSERVED"
        assert dims["connectivity"]["value"] == "NOT_CHECKED"
    assert data["mode"] == "DESIGN"


def test_reseed_preserves_decision_side_state(db):
    seed_run(db)
    row = db.get(ComponentReference, "claude-code")
    row.enablement = "ENABLED"
    row.admission_status = "APPROVED"
    row.decision_refs = ["decision-1"]
    db.commit()
    seed_run(db)
    db.commit()
    row = db.get(ComponentReference, "claude-code")
    assert (row.enablement, row.admission_status, row.decision_refs) == ("ENABLED", "APPROVED", ["decision-1"])


def test_removed_component_is_tombstoned_not_deleted(db, tmp_path: Path):
    seed_run(db)
    db.commit()
    data = yaml.safe_load((repo_root() / mf.MANIFEST_REL).read_text(encoding="utf-8"))
    data["components"] = [c for c in data["components"] if c["component_id"] != "ralph"]
    root = tmp_path / "repo"
    (root / "install-manifest").mkdir(parents=True)
    (root / "docs" / "architecture").mkdir(parents=True)
    (root / mf.MANIFEST_REL).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (root / mf.SCHEMA_REL).write_text((repo_root() / mf.SCHEMA_REL).read_text(encoding="utf-8"), encoding="utf-8")
    (root / mf.MASTER_REL).write_text((repo_root() / mf.MASTER_REL).read_text(encoding="utf-8"), encoding="utf-8")
    result = mf.apply_manifest(db, actor="test", root=root)
    db.commit()
    assert result["tombstoned"] == 1 and result["stored_new_record"] is True
    ralph = db.get(ComponentReference, "ralph")
    assert ralph is not None and ralph.tombstoned_at is not None  # history kept
    rel = db.scalar(select(ComponentRelation).where(ComponentRelation.source_id == "ralph"))
    assert rel is not None and rel.tombstoned_at is not None
    assert db.scalar(select(func.count()).select_from(InstallManifestRecord)) == 2


def test_missing_or_invalid_manifest_fails_closed(tmp_path: Path):
    with pytest.raises(ValidationFailed) as ei:
        mf.load_manifest(tmp_path / "nope.yaml", root=tmp_path)
    assert ei.value.code == "MANIFEST_MISSING"
    bad = tmp_path / "install-manifest"
    bad.mkdir()
    (bad / "manifest.yaml").write_text("components: [", encoding="utf-8")
    with pytest.raises(ValidationFailed) as ei:
        mf.load_manifest(bad / "manifest.yaml", root=tmp_path)
    assert ei.value.code == "MANIFEST_INVALID"


def test_schema_file_is_valid_json_schema():
    from jsonschema import Draft202012Validator

    schema = json.loads((repo_root() / mf.SCHEMA_REL).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
