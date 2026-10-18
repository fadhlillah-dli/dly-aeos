"""Publication outbox: preview, interlock, ordering, idempotency, partial, reconciliation (§14, §29, §41, §50).

§50 rows covered here:
* "Bootstrap belum selesai tetapi publish diminta → Interlock menolak" (fake interlock denies)
* "Approval PlanVersion lama → Ditolak"
* "Revisi baru muncul selama publish → Job tetap memakai versi disetujui"
* "Provider timeout setelah create resource → Rekonsiliasi ID/target sebelum retry"
* "Salah satu target publikasi gagal → PARTIAL, hasil sukses tetap tercatat"
"""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked, Conflict
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.decisions import Decision
from aeos.models.planning import Plan
from aeos.models.publishing import (
    DocumentRecord,
    ExternalReference,
    OperationalWorkProjection,
    PublicationOperation,
)
from aeos.publisher import jobs
from aeos.publisher.providers.base import ProviderError
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
            "version": version}


def _preview(env, **opts):
    pub = jobs.create_preview(env["db"], env["settings"], plan_id=env["plan"].id, actor_user_id="owner-user",
                              options=opts, render_visuals=False)
    env["db"].commit()
    return pub


def _execute(env, pub):
    out = jobs.execute_publication(env["db"], pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    env["db"].commit()
    return out


def _ops(db, pub):
    return jobs._ops_for(db, pub.id)


def test_idempotency_key_is_stable_and_specific():
    k1 = jobs.idempotency_key("p", "google-drive", "00-product-home", "create_doc", "sha256:aa")
    assert k1 == jobs.idempotency_key("p", "google-drive", "00-product-home", "create_doc", "sha256:aa")
    assert k1.startswith("sha256:") and len(k1) <= 128
    for other in (("p2", "google-drive", "00-product-home", "create_doc", "sha256:aa"),
                  ("p", "github-issues", "00-product-home", "create_doc", "sha256:aa"),
                  ("p", "google-drive", "01-product-overview", "create_doc", "sha256:aa"),
                  ("p", "google-drive", "00-product-home", "update_doc", "sha256:aa"),
                  ("p", "google-drive", "00-product-home", "create_doc", "sha256:bb")):
        assert jobs.idempotency_key(*other) != k1


def test_preview_requires_an_approved_version(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    project = make_project(db)
    plan, _v = make_plan(db, project, approved=False)
    with pytest.raises(Conflict) as exc:
        jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="u", render_visuals=False)
    assert exc.value.code == "PLAN_NOT_APPROVED"


def test_preview_fails_closed_when_planning_service_missing(settings, db, monkeypatch):
    install_fakes(monkeypatch, planning=False)
    import sys

    monkeypatch.setitem(sys.modules, "aeos.planning.service", None)  # import raises ImportError
    project = make_project(db)
    plan, _v = make_plan(db, project)
    with pytest.raises(Blocked) as exc:
        jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="u", render_visuals=False)
    assert exc.value.code == "PLANNING_UNAVAILABLE"


def test_preview_lists_concrete_operations_with_targets(env):
    pub = _preview(env)
    ops = _ops(env["db"], pub)
    assert pub.status == "PREVIEW" and pub.preview_hash and pub.decision_id
    kinds = {o.operation for o in ops}
    assert {"create_folder", "create_doc", "ensure_label", "create_issue"} <= kinds
    assert all(o.idempotency_key == jobs.idempotency_key(env["project"].id, o.provider, o.logical_id, o.operation,
                                                         env["version"].manifest_hash) for o in ops)
    issues = [o for o in ops if o.operation == "create_issue"]
    assert {o.logical_id for o in issues} == {"issue:task:T1", "issue:task:T2"}  # one per TaskSpec
    assert all("<!-- aeos:logical_id=" in o.payload["body"] for o in issues)
    assert "Satu persetujuan mencakup" in pub.preview["approval_statement"]
    assert not pub.preview["blocked_targets"]


