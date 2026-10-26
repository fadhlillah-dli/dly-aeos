"""Publisher gaps closed in the C3 cluster (§08 gates, §27 status lifecycle, §28/§29 human-governed content,
DOCX size, Confluence attachments, OFFICE default deny). Network is always respx/in-memory."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.errors import Blocked
from aeos.core.pii import redact_pii
from aeos.core.timeutil import utcnow
from aeos.docs_engine import renderer as rnd
from aeos.docs_engine.docx import markdown_to_docx
from aeos.models.planning import Plan
from aeos.models.portfolio import ProjectIntegration
from aeos.models.publishing import DocumentRecord, DocumentRevision, ExternalReference
from aeos.publisher import human_sections as hs
from aeos.publisher import jobs
from aeos.publisher import providers as providers_mod
from aeos.publisher.providers import atlassian as atl
from aeos.publisher.providers import google as gg
from aeos.publisher.providers.base import OpContext, ProviderConflict
from aeos.publisher.targets import resolve_target
from tests.e2e.fakes import FakeDrive
from tests.unit.test_publisher_support import (
    SAMPLE_CONTENT,
    add_version,
    approve,
    confirm_of,
    install_fakes,
    make_plan,
    make_project,
    schema_sample_content,
    seed_personal_secrets,
    use_fake_providers,
)

TOKEN = json.dumps({"token": "ya29.unit-test-access-token-0123456789", "refresh_token": "1//unit-refresh-0123456789abc",
                    "client_id": "cid", "client_secret": "csecret-value", "token_uri": gg.DEFAULT_TOKEN_URI,
                    "scopes": [gg.DRIVE_FILE_SCOPE], "expiry": (utcnow() + timedelta(hours=1)).isoformat()})

TEST_PLAN_TITLES = [s.title for s in rnd.p1.DOC_TYPES["TEST_PLAN"].sections]
TP_SPEC = [(s.title, s.content_type.value) for s in rnd.p1.DOC_TYPES["TEST_PLAN"].sections]


def _render_test_plan() -> str:
    res = rnd.render_document_set(
        project=rnd.ProjectIdentity(id="p", key="k", name="Contoh", domain="PERSONAL"),
        version=rnd.VersionIdentity(id="v", plan_id="pl", version=1, manifest_hash="sha256:" + "a" * 64, approved=True),
        content=copy.deepcopy(SAMPLE_CONTENT))
    return res.by_id()["06-testing/test-plan-tasks"].body_markdown


def _ctx(**kw) -> OpContext:
    base = dict(operation_id="op", publication_id="pub", project_id="proj", project_key="k", project_name="Contoh",
                domain="PERSONAL", provider="google-drive", operation="create_doc", logical_id="06-testing/test-plan-x",
                idempotency_key="sha256:" + "1" * 64, title="Test Plan", target={}, payload={})
    base.update(kw)
    return OpContext(**base)


def _google(client: httpx.Client) -> gg.GoogleDriveDocsProvider:
    return gg.GoogleDriveDocsProvider(client=client, auth=gg.GoogleAuth(client, token_json=TOKEN))


def _human_sign_off(text: str, line: str) -> str:
    """The text a human produces by typing *line* under the Sign-off heading of the published doc."""
    lines = text.split("\n")
    i = lines.index("Sign-off")
    return "\n".join([*lines[:i + 1], line, *lines[i + 1:]])


# ---------------------------------------------------------------------------------------------
# HUMAN_GOVERNED content survives an update (§28, §29) — never a blind body replacement


def test_human_lines_are_carried_and_aeos_placeholders_are_not():
    md = _render_test_plan()
    human = {t for t, ct in TP_SPEC if ct == "HUMAN_GOVERNED"}
    external = "\n".join(t for title in TEST_PLAN_TITLES
                         for t in (title, hs._plain(rnd.HUMAN_TEXT) if title in human else "isi lama")) + "\n"
    external = _human_sign_off(external, "Disetujui QA: Budi, 2026-10-01")
    guard = hs.guard_for(TP_SPEC, md, human_content_possible=True)
    out, kept = hs.preserve(md, external, guard)
    assert kept == {"Sign-off": ["Disetujui QA: Budi, 2026-10-01"]}
    sign = out.split("## Sign-off", 1)[1].split("\n## ", 1)[0]
    assert "> Disetujui QA: Budi, 2026-10-01" in sign and hs.MARKER in sign
    # AI_MAINTAINED sections are replaced by the approved version ("isi lama" is not carried)
    assert "isi lama" not in out
    # a second update over the spliced text keeps it exactly once (no duplication, no loss)
    plain_again = "\n".join(hs._plain(x) for x in out.split("\n") if x.strip() and not x.startswith("<!--"))
    out2, kept2 = hs.preserve(md, plain_again, guard)
    assert kept2 == kept and out2.count("Disetujui QA: Budi") == 1


def test_unlocatable_human_section_refuses_the_update():
    md = _render_test_plan()
    external = "\n".join(t for t in TEST_PLAN_TITLES if t != "Sign-off") + "\n"
    with pytest.raises(hs.HumanContentUnlocatable):
        hs.preserve(md, external, hs.guard_for(TP_SPEC, md, human_content_possible=True))


def test_a_document_only_aeos_ever_wrote_has_nothing_to_preserve():
    md = _render_test_plan()
    guard = hs.guard_for(TP_SPEC, md, human_content_possible=False)
    out, kept = hs.preserve(md, "00. Product Home\n", guard)  # unreadable/unstructured text, no human revision
    assert out == md and kept == {}


def test_metadata_cells_before_the_first_heading_are_not_sections():
    titles = ["Data Types", "Classification", "Owner"]
    ext = hs.external_sections("Metadata\nOwner\nBudi\nData Types\nx\nClassification\ny\nOwner\nz\n", titles)
    assert ext == {"Data Types": ["x"], "Classification": ["y"], "Owner": ["z"]}


def test_google_update_keeps_the_human_sign_off_written_in_the_doc():
    md = _render_test_plan()
    drive = FakeDrive()
    with respx.mock(assert_all_called=False) as router:
        drive.install(router)
        client = httpx.Client()
        p = _google(client)
        created = p.execute(_ctx(attachments={"document.docx": markdown_to_docx(md, title="Test Plan")}))
        f = drive.files[created.external_id]
        assert "Sign-off" in f["text"].split("\n")  # the DOCX import keeps the section headings
        drive.human_edit(created.external_id, _human_sign_off(f["text"], "Disetujui QA: Budi, 2026-10-01"))
        # the owner acknowledged the edit: the observed revision is the new baseline (docs_engine acknowledge)
        observed = drive.files[created.external_id]["revision"]
        new_md = md.replace("## Overview", "## Overview\n\nVersi baru yang disetujui.", 1)
        res = p.execute(_ctx(operation="update_doc", existing_external_id=created.external_id,
                             existing_revision=observed,
                             payload={"markdown": new_md, "human_guard": hs.guard_for(TP_SPEC, md, human_content_possible=True)}))
        text = drive.files[created.external_id]["text"]
        assert "Disetujui QA: Budi, 2026-10-01" in text and "Versi baru yang disetujui." in text
        assert res.response["human_sections_kept"] == {"Sign-off": 1}
        client.close()


def test_google_update_without_a_locatable_human_section_is_a_conflict():
    md = _render_test_plan()
    drive = FakeDrive()
    with respx.mock(assert_all_called=False) as router:
        drive.install(router)
        client = httpx.Client()
        p = _google(client)
        created = p.execute(_ctx(attachments={"document.docx": markdown_to_docx(md, title="Test Plan")}))
        drive.human_edit(created.external_id, "Seluruh isi diganti manusia\n")
        with pytest.raises(ProviderConflict) as exc:
            p.execute(_ctx(operation="update_doc", existing_external_id=created.external_id,
                           existing_revision=drive.files[created.external_id]["revision"],
                           payload={"markdown": md, "human_guard": hs.guard_for(TP_SPEC, md, human_content_possible=True)}))
        assert "human-governed content would be lost" in exc.value.message
        assert drive.files[created.external_id]["text"] == "Seluruh isi diganti manusia\n"  # nothing written
        client.close()


@respx.mock
def test_confluence_update_keeps_human_content_of_the_page():
    md = _render_test_plan()
    storage = atl.markdown_to_storage(md).replace(
        "<h2>Sign-off</h2>", "<h2>Sign-off</h2><p>Disetujui QA: Budi &amp; Ani</p>")
    base = "https://corp.atlassian.net"
    respx.get(f"{base}/wiki/api/v2/spaces/S1").mock(return_value=httpx.Response(200, json={"id": "S1"}))
    respx.get(f"{base}/wiki/api/v2/folders/PARENT").mock(return_value=httpx.Response(200, json={"id": "PARENT"}))
    respx.get(f"{base}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 4}, "body": {"storage": {"value": storage}}}))
    put = respx.put(f"{base}/wiki/api/v2/pages/P1").mock(return_value=httpx.Response(200, json={
        "id": "P1", "version": {"number": 5}}))
    cfg = atl.AtlassianConfig(base_url=base, enabled=True, deployment="CLOUD", space_id="S1", parent_id="PARENT")
    p = atl.ConfluenceProvider(cfg, client=httpx.Client(), auth_header="Basic fake")
    res = p.execute(_ctx(provider="confluence", domain="OFFICE", operation="update_doc", existing_external_id="P1",
                         existing_revision="4", payload={"markdown": md, "human_guard": hs.guard_for(TP_SPEC, md, human_content_possible=True)}))
    sent = json.loads(put.calls[0].request.content)["body"]["value"]
    assert "Disetujui QA: Budi &amp; Ani" in sent and res.response["human_sections_kept"] == {"Sign-off": 1}


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


def _publish(env):
    pub = _preview(env)
    assert jobs.execute_publication(env["db"], pub.id, actor_user_id="owner-user", confirm=confirm_of(pub)).allowed
    env["db"].commit()
    jobs.process_publication(env["db"], pub.id)
    env["db"].refresh(pub)
    return pub


def _new_version(env, content):
    db = env["db"]
    plan = db.get(Plan, env["plan"].id)
    v = add_version(db, plan, content)
    approve(db, plan, v)
    db.commit()
    return v


def test_edit_acknowledge_new_version_publish_keeps_the_sign_off(env):
    """§29 end to end with the real Google adapter (respx Drive): a human signs off in the official doc, the owner
    acknowledges the edit, a newer approved version is published — the sign-off is still there."""
    db, drive = env["db"], FakeDrive()
    client = httpx.Client()
    env["monkeypatch"].setitem(providers_mod._overrides, "google", lambda _cfg: _google(client))
    with respx.mock(assert_all_called=False) as router:
        drive.install(router)
        assert _publish(env).status == "PUBLISHED"
        lid = "06-testing/test-plan-tasks"
        ref = db.scalar(select(ExternalReference).where(ExternalReference.logical_id == lid))
        f = drive.files[ref.external_id]
        drive.human_edit(ref.external_id, _human_sign_off(f["text"], "Disetujui QA: Budi, 2026-10-01"))
        first = db.scalar(select(jobs.Publication).where(jobs.Publication.id == ref.last_publication_id))
        assert jobs.reconcile_publication(db, first.id, actor_user_id="owner-user")["human_edits"] == 1
        rec = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == lid))
        rev = db.scalar(select(DocumentRevision).where(DocumentRevision.document_id == rec.id,
                                                       DocumentRevision.kind == "HUMAN_EDIT_INPUT"))
        rev.status, rev.acknowledged_by, rev.acknowledged_at = "ACKNOWLEDGED", "owner-user", utcnow()
        ref.external_revision, ref.sync_status = rev.observed_revision, "STALE"  # what /acknowledge records
        db.commit()
        content = copy.deepcopy(SAMPLE_CONTENT)
        content["features"][0]["acceptance"].append("User dapat mengarsipkan tugas")
        _new_version(env, content)
        pub2 = _preview(env)
        op = next(o for o in jobs._ops_for(db, pub2.id) if o.logical_id == lid)
        assert op.status == "PENDING" and op.payload["human_guard"]["human"] == ["Final Assessment", "Sign-off"]
        assert any("human edit yang sudah di-acknowledge" in n for n in pub2.preview["notes"])
        assert jobs.execute_publication(db, pub2.id, actor_user_id="owner-user", confirm=confirm_of(pub2)).allowed
        db.commit()
        jobs.process_publication(db, pub2.id)
        db.refresh(op)
        assert op.status == "SUCCEEDED", op.error
        text = drive.files[ref.external_id]["text"]
        assert "Disetujui QA: Budi, 2026-10-01" in text and "User dapat mengarsipkan tugas" in text
    client.close()


# ---------------------------------------------------------------------------------------------
# §27 status lifecycle at the provider: a superseded document is marked, never left looking approved


def test_superseded_document_gets_a_banner_and_is_never_deleted(env):
    db = env["db"]
    _publish(env)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["features"] = [content["features"][0]]  # 'auth' is dropped
    _new_version(env, content)
    pub2 = _preview(env)
    ops = {o.logical_id: o for o in jobs._ops_for(db, pub2.id)}
    sup = ops["02-requirements/prd-auth"]
    assert sup.operation == "update_doc" and sup.status == "PENDING" and sup.title.startswith("[SUPERSEDED] ")
    assert "**SUPERSEDED**" in sup.payload["markdown"] and "| Status (saat render) | SUPERSEDED" in \
        sup.payload["markdown"]
    assert jobs.execute_publication(db, pub2.id, actor_user_id="owner-user", confirm=confirm_of(pub2)).allowed
    db.commit()
    jobs.process_publication(db, pub2.id)
    store = env["fakes"]["google"].store["02-requirements/prd-auth"]
    assert "**SUPERSEDED**" in store["payload"]["markdown"]
    rec = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "02-requirements/prd-auth"))
    assert rec.status == "SUPERSEDED"
    assert not any(c[0].startswith("delete") for c in env["fakes"]["google"].calls)
    pub3 = _preview(env)  # same version again: the banner write is done, never repeated
    assert not [o for o in jobs._ops_for(db, pub3.id) if o.logical_id == "02-requirements/prd-auth"
                and o.status == "PENDING"]
    assert any(x["logical_id"] == "02-requirements/prd-auth" for x in pub3.preview["already_done"])


# ---------------------------------------------------------------------------------------------
# §29 DOCX size / §08 capability gates


def test_oversized_docx_is_refused_before_any_request(env, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_DOCX_BYTES", 1000)
    pub = _publish(env)
    docs = [o for o in jobs._ops_for(env["db"], pub.id) if o.operation == "create_doc"]
    assert docs and all(o.status == "FAILED" and o.response["blocked"]["code"] == "DOCX_TOO_LARGE" for o in docs)
    assert all(o.attempts == 0 for o in docs)  # nothing was sent
    assert not any(c[0] == "create_doc" for c in env["fakes"]["google"].calls)


def _gate_state(status: str, checks: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(status=status, reasons=[f"{c['check_id']}: {c['effective_status']}" for c in checks],
                           checks=checks, remedies=[])


def test_failed_publish_gate_check_refuses_execute_and_unknown_is_advisory(env, monkeypatch):
    from aeos.doctor import facade as doctor

    unknown = _gate_state("UNKNOWN", [{"check_id": "adapter.github_token", "tier": "required",
                                       "effective_status": "UNKNOWN"}])
    monkeypatch.setattr(doctor, "capability_state", lambda *a, **k: unknown)
    pub = _preview(env)
    assert pub.preview["capability_gate"]["status"] == "UNKNOWN"
    assert any("CAN_PUBLISH_PERSONAL UNKNOWN" in n for n in pub.preview["notes"])
    failed = _gate_state("BLOCKED", [{"check_id": "secret.no_raw_exposure", "tier": "critical",
                                      "effective_status": "FAILED"}])
    monkeypatch.setattr(doctor, "capability_state", lambda *a, **k: failed)
    with pytest.raises(Blocked) as exc:
        jobs.execute_publication(env["db"], pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    assert exc.value.code == "CAPABILITY_NOT_READY" and "secret.no_raw_exposure" in exc.value.message
    assert sum(len(f.calls) for f in env["fakes"].values()) == 0


def test_office_targets_stay_blocked_without_the_office_gate(settings, db, secrets_memory):
    secrets_memory.put("office/atlassian", json.dumps({"email": "a@b.invalid", "api_token": "atl-token-1234567890"}),
                       actor="test")
    project = make_project(db, domain="OFFICE")
    db.add(ProjectIntegration(project_id=project.id, domain="OFFICE", provider="confluence", purpose="docs",
                              tenant_id="corp.atlassian.net", config={"enabled": True, "deployment": "CLOUD",
                                                                      "space_id": "SP1",
                                                                      "CONFLUENCE_PROJECTS_PARENT_ID": "PARENT"}))
    db.commit()
    with respx.mock(assert_all_mocked=True):  # any OFFICE network call would fail this test
        t = resolve_target(db, project, "docs")
    assert t.status == "BLOCKED" and any("CAN_USE_OFFICE_INTEGRATIONS" in r for r in t.reasons)


# ---------------------------------------------------------------------------------------------
# Confluence attachment upload (IMPLEMENTED_UNTESTED → tested against the documented REST contract)


@respx.mock
def test_confluence_attachment_upload_and_reconcile():
    base = "https://corp.atlassian.net"
    respx.get(f"{base}/wiki/api/v2/spaces/S1").mock(return_value=httpx.Response(200, json={"id": "S1"}))
    respx.get(f"{base}/wiki/api/v2/folders/PARENT").mock(return_value=httpx.Response(200, json={"id": "PARENT"}))
    up = respx.put(f"{base}/wiki/rest/api/content/P1/child/attachment").mock(return_value=httpx.Response(200, json={
        "results": [{"id": "att-77", "title": "aeos-context.png"}]}))
    cfg = atl.AtlassianConfig(base_url=base, enabled=True, deployment="CLOUD", space_id="S1", parent_id="PARENT")
    p = atl.ConfluenceProvider(cfg, client=httpx.Client(), auth_header="Basic fake")
    name = atl.attachment_filename("context")
    res = p.execute(_ctx(provider="confluence", domain="OFFICE", operation="upload_attachment",
                         logical_id="03-architecture-overview#context", parent_external_id="P1",
                         payload={"filename": name}, attachments={name: b"\x89PNG fake"}))
    req = up.calls[0].request
    assert req.headers["X-Atlassian-Token"] == "no-check" and req.headers["Authorization"] == "Basic fake"
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    assert b'filename="aeos-context.png"' in req.content and b"\x89PNG fake" in req.content
    assert res.external_id == "att-77" and res.kind == "attachment"
    respx.get(f"{base}/wiki/rest/api/content/P1/child/attachment").mock(return_value=httpx.Response(200, json={
        "results": [{"id": "att-77"}]}))
    found = p.reconcile(_ctx(provider="confluence", operation="upload_attachment", parent_external_id="P1",
                             payload={"filename": name}))
    assert found is not None and found.external_id == "att-77"


# ---------------------------------------------------------------------------------------------
# Schema plans: issue links point at documents that exist (feature key "F1" → prd-f1, test-plan-tp1)


def test_issue_links_and_feature_doc_links_use_document_keys(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, _v = make_plan(db, project, schema_sample_content(), tasks=[
        {"task_key": "T1", "feature_key": "F1", "title": "API tugas", "acceptance_criteria": ["T1 works"]}])
    pub = jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="owner-user", render_visuals=False)
    db.commit()
    # §41 readiness: the project-creation set is complete; F2's derived Test Plan has no scope (no task, no plan)
    readiness = pub.preview["documentation_readiness"]
    assert readiness["PROJECT_CREATION"] == []
    assert readiness["FEATURE_APPROVAL"] == [{"logical_doc_id": "06-testing/test-plan-f2", "title": "Test Plan — Login",
                                              "sections": ["Scope"]}]
    assert any("documentation pending" in n and "Test Plan — Login: Scope" in n for n in pub.preview["notes"])
    issue = next(o for o in jobs._ops_for(db, pub.id) if o.logical_id == "issue:task:T1")
    assert "`02-requirements/prd-f1`" in issue.payload["body"] and "`06-testing/test-plan-tp1`" in issue.payload["body"]
    assert jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub)).allowed
    db.commit()
    jobs.process_publication(db, pub.id)
    for lid in ("02-requirements/prd-f1", "06-testing/test-plan-tp1"):
        doc = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == lid))
        assert [i["logical_id"] for i in doc.links["issues"]] == ["issue:task:T1"], lid
    assert fakes["github-issues"].calls


def test_idempotency_selftest_records_proof_only_when_every_test_passes(settings, db):
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import make_context, run_check
    from aeos.models.evidence import EvidenceArtifact
    from aeos.publisher import selftest
    from tests.unit.test_inventory_fakes import base_runner

    seen: list = []

    def failing(argv, cwd):
        seen.append(argv)
        return selftest.RunOutcome(1, "1 failed, 5 passed")

    out = selftest.run_idempotency_selftest(db, actor="owner", runner=failing, commit="f" * 40)
    assert out["status"] == "FAILED" and out["evidence_id"] is None
    assert not db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == selftest.EVIDENCE_KIND))
    assert all(t in seen[0] for t in selftest.TESTS) and seen[0][1:3] == ["-m", "pytest"]
    load_all()
    check = REGISTRY["recovery.publication_idempotency"]
    unknown = run_check(check, make_context(settings, db, runner=base_runner()))
    assert unknown.status == "UNKNOWN" and "publish idempotency-test" in unknown.remediation["command"]
    # passing tests over a working tree with uncommitted code: the evidence could not name the code it ran
    dirty = selftest.run_idempotency_selftest(db, actor="owner", runner=lambda a, c: selftest.RunOutcome(0, "6 passed"),
                                              commit="f" * 40, uncommitted=["backend/aeos/publisher/jobs.py"])
    assert dirty["status"] == "NOT_RECORDED" and dirty["evidence_id"] is None and "uncommitted" in dirty["reason"]
    assert not db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == selftest.EVIDENCE_KIND))
    out = selftest.run_idempotency_selftest(db, actor="owner", runner=lambda a, c: selftest.RunOutcome(0, "6 passed"),
                                            commit="f" * 40, uncommitted=[])
    db.commit()
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert out["status"] == "PASSED" and ev.trusted and ev.origin == "collector" and ev.mode == "LIVE"
    assert ev.environment["commit"] == "f" * 40
    verified = run_check(check, make_context(settings, db, runner=base_runner()))
    assert verified.status == "VERIFIED" and ("f" * 12) in verified.summary  # the proof names its commit


def test_recovery_queue_reports_runs_without_lease_and_stale_in_flight_publications(settings, db):
    """§08 Recovery "lease/queue restart": nothing may keep running without an owner after a restart."""
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import make_context, run_check
    from aeos.models.publishing import PublicationOperation
    from aeos.models.workforce import Lease, WorkerRun
    from tests.unit.test_inventory_fakes import base_runner

    load_all()
    spec = REGISTRY["recovery.queue"]

    def run():
        return run_check(spec, make_context(settings, db, runner=base_runner()))

    assert run().status == "VERIFIED"
    now = utcnow()
    lease = Lease(task_id="t1", run_id="r1", owner="ctl", acquired_at=now, expires_at=now + timedelta(minutes=2),
                  state="ACTIVE")
    db.add(lease)
    db.flush()
    wr = WorkerRun(id="r1", task_id="t1", project_id="proj-777", domain="PERSONAL", execution_status="RUNNING",
                   lease_id=lease.id, mode="LIVE")
    db.add_all([wr, WorkerRun(task_id="t2", project_id="proj-777", domain="PERSONAL", execution_status="RUNNING",
                              mode="DEMO")])  # DEMO fixtures are not part of the live queue
    db.commit()
    assert run().status == "VERIFIED"
    lease.state = "RELEASED"  # the scheduler lost the run: a duplicate could be started
    db.commit()
    res = run()
    assert res.status == "FAILED" and res.error_code == "QUEUE_NOT_RECOVERED"
    assert res.observed_evidence["runs_without_active_lease"] == 1
    wr.execution_status = "CANCELLED"
    db.add(PublicationOperation(publication_id="pub-x", provider="google-drive", operation="create_doc",
                                logical_id="root", idempotency_key="k1", payload_hash="h1", status="IN_FLIGHT",
                                last_attempt_at=now - jobs.IN_FLIGHT_STALE_AFTER - timedelta(minutes=1)))
    db.commit()
    res = run()
    assert res.status == "FAILED" and res.observed_evidence["stale_in_flight_publication_ops"] == 1
    assert res.observed_evidence["runs_without_active_lease"] == 0
    assert "proj-777" not in json.dumps(res.as_dict())  # counts only, no ids of either domain


# ---------------------------------------------------------------------------------------------
# Review fixes: gate semantics, OFFICE gate at execute time, DRAFT stays DRAFT


def _check_row(check_id: str, status: str):
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.models.inventory import CheckResult

    load_all()
    spec = REGISTRY[check_id]
    now = utcnow()
    return CheckResult(check_id=check_id, title=spec.title, group=spec.group, component_id=spec.component_id,
                       scope=dict(spec.scope), status=status, severity="HIGH", summary=f"unit: {status}",
                       observed_evidence={}, last_checked_at=now, expires_at=now + timedelta(hours=1),
                       invalidators=list(spec.invalidators), fingerprint=None, remediation={})


def test_observed_missing_critical_publish_dependency_refuses_execute(env):
    """§08 "operasi dengan dependency critical gagal tidak dijalankan", with the REAL gate evaluation: a Doctor run
    that observed the Google token MISSING blocks CAN_PUBLISH_PERSONAL, so the run is refused before any write.
    Checks that were never run (UNKNOWN) stay advisory."""
    db = env["db"]
    pub = _preview(env)
    assert pub.preview["capability_gate"]["status"] == "UNKNOWN" and pub.preview["capability_gate"][
        "failed_checks"] == []
    db.add(_check_row("adapter.google_oauth", "MISSING"))
    db.commit()
    pub = _preview(env)
    gate = pub.preview["capability_gate"]
    assert gate["status"] == "BLOCKED" and gate["failed_checks"] == ["adapter.google_oauth"]
    assert any("eksekusi akan ditolak" in n for n in pub.preview["notes"])
    with pytest.raises(Blocked) as exc:
        jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    assert exc.value.code == "CAPABILITY_NOT_READY" and "adapter.google_oauth" in exc.value.message
    assert sum(len(f.calls) for f in env["fakes"].values()) == 0


def test_office_gate_is_enforced_again_at_execute_time(settings, db, secrets_memory, monkeypatch):
    """§23 OFFICE default deny: a company authorisation withdrawn after the preview stops the run (no OFFICE call)."""
    from aeos.publisher import targets as targets_mod

    install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    gate = {"ready": True}
    monkeypatch.setattr(targets_mod, "office_gate", lambda session, project: (
        (True, []) if gate["ready"] else (False, ["CAN_USE_OFFICE_INTEGRATIONS BLOCKED: otorisasi dicabut"])))
    # the target is one the company approved (approved_targets: test_office_company_scope)
    monkeypatch.setattr(targets_mod, "_company_approved", lambda session, family, acfg: (True, "test: approved"))
    secrets_memory.put("office/atlassian", json.dumps({"email": "a@b.invalid", "api_token": "atl-token-1234567890"}),
                       actor="test")
    project = make_project(db, domain="OFFICE")
    db.add(ProjectIntegration(project_id=project.id, domain="OFFICE", provider="confluence", purpose="docs",
                              tenant_id="corp.atlassian.net", config={"enabled": True, "deployment": "CLOUD",
                                                                      "space_id": "SP1",
                                                                      "CONFLUENCE_PROJECTS_PARENT_ID": "PARENT"}))
    db.commit()
    plan, _v = make_plan(db, project)
    pub = jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="owner", options={"targets": ["docs"]},
                              render_visuals=False)
    db.commit()
    assert pub.preview["capability_gate"]["status"] == "READY" and not pub.preview["blocked_targets"]
    gate["ready"] = False  # the company authorisation is withdrawn after the preview
    with pytest.raises(Blocked) as exc:
        jobs.execute_publication(db, pub.id, actor_user_id="owner", confirm=confirm_of(pub))
    assert exc.value.code == "CAPABILITY_NOT_READY" and "CAN_USE_OFFICE_INTEGRATIONS" in exc.value.message
    assert not fakes["confluence"].calls


def test_a_published_draft_stays_draft(settings, db, secrets_memory, monkeypatch):
    """§19/§28: a DRAFT of the approved version (operations proposal before production-validation evidence)
    is published with its DRAFT label but never becomes PUBLISHED (= approved and published) by publishing it."""
    install_fakes(monkeypatch)
    use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, _v = make_plan(db, project, schema_sample_content(), tasks=[
        {"task_key": "T1", "feature_key": "F1", "title": "API tugas", "acceptance_criteria": ["T1 works"]}])
    pub = jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="owner-user", render_visuals=False)
    db.commit()
    assert jobs.execute_publication(db, pub.id, actor_user_id="owner-user", confirm=confirm_of(pub)).allowed
    db.commit()
    jobs.process_publication(db, pub.id)
    ops_doc = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "08-operational-overview"))
    home = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "00-product-home"))
    assert ops_doc.status == "DRAFT" and ops_doc.links.get("external_id")  # in the folder, still a draft
    assert home.status == "PUBLISHED"
    assert db.scalar(select(ExternalReference).where(ExternalReference.logical_id == "08-operational-overview"))


def test_pii_minimisation_never_mangles_record_ids():
    uid = "41111111-1111-1111-a111-111111111111"  # its digit run is a Luhn-valid card number
    assert redact_pii(f"operation_id {uid}") == f"operation_id {uid}"
    assert redact_pii("sha256:" + "4111111111111111" * 4) == "sha256:" + "4111111111111111" * 4
    assert redact_pii("kartu 4111 1111 1111 1111") == "kartu [PII:CARD]"
    assert redact_pii("mail 0123456789abcdef0123456789abcdef@example.com") == "mail [PII:EMAIL]"


def test_every_operation_gate_is_consulted_by_its_operation():
    """§08 "operasi dengan dependency critical gagal tidak dijalankan": every capability gate (except the dashboard,
    which no operation runs) has a call site that asks the Doctor before the operation - CAN_VALIDATE_STAGING is
    consulted by the staging observation and the staging screenshot capture (aeos.delivery.deployment).
    Static check over the backend source, so a gate that is computed but never consulted fails here."""
    import re as _re
    from pathlib import Path

    from aeos.core.enums import Capability

    root = Path(jobs.__file__).resolve().parents[1]
    text = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py")
                     if "doctor" not in p.parts and p.name != "enums.py")
    consulted = set(_re.findall(r"Capability\.(CAN_[A-Z_]+)", text))
    exempt = {"CAN_OPEN_DASHBOARD"}
    # CAN_PLAN_PERSONAL is the planning module's own audit item (§08 gates, owning_module planning); listed here
    # so this cluster's gates (publish / office) can never silently lose their call site.
    other_module = {"CAN_PLAN_PERSONAL"}
    missing = {c.value for c in Capability} - exempt - other_module - consulted
    assert missing == set(), f"gates without a call site: {sorted(missing)}"
    assert {"CAN_PUBLISH_PERSONAL", "CAN_USE_OFFICE_INTEGRATIONS", "CAN_VALIDATE_STAGING"} <= consulted
