"""Evidence collector (§34). §50 rows: "Worker exit 0 / menulis 'all tests pass'" (worker evidence is
an untrusted E0 claim), "Trace berisi secret/PII" (redaction before persistence), and the AGENTS.md
immutability invariant (EvidenceArtifact append-only)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from aeos.core.errors import PolicyDenied, ValidationFailed
from aeos.models.portfolio import Project
from aeos.verification import collector, facade, suite
from tests.unit.test_verification_support import fake_secret, make_controller


def _project(db, domain: str = "PERSONAL", key: str = "demo") -> Project:
    p = Project(project_key=key, name=key, domain=domain)
    db.add(p)
    db.commit()
    return p


def test_trust_is_derived_from_origin_and_cannot_be_forced(db):
    assert "trusted" not in inspect.signature(facade.collect_evidence).parameters
    with pytest.raises(TypeError):
        facade.collect_evidence(db, kind="NOTE", origin="worker", collector_identity="w", claim="x",
                                trusted=True)  # type: ignore[call-arg]
    for origin in ("worker", "human", "verifier-worker", "Verifier2", ""):
        if not origin:
            with pytest.raises(ValidationFailed):
                facade.collect_evidence(db, kind="NOTE", origin=origin, collector_identity="w", claim="x")
            continue
        ev = facade.collect_evidence(db, kind="TEST_REPORT", origin=origin, collector_identity="w",
                                     claim="all tests pass", content="ok", evidence_level="E4")
        assert ev.trusted is False
        assert ev.evidence_level == "E0", origin
    ev = facade.collect_evidence(db, kind="TEST_REPORT", origin=" Verifier ", collector_identity="v", claim="c",
                                 content={"passed": 1}, evidence_level="E2")
    assert ev.trusted is True and ev.origin == "verifier" and ev.evidence_level == "E2"


def test_level_ceilings_per_kind(db):
    cases = [
        ("DIFF", "verifier", "E3", "E1"),
        ("LINT_REPORT", "verifier", "E2", "E1"),
        ("PROVIDER_CAPABILITY", "collector", "E3", "E1"),  # provider docs/config support E1 only (§34)
        ("TEST_REPORT", "verifier", "E3", "E2"),
        ("CI_STATUS", "provider-ci", "E3", "E3"),
        ("PROBE", "doctor", "E4", "E3"),
        ("TEST_REPORT", "verifier", "E1", "E1"),  # requested level is a ceiling, never an upgrade
        ("TEST_REPORT", "verifier", "E4", "E2"),  # E4 needs a protected-suite reference
    ]
    for kind, origin, requested, expected in cases:
        ev = collector.collect_evidence(db, kind=kind, origin=origin, collector_identity="x", claim=kind,
                                        content="c", evidence_level=requested)
        assert ev.evidence_level == expected, (kind, origin, requested)
        assert ev.inline["level"]["requested"] == requested


def test_e4_only_with_current_protected_suite_reference(db, tmp_path: Path, monkeypatch):
    root = make_controller(tmp_path / "ctl")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    current = suite.trusted_suite_hash(root)
    good = {"protected_suite": {"suite_hash": current, "test_ids": ["tests.acceptance.test_calc::test_add"]}}
    ev = collector.collect_evidence(db, kind="TEST_REPORT", origin="verifier", collector_identity="v", claim="c",
                                    content="ok", evidence_level="E4", environment=good)
    assert ev.evidence_level == "E4"
    stale = {"protected_suite": {"suite_hash": "sha256:old", "test_ids": ["x"]}}
    ev = collector.collect_evidence(db, kind="TEST_REPORT", origin="verifier", collector_identity="v", claim="c",
                                    content="ok", evidence_level="E4", environment=stale)
    assert ev.evidence_level == "E2"
    ev = collector.collect_evidence(db, kind="TEST_REPORT", origin="collector", collector_identity="v", claim="c",
                                    content="ok", evidence_level="E4", environment=good)
    assert ev.evidence_level == "E2", "E4 is reserved for the verifier"


def test_content_is_redacted_hashed_and_stored_under_project_evidence(db, settings):
    p = _project(db)
    token = fake_secret()
    ev = collector.collect_evidence(db, kind="LOG", origin="collector", collector_identity="c",
                                    claim=f"log with {token}", content=f"line one\nAuthorization: Bearer {token}\n",
                                    project_id=p.id, evidence_level="E3", environment={"api_key": token})
    db.commit()
    assert token not in ev.claim
    assert ev.environment["api_key"] == "[REDACTED]"
    assert ev.location and ev.location.startswith("personal/projects/demo/evidence/")
    path = settings.layout.root / ev.location
    data = path.read_bytes()
    assert token.encode() not in data and b"[REDACTED]" in data
    raw, integrity = collector.read_content(ev)
    assert integrity == "VERIFIED" and raw == data
    assert ev.size_bytes == len(data)
    assert ev.domain == "PERSONAL"


def test_dict_content_inline_and_large_summary(db):
    small = collector.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="s",
                                       content={"status": "PASS", "n": 1})
    assert small.inline["content"] == {"status": "PASS", "n": 1}
    assert small.content_type == "application/json"
    big = collector.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="b",
                                     content={"status": "FAIL", "blob": "x" * 10000, "items": list(range(10))})
    assert big.inline["truncated"] is True
    assert big.inline["summary"]["status"] == "FAIL"
    assert "items" not in big.inline["summary"]
    assert collector.evidence_summary(big)["status"] == "FAIL"
    assert json.loads(collector.read_content(big)[0])["blob"] == "x" * 10000


def test_platform_and_domain_evidence_dirs(db, settings):
    ev = collector.collect_evidence(db, kind="PROBE", origin="doctor", collector_identity="d", claim="c", content="x")
    assert ev.location.startswith("platform/evidence/") and ev.domain is None
    ev = collector.collect_evidence(db, kind="PROBE", origin="doctor", collector_identity="d", claim="c", content="x",
                                    domain="OFFICE")
    assert ev.location.startswith("office/evidence/")


def test_domain_cannot_be_relabelled(db):
    p = _project(db, domain="OFFICE", key="office-app")
    with pytest.raises(ValidationFailed):
        collector.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="c",
                                   project_id=p.id, domain="PERSONAL")
    ev = collector.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="c",
                                    project_id=p.id)
    assert ev.domain == "OFFICE"


def test_binary_content_with_secret_is_refused(db):
    token = fake_secret()
    with pytest.raises(PolicyDenied):
        collector.collect_evidence(db, kind="SCREENSHOT", origin="collector", collector_identity="c", claim="png",
                                   content=b"\x89PNG\xff\xfe" + token.encode())
    ev = collector.collect_evidence(db, kind="SCREENSHOT", origin="collector", collector_identity="c", claim="png",
                                    content=b"\x89PNG\xff\xfe\x00\x01", evidence_level="E3")
    assert ev.content_type == "application/octet-stream" and ev.inline["binary"] is True


def test_invalid_inputs_rejected(db):
    for kwargs in ({"kind": "bad kind"}, {"evidence_level": "E9"}, {"classification": "SECRET"}, {"mode": "FAKE"},
                   {"domain": "HOME"}, {"collector_identity": ""}):
        args = {"kind": "NOTE", "origin": "collector", "collector_identity": "c", "claim": "x", **kwargs}
        with pytest.raises(ValidationFailed):
            collector.collect_evidence(db, **args)
    with pytest.raises(ValidationFailed):
        collector.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="x",
                                   content=[1, 2])  # type: ignore[arg-type]


def test_evidence_is_append_only(db):
    ev = collector.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="x")
    db.commit()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE evidence_artifacts SET trusted=1, evidence_level='E4' WHERE id=:i"), {"i": ev.id})
        db.commit()
    db.rollback()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("DELETE FROM evidence_artifacts WHERE id=:i"), {"i": ev.id})
        db.commit()
    db.rollback()


def test_tampered_file_is_detected(db, settings):
    ev = collector.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="x",
                                    content="original")
    db.commit()
    (settings.layout.root / ev.location).write_text("tampered", encoding="utf-8")
    assert collector.read_content(ev)[1] == "MISMATCH"
    assert collector.read_json(ev) is None


def test_collection_emits_dashboard_event(db):
    from aeos.events import service as events

    ev = collector.collect_evidence(db, kind="NOTE", origin="worker", collector_identity="w", claim="claim")
    db.commit()
    views = [v for v in events.replay(db) if v.kind == "evidence.collected" and v.entity_id == ev.id]
    assert views and views[0].payload["trusted"] is False and views[0].source == "untrusted-input"


def test_retention_policy_comes_from_the_catalog(db):
    """§13 EvidenceArtifact retention policy: chosen from a catalog (default/legal-hold/short), stored on the
    append-only row; an unknown policy is refused, never silently mapped."""
    held = facade.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="hold",
                                   content={"x": 1}, retention_policy="legal-hold")
    plain = facade.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="plain")
    db.commit()
    assert held.retention_policy == "legal-hold" and plain.retention_policy == "default"
    assert set(collector.RETENTION_POLICIES) == {"default", "legal-hold", "short"}
    with pytest.raises(ValidationFailed) as e:
        facade.collect_evidence(db, kind="NOTE", origin="collector", collector_identity="c", claim="x",
                                retention_policy="delete-after-7d")
    assert e.value.code == "EVIDENCE_RETENTION_INVALID"
    with pytest.raises((IntegrityError, OperationalError)):  # still append-only: no retention job can rewrite it
        db.execute(text("UPDATE evidence_artifacts SET retention_policy='short' WHERE id=:i"), {"i": held.id})
        db.commit()
    db.rollback()
