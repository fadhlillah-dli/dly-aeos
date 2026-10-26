"""§12 manual fallback package roundtrip: written for official Claude Code, never the API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeos.claude_bridge.facade import ClaudeRequest, read_manual_result, write_manual_package
from aeos.core.enums import Domain
from aeos.core.errors import Conflict, NotFound, PolicyDenied, ValidationFailed
from tests.unit.test_claude_bridge_parsing import FABLE_FALLBACK, OPUS_OK, SCHEMA
from tests.unit.test_claude_bridge_run import make_project
from tests.unit.test_routing_fakes import install_fakes


def _req(tmp_path: Path, **kw) -> ClaudeRequest:
    ctx = tmp_path / "brief.md"
    ctx.write_text("ringkasan proyek", encoding="utf-8")
    base = {"purpose": "planning.primary", "role": "Primary Planner", "model": "opus",
            "prompt": "Buat rencana. Jawab sesuai schema.", "json_schema": SCHEMA,
            "domain": "PERSONAL", "context_files": [ctx]}
    return ClaudeRequest(**{**base, **kw})


def test_package_layout_and_instructions(settings, tmp_path):
    pkg = write_manual_package(_req(tmp_path), package_id="pkg-1")
    assert pkg.parent == settings.layout.manual_packages
    for name in ("PROMPT.md", "schema.json", "INSTRUCTIONS.md", "package.json"):
        assert (pkg / name).is_file(), name
    assert (pkg / "context" / "brief.md").is_file()
    assert json.loads((pkg / "schema.json").read_text(encoding="utf-8")) == SCHEMA
    text = (pkg / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert "--bare" in text and "Jangan pernah memakai `--bare`" in text  # only as a prohibition
    assert "claude -p --output-format json --json-schema" in text
    assert "Get-Content -Raw -Encoding UTF8 PROMPT.md | claude -p" in text  # Windows PowerShell
    assert '"$(cat schema.json)"' in text  # macOS/Linux
    assert "--restricted" in text and "--permission-prompts none" in text and "result.json" in text
    assert "ANTHROPIC_API_KEY" in text  # tells the owner to unset it
    assert read_manual_result("pkg-1") is None  # no result yet
    with pytest.raises(Conflict):
        write_manual_package(_req(tmp_path), package_id="pkg-1")


def test_raw_claude_json_result_roundtrip(settings, tmp_path):
    pkg = write_manual_package(_req(tmp_path), package_id="pkg-raw")
    (pkg / "result.json").write_text(json.dumps(OPUS_OK), encoding="utf-8-sig")  # PowerShell Out-File BOM
    res = read_manual_result("pkg-raw")
    assert res and res.ok and res.mode == "MANUAL_FALLBACK" and res.model_resolved == "claude-opus-5-5"


def test_raw_result_still_subject_to_model_policy(settings, tmp_path):
    pkg = write_manual_package(_req(tmp_path, model="fable"), package_id="pkg-fable")
    (pkg / "result.json").write_text(json.dumps(FABLE_FALLBACK), encoding="utf-16")  # PS 5.1 '>' redirection
    res = read_manual_result("pkg-fable")
    assert res and not res.ok and res.error_code == "MODEL_POLICY_MISMATCH" and res.structured is None


def test_bare_object_accepted_with_unverified_identity(settings, tmp_path):
    pkg = write_manual_package(_req(tmp_path), package_id="pkg-bare")
    (pkg / "result.json").write_text('{"ok": true}', encoding="utf-8")
    res = read_manual_result("pkg-bare")
    assert res and res.ok and res.model_resolved == "UNKNOWN" and res.usage["model_identity"] == "UNVERIFIED"
    (pkg / "result.json").write_text('{"ok": "yes"}', encoding="utf-8")
    bad = read_manual_result("pkg-bare")
    assert bad and not bad.ok and bad.error_code == "SCHEMA_INVALID"


def test_tampered_package_rejected(settings, tmp_path):
    pkg = write_manual_package(_req(tmp_path), package_id="pkg-t")
    (pkg / "PROMPT.md").write_text("prompt lain", encoding="utf-8")
    (pkg / "result.json").write_text('{"ok": true}', encoding="utf-8")
    res = read_manual_result("pkg-t")
    assert res and not res.ok and res.error_code == "PACKAGE_TAMPERED"


def test_package_refuses_secrets_and_bad_ids(settings, tmp_path):
    with pytest.raises(ValidationFailed):
        write_manual_package(_req(tmp_path, prompt="token ghp_" + "d" * 36), package_id="pkg-s")
    with pytest.raises(ValidationFailed):
        write_manual_package(_req(tmp_path), package_id="../escape")
    with pytest.raises(NotFound):
        read_manual_result("does-not-exist")


def test_package_for_registered_project_records_it(settings, db, tmp_path):
    pid = make_project(db, "manual-p1", "PERSONAL")
    pkg = write_manual_package(_req(tmp_path, project_id=pid), package_id="pkg-proj")
    assert json.loads((pkg / "package.json").read_text(encoding="utf-8"))["project_id"] == pid


def test_manual_package_obeys_the_same_preflight_as_the_bridge(settings, db, tmp_path, monkeypatch):
    """§23: the Office policy holds from the first planning/review call - a manual package would have the
    owner paste Office data into Claude Max just the same. Domain-first, read-only tools and the domain
    partition boundary apply too; a refused package leaves nothing on disk."""
    reg = install_fakes(monkeypatch)
    office = make_project(db, "manual-office", "OFFICE")
    root = settings.layout.manual_packages
    with pytest.raises(PolicyDenied) as ei:  # company authorisation not recorded
        write_manual_package(_req(tmp_path, project_id=office, domain="OFFICE"), package_id="pkg-off")
    assert ei.value.code == "OFFICE_POLICY_DENIED"
    with pytest.raises(PolicyDenied) as ei:  # OFFICE project relabelled PERSONAL
        write_manual_package(_req(tmp_path, project_id=office), package_id="pkg-mis")
    assert ei.value.code == "DOMAIN_MISMATCH"
    with pytest.raises(ValidationFailed) as ei:
        write_manual_package(_req(tmp_path, domain=None), package_id="pkg-nodomain")
    assert ei.value.code == "DOMAIN_REQUIRED"
    with pytest.raises(ValidationFailed) as ei:
        write_manual_package(_req(tmp_path, allowed_tools=["Read", "Bash"]), package_id="pkg-tools")
    assert ei.value.code == "TOOL_NOT_ALLOWED"
    with pytest.raises(NotFound):
        write_manual_package(_req(tmp_path, project_id="never-registered"), package_id="pkg-unknown")
    office_file = settings.layout.domain_root(Domain.OFFICE) / "projects" / "acme" / "attachments" / "x.md"
    office_file.parent.mkdir(parents=True, exist_ok=True)
    office_file.write_text("dokumen kantor", encoding="utf-8")
    with pytest.raises(PolicyDenied) as ei:  # passes preflight, refused while copying context
        write_manual_package(_req(tmp_path, context_files=[office_file]), package_id="pkg-ctx")
    assert ei.value.code == "CONTEXT_DOMAIN_DENIED"
    assert not root.exists() or not any(root.iterdir())  # nothing half-written remains
    # recorded company authorisation: Claude Max for OFFICE data, for the project's (INTERNAL) data class
    reg.office.update(claude_max_allowed_for_office_data=True, office_authorization=True,
                      approved_data_classes=["INTERNAL"])
    assert write_manual_package(_req(tmp_path, project_id=office, domain="OFFICE"), package_id="pkg-off").is_dir()
