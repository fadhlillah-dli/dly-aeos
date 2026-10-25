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


def test_a_trust_table_larger_than_the_cap_is_never_reported_clean(db, settings, monkeypatch):
    """Fail closed: rows beyond MAX_ROWS_PER_TABLE are not compared, so such a window is UNKNOWN (a blocking
    NOT-PROVEN in the verifier), never a silent PASS (review 2026-10-19: the LIMIT truncated silently)."""
    db.add(Decision(kind="POLICY", actor_user_id="o", actor_kind="HUMAN", target_type="t", target_id="cap"))
    db.commit()
    monkeypatch.setattr(tamper, "MAX_ROWS_PER_TABLE", 0)
    tamper.install()
    win = tamper.Window(db.get_bind())
    result = win.close()
    assert result["status"] == "UNKNOWN", result
    assert "decisions" in result["truncated_tables"] and "only partly compared" in result["error"]


def test_forged_session_and_containment_proof_are_foreign_writes(db, settings):
    """Candidate code under the WEAK runner forging a server-side session (impersonating a human, AUTH) or a PASS
    container containment report (the isolation proof itself) is caught; a cache row of system_settings that other
    AEOS processes update legitimately is not compared; the controller's own setting write is journaled by key."""
    import sqlite3

    from aeos.models.identity import SystemSetting

    tamper.install()
    win = tamper.Window(db.get_bind())
    db.add(SystemSetting(key="flags.own-controller-write", value=True))  # this process: journaled by its key
    db.commit()
    raw = sqlite3.connect(str(settings.layout.db_path))
    try:
        raw.execute("INSERT INTO auth_sessions (id, token_hash, user_id, csrf_token, created_at, last_seen_at, "
                    "expires_at, user_agent, client_ip) VALUES ('forged-session', 'h', 'owner', 'c', "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00', '2099-01-01 00:00:00', '', '')")
        raw.execute("INSERT INTO system_settings (key, value, updated_at, version) VALUES "
                    "('worker_runtime.containment.container', '{\"overall\": \"PASS\"}', '2026-01-01 00:00:00', 1)")
        raw.execute("INSERT INTO system_settings (key, value, updated_at, version) VALUES "
                    "('doctor.cache.probe', '1', '2026-01-01 00:00:00', 1)")
        raw.commit()
        result = win.close()
    finally:
        for stmt in ("DELETE FROM auth_sessions WHERE id='forged-session'",
                     "DELETE FROM system_settings WHERE key IN ('worker_runtime.containment.container', "
                     "'doctor.cache.probe')"):
            raw.execute(stmt)
        raw.commit()
        raw.close()
    found = {(f["table"], f["id"]) for f in result["foreign"]}
    assert result["status"] == "FAIL", result
    assert ("auth_sessions", "forged-session") in found
    assert ("system_settings", "worker_runtime.containment.container") in found
    assert not [f for f in found if f[1] in ("doctor.cache.probe", "flags.own-controller-write")], found


def test_dropping_an_append_only_trigger_is_a_foreign_write(db, settings):
    """Candidate code that drops the append-only/immutability trigger guarding evidence or decisions (so it can
    rewrite "immutable" history) changes the schema: a foreign write, never a clean window (review 2026-10-19)."""
    import sqlite3

    tamper.install()
    raw = sqlite3.connect(str(settings.layout.db_path))
    try:
        name, sql = raw.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                                "AND tbl_name='evidence_artifacts' ORDER BY name").fetchone()
    finally:
        raw.close()
    win = tamper.Window(db.get_bind())
    raw = sqlite3.connect(str(settings.layout.db_path))
    try:
        raw.execute(f'DROP TRIGGER "{name}"')
        raw.commit()
        result = win.close()
        raw.execute(sql)  # restore the guard for the rest of the test session
        raw.commit()
    finally:
        raw.close()
    assert result["status"] == "FAIL", result
    assert {"table": tamper.SCHEMA_KEY, "id": f"trigger:{name}", "change": "removed"} in result["foreign"]


