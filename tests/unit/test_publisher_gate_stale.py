"""§08 gate CAN_PUBLISH_PERSONAL: a publish dependency that passed and was then invalidated (STALE - token rotated,
target/config changed) refuses execute until the Doctor re-checks it; never-run checks stay advisory (full READY
enforcement is the owner's decision over the protected acceptance suite, ADR-0094) - and the gate says so."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aeos.core.errors import Blocked
from aeos.core.timeutil import utcnow
from aeos.publisher import jobs
from tests.unit.test_publisher_support import (
    confirm_of,
    install_fakes,
    make_plan,
    make_project,
    seed_personal_secrets,
    use_fake_providers,
)


@pytest.fixture
def env(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, version = make_plan(db, project)
    return {"settings": settings, "db": db, "fakes": fakes, "project": project, "plan": plan}


def _row(check_id: str, status: str):
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.models.inventory import CheckResult

    load_all()
    spec = REGISTRY[check_id]
    now = utcnow()
    return CheckResult(check_id=check_id, title=spec.title, group=spec.group, component_id=spec.component_id,
                       scope=dict(spec.scope), status=status, severity="INFO", summary=f"unit: {status}",
                       observed_evidence={}, last_checked_at=now, expires_at=now + timedelta(hours=1),
                       invalidators=list(spec.invalidators), fingerprint=None, remediation={})


def _preview(env):
    pub = jobs.create_preview(env["db"], env["settings"], plan_id=env["plan"].id, actor_user_id="owner-user",
                              render_visuals=False)
    env["db"].commit()
    return pub


def test_rotated_google_token_invalidates_the_gate_until_rechecked(env):
    from aeos.secrets import facade as secrets

    db = env["db"]
    db.add(_row("adapter.google_oauth", "CONFIGURED"))
    db.commit()
    pub = _preview(env)
    gate = pub.preview["capability_gate"]
    assert "adapter.google_oauth" not in gate["failed_checks"]  # passed; other checks never ran -> advisory
    assert gate["full_ready_enforcement"] == "OWNER_DECISION_PENDING" and "advisory" in gate["enforcement"]
    # the owner rotates the Google token: the stored result becomes STALE (secret:<ref> invalidator)
    secrets.put_secret("personal/google-oauth-token", '{"token": "rotated-token-value-xyz"}', actor="owner",
                       session=db)
    db.commit()
    pub = _preview(env)
    assert "adapter.google_oauth" in pub.preview["capability_gate"]["failed_checks"]
    with pytest.raises(Blocked) as exc:
        jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    assert exc.value.code == "CAPABILITY_NOT_READY" and "STALE" in exc.value.message
    assert sum(len(f.calls) for f in env["fakes"].values()) == 0  # nothing was written
    db.add(_row("adapter.google_oauth", "CONFIGURED"))  # the Doctor re-checked it
    db.commit()
    pub = _preview(env)
    assert jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub)).allowed


def test_never_run_and_expired_checks_stay_advisory(env):
    db = env["db"]
    row = _row("adapter.google_oauth", "CONFIGURED")
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.add(row)
    db.commit()
    pub = _preview(env)
    assert pub.preview["capability_gate"]["failed_checks"] == []
    assert any("CAN_PUBLISH_PERSONAL" in n and "informasi" in n for n in pub.preview["notes"])
    assert jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub)).allowed


def test_a_stale_project_target_check_refuses_execute_until_rechecked():
    """§08 "memicu recheck sebelum capability tersebut digunakan kembali": the project's own target check (Drive root
    changed, token invalidated -> STALE) blocks like the platform checks; a never-run or not-applicable project
    target stays advisory (ADR-0094)."""
    from types import SimpleNamespace

    def state(eff, *, ok=False, status="UNKNOWN"):
        return SimpleNamespace(status=status, capability="CAN_PUBLISH_PERSONAL", checks=[
            {"check_id": "adapter.project_drive_root", "tier": "project", "effective_status": eff, "ok": ok}])

    assert jobs._blocking_checks(state("STALE")) == ["adapter.project_drive_root"]
    assert jobs._blocking_checks(state("FAILED", status="BLOCKED")) == ["adapter.project_drive_root"]
    assert jobs._blocking_checks(state("UNKNOWN")) == []  # never run: advisory
    assert jobs._blocking_checks(state("EXPIRED")) == []  # TTL only: advisory
    assert jobs._blocking_checks(state("NOT_APPLICABLE", ok=True, status="READY")) == []
