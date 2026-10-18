from __future__ import annotations

from pathlib import Path

import pytest

from aeos.core.config import Settings
from aeos.core.hashing import content_hash
from aeos.core.paths import DataLayout, UnsafePathError, ensure_within, safe_segment
from aeos.core.redaction import redact, redact_text, register_secret


def test_loopback_only_binding():
    with pytest.raises(ValueError):
        Settings(host="0.0.0.0")
    assert Settings(host="127.0.0.1").host == "127.0.0.1"


def test_config_file_rejects_secrets(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "aeos.yaml"
    cfg.write_text("openrouter_api_key: sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxx\n", encoding="utf-8")
    monkeypatch.setenv("AEOS_CONFIG", str(cfg))
    with pytest.raises(ValueError, match="secrets are not allowed"):
        Settings()


def test_safe_segment_blocks_traversal():
    for bad in ("..", "../x", "a/b", "a\\b", "", ".hidden", "x" * 200):
        with pytest.raises(UnsafePathError):
            safe_segment(bad)
    assert safe_segment("dly-aeos") == "dly-aeos"


def test_ensure_within(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("x")
    assert ensure_within(root, root / "ok.txt")
    with pytest.raises(UnsafePathError):
        ensure_within(root, root / ".." / "escape.txt")


def test_domain_partitions_are_separate(tmp_path: Path):
    from aeos.core.enums import Domain

    layout = DataLayout(tmp_path)
    assert layout.project_root(Domain.PERSONAL, "p") != layout.project_root(Domain.OFFICE, "p")
    assert "personal" in str(layout.project_root(Domain.PERSONAL, "p"))


def test_content_hash_is_canonical():
    assert content_hash({"b": 1, "a": [1, 2]}) == content_hash({"a": [1, 2], "b": 1})
    assert content_hash({"a": 1}).startswith("sha256:")


def test_redaction_patterns_and_registry():
    assert "REDACTED" in redact_text("token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "REDACTED" in redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    register_secret("my-very-custom-secret-value")
    assert "my-very-custom" not in redact_text("leaked my-very-custom-secret-value here")
    assert redact({"password": "x" * 8, "nested": {"token": "abcdefgh"}})["nested"]["token"] == "[REDACTED]"
