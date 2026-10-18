"""`aeos seed` policy step: ADR register (idempotent), ADR-0003 owner exception as a recorded
HUMAN_RECORDED decision, initial PolicySnapshot (§02, §09, §41)."""

from __future__ import annotations

import shutil

from sqlalchemy import select

from aeos.models.decisions import ArchitectureDecisionRecord, Decision, PolicySnapshot
from aeos.policy import adrs, seed
from aeos.policy import decisions as registry
from tests.conftest import create_admin


def test_parse_adr_sections_and_meta():
    text = ("# ADR-0042 — Example title\n\n* Status: ACCEPTED\n* Date: 2026-10-18\n* Deciders: owner\n"
            "* Source: `x`\n  (continued)\n\n## Context\n\nctx line\n\n## Decision\n\n1. do it\n\n## Consequences\n\nok\n")
    p = adrs.parse_adr(text, filename="ADR-0042-example.md")
    assert p["key"] == "ADR-0042" and p["title"] == "Example title" and p["status"] == "ACCEPTED"
    assert p["meta"]["date"] == "2026-10-18" and p["meta"]["source"] == "`x` (continued)"
    assert p["context"] == "ctx line" and p["decision"] == "1. do it" and p["consequences"] == "ok"
    assert set(p["sections"]) == {"Context", "Decision", "Consequences"} and p["warnings"] == []


def test_seed_is_idempotent_and_records_adr0003_exception(db):
    first = seed.run(db)
    db.commit()
    keys = [r.adr_key for r in db.scalars(select(ArchitectureDecisionRecord).order_by(ArchitectureDecisionRecord.adr_key))]
    assert {"ADR-0001", "ADR-0002", "ADR-0003"} <= set(keys)
    assert first["adrs"]["created"] == len(keys) and first["adr_0003_exception"] == "created"
    assert first["mci"] == "UNVERIFIED"
    d = db.scalar(select(Decision).where(Decision.actor_user_id == "owner:ADR-0003"))
    adr3 = db.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == "ADR-0003"))
    assert d.kind == "EXCEPTION" and d.actor_kind == "HUMAN_RECORDED" and d.status == "ACTIVE"
    assert d.target_type == "repository" and d.target_id == "fadhlillah-dli/dly-aeos"
    assert d.allowed_operations == ["commit", "push", "open_pr", "merge_pr"]
    assert d.target_hash == adr3.content_hash and d.details["adr_hash"] == adr3.content_hash
    assert adr3.decision_id == d.id and adr3.status == "ACCEPTED" and "Context" in adr3.sections
    snap = db.scalar(select(PolicySnapshot).where(PolicySnapshot.active.is_(True)))
    assert snap is not None and any(x["id"] == d.id for x in snap.content["decisions"])

    second = seed.run(db)
    db.commit()
    assert second["adrs"]["created"] == 0 and second["adrs"]["updated"] == 0
    assert second["adrs"]["unchanged"] == len(keys) and second["adr_0003_exception"] == "present"
    assert db.query(Decision).filter(Decision.actor_user_id == "owner:ADR-0003").count() == 1
    assert second["policy_snapshot"]["content_hash"] == first["policy_snapshot"]["content_hash"]
    assert db.query(PolicySnapshot).count() == 1


def test_revoked_exception_is_not_recreated(db):
    owner = create_admin(db)
    seed.run(db)
    d = db.scalar(select(Decision).where(Decision.actor_user_id == "owner:ADR-0003"))
    registry.revoke_decision(db, d.id, actor_user_id=owner.id, reason="owner revokes delegation")
    db.commit()
    res = seed.run(db)
    assert res["adr_0003_exception"] == "present (INVALIDATED)"
    assert db.query(Decision).filter(Decision.actor_user_id == "owner:ADR-0003").count() == 1


def test_changed_adr_supersedes_binding(db, monkeypatch, tmp_path):
    src = adrs.decisions_dir()
    for p in src.glob("ADR-*.md"):
        shutil.copy(p, tmp_path / p.name)
    monkeypatch.setattr(adrs, "decisions_dir", lambda: tmp_path)
    seed.run(db)
    old = db.scalar(select(Decision).where(Decision.actor_user_id == "owner:ADR-0003"))
    path = tmp_path / "ADR-0003-seed-build-authority.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n<!-- clarified -->\n", encoding="utf-8")
    res = seed.run(db)
    db.commit()
    assert res["adrs"]["updated"] == 1 and res["adr_0003_exception"] == "superseded"
    assert old.status == "SUPERSEDED"
    new = db.get(Decision, old.superseded_by)
    assert new.status == "ACTIVE" and new.target_hash != old.target_hash
