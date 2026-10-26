from __future__ import annotations

import sys
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows ACLs are checked by the Doctor")
def test_layout_is_private_to_the_os_user_and_tightens_an_open_data_dir(tmp_path: Path):
    from aeos.core.enums import Domain

    root = tmp_path / "data"
    (root / "personal").mkdir(parents=True)
    for p in (root, root / "personal"):
        p.chmod(0o755)  # left open by an older bootstrap under umask 022
    layout = DataLayout(root)
    layout.ensure()
    for p in (layout.root, layout.backups, layout.domain_root(Domain.PERSONAL), layout.domain_root(Domain.OFFICE)):
        assert p.stat().st_mode & 0o077 == 0, p


def test_layout_keeps_inherited_acls_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """On Windows, Python 3.13 turns mkdir mode 0o700 into a protected DACL; the layout must not request it."""
    import types

    import aeos.core.paths as paths_mod

    modes: list[int] = []
    real_mkdir = Path.mkdir

    def spy_mkdir(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        modes.append(mode)
        real_mkdir(self, parents=parents, exist_ok=exist_ok)

    def no_chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        raise AssertionError(f"chmod on Windows: {self}")

    monkeypatch.setattr(paths_mod, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(Path, "mkdir", spy_mkdir)
    monkeypatch.setattr(Path, "chmod", no_chmod)
    DataLayout(tmp_path / "data").ensure()
    assert modes and set(modes) == {0o777}


def test_content_hash_is_canonical():
    assert content_hash({"b": 1, "a": [1, 2]}) == content_hash({"a": [1, 2], "b": 1})
    assert content_hash({"a": 1}).startswith("sha256:")


def test_redaction_patterns_and_registry():
    assert "REDACTED" in redact_text("token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "REDACTED" in redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    register_secret("my-very-custom-secret-value")
    assert "my-very-custom" not in redact_text("leaked my-very-custom-secret-value here")
    assert redact({"password": "x" * 8, "nested": {"token": "abcdefgh"}})["nested"]["token"] == "[REDACTED]"


def test_sensitive_key_rule_spares_counters_and_flags():
    from aeos.core.redaction import contains_high_confidence_secret, is_sensitive_key

    for k in ("password", "csrf_token", "api_key", "apiKey", "client_secret", "clientSecret", "refreshToken",
              "Authorization", "secret_id", "github_pat", "capability_token"):
        assert is_sensitive_key(k), k
    for k in ("input_tokens", "max_output_tokens", "maxTokens", "token_hash", "tokens_used", "credential_ref"):
        assert not is_sensitive_key(k), k
    # booleans/ints under sensitive-looking keys are flags, not secrets
    assert redact({"office_authorization": True, "token_count": 3}) == {"office_authorization": True, "token_count": 3}
    assert not contains_high_confidence_secret("token: string")
    assert contains_high_confidence_secret("sk-or-v1-abcdefghijklmnopqrstuvwxyz0123")


def test_pii_minimisation_detects_identifiers_but_spares_versions_and_ids():
    from aeos.core.pii import contains_pii, redact_pii

    assert redact_pii("telp +62 812-3456-7890 / 0812-3456-7890") == "telp [PII:PHONE] / [PII:PHONE]"
    assert redact_pii("mail " + "budi" + "@example.com") == "mail [PII:EMAIL]"
    assert redact_pii("card 4111 1111 1111 1111") == "card [PII:CARD]"
    assert redact_pii("NIK 3174015505900001") == "NIK [PII:NIK]"
    for benign in ("version 0.13.1 on 2026-10-18 12:30:45", "run 1234567890123456", "pid 12345 tokens 123456789",
                   "sha256:8F21F1F126A586C0E77558DDFBA175CC", "ts 1760780000000000"):
        assert not contains_pii(benign), benign
    # opt-in through the redactor; default redaction leaves personal data untouched
    assert "[PII:EMAIL]" in redact({"note": "x " + "a" + "@b.co"}, pii=True)["note"]
    assert "@b.co" in redact({"note": "x " + "a" + "@b.co"})["note"]


def test_pii_minimisation_never_corrupts_uuids_or_digests():
    """§29: the audit record binding a write to its interlock check must keep operation/decision/
    publication ids intact. Regression: the digit tail of ~0.4% of uuid4 values became [PII:CARD]."""
    import uuid

    from aeos.audit.service import record
    from aeos.core.pii import redact_pii

    known = "289f8778-6cba-415a-8323-2390263811d2"
    assert redact_pii(known) == known
    assert redact_pii(f"operation {known} published") == f"operation {known} published"
    assert redact({"operation_id": known, "note": f"plan:{known}"}, pii=True) == {
        "operation_id": known, "note": f"plan:{known}"}
    all_digit_uuid = "12345678-1234-1234-1234-123456789012"
    assert redact_pii(all_digit_uuid) == all_digit_uuid
    for _ in range(10_000):
        u = str(uuid.uuid4())
        assert redact_pii(u) == u, u
        assert redact_pii(f"id={u};") == f"id={u};", u
    digest = "sha256:" + "4111111111111111" + "ab" * 24
    assert redact_pii(digest) == digest
    assert redact_pii("run-4111111111111111") == "run-4111111111111111"
    # real identifiers are still minimised, including under a key that ends in _id
    assert redact_pii("card 4111-1111-1111-1111.") == "card [PII:CARD]."
    assert redact_pii("card 4111111111111111") == "card [PII:CARD]"
    assert redact_pii("hp 0812-3456-7890") == "hp [PII:PHONE]"
    assert redact({"national_id": "3174015505900001"}, pii=True) == {"national_id": "[PII:NIK]"}

    class _Sess:
        def add(self, obj):
            self.obj = obj

        def flush(self):
            pass

    s = _Sess()
    ids = {f"k{i}": str(uuid.uuid4()) for i in range(200)}
    ev = record(s, actor_type="user", actor_id="u", action="publication.write", details={"ids": ids, **ids})
    assert ev.details["ids"] == ids and all(ev.details[k] == v for k, v in ids.items())
