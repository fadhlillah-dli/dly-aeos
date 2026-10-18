"""Local runtime boundary (§06, §26). §50 rows: "Port lokal konflik" (explained, no public bind),
"File path traversal/symlink keluar sandbox" (managed data paths), config never holds secrets,
"Trace berisi secret/PII" (redaction before persistence in audit/events/evidence)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeos.audit import service as audit
from aeos.core import config as config_mod
from aeos.core.paths import DataLayout, UnsafePathError, ensure_within, safe_segment
from aeos.events import service as events
from aeos.verification import facade as verification
from tests.acceptance.support import fake_token

pytestmark = pytest.mark.acceptance


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"],
                         ids=["any-ipv4", "lan-ip", "any-ipv6", "hostname"])
def test_only_loopback_binding(host, monkeypatch):
    monkeypatch.setenv("AEOS_HOST", host)
    with pytest.raises(ValueError):
        config_mod.Settings()


def test_default_bind_is_loopback(settings):
    assert settings.host in config_mod.LOOPBACK_HOSTS
    assert settings.base_url.startswith("http://127.0.0.1:") or settings.host != "127.0.0.1"


def test_config_file_with_secrets_rejected(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "aeos.yaml"
    cfg.write_text("port: 8788\ngithub_token: abc\n", encoding="utf-8")
    monkeypatch.setenv("AEOS_CONFIG", str(cfg))
    with pytest.raises(ValueError):
        config_mod.Settings()


def test_port_conflict_is_explained_not_rebound(settings):
    from aeos.cli import app

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        result = CliRunner().invoke(app, ["serve", "--port", str(port)])
    assert result.exit_code == 2
    assert "already in use" in result.output


@pytest.mark.parametrize("segment", ["..", "../x", "a/b", "a\\b", "", "."],
                         ids=["dotdot", "dotdot-slash", "slash", "backslash", "empty", "dot"])
def test_path_traversal_segments_rejected(segment):
    with pytest.raises(UnsafePathError):
        safe_segment(segment)


def test_managed_paths_cannot_escape(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(UnsafePathError):
        ensure_within(root, root / ".." / "outside")
    layout = DataLayout(root)
    with pytest.raises(UnsafePathError):
        layout.project_root(__import__("aeos.core.enums", fromlist=["Domain"]).Domain.PERSONAL, "../escape")


def test_secrets_redacted_before_persistence(db, settings):
    token = fake_token()
    ev_a = audit.record(db, actor_type="system", actor_id="acc", action="acc.secret",
                        reason=f"failed with {token}", details={"token": token, "note": f"Bearer {token}"})
    ev_e = events.emit(db, source="acc", kind="acc.secret", payload={"msg": f"key={token}"})
    ev_v = verification.collect_evidence(db, kind="LOG", origin="collector", collector_identity="acc",
                                         claim=f"trace {token}", content=f"trace line {token}")
    db.commit()
    assert token not in ev_a.reason and token not in str(ev_a.details)
    assert token not in str(ev_e.payload)
    assert token not in ev_v.claim
    stored = (settings.layout.root / ev_v.location).read_text(encoding="utf-8")
    assert token not in stored