def _verify(db, world, **kw):
    out = verifier.verify_run(db, world.run.id, actor="tester", **kw)
    db.commit()
    return out


# A candidate test that inserts an approval decision - exactly the §21 attack through verifier-executed code.
# It reaches the controller DB by an absolute path it was told (a candidate that discovered
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
    # the injected row is real (the WEAK runner did not prevent it) - the verifier catches it and neutralises it:
    # INVALIDATED, so no gate honours it while the owner reviews the quarantine (review W6-057)
    from aeos.policy import decisions as registry

    db.expire_all()
    injected = db.scalar(select(Decision).where(Decision.id == "candidate-injected"))
    assert injected is not None and injected.status == "INVALIDATED", injected.status if injected else None
    assert "VERIFIER_TAMPER" in (injected.invalidation_reason or "")
    assert registry.find_active_decision(db, kind="APPROVAL", target_type="release", target_id="x") is None
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert {"table": "decisions", "id": "candidate-injected", "change": "added",
            "action": "INVALIDATED"} in data["tamper"]["neutralized"]["actions"]
    # everything foreign was neutralised: nothing stays in effect, so no integrity hold on admission
    assert "integrity_hold" not in data["tamper"]["neutralized"] and not wf.of("set_scheduling_paused")
    # the run was sent to BLOCKED for special review, never repaired as an ordinary failure
    assert wf.of("transition_task") and wf.of("transition_task")[0]["to"] == "BLOCKED"
    assert not wf.of("request_repair")
    from aeos.models.audit import AuditEvent, Event

    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "verification.controller_modified",
                                              AuditEvent.target_id == world.run.id)) is not None
    alert = db.scalar(select(Event).where(Event.kind == "verification.alert", Event.entity_id == world.run.id))
    assert alert is not None and alert.payload["severity"] == "HIGH"
    assert any(a["id"] == "candidate-injected" for a in alert.payload["neutralized"])
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "verification.tamper.neutralize",
                                              AuditEvent.target_id == world.run.id)) is not None


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
    assert out.checks["step_containment"].status == "PASS" and data["tamper"]["neutralized"] == {}


# ------------------------------------------------------------------------------------------------------------------
# §21 policy / routing / approval records (review W6-056, W6-057, W6-060)


def test_the_trust_tables_pin_policy_decision_route_and_session_tables():
    """Removing any of these from the window would let verifier-executed code change them undetected."""
    must = {"decisions", "decision_requests", "policy_snapshots", "model_routes", "acceptance_records",
            "auth_sessions", "capability_grants", "users", "system_settings", "agent_definitions", "budgets",
            "eval_suites", "task_specs", "plan_versions", "evidence_artifacts"}
    assert must <= set(tamper.TRUST_TABLES), sorted(must - set(tamper.TRUST_TABLES))


def _raw(settings, *statements: str) -> None:
    import sqlite3

    conn = sqlite3.connect(str(settings.layout.db_path))
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _route(db, key: str = "worker-tamper", status: str = "APPROVED"):
    from aeos.models.routing import ModelRoute

    route = ModelRoute(route_key=key, pool="WORKER", provider="openrouter", model_id="m/x", status=status,
                       fallback_chain=[])
    db.add(route)
    db.commit()
    return route


def test_a_raw_model_route_update_inside_a_window_is_a_foreign_write(db, settings):
    route = _route(db)
    tamper.install()
    win = tamper.Window(db.get_bind())
    _raw(settings, f"UPDATE model_routes SET fallback_chain='[\"paid-route\"]', status='APPROVED' "
                   f"WHERE id='{route.id}'")
    result = win.close()
    assert result["status"] == "FAIL", result
    assert {"table": "model_routes", "id": route.id, "change": "modified"} in result["foreign"]


