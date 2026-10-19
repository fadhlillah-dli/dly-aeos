"""§21 "workers may not change approval records / policy / routing policy" + §20 Verifier ENF: a foreign write
to the control-plane trust tables while the verifier runs candidate code is detected (defence in depth for the
WEAK process runner), while the controller's own legitimate writes in the same window are not mistaken for
tampering."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, text

from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, suite, tamper, verifier
from tests.unit.test_verification_support import (
    FakeWorkforce,
    bind_candidate,
    make_candidate,
    make_controller,
    make_world,
    requires_git,
)

pytestmark = requires_git
FAST = {"ruff": False}


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


@pytest.fixture
def wf(monkeypatch: pytest.MonkeyPatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def test_window_detects_a_foreign_decision_write_but_not_the_controllers_own(db, settings):
    import sqlite3

    tamper.install()
    engine = db.get_bind()
    win = tamper.Window(engine)
    # the controller's own write in the window is journaled (ORM flush) -> not foreign
    db.add(Decision(kind="POLICY", actor_user_id="owner", actor_kind="HUMAN", target_type="t", target_id="own"))
    db.commit()
    # a foreign writer: a separate sqlite connection to the same file (candidate code in its own process, which
    # never goes through the controller's SQLAlchemy engine)
    raw = sqlite3.connect(str(settings.layout.db_path))
    try:
        raw.execute("INSERT INTO decisions (id, created_at, kind, actor_user_id, actor_kind, target_type, "
                    "target_id, auth_scope, allowed_operations, rationale, details, status) VALUES "
                    "('foreign1', '2026-01-01T00:00:00', 'APPROVAL', 'intruder', 'HUMAN', 't', 'x', '{}', '[]', "
                    "'', '{}', 'ACTIVE')")
        raw.commit()
    finally:
        raw.close()
    result = win.close()
    assert result["status"] == "FAIL"
    assert {f["id"] for f in result["foreign"]} == {"foreign1"}
    assert result["foreign"][0]["table"] == "decisions" and result["foreign"][0]["change"] == "added"
    assert result["own_writes"] >= 1  # the controller's own decision was journaled
    db.execute(text("DELETE FROM decisions WHERE id='foreign1'"))
    db.commit()


def test_clean_window_passes(db, settings):
    tamper.install()
    win = tamper.Window(db.get_bind())
    db.add(Decision(kind="POLICY", actor_user_id="o", actor_kind="HUMAN", target_type="t", target_id="a"))
    db.commit()
    result = win.close()
    assert result["status"] == "PASS" and not result["foreign"]


def _verify(db, world, **kw):
    out = verifier.verify_run(db, world.run.id, actor="tester", **kw)
    db.commit()
    return out


# A candidate test that opens the AEOS data DB directly (the path leaks via AEOS_DATA_DIR in the WEAK runner)
# and inserts an approval decision - exactly the §21 attack through verifier-executed code.
# A candidate test that reaches the controller DB by an absolute path it was told (a candidate that discovered
# the path some other way than the - redirected - environment). The WEAK runner cannot stop this; the tamper
# window is the defence in depth that catches it.
DB_TAMPER_TEST = r'''
import sqlite3

DB = __DB__


def test_write_a_decision_row():
    conn = sqlite3.connect(DB)
    try:
        conn.execute("INSERT INTO decisions (id, created_at, kind, actor_user_id, actor_kind, target_type, "
                     "target_id, auth_scope, allowed_operations, rationale, details, status) VALUES "
                     "('candidate-injected', '2026-01-01T00:00:00', 'APPROVAL', 'worker', 'HUMAN', 'release', "
                     "'x', '{}', '[]', '', '{}', 'ACTIVE')")
        conn.commit()
    finally:
        conn.close()
    assert True
'''


def test_candidate_code_writing_the_trust_db_is_quarantined(db, settings, controller, wf, monkeypatch):
    """The WEAK process runner cannot stop the write (same OS user), but the verifier detects the foreign row,
    QUARANTINES the run (owner special review) and raises a HIGH alert - the write never counts as a pass."""
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={
        "tests/unit/test_tamper.py": DB_TAMPER_TEST.replace("__DB__", repr(str(settings.layout.db_path)))})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED", (out.reasons, out.status)
    assert out.quarantine_reason and "VERIFIER_TAMPER" in out.quarantine_reason
    assert out.checks["trust_tables"].status == "FAIL"
    assert any(f["kind"] == "db" and "decisions:candidate-injected" in f["path"] for f in out.tamper_findings)
    # the injected row is real (the WEAK runner did not prevent it) - the verifier's job is to catch it
    assert db.scalar(select(Decision).where(Decision.id == "candidate-injected")) is not None
    # the run was sent to BLOCKED for special review, never repaired as an ordinary failure
    assert wf.of("transition_task") and wf.of("transition_task")[0]["to"] == "BLOCKED"
    assert not wf.of("request_repair")
    from aeos.models.audit import AuditEvent, Event

    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "verification.controller_modified",
                                              AuditEvent.target_id == world.run.id)) is not None
    alert = db.scalar(select(Event).where(Event.kind == "verification.alert", Event.entity_id == world.run.id))
    assert alert is not None and alert.payload["severity"] == "HIGH"


def test_clean_candidate_run_records_the_tamper_window_as_pass(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "PASS", (out.reasons, out.not_proven)
    assert out.checks["controller_integrity"].status == "PASS"
    assert out.checks["trust_tables"].status == "PASS"
    assert not out.tamper_findings
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert data["tamper"]["findings"] == []
