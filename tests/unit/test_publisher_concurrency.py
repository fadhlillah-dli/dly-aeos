"""Outbox safety under concurrency, stale state and crashes (§14, §29, §41, §50).

§50 rows covered here:
* "Approval PlanVersion lama → Ditolak" — also for a human retry while the run's decision is still valid
* "Provider timeout setelah create resource → Rekonsiliasi ID/target sebelum retry" — across approved versions
  (an UNKNOWN create of v1 is never re-created or silently adopted by v2)
* "Salah satu target publikasi gagal → PARTIAL" — interlock denial mid-run is visible per operation
* "Bootstrap belum selesai tetapi publish diminta → Interlock menolak" — denied ops are FAILED + audited, not
  silently PENDING
* Crash recovery: an IN_FLIGHT op is reconciled before retry, but never while its call may still be running
"""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from aeos.core.errors import Conflict
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.planning import Plan
from aeos.models.publishing import ExternalReference, Publication
from aeos.models.workforce import TaskExecution
from aeos.publisher import jobs
from aeos.publisher.providers.base import ProviderBlocked, ProviderError
from tests.unit.test_publisher_support import (
    SAMPLE_CONTENT,
    add_version,
    approve,
    confirm_of,
    install_fakes,
    make_plan,
    make_project,
    seed_personal_secrets,
    use_fake_providers,
)


@pytest.fixture
def env(settings, db, secrets_memory, monkeypatch):
    policy = install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, version = make_plan(db, project)
    return {"settings": settings, "db": db, "policy": policy, "fakes": fakes, "project": project, "plan": plan,
            "version": version, "monkeypatch": monkeypatch}


def _preview(env, **opts):
    pub = jobs.create_preview(env["db"], env["settings"], plan_id=env["plan"].id, actor_user_id="owner-user",
                              options=opts, render_visuals=False)
    env["db"].commit()
    return pub