def test_every_supported_foreign_row_is_neutralised(db, settings):
    """Each kind of forged/modified trust row stops being effective; what has no safe inverse is listed for the
    owner (manual), never silently kept."""
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.models.decisions import PolicySnapshot
    from aeos.models.evidence import AcceptanceRecord
    from aeos.models.identity import AuthSession, SystemSetting
    from aeos.models.policy_ext import DecisionRequest
    from aeos.models.routing import CapabilityGrant, ModelRoute
    from aeos.policy import decisions as registry
    from aeos.verification import neutralize
    from tests.conftest import create_admin

    owner = create_admin(db)
    later = utcnow() + timedelta(hours=1)
    d = Decision(kind="APPROVAL", actor_user_id=owner.id, actor_kind="HUMAN", target_type="release", target_id="r1",
                 status="INVALIDATED")
    req = DecisionRequest(kind="EXCEPTION", title="t", requested_by="system")
    sess = AuthSession(token_hash="h-tamper", user_id=owner.id, csrf_token="c", expires_at=later)
    grant = CapabilityGrant(token_hash="g-tamper", run_id="run-x", project_id="p", domain="PERSONAL",
                            kind="MODEL_PROXY", expires_at=later)
    route = _route(db, "worker-neutral")
    snap = PolicySnapshot(version="999", content={"forged": True}, content_hash="sha256:" + "0" * 64, active=False)
    proof = SystemSetting(key="worker_runtime.containment.container", value={"overall": "FAIL"})
    write = SystemSetting(key="write_enabled.github", value=False)
    acc = AcceptanceRecord(task_id="task-x", run_id="run-x", project_id="p", artifact_hash="sha256:" + "1" * 64,
                           acceptance_status="ACCEPTED")
    db.add_all([d, req, sess, grant, snap, proof, write, acc])
    db.commit()
    tamper.install()
    win = tamper.Window(db.get_bind())
    _raw(settings,
         f"UPDATE decisions SET status='ACTIVE' WHERE id='{d.id}'",
         f"UPDATE decision_requests SET title='approve me' WHERE id='{req.id}'",
         f"UPDATE auth_sessions SET expires_at='2099-01-01 00:00:00' WHERE id='{sess.id}'",
         f"UPDATE capability_grants SET scope='{{\"models\": \"*\"}}' WHERE id='{grant.id}'",
         f"UPDATE model_routes SET fallback_chain='[\"paid\"]' WHERE id='{route.id}'",
         f"UPDATE policy_snapshots SET active=1 WHERE id='{snap.id}'",
         "UPDATE system_settings SET value='{\"overall\": \"PASS\"}' WHERE key='worker_runtime.containment.container'",
         "UPDATE system_settings SET value='true' WHERE key='write_enabled.github'",
         f"UPDATE acceptance_records SET reasons='[\"forged\"]' WHERE id='{acc.id}'",
         f"UPDATE users SET role='SUPER_ADMIN', display_name='x' WHERE id='{owner.id}'")
    result = win.close()
    assert result["status"] == "FAIL" and result["foreign_count"] >= 10, result
    out = neutralize.neutralize_foreign_rows(db, result["foreign"], run_id="run-t",
                                             foreign_count=result["foreign_count"])
    db.commit()
    db.expire_all()
    acted = {(a["table"], a["id"]): a["action"] for a in out["actions"]}
    assert acted[("decisions", d.id)] == "INVALIDATED" and db.get(Decision, d.id).status == "INVALIDATED"
    assert registry.find_active_decision(db, kind="APPROVAL", target_type="release", target_id="r1") is None
    assert db.get(DecisionRequest, req.id).status == "WITHDRAWN"
    assert db.get(AuthSession, sess.id).revoked_at is not None
    g = db.get(CapabilityGrant, grant.id)
    assert g.revoked_at is not None and g.active is False
    assert db.get(ModelRoute, route.id).status == "DISABLED"  # ineligible until a human re-activates its hash
    assert db.get(PolicySnapshot, snap.id).active is False
    active = db.scalars(select(PolicySnapshot).where(PolicySnapshot.active.is_(True))).all()
    assert len(active) == 1 and active[0].id != snap.id  # rebuilt from the policy files + decisions
    assert db.get(SystemSetting, "worker_runtime.containment.container") is None  # the proof must be re-run
    assert db.get(SystemSetting, "write_enabled.github").value is False
    assert db.get(AcceptanceRecord, acc.id).acceptance_status == "INVALIDATED"
    # a modified existing account (its password hash or role may be the candidate's): deactivated, so neither a web
    # sign-in nor a CLI decision (--as: password + is_active) works with it; listed for the owner's role review and
    # recovered locally - before, it stayed active and only its sessions were revoked
    from aeos.identity import service as identity
    from aeos.models.identity import User

    assert acted[("users", owner.id)] == "DEACTIVATED" and db.get(User, owner.id).is_active is False
    assert any(m["table"] == "users" and "aeos admin recover" in m["remedy"] for m in out["manual"])
    assert not out["failed"], out["failed"]
    identity.recover_user(db, username=owner.username)
    assert db.get(User, owner.id).is_active is True  # no lock-out: the local operator always recovers it