def test_operations_run_in_dependency_order_folder_before_docs(env):
    pub = _preview(env)
    ops = _ops(env["db"], pub)
    pos = {o.logical_id: o.order_index for o in ops if o.provider.startswith("google")}
    assert pos["root"] < pos["02-requirements"] < pos["02-requirements/prd-tasks"]
    assert pos["root"] < pos["00-product-home"]
    by_id = {o.id: o for o in ops}
    prd = next(o for o in ops if o.logical_id == "02-requirements/prd-tasks")
    assert [by_id[d].logical_id for d in prd.depends_on] == ["02-requirements"]
    issue = next(o for o in ops if o.logical_id == "issue:task:T1")
    assert {by_id[d].logical_id for d in issue.depends_on} == {"label:aeos", "label:feature:tasks"}
    _execute(env, pub)
    jobs.process_publication(env["db"], pub.id)
    calls = env["fakes"]["google"].calls
    order = [lid for op, lid in calls if op.startswith("create")]
    assert order.index("root") < order.index("02-requirements") < order.index("02-requirements/prd-tasks")
    # documents were created inside their real parent folder (never the Drive root by accident)
    store = env["fakes"]["google"].store
    assert store["02-requirements/prd-tasks"]["parent"] == store["02-requirements"]["id"]


def test_execute_denied_by_interlock_without_write_enablement(env):
    env["policy"].write_enabled = False
    pub = _preview(env)
    out = _execute(env, pub)
    assert not out.allowed and any("write disabled" in r for r in out.reasons)
    db = env["db"]
    db.refresh(pub)
    assert pub.status == "PREVIEW" and pub.publication_decision_id is None
    assert env["fakes"]["google"].calls == []  # nothing ran
    d = db.scalar(select(Decision).where(Decision.kind == "PUBLICATION", Decision.target_id == pub.id))
    assert d is not None and d.status == "INVALIDATED"
    denied = db.scalar(select(AuditEvent).where(AuditEvent.action == "publication.execute",
                                                AuditEvent.result == "DENIED"))
    assert denied is not None


def test_execute_without_policy_module_fails_closed(env, monkeypatch):
    import sys

    pub = _preview(env)
    monkeypatch.setitem(sys.modules, "aeos.policy.decisions", None)
    with pytest.raises(Blocked):
        jobs.execute_publication(env["db"], pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))


def test_execute_requires_exact_confirmation(env):
    pub = _preview(env)
    bad = confirm_of(pub) | {"approved_hash": "sha256:other"}
    with pytest.raises(Conflict) as exc:
        jobs.execute_publication(env["db"], pub.id, actor_user_id="owner-user", confirm=bad)
    assert exc.value.code == "STALE_PREVIEW"


def test_one_approval_covers_every_operation_of_the_run(env):
    pub = _preview(env)
    out = _execute(env, pub)
    assert out.allowed
    db = env["db"]
    decisions = list(db.scalars(select(Decision).where(Decision.kind == "PUBLICATION")))
    assert len(decisions) == 1
    d = decisions[0]
    assert d.target_type == "publication" and d.target_id == pub.id
    assert d.target_version == env["version"].id and d.target_hash == env["version"].manifest_hash
    assert d.expires_at is not None
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert pub.status == "PUBLISHED"
    # interlock was evaluated for every write, always with the SAME decision (no per-call re-prompt)
    assert {i.decision_id for i in env["policy"].calls} == {d.id}
    assert len(list(db.scalars(select(Decision).where(Decision.kind == "PUBLICATION")))) == 1
    plan = db.get(Plan, env["plan"].id)
    assert plan.state == "PUBLISHED"