def _execute(env, pub):
    out = jobs.execute_publication(env["db"], pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    env["db"].commit()
    assert out.allowed, out.reasons
    return out


def _op(env, pub, logical_id, operation=None):
    return next(o for o in jobs._ops_for(env["db"], pub.id)
                if o.logical_id == logical_id and (operation is None or o.operation == operation))


def _approve_v2(env, summary="Versi 2"):
    db = env["db"]
    plan = db.get(Plan, env["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["summary"] = summary
    v2 = add_version(db, plan, content)
    approve(db, plan, v2)
    db.commit()
    return v2


# ---------------------------------------------------------------------------------------------- approval


def test_retry_is_rejected_when_a_newer_version_was_approved(env):
    """A retry is a new human action: a stale approval is refused even though the run's decision is valid."""
    env["fakes"]["github-issues"].fail["issue:task:T2"] = ProviderError("422", retryable=False)
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(env["db"], pub.id)
    env["db"].refresh(pub)
    assert pub.status == "PARTIAL"
    assert jobs._decision_expiry(env["db"], pub) is not None  # run decision still valid
    _approve_v2(env)
    with pytest.raises(Conflict) as exc:
        jobs.retry_publication(env["db"], pub.id, actor_user_id="owner-user")
    assert exc.value.code == "APPROVAL_STALE"


# ------------------------------------------------------------------------------------- concurrent runs


def test_second_run_of_the_same_project_is_refused_while_one_is_running(env):
    pub1 = _preview(env)
    _execute(env, pub1)  # RUNNING, not processed yet
    v2 = _approve_v2(env)
    pub2 = _preview(env)
    assert pub2.plan_version_id == v2.id
    with pytest.raises(Conflict) as exc:
        jobs.execute_publication(env["db"], pub2.id, actor_user_id="owner-user", confirm=confirm_of(pub2))
    assert exc.value.code == "PUBLICATION_RUNNING" and exc.value.details["publication_id"] == pub1.id


def test_execute_refuses_a_preview_whose_targets_changed_meanwhile(env):
    """A preview that planned create_* cannot execute after another run created the target (no duplicate)."""
    db = env["db"]
    pub = _preview(env)
    db.add(ExternalReference(project_id=env["project"].id, domain="PERSONAL", provider="google-drive",
                             logical_id="00-product-home", external_id="doc-from-elsewhere"))
    db.commit()
    with pytest.raises(Conflict) as exc:
        jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    assert exc.value.code == "TARGETS_CHANGED" and "00-product-home" in exc.value.details["logical_ids"]
    assert env["fakes"]["google"].calls == []


@pytest.mark.parametrize("same_content", [True, False])
def test_create_never_runs_when_the_target_appeared_after_execute(env, same_content):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    op = _op(env, pub, "01-product-overview", "create_doc")
    db.add(ExternalReference(project_id=env["project"].id, domain="PERSONAL", provider="google-drive",
                             logical_id="01-product-overview", external_id="doc-race",
                             content_hash=op.payload["content_hash"] if same_content else "sha256:other"))
    db.commit()
    jobs.process_publication(db, pub.id)
    db.refresh(op)
    assert ("create_doc", "01-product-overview") not in env["fakes"]["google"].calls
    assert op.status == ("SKIPPED" if same_content else "CONFLICT") and op.external_id == "doc-race"
    assert "target already exists" in op.error and "doc-race" in op.error


def test_unknown_create_of_an_older_version_blocks_a_new_preview_until_reconciled(env):
    """§29 reconcile-before-retry across versions: the new version neither duplicates nor adopts the old doc."""
    db, fakes = env["db"], env["fakes"]
    fakes["google"].timeout_after_create.add("00-product-home")
    pub1 = _preview(env)
    _execute(env, pub1)
    jobs.process_publication(db, pub1.id)
    assert _op(env, pub1, "00-product-home").status == "UNKNOWN"
    pub1.status = "PARTIAL"  # e.g. the operator stopped waiting
    db.commit()
    _approve_v2(env, summary="Ringkasan v2 untuk Product Home")
    with pytest.raises(Conflict) as exc:
        _preview(env)
    assert exc.value.code == "UNRESOLVED_OPERATIONS" and exc.value.details["publication_ids"] == [pub1.id]
    db.rollback()
    # the human reconciles the old run (read-only): the doc created by v1 is found and recorded with v1 content
    res = jobs.reconcile_publication(db, pub1.id, actor_user_id="owner-user")
    db.commit()
    assert res["found"] >= 1
    old = _op(env, pub1, "00-product-home")
    ref = db.scalar(select(ExternalReference).where(ExternalReference.logical_id == "00-product-home"))
    assert old.status == "SUCCEEDED" and ref.content_hash == old.payload["content_hash"]
    # v2 now plans an UPDATE of that document under revision control — never a second create
    pub2 = _preview(env)
    ops = [o for o in jobs._ops_for(db, pub2.id) if o.logical_id == "00-product-home"]
    assert [o.operation for o in ops] == ["update_doc"] and ops[0].status == "PENDING"
    _execute(env, pub2)
    jobs.process_publication(db, pub2.id)
    assert [c for c in fakes["google"].calls if c == ("create_doc", "00-product-home")] == \
        [("create_doc", "00-product-home")]
    assert "Ringkasan v2" in fakes["google"].store["00-product-home"]["payload"]["markdown"]


def test_unknown_update_of_another_run_blocks_preview(env):
    db = env["db"]
    pub1 = _preview(env)
    _execute(env, pub1)
    jobs.process_publication(db, pub1.id)
    _approve_v2(env, summary="v2")
    pub2 = _preview(env)
    _execute(env, pub2)
    upd = _op(env, pub2, "00-product-home", "update_doc")
    upd.status = "UNKNOWN"
    pub2.status = "PARTIAL"
    db.commit()
    _approve_v2(env, summary="v3")
    with pytest.raises(Conflict) as exc:
        _preview(env)
    assert exc.value.code == "UNRESOLVED_OPERATIONS"
    assert any(o["operation"] == "update_doc" for o in exc.value.details["operations"])


def test_human_retry_and_reconcile_never_interleave_with_the_executor(env):
    pub = _preview(env)
    _execute(env, pub)
    lock = jobs._lock(pub.id)
    assert lock.acquire(blocking=False)
    try:
        for call in (lambda: jobs.retry_publication(env["db"], pub.id, actor_user_id="owner-user"),
                     lambda: jobs.reconcile_publication(env["db"], pub.id, actor_user_id="owner-user")):
            with pytest.raises(Conflict) as exc:
                call()
            assert exc.value.code == "PUBLICATION_BUSY"
    finally:
        lock.release()


# ------------------------------------------------------------------------------------------- interlock


def test_interlock_denial_mid_run_is_failed_per_op_audited_and_retryable(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    env["policy"].write_enabled = False
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    root = _op(env, pub, "root")
    assert root.status == "FAILED" and root.response["blocked"]["code"] == "INTERLOCK_DENIED"
    assert pub.status == "FAILED" and "interlock denied" in pub.status_reason
    assert "BLOCKED: needs action" not in pub.status_reason  # not double-reported as a provider block
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "publication.op.create_folder",
                                              AuditEvent.result == "DENIED")) is not None
    assert env["fakes"]["google"].calls == []
    env["policy"].write_enabled = True
    out = jobs.retry_publication(db, pub.id, actor_user_id="owner-user")
    db.commit()
    assert out.allowed
    db.refresh(root)
    assert "blocked" not in root.response  # stale BLOCKED marker does not leak into the next outcome
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert pub.status == "PUBLISHED"


# --------------------------------------------------------------------------------------- reconciliation


def test_blocked_reconciliation_backs_off_and_finalises_instead_of_spinning(env):
    db, fakes, mp = env["db"], env["fakes"], env["monkeypatch"]
    fakes["google"].timeout_after_create.add("root")
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    assert _op(env, pub, "root").status == "UNKNOWN"
    from aeos.publisher import providers

    def blocked(_cfg):
        raise ProviderBlocked("google token missing", code="GOOGLE_TOKEN_EXPIRED", remedy="aeos publish google-connect")

    mp.setitem(providers._overrides, "google", blocked)
    t = utcnow()
    for i in range(1, 12):
        jobs.process_publication(db, pub.id, now=t + timedelta(hours=i))
        db.refresh(pub)
        if pub.status != "RUNNING":
            break
    root = _op(env, pub, "root")
    assert root.status == "UNKNOWN" and root.response["reconcile_failures"] == jobs.MAX_RECONCILE_FAILURES
    assert "reconcile blocked" in root.error and root.next_attempt_at is not None
    assert pub.status in ("PARTIAL", "FAILED") and "UNKNOWN operations need manual reconciliation" in pub.status_reason
    # issues (another family) were not held hostage by the blocked docs provider
    assert _op(env, pub, "issue:task:T1").status == "SUCCEEDED"


def test_fresh_in_flight_op_is_left_alone_and_keeps_the_run_open(env):
    db, fakes = env["db"], env["fakes"]
    pub = _preview(env)
    _execute(env, pub)
    root = _op(env, pub, "root")
    now = utcnow()
    root.status, root.attempts, root.last_attempt_at = "IN_FLIGHT", root.max_attempts, now
    db.commit()
    jobs.process_publication(db, pub.id, now=now + timedelta(minutes=1))
    db.refresh(pub)
    db.refresh(root)
    assert root.status == "IN_FLIGHT" and ("reconcile", "root") not in fakes["google"].calls
    assert pub.status == "RUNNING"  # never finalised around an outcome that is still pending
    jobs.process_publication(db, pub.id, now=now + jobs.IN_FLIGHT_STALE_AFTER + timedelta(minutes=1))
    db.refresh(root)
    assert ("reconcile", "root") in fakes["google"].calls and root.status != "IN_FLIGHT"


def test_startup_recovery_defers_reconciliation_of_recent_in_flight_calls(env):
    db, fakes = env["db"], env["fakes"]
    pub = _preview(env)
    _execute(env, pub)
    root = _op(env, pub, "root")
    now = utcnow()
    root.status, root.attempts, root.last_attempt_at = "IN_FLIGHT", 1, now
    db.commit()
    assert jobs.recover_in_flight(db, now=now) == 1
    db.commit()
    db.refresh(root)
    assert root.status == "UNKNOWN"
    assert abs((jobs.ensure_aware(root.next_attempt_at) - (now + jobs.RECOVERY_GRACE)).total_seconds()) < 2
    jobs.process_publication(db, pub.id, now=now + timedelta(seconds=30))
    assert ("reconcile", "root") not in fakes["google"].calls
    jobs.process_publication(db, pub.id, now=now + jobs.RECOVERY_GRACE + timedelta(seconds=5))
    assert ("reconcile", "root") in fakes["google"].calls


def test_request_preparation_failure_is_failed_without_any_call(env):
    db, fakes, mp = env["db"], env["fakes"], env["monkeypatch"]

    def broken_docx(*_a, **_kw):
        raise ValueError("cannot build snapshot")

    mp.setattr(jobs, "markdown_to_docx", broken_docx)
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    home = _op(env, pub, "00-product-home")
    assert home.status == "FAILED" and "nothing was sent" in home.error and home.attempts == 0
    assert not any(c[0] == "create_doc" for c in fakes["google"].calls)
    assert ("create_folder", "root") in fakes["google"].calls  # folders were not affected
    assert pub.status == "PARTIAL"


# ------------------------------------------------------------------------------------- files and payloads


def test_only_files_inside_the_data_directory_are_ever_published(env, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG")
    assert jobs._managed_file(str(outside)) is None
    inside = env["settings"].layout.root / "PERSONAL" / "x.png"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"\x89PNG")
    assert jobs._managed_file(str(inside)) == inside.resolve()
    assert jobs._managed_file(str(env["settings"].layout.root / ".." / "outside.png")) is None
    assert jobs._managed_file(None) is None


def test_google_update_flags_what_the_incremental_path_cannot_carry(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    _approve_v2(env, summary="v2 dengan tabel")
    pub2 = _preview(env)
    upd = _op(env, pub2, "00-product-home", "update_doc")
    assert upd.status == "PENDING" and "tabel (menjadi teks)" in upd.payload["lossy_update"]
    assert any("snapshot DOCX baru" in n for n in pub2.preview["notes"])
    assert jobs.operation_view(upd)["payload_summary"]["lossy_update"] == upd.payload["lossy_update"]


# ------------------------------------------------------------------------------------------ dimensions


def test_implemented_dimension_counts_tasks_with_a_done_execution(env):
    db = env["db"]
    pub = _preview(env)
    tasks = {t.task_key: t for t in jobs.planning.tasks_for_version(db, env["version"].id)}
    db.add(TaskExecution(task_id=tasks["T1"].id, project_id=env["project"].id, domain="PERSONAL", state="DONE"))
    db.commit()
    dims = jobs.status_dimensions(db, pub)
    assert dims["IMPLEMENTED"]["status"] == "IN_PROGRESS" and dims["IMPLEMENTED"]["detail"].startswith("1/2")
    db.add(TaskExecution(task_id=tasks["T2"].id, project_id=env["project"].id, domain="PERSONAL", state="REVIEW"))
    db.commit()
    assert jobs.status_dimensions(db, pub)["IMPLEMENTED"]["status"] == "IN_PROGRESS"  # REVIEW is not DONE
    # VERIFIED is never inferred from IMPLEMENTED/DOCS_PUBLISHED
    assert jobs.status_dimensions(db, pub)["VERIFIED"]["status"] in ("UNKNOWN", "NOT_PROVEN")
    assert db.get(Publication, pub.id).status == "PREVIEW"