def test_activity_timestamps_are_not_foreign_writes_but_session_and_account_changes_are(db, settings):
    """The API server rewrites a session's last_seen_at on ordinary use and an account's last_login_at on sign-in: an
    open dashboard during a CLI verification (another AEOS process) is not tampering, and neutralisation must never
    revoke the owner's live session for it. Any other change to those rows still is a foreign write."""
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.models.identity import AuthSession
    from tests.conftest import create_admin

    owner = create_admin(db)
    sess = AuthSession(token_hash="h-activity", user_id=owner.id, csrf_token="c",
                       expires_at=utcnow() + timedelta(hours=1))
    db.add(sess)
    db.commit()
    tamper.install()
    win = tamper.Window(db.get_bind())
    _raw(settings, f"UPDATE auth_sessions SET last_seen_at='2099-01-01 00:00:00' WHERE id='{sess.id}'",
         f"UPDATE users SET last_login_at='2099-01-01 00:00:00' WHERE id='{owner.id}'")
    assert win.close()["status"] == "PASS"
    win = tamper.Window(db.get_bind())
    _raw(settings, f"UPDATE auth_sessions SET expires_at='2099-01-01 00:00:00' WHERE id='{sess.id}'",
         f"UPDATE users SET password_hash='forged' WHERE id='{owner.id}'")
    result = win.close()
    assert result["status"] == "FAIL"
    assert {(f["table"], f["id"]) for f in result["foreign"]} == {("auth_sessions", sess.id), ("users", owner.id)}


def test_every_foreign_row_is_neutralised_not_only_the_listed_ones(db, settings):
    """The report lists the first 100 foreign rows with keys cut to 64 characters; neutralisation uses every row
    with its full key, so 120 forged approvals - one with a 100-character id - all stop being honoured."""
    from aeos.policy import decisions as registry
    from aeos.verification import neutralize

    tamper.install()
    win = tamper.Window(db.get_bind())
    long_id = "forged-" + "x" * 93
    ids = [f"forged-{n:03d}" for n in range(119)] + [long_id]
    _raw(settings, *[
        "INSERT INTO decisions (id, created_at, kind, actor_user_id, actor_kind, target_type, target_id, auth_scope, "
        f"allowed_operations, rationale, details, status) VALUES ('{i}', '2026-01-01T00:00:00', 'APPROVAL', 'worker', "
        f"'HUMAN', 'release', '{i}', '{{}}', '[]', '', '{{}}', 'ACTIVE')" for i in ids])
    result = win.close()
    assert result["foreign_count"] == 120 and len(result["foreign"]) == 100
    assert all(len(f["id"]) <= 64 for f in result["foreign"]) and len(win.foreign_all) == 120
    out = neutralize.neutralize_foreign_rows(db, win.foreign_all, run_id="run-many", foreign_count=120)
    db.commit()
    db.expire_all()
    assert out["counts"]["actions"] == 120 and out["not_listed"] == 0 and not out["manual"], out["manual"][:3]
    assert db.get(Decision, long_id).status == "INVALIDATED"
    assert all(registry.find_active_decision(db, kind="APPROVAL", target_type="release", target_id=i) is None
               for i in (ids[0], ids[118], long_id))


