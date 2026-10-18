"""Approval -> publication binding with the REAL planning service, decision registry and interlock
(§05 step 9-10, §14, §26, U-10; ADR-0031/ADR-0074 amendments 2026-10-18).

One model: the plan APPROVAL (Approve & Publish) binds version + manifest hash + {provider, root_id}
targets + operation kinds; the run's PUBLICATION decision binds one preview (publication id, preview hash,
operation ids, targets) and references that approval; the interlock checks both for every write. Providers
are in-memory fakes (no network)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.errors import Blocked, Conflict
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.decisions import Decision
from aeos.models.planning import Plan, PlanVersion
from aeos.planning import service as planning
from aeos.policy import decisions as registry
from aeos.policy import recording
from aeos.policy.facade import check_external_write
from aeos.publisher import jobs, plan_state
from tests.conftest import create_admin
from tests.unit.test_planning_support import sample_content
from tests.unit.test_publisher_support import (
    confirm_of,
    make_project,
    seed_personal_secrets,
    use_fake_providers,
)

DRIVE = {"provider": "google-drive", "root_id": "root"}
ISSUES = {"provider": "github-issues", "root_id": "owner/contoh"}


@pytest.fixture
def env(settings, db, secrets_memory, monkeypatch):
    owner = create_admin(db)
    seed_personal_secrets(secrets_memory)
    fakes = use_fake_providers(monkeypatch)
    project = make_project(db)  # PERSONAL, GitHub repository owner/contoh, no Drive folder (My Drive root)
    content = sample_content("Contoh")
    plan = Plan(project_id=project.id, domain=project.domain, title="Rencana", state="READY_FOR_HUMAN_REVIEW")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content), mode="LIVE")
    db.add(v)
    db.flush()
    plan.current_version_id = v.id
    db.commit()
    for provider in ("google-drive", "github-issues"):
        recording.record_generic_decision(db, actor_user_id=owner.id, domain=Domain.PERSONAL, kind="WRITE_ENABLEMENT",
                                          rationale="publikasi diizinkan", auth_scope={"p": provider},
                                          provider=provider, enabled=True)
    db.commit()
    return {"db": db, "settings": settings, "owner": owner, "project": project, "plan": plan, "version": v,
            "fakes": fakes}


def _approve(env, targets=(DRIVE, ISSUES), action="APPROVE_AND_PUBLISH") -> str:
    db, v = env["db"], env["version"]
    res = planning.decide(db, env["plan"].id, action=action, version_id=v.id, version_hash=v.manifest_hash,
                          targets=list(targets), rationale="setuju", actor_user_id=env["owner"].id)
    db.commit()
    return res["decision"]["id"]


def _preview(env, **options):
    pub = jobs.create_preview(env["db"], env["settings"], plan_id=env["plan"].id, actor_user_id=env["owner"].id,
                              options=options, render_visuals=False)
    env["db"].commit()
    return pub


def _execute(env, pub):
    out = jobs.execute_publication(env["db"], pub.id, actor_user_id=env["owner"].id, confirm=confirm_of(pub))
    env["db"].commit()
    return out


def _codes(res):
    return {r.split(":", 1)[0] for r in res.reasons}


def test_one_approval_and_one_run_decision_publish_every_operation(env):
    db = env["db"]
    approval_id = _approve(env)
    pub = _preview(env)
    out = _execute(env, pub)
    assert out.allowed, out.reasons
    runs = list(db.scalars(select(Decision).where(Decision.kind == "PUBLICATION")))
    assert len(runs) == 1 and runs[0].auth_scope["approval_decision_id"] == approval_id
    assert sorted(runs[0].auth_scope["targets"], key=str) == sorted([DRIVE, ISSUES], key=str)
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert pub.status == "PUBLISHED", pub.status_reason
    assert db.get(Plan, env["plan"].id).state == "PUBLISHED"  # via planning.facade.transition_plan
    ops = jobs._ops_for(db, pub.id)
    checks = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "policy.interlock.check",
                                                      AuditEvent.result == "SUCCESS")))
    assert {c.details["operation_id"] for c in checks} >= {o.id for o in ops if o.status == "SUCCEEDED"}
    assert all(c.details["decision_id"] == runs[0].id for c in checks)  # no per-API re-prompt (U-10)


def test_approve_without_publish_never_publishes(env):
    _approve(env, targets=(), action="APPROVE")
    with pytest.raises(Conflict) as exc:
        _preview(env)
    assert exc.value.code == "PUBLICATION_NOT_APPROVED"


def test_configured_target_outside_the_approval_is_blocked(env):
    _approve(env, targets=(ISSUES,))
    pub = _preview(env)  # only the approved purpose is planned by default
    assert [t["provider"] for t in pub.targets] == ["github-issues"]
    pub = _preview(env, targets=["docs", "issues"])  # asking for docs explicitly is refused, not published
    docs = next(t for t in pub.targets if t["provider"] == "google-drive")
    assert docs["status"] == "BLOCKED" and any("TARGET_NOT_APPROVED" in r for r in docs["reasons"])
    with pytest.raises(Blocked) as exc:
        _execute(env, pub)
    assert exc.value.code == "TARGET_NOT_READY"
    _approve(env, targets=({"provider": "google-drive", "root_id": "SomeOtherFolder123"}, ISSUES))
    pub = _preview(env, targets=["docs"])  # another folder than the configured one is not this target
    assert pub.targets[0]["status"] == "BLOCKED"


def test_reapproval_after_preview_makes_the_preview_stale(env):
    _approve(env)
    pub = _preview(env)
    _approve(env, targets=(DRIVE,))  # owner narrows the approval after looking at the preview
    with pytest.raises(Conflict) as exc:
        _execute(env, pub)
    assert exc.value.code == "APPROVAL_STALE"


def test_newer_version_stops_the_run_and_is_never_published(env):
    db = env["db"]
    _approve(env)
    pub = _preview(env)
    assert _execute(env, pub).allowed
    jobs.process_publication(db, pub.id, max_ops=1)
    calls_before = sum(len(f.calls) for f in env["fakes"].values())
    v2_content = sample_content("Contoh", note="revisi")
    planning.create_version(db, env["plan"], v2_content, source="HUMAN_EDIT", parent_version_id=env["version"].id,
                            run_ids=[], created_by=env["owner"].id)  # invalidates the approval of v1
    db.commit()
    jobs.process_publication(db, pub.id, now=utcnow() + timedelta(minutes=1))
    db.refresh(pub)
    denied = [o for o in jobs._ops_for(db, pub.id) if (o.response or {}).get("blocked", {}).get("code")
              == "INTERLOCK_DENIED"]
    assert denied and all("VERSION_NOT_APPROVED" in o.error or "DECISION_NOT_ACTIVE" in o.error for o in denied)
    assert sum(len(f.calls) for f in env["fakes"].values()) == calls_before  # nothing else was written
    with pytest.raises(Conflict) as exc:
        _preview(env)
    assert exc.value.code == "PLAN_NOT_APPROVED"  # the newer, unapproved version is never published


def test_run_decision_binds_exactly_one_preview(env):
    db = env["db"]
    _approve(env)
    pub = _preview(env)
    assert _execute(env, pub).allowed
    op = next(o for o in jobs._ops_for(db, pub.id) if o.status == "PENDING")
    intent = jobs._intent(pub, op, utcnow() + timedelta(minutes=5))
    assert check_external_write(db, intent).allowed
    assert "PUBLICATION_MISMATCH" in _codes(check_external_write(db, replace(
        intent, extra={**intent.extra, "publication_id": "another-publication"})))
    assert "PREVIEW_HASH_MISMATCH" in _codes(check_external_write(db, replace(
        intent, extra={**intent.extra, "preview_hash": "sha256:" + "9" * 64})))
    assert "OPERATION_NOT_IN_RUN" in _codes(check_external_write(db, replace(
        intent, extra={**intent.extra, "operation_id": "op-not-previewed"})))
    approval = db.get(Decision, pub.decision_id)
    assert "PUBLICATION_DECISION_REQUIRED" in _codes(check_external_write(db, replace(intent, decision_id=approval.id)))
    registry.revoke_decision(db, pub.publication_decision_id, actor_user_id=env["owner"].id, reason="stop dulu")
    db.commit()
    assert "PUBLICATION_DECISION_NOT_ACTIVE" in _codes(check_external_write(db, intent))


def test_publisher_moves_the_plan_only_through_the_planning_facade(env, absent_modules, monkeypatch):
    import aeos.planning

    db, plan = env["db"], env["plan"]
    _approve(env)
    applied, note = plan_state.set_plan_state(db, plan.id, "PUBLISHED", reason="skip")  # APPROVED -> PUBLISHED
    assert applied is False and "refused" in note and db.get(Plan, plan.id).state == "APPROVED"  # no fallback
    applied, _ = plan_state.set_plan_state(db, plan.id, "PUBLISHING", reason="run")
    assert applied is True and db.get(Plan, plan.id).state == "PUBLISHING"
    monkeypatch.delattr(aeos.planning, "facade")
    absent_modules("aeos.planning.facade")
    applied, note = plan_state.set_plan_state(db, plan.id, "PUBLISHED", reason="x")
    assert applied is False and "unavailable" in note and db.get(Plan, plan.id).state == "PUBLISHING"
