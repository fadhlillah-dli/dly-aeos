"""§12 manual fallback package roundtrip: written for official Claude Code, never the API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeos.claude_bridge.facade import ClaudeRequest, read_manual_result, write_manual_package
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from tests.unit.test_claude_bridge_parsing import FABLE_FALLBACK, OPUS_OK, SCHEMA


def _req(tmp_path: Path, **kw) -> ClaudeRequest:
    ctx = tmp_path / "brief.md"
    ctx.write_text("ringkasan proyek", encoding="utf-8")
    base = {"purpose": "planning.primary", "role": "Primary Planner", "model": "opus",
            "prompt": "Buat rencana. Jawab sesuai schema.", "json_schema": SCHEMA, "project_id": "p1",
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
    assert res and not res.ok and res.error_code == "MODEL_POLICY_MISMATCH"


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