def test_a_foreign_row_that_cannot_be_found_again_is_never_reported_neutralised(db, settings):
    from aeos.verification import neutralize

    out = neutralize.neutralize_foreign_rows(db, [{"table": "decisions", "id": "no-such-row", "change": "added"}],
                                             run_id="run-gone")
    assert not out["actions"] and out["manual"][0]["id"] == "no-such-row" and "not found" in out["manual"][0]["remedy"]


def test_removed_rows_and_schema_changes_are_listed_for_manual_review(db, settings):
    from aeos.verification import neutralize

    out = neutralize.neutralize_foreign_rows(db, [
        {"table": "decisions", "id": "gone", "change": "removed"},
        {"table": tamper.SCHEMA_KEY, "id": "trigger:x", "change": "removed"},
        {"table": "budgets", "id": "b1", "change": "modified"},
    ], run_id="run-m", foreign_count=5)
    assert not out["actions"] and len(out["manual"]) == 4 and out["not_listed"] == 2
    assert all(m.get("remedy") for m in out["manual"])


# Candidate tests reaching the controller DB / checkout by an absolute path (a candidate that found it some way other
# than the redirected environment). The WEAK runner cannot stop them; the window catches and neutralises them.
POLICY_FILE_TAMPER_TEST = r'''
POLICY = __POLICY__


def test_open_office_policy():
    with open(POLICY, "a", encoding="utf-8") as fh:
        fh.write("\nexternal_worker_allowed: true\n")
'''

SNAPSHOT_TAMPER_TEST = r'''
import sqlite3

DB = __DB__


def test_insert_policy_snapshot():
    conn = sqlite3.connect(DB)
    try:
        conn.execute("UPDATE policy_snapshots SET active=0")
        conn.execute("INSERT INTO policy_snapshots (id, created_at, version, content, content_hash, source, "
                     "mci_reference, active) VALUES ('candidate-policy', '2026-01-01 00:00:00', '99', "
                     "'{\"office\": {\"external_worker_allowed\": true}}', 'sha256:forged', 'candidate', '{}', 1)")
        conn.commit()
    finally:
        conn.close()
'''

ROUTE_TAMPER_TEST = r'''
import sqlite3

DB = __DB__


def test_widen_route():
    conn = sqlite3.connect(DB)
    try:
        conn.execute("UPDATE model_routes SET fallback_chain='[\"paid-route\"]', eligible_domains='[\"OFFICE\"]' "
                     "WHERE route_key='worker-e2e'")
        conn.commit()
    finally:
        conn.close()
'''

ORPHAN_TEST = r'''
import os
import subprocess
import sys


def test_leave_a_detached_process_behind():
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
'''