def test_old_approval_rejected_after_new_version_approved(env):
    pub = _preview(env)
    db = env["db"]
    plan = db.get(Plan, env["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["summary"] = "Versi 2"
    v2 = add_version(db, plan, content)
    approve(db, plan, v2)
    db.commit()
    with pytest.raises(Conflict) as exc:
        jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    assert exc.value.code == "APPROVAL_STALE"


def test_newer_unapproved_version_is_ignored(env):
    db = env["db"]
    plan = db.get(Plan, env["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["features"][0]["title"] = "JUDUL DRAFT BARU"
    add_version(db, plan, content)  # newer, NOT approved; becomes current_version_id
    db.commit()
    pub = _preview(env)
    assert pub.plan_version_id == env["version"].id
    ops = _ops(db, pub)
    assert not any("JUDUL DRAFT BARU" in (o.payload or {}).get("markdown", "") for o in ops)


def test_job_keeps_approved_snapshot_when_new_revision_appears_during_publish(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    plan = db.get(Plan, env["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["summary"] = "REVISI BARU SAAT PUBLISH"
    add_version(db, plan, content)
    plan.state = "READY_FOR_HUMAN_REVIEW"  # planning moved on
    db.commit()
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert pub.status == "PUBLISHED"
    published = env["fakes"]["google"].store["00-product-home"]["payload"]["markdown"]
    assert "REVISI BARU SAAT PUBLISH" not in published
    assert db.get(Plan, plan.id).state == "READY_FOR_HUMAN_REVIEW"  # not overwritten
    assert pub.summary["plan_state"]["applied"] is False


def test_partial_failure_keeps_successes_and_retry_does_not_recreate(env):
    fakes = env["fakes"]
    fakes["github-issues"].fail["issue:task:T2"] = ProviderError("422 validation", retryable=False)
    pub = _preview(env)
    _execute(env, pub)
    db = env["db"]
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert pub.status == "PARTIAL"
    ops = {o.logical_id: o for o in _ops(db, pub)}
    assert ops["issue:task:T2"].status == "FAILED"
    assert ops["issue:task:T1"].status == "SUCCEEDED" and ops["00-product-home"].status == "SUCCEEDED"
    assert db.get(Plan, env["plan"].id).state == "PUBLICATION_PARTIAL"
    refs = list(db.scalars(select(ExternalReference).where(ExternalReference.project_id == env["project"].id)))
    assert any(r.logical_id == "00-product-home" for r in refs)
    created_before = len([c for c in fakes["google"].calls if c[0].startswith("create")])
    out = jobs.retry_publication(db, pub.id, actor_user_id="owner-user")
    db.commit()
    assert out.allowed
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert pub.status == "PUBLISHED"
    created_after = len([c for c in fakes["google"].calls if c[0].startswith("create")])
    assert created_after == created_before  # successes were not re-created
    assert [c for c in fakes["github-issues"].calls if c == ("create_issue", "issue:task:T1")] == \
        [("create_issue", "issue:task:T1")]
    assert db.scalar(select(OperationalWorkProjection).where(OperationalWorkProjection.task_id.is_not(None)))


def test_timeout_becomes_unknown_then_reconciled_without_duplicate(env):
    fakes = env["fakes"]
    fakes["google"].timeout_after_create.add("00-product-home")
    pub = _preview(env)
    _execute(env, pub)
    db = env["db"]
    jobs.process_publication(db, pub.id)
    op = next(o for o in _ops(db, pub) if o.logical_id == "00-product-home")
    assert op.status == "UNKNOWN" and op.next_attempt_at is not None
    db.refresh(pub)
    assert pub.status == "RUNNING"  # waits for reconciliation instead of guessing
    jobs.process_publication(db, pub.id, now=utcnow() + timedelta(hours=1))
    db.refresh(op)
    assert op.status == "SUCCEEDED" and op.response.get("reconciled") is True
    creates = [c for c in fakes["google"].calls if c == ("create_doc", "00-product-home")]
    assert len(creates) == 1  # reconciliation found it: no duplicate create
    assert ("reconcile", "00-product-home") in fakes["google"].calls


def test_unknown_with_absent_resource_is_retried_once_reconciled(env):
    fakes = env["fakes"]
    pub = _preview(env)
    _execute(env, pub)
    db = env["db"]
    from aeos.publisher.providers.base import ProviderTimeout

    fakes["google"].fail["01-product-overview"] = ProviderTimeout("lost before reaching provider")
    jobs.process_publication(db, pub.id)
    op = next(o for o in _ops(db, pub) if o.logical_id == "01-product-overview")
    assert op.status == "UNKNOWN"
    jobs.process_publication(db, pub.id, now=utcnow() + timedelta(hours=1))
    db.refresh(op)
    assert op.status == "SUCCEEDED" and op.attempts == 2
    seq = [c for c in fakes["google"].calls if c[1] == "01-product-overview"]
    assert seq == [("create_doc", "01-product-overview"), ("reconcile", "01-product-overview"),
                   ("create_doc", "01-product-overview")]


def test_retries_are_bounded(env):
    fakes = env["fakes"]
    pub = _preview(env)
    _execute(env, pub)
    db = env["db"]

    class AlwaysFail(ProviderError):
        pass

    orig = fakes["google"].execute

    def flaky(ctx):
        if ctx.logical_id == "05-development":
            fakes["google"].calls.append((ctx.operation, ctx.logical_id))
            raise AlwaysFail("HTTP 429", retryable=True, status_code=429)
        return orig(ctx)

    fakes["google"].execute = flaky
    t = utcnow()
    for i in range(6):
        jobs.process_publication(db, pub.id, now=t + timedelta(hours=i + 1))
    op = next(o for o in _ops(db, pub) if o.logical_id == "05-development")
    assert op.status == "FAILED" and op.attempts == op.max_attempts == jobs.DEFAULT_MAX_ATTEMPTS
    db.refresh(pub)
    assert pub.status == "PARTIAL"


def test_backoff_is_exponential_and_capped():
    assert jobs.backoff_seconds(1) == 30 and jobs.backoff_seconds(2) == 60 and jobs.backoff_seconds(3) == 120
    assert jobs.backoff_seconds(20) == jobs.BACKOFF_CAP_S


def test_republish_same_version_skips_unchanged_documents(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    pub2 = _preview(env)
    ops2 = _ops(db, pub2)
    assert ops2, "a second preview lists the current state"
    docs = [o for o in ops2 if o.provider.startswith("google") and o.operation in ("update_doc", "create_doc")]
    assert docs and all(o.status == "SKIPPED" for o in docs)
    assert all(o.status == "SKIPPED" for o in ops2 if o.operation == "create_issue")


def test_delta_publish_updates_only_changed_documents(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    plan = db.get(Plan, env["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["features"][0]["acceptance"].append("User dapat menghapus tugas")
    v2 = add_version(db, plan, content)
    approve(db, plan, v2)
    db.commit()
    pub2 = _preview(env)
    pending = {o.logical_id for o in _ops(db, pub2) if o.status == "PENDING"}
    assert {"02-requirements/prd-tasks", "06-testing/test-plan-tasks"} <= pending
    assert "00-product-home" not in pending and "05-development" not in pending
    assert all(o.operation == "update_doc" for o in _ops(db, pub2) if o.logical_id in pending and
               o.provider.startswith("google"))


def test_reprevew_supersedes_unexecuted_preview(env):
    db = env["db"]
    pub1 = _preview(env)
    pub2 = _preview(env)
    db.refresh(pub1)
    assert pub1.status == "CANCELLED" and pub2.status == "PREVIEW"
    assert _ops(db, pub1) == []  # outbox rows adopted by the new preview (one row per side effect)
    keys = [o.idempotency_key for o in _ops(db, pub2)]
    assert len(keys) == len(set(keys))


def test_unknown_ops_are_adopted_as_unknown_not_pending(env):
    db = env["db"]
    env["fakes"]["google"].timeout_after_create.add("root")
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    root = next(o for o in _ops(db, pub) if o.logical_id == "root")
    assert root.status == "UNKNOWN"
    pub.status = "PARTIAL"  # e.g. an operator gave up waiting
    db.commit()
    pub2 = _preview(env)
    adopted = next(o for o in _ops(db, pub2) if o.logical_id == "root")
    assert adopted.id == root.id and adopted.status == "UNKNOWN"  # reconcile before any retry
    assert adopted.response["history"][0]["publication_id"] == pub.id


def test_interlock_revocation_mid_run_stops_writes(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    env["policy"].write_enabled = False
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert env["fakes"]["google"].calls == []
    assert pub.status == "FAILED" and "interlock denied" in pub.status_reason
    assert db.get(Plan, env["plan"].id).state == "PUBLICATION_FAILED"


def test_documents_marked_published_with_external_links(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    rec = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "00-product-home"))
    assert rec.status == "PUBLISHED" and rec.links["external_url"].startswith("https://")
    assert rec.last_synced_at is not None
    future = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "08-monitoring"))
    assert future.status == "NOT_STARTED"  # skeleton published, status stays honest


def test_crash_recovery_marks_in_flight_unknown(env):
    db = env["db"]
    pub = _preview(env)
    _execute(env, pub)
    op = _ops(db, pub)[0]
    op.status = "IN_FLIGHT"
    db.commit()
    assert jobs.recover_in_flight(db) == 1
    db.refresh(op)
    assert op.status == "UNKNOWN"


def test_status_dimensions_are_separate(env):
    db = env["db"]
    pub = _preview(env)
    dims = jobs.status_dimensions(db, pub)
    assert set(dims) == {"APPROVED", "IMPLEMENTED", "ACTIVE", "VERIFIED", "ARCHITECTURE_SOURCE_UPDATED",
                         "DOCS_PUBLISHED"}
    assert dims["APPROVED"]["status"] == "APPROVED"
    assert dims["DOCS_PUBLISHED"]["status"] == "NOT_PUBLISHED"
    assert dims["ACTIVE"]["status"] == "UNKNOWN"  # never inferred from docs/approval
    assert dims["VERIFIED"]["status"] in ("UNKNOWN", "NOT_PROVEN")


def test_final_status_rules():
    def op(s):
        return PublicationOperation(status=s)

    assert jobs.final_status([op("SUCCEEDED"), op("SKIPPED")]) == "PUBLISHED"
    assert jobs.final_status([op("SUCCEEDED"), op("FAILED")]) == "PARTIAL"
    assert jobs.final_status([op("SUCCEEDED"), op("CONFLICT")]) == "PARTIAL"
    assert jobs.final_status([op("CONFLICT"), op("SKIPPED")]) == "CONFLICT"
    assert jobs.final_status([op("FAILED"), op("UNKNOWN")]) == "FAILED"


def test_office_project_targets_never_route_to_personal_providers(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    from aeos.models.portfolio import ProjectIntegration

    project = make_project(db, domain="OFFICE")
    db.add(ProjectIntegration(project_id=project.id, domain="OFFICE", provider="google-drive", purpose="docs",
                              root_id="folder-x"))
    db.commit()
    plan, _v = make_plan(db, project)
    pub = jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="u", render_visuals=False)
    db.commit()
    fams = {t["family"] for t in pub.targets}
    assert fams == {"confluence", "jira"}
    assert all(t["status"] != "READY" for t in pub.targets)  # disabled by default
    assert _ops(db, pub) == []
    with pytest.raises(Blocked) as exc:
        jobs.execute_publication(db, pub.id, actor_user_id="u", confirm=confirm_of(pub))
    assert exc.value.code == "TARGET_NOT_READY"
    assert all(not f.calls for f in fakes.values())