def test_a_candidate_writing_controller_policy_is_quarantined(db, settings, controller, wf, monkeypatch):
    """§21 "workers may not change policy": the controller's policy/** is fingerprinted around candidate code."""
    from aeos.core import paths

    policy = controller / "policy" / "office-defaults.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("external_worker_allowed: false\napproved_worker_routes: []\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: controller)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={
        "tests/unit/test_policy.py": POLICY_FILE_TAMPER_TEST.replace("__POLICY__", repr(str(policy)))})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert out.checks["controller_integrity"].status == "FAIL"
    assert any(f["kind"] == "controller" and f["path"] == "policy/office-defaults.yaml" for f in out.tamper_findings)
    assert not wf.of("request_repair")
    # the changed policy file stays in effect (never restored automatically - the checkout may hold the owner's own
    # work): admission of new worker runs is paused until the owner reconciles (review W6-056 "nothing pauses")
    pauses = wf.of("set_scheduling_paused")
    assert len(pauses) == 1 and pauses[0]["paused"] is True and pauses[0]["actor"] == "verifier", pauses
    assert "VERIFIER_TAMPER integrity hold" in pauses[0]["reason"] and "aeos work resume" in pauses[0]["reason"]
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    hold = data["tamper"]["neutralized"]["integrity_hold"]
    assert hold["admission_paused"] is True
    assert hold["controller_changes"][0]["path"] == "policy/office-defaults.yaml"
    assert "aeos policy snapshot --refresh" in hold["remedies"][0]["remedy"]
    from aeos.models.audit import Event

    alert = db.scalar(select(Event).where(Event.kind == "verification.alert", Event.entity_id == world.run.id))
    assert alert is not None and alert.payload["integrity_hold"]["admission_paused"] is True


def test_the_integrity_hold_pauses_admission_through_the_real_scheduler(db, settings, controller, monkeypatch):
    """The same hold through the real workforce scheduler (only the task/run transitions are stubbed): admission
    reports paused, by the verifier, with the tamper reason - only the owner's resume lifts it."""
    from aeos.core import paths
    from aeos.workforce import facade as workforce
    from aeos.workforce import scheduler

    stubbed: list[str] = []
    for name in ("update_run", "transition_task", "request_repair"):
        monkeypatch.setattr(scheduler, name, lambda *a, _n=name, **k: stubbed.append(_n))
    assert not workforce.scheduling_paused(db)
    policy = controller / "policy" / "routing.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("routes: []\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: controller)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={
        "tests/unit/test_policy.py": POLICY_FILE_TAMPER_TEST.replace("__POLICY__", repr(str(policy)))})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert "transition_task" in stubbed and "request_repair" not in stubbed
    db.expire_all()
    assert workforce.scheduling_paused(db)
    info = scheduler.pause_info(db)
    assert info["by"] == "verifier" and "VERIFIER_TAMPER integrity hold" in info["reason"], info


def test_a_candidate_writing_a_policy_snapshot_is_quarantined_and_neutralised(db, settings, controller, wf):
    from aeos.models.decisions import PolicySnapshot
    from aeos.policy import snapshot as policy_snapshot

    policy_snapshot.ensure_active_snapshot(db, source="test")
    db.commit()
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={
        "tests/unit/test_snap.py": SNAPSHOT_TAMPER_TEST.replace("__DB__", repr(str(settings.layout.db_path)))})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert any(f["kind"] == "db" and f["path"] == "policy_snapshots:candidate-policy" for f in out.tamper_findings)
    db.expire_all()
    assert db.get(PolicySnapshot, "candidate-policy").active is False
    active = db.scalars(select(PolicySnapshot).where(PolicySnapshot.active.is_(True))).all()
    assert len(active) == 1 and active[0].id != "candidate-policy" and active[0].source != "candidate"


def test_a_candidate_widening_a_model_route_is_quarantined_and_the_route_disabled(db, settings, controller, wf):
    from aeos.models.routing import ModelRoute

    route = _route(db, "worker-e2e")
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={
        "tests/unit/test_route.py": ROUTE_TAMPER_TEST.replace("__DB__", repr(str(settings.layout.db_path)))})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert any(f["kind"] == "db" and f["path"] == f"model_routes:{route.id}" for f in out.tamper_findings)
    db.expire_all()
    assert db.get(ModelRoute, route.id).status == "DISABLED"


REMOVE_ROW_TEST = r'''
import sqlite3

DB = __DB__


def test_remove_a_decision():
    conn = sqlite3.connect(DB)
    try:
        conn.execute("DELETE FROM decisions WHERE id='owner-keeps-this'")
        conn.commit()
    finally:
        conn.close()
'''


def test_a_row_without_a_safe_inverse_puts_admission_on_hold(db, settings, controller, wf):
    """A removed approval row cannot be restored automatically: it is listed for the owner (manual) and admission
    is paused until the owner reconciles (restore from the pre-run backup) and resumes."""
    db.add(Decision(id="owner-keeps-this", kind="POLICY", actor_user_id="owner", actor_kind="HUMAN",
                    target_type="t", target_id="keep"))
    db.commit()
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={
        "tests/unit/test_remove.py": REMOVE_ROW_TEST.replace("__DB__", repr(str(settings.layout.db_path)))})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    manual = data["tamper"]["neutralized"]["manual"]
    assert any(m["id"] == "owner-keeps-this" and m["change"] == "removed" and "backup" in m["remedy"] for m in manual)
    assert data["tamper"]["neutralized"]["integrity_hold"]["admission_paused"] is True
    assert wf.of("set_scheduling_paused") and wf.of("set_scheduling_paused")[0]["paused"] is True


def test_a_degraded_kill_group_is_stated_not_proven(db, settings, controller, wf, monkeypatch):
    """A step whose kill group fell back to the environment-marker sweep (e.g. no Job Object) cannot prove that
    nothing outlived it: listed as NOT-PROVEN, never reported as a clean containment."""
    from aeos.verification import runner

    real = runner.run_process

    def degraded(*args, **kwargs):
        res = real(*args, **kwargs)
        res.containment = "marker-sweep"
        res.containment_errors = ["job object unavailable (test)"]
        return res

    monkeypatch.setattr(runner, "run_process", degraded)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.checks["step_containment"].status == "NOT_PROVEN", out.checks["step_containment"].summary
    assert any("kill group degraded" in n and "job object unavailable" in n for n in out.not_proven), out.not_proven
    assert not out.tamper_findings
    data = collector.read_json(db.get(EvidenceArtifact, out.report_evidence_id))
    assert data["runner"]["verifier_step_containment"] == "marker-sweep"


def test_a_survivor_of_a_trusted_only_step_is_stated_not_blamed_on_the_candidate(db, settings, controller, wf,
                                                                                   monkeypatch):
    """The expected-id collection and the base-commit baseline import trusted code only: a process outliving them
    is a harness defect (NOT-PROVEN note), not candidate tampering - while any other step's survivor is."""
    from aeos.verification import runner

    def survivors(self):
        return [{"step": "collect_expected", "survivors": [{"pid": 1, "name": "leaked-helper"}]}]

    monkeypatch.setattr(runner.ProcessRunner, "survivors", survivors)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "PASS" and not out.tamper_findings, (out.status, out.reasons)
    assert out.checks["step_containment"].status == "NOT_PROVEN"
    assert any("trusted-only verifier step (collect_expected)" in n for n in out.not_proven), out.not_proven


def test_a_survivor_of_an_unnamed_step_is_never_presumed_trusted(db, settings, controller, wf, monkeypatch):
    from aeos.verification import runner

    def survivors(self):
        return [{"step": "", "survivors": [{"pid": 2, "name": "x"}]}]

    monkeypatch.setattr(runner.ProcessRunner, "survivors", survivors)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED" and out.checks["step_containment"].status == "FAIL", out.status


def test_a_candidate_leaving_a_process_behind_is_quarantined(db, settings, controller, wf):
    """The orphan case (review W6-054/W6-057): a detached process started by a candidate test outlives its step;
    it is killed when the step ends and the verification is QUARANTINED, never PASS."""
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"tests/unit/test_orphan.py": ORPHAN_TEST})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED", (out.status, out.reasons)
    assert out.checks["step_containment"].status == "FAIL"
    assert any(f["kind"] == "process" and f["path"] == "candidate_tests" for f in out.tamper_findings)
    assert "left processes running after its step" in (out.quarantine_reason or "")
    assert not wf.of("request_repair")
