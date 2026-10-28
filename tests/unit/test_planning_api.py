"""Planning + policy HTTP API (§05 steps 7-10, §35 backend enforcement).

§50 rows: "Submit prompt tanpa domain" (rejected, no model call), "VIEWER memanggil write endpoint
langsung" (rejected), "Approval PlanVersion lama" (409 + latest version), "Dashboard Personal mencoba
run/evidence Office" (404), "Fable Max quota habis"/bridge not proven (manual path, no API)."""

from __future__ import annotations

import pytest

from aeos.core.enums import Domain
from aeos.planning import contracts, runner
from tests.conftest import VIEWER_PW, create_viewer
from tests.unit.test_planning_support import FakeBridge, make_project


@pytest.fixture(autouse=True)
def _join_threads():
    yield
    runner.join_all(10)


def _create(client, project_id, prompt="Aplikasi pencatat keuangan pribadi dengan ekspor CSV"):
    r = client.post(f"/api/projects/{project_id}/plans", json={"title": "Rencana API", "prompt": prompt})
    assert r.status_code == 201, r.text
    return r.json()


def _run_to_ready(client, plan_id):
    r = client.post(f"/api/plans/{plan_id}/run")
    assert r.status_code == 202, r.text
    runner.join_all(20)
    plan = client.get(f"/api/plans/{plan_id}").json()
    assert plan["state"] == "READY_FOR_HUMAN_REVIEW", plan["state_reason"]
    return plan


def test_create_requires_selected_domain(admin_client, db):
    project = make_project(db)
    r = admin_client.post(f"/api/projects/{project.id}/plans", json={"title": "x", "prompt": "y"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_viewer_cannot_create_or_decide(client, db):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    project = make_project(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post(f"/api/projects/{project.id}/plans", json={"title": "x", "prompt": "y"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"
    assert client.post("/api/decisions", json={"kind": "EXCEPTION"}).status_code == 403
    # every other write of the module is refused for VIEWER by the backend (not just hidden in the UI)
    for path, body in (("/api/decisions/x/revoke", {"reason": "weg damit"}),
                       ("/api/decision-requests", {"kind": "BUDGET", "title": "naik"}),
                       ("/api/decision-requests/x/decline", {"note": "tidak perlu"}),
                       ("/api/policy/snapshot/refresh", {}), ("/api/plans/x/run", {}),
                       ("/api/plans/x/manual-result", {}), ("/api/plans/x/compile", {}),
                       ("/api/plans/x/versions", {"parent_version_id": "v", "content": {}, "rationale": "edit"}),
                       ("/api/plans/x/decision", {"action": "APPROVE", "version_id": "v", "version_hash": "h"})):
        r = client.post(path, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE", path
    assert client.get("/api/plans").status_code == 200  # read stays available


def test_full_flow_create_run_approve_dag(personal_client, db, monkeypatch, tmp_path):
    bridge = FakeBridge(monkeypatch, tmp_path)
    project = make_project(db)
    plan = _create(personal_client, project.id)
    assert plan["state"] == "CONTEXT_READY" and plan["context"]["facts"] and plan["allowed"]["run"]
    detail = _run_to_ready(personal_client, plan["id"])
    assert len(bridge.calls) == 4
    stages = {s["stage"]: s["latest"] for s in detail["stages"]}
    assert stages["CHALLENGE_REVIEW"]["model_requested"] == "opus"  # ADR-0131: never fable
    assert stages["CHALLENGE_REVIEW"]["model_resolved"] == "claude-opus-5-5"
    assert stages["SYNTHESIS"]["model_matches_policy"] is True
    assert detail["findings"]["missing_dependencies"] and detail["verdict"]["verdict"] == "PASS-STATIC"
    assert detail["needs_decision"] is True and len(detail["versions"]) == 2

    listing = personal_client.get("/api/plans").json()
    assert listing["needs_decision"] == 1 and listing["items"][0]["id"] == plan["id"]
    queue = personal_client.get("/api/decisions/queue").json()
    assert any(i["kind"] == "PLAN" and i["target"]["id"] == plan["id"] for i in queue["items"])

    v1, v2 = detail["versions"]
    stale = personal_client.post(f"/api/plans/{plan['id']}/decision", json={
        "action": "APPROVE", "version_id": v1["id"], "version_hash": v1["manifest_hash"]})
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "STALE_PLAN_VERSION" and err["details"]["latest_version_id"] == v2["id"]

    diff = personal_client.get(f"/api/plans/{plan['id']}/versions/{v2['id']}/diff").json()
    assert "overview" in diff["changed_sections"]
    full = personal_client.get(f"/api/plans/{plan['id']}/versions/{v2['id']}").json()
    assert full["hash_valid"] is True and full["is_current"] is True

    preview = personal_client.get(f"/api/plans/{plan['id']}/dag").json()
    assert preview["compiled"] is False and len(preview["nodes"]) == 2

    ok = personal_client.post(f"/api/plans/{plan['id']}/decision", json={
        "action": "APPROVE_AND_PUBLISH", "version_id": v2["id"], "version_hash": v2["manifest_hash"],
        "targets": [{"provider": "google-drive", "root_id": "ROOT"}, {"provider": "google-docs", "root_id": "ROOT"}],
        "rationale": "siap"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["plan"]["state"] == "APPROVED" and body["compile"]["tasks"] == 2
    assert {"create_folder", "create_doc"} <= set(body["decision"]["allowed_operations"])

    dag = personal_client.get(f"/api/plans/{plan['id']}/dag").json()
    assert dag["compiled"] is True and dag["approved"] is True and {e["id"] for e in dag["edges"]} == {"T1->T2"}
    assert personal_client.post(f"/api/plans/{plan['id']}/compile", json={}).json()["created"] is False

    decisions = personal_client.get("/api/decisions?kind=APPROVAL").json()["items"]
    assert decisions[0]["target_hash"] == v2["manifest_hash"]
    revoked = personal_client.post(f"/api/decisions/{decisions[0]['id']}/revoke", json={"reason": "salah target"})
    assert revoked.status_code == 200 and revoked.json()["status"] == "INVALIDATED"
    after = personal_client.get(f"/api/plans/{plan['id']}").json()
    assert after["state"] == "READY_FOR_HUMAN_REVIEW" and after["approved_version"] is None
    assert personal_client.post(f"/api/plans/{plan['id']}/compile", json={}).status_code == 409


def test_cross_domain_plan_is_not_found(personal_client, db, monkeypatch, tmp_path):
    FakeBridge(monkeypatch, tmp_path)
    plan = _create(personal_client, make_project(db).id)
    personal_client.select_domain(Domain.OFFICE)
    assert personal_client.get(f"/api/plans/{plan['id']}").status_code == 404
    assert personal_client.post(f"/api/plans/{plan['id']}/run").status_code == 404
    assert personal_client.get("/api/plans").json()["items"] == []


def test_office_run_blocked_before_model_call(personal_client, db, monkeypatch, tmp_path):
    bridge = FakeBridge(monkeypatch, tmp_path)
    personal_client.select_domain(Domain.OFFICE)
    plan = _create(personal_client, make_project(db, domain=Domain.OFFICE).id)
    r = personal_client.post(f"/api/plans/{plan['id']}/run")
    assert r.status_code == 409 and r.json()["error"]["code"] == "OFFICE_PLANNING_BLOCKED"
    assert r.json()["error"]["details"]["reasons"]
    runner.join_all(5)
    assert bridge.calls == []
    assert personal_client.get(f"/api/plans/{plan['id']}").json()["state"] == "NEEDS_DECISION"


def test_manual_fallback_over_api(personal_client, db, monkeypatch, tmp_path):
    bridge = FakeBridge(monkeypatch, tmp_path, blocked={"PRIMARY_PLANNING"})
    plan = _create(personal_client, make_project(db).id)
    personal_client.post(f"/api/plans/{plan['id']}/run")
    runner.join_all(20)
    detail = personal_client.get(f"/api/plans/{plan['id']}").json()
    assert detail["state"] == "AWAITING_MANUAL_CLAUDE" and detail["manual"]["package_path"]
    missing = personal_client.post(f"/api/plans/{plan['id']}/manual-result")
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "MANUAL_RESULT_MISSING"
    bridge.complete_manual(detail["manual"]["package_id"])
    bridge.blocked.clear()
    assert personal_client.post(f"/api/plans/{plan['id']}/manual-result").status_code == 202
    runner.join_all(20)
    assert personal_client.get(f"/api/plans/{plan['id']}").json()["state"] == "READY_FOR_HUMAN_REVIEW"


def test_human_edit_endpoint_and_request_revision(personal_client, db, monkeypatch, tmp_path):
    FakeBridge(monkeypatch, tmp_path)
    plan = _create(personal_client, make_project(db).id)
    detail = _run_to_ready(personal_client, plan["id"])
    cur = detail["current_version"]
    content = personal_client.get(f"/api/plans/{plan['id']}/versions/{cur['id']}").json()["content"]
    content["overview"]["vision"] = "visi pemilik"
    r = personal_client.post(f"/api/plans/{plan['id']}/versions",
                             json={"parent_version_id": cur["id"], "content": content, "rationale": "edit visi"})
    assert r.status_code == 201 and r.json()["source"] == "HUMAN_EDIT"
    bad = personal_client.post(f"/api/plans/{plan['id']}/versions",
                               json={"parent_version_id": cur["id"], "content": content, "rationale": "basis lama"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "STALE_PLAN_VERSION"
    new = r.json()
    rev = personal_client.post(f"/api/plans/{plan['id']}/decision", json={
        "action": "REQUEST_REVISION", "version_id": new["id"], "version_hash": new["manifest_hash"],
        "rationale": "tambahkan bagian keamanan"})
    assert rev.status_code == 200 and rev.json()["plan"]["state"] == "REVISION_REQUESTED"


def test_generic_decision_endpoint_rules(personal_client, db):
    base = {"auth_scope": {"scope": "repo"}, "rationale": "alasan yang cukup panjang"}
    r = personal_client.post("/api/decisions", json={**base, "kind": "EXCEPTION", "target_type": "repository",
                                                     "target_id": "o/r"})
    assert r.status_code == 422  # exceptions need an explicit expiry
    r = personal_client.post("/api/decisions", json={"kind": "EXCEPTION", "target_type": "repository",
                                                     "target_id": "o/r", "rationale": "alasan yang cukup panjang"})
    assert r.status_code == 422  # explicit scope required
    r = personal_client.post("/api/decisions", json={**base, "kind": "WRITE_ENABLEMENT", "provider": "jira",
                                                     "enabled": True})
    assert r.status_code == 422  # jira is not a PERSONAL provider
    # legacy spelling "google-docs" is accepted on input and recorded on the one provider id "google-drive"
    r = personal_client.post("/api/decisions", json={**base, "kind": "WRITE_ENABLEMENT", "provider": "google-docs",
                                                     "enabled": True})
    assert r.status_code == 201 and r.json()["details"]["enabled"] is True
    assert r.json()["target_id"] == "google-drive"
    status = {i["provider"]: i for i in personal_client.get("/api/policy/write-enablement").json()["items"]}
    assert set(status) == {"google-drive", "github-issues", "github-repo"}
    assert status["google-drive"]["effective"] is True and status["github-repo"]["effective"] is False
    r = personal_client.post("/api/decisions", json={**base, "kind": "WRITE_ENABLEMENT", "provider": "google-drive",
                                                     "enabled": False})
    assert r.status_code == 201
    status = {i["provider"]: i for i in personal_client.get("/api/policy/write-enablement").json()["items"]}
    assert status["google-drive"]["flag"] is False and status["google-drive"]["effective"] is False
    r = personal_client.post("/api/decisions", json={**base, "kind": "POLICY", "office_authorization": True,
                                                     "company_reference": "surat 1", "allowed_providers": ["jira"]})
    assert r.status_code == 422  # office authorisation only in the OFFICE domain
    personal_client.select_domain(Domain.OFFICE)
    r = personal_client.post("/api/decisions", json={**base, "kind": "POLICY", "office_authorization": True,
                                                     "company_reference": "surat persetujuan 1",
                                                     "allowed_providers": ["jira"]})
    assert r.status_code == 201
    office = personal_client.get("/api/policy/office").json()
    assert office["office_authorization"] is True and office["authorized_providers"] == ["jira"]
    assert office["external_worker_allowed"] is False  # authorisation alone does not open worker routes
    personal_client.select_domain(Domain.PERSONAL)
    listing = personal_client.get("/api/decisions").json()
    assert all(i["domain"] in (None, "PERSONAL") for i in listing["items"])


@pytest.mark.parametrize("details", [{"office_authorization": True}, {"office_policy": {"external_worker_allowed": True}},
                                     {"enabled": True}, {"allowed_providers": ["jira"]}])
def test_policy_meaning_cannot_be_smuggled_through_details(personal_client, db, details):
    """A company authorisation needs the dedicated fields (company reference + explicit providers); free-form
    details can never mint one or relax OFFICE defaults (fail closed)."""
    personal_client.select_domain(Domain.OFFICE)
    r = personal_client.post("/api/decisions", json={
        "kind": "POLICY", "target_type": "route", "target_id": "r1", "auth_scope": {"route": "r1"},
        "rationale": "kebijakan route kantor", "details": details})
    assert r.status_code == 422 and r.json()["error"]["code"] == "RESERVED_DETAIL_KEYS"
    office = personal_client.get("/api/policy/office").json()
    assert office["office_authorization"] is False and office["external_worker_allowed"] is False


def test_office_decision_metadata_never_reaches_personal_views(personal_client, db):
    """§35/§50 "Viewer mengakses ... Office": OFFICE decision ids/targets/counts stay in OFFICE sessions —
    the policy snapshot, the OFFICE policy projection and decision requests are scoped."""
    base = {"auth_scope": {"company": "ACME"}, "rationale": "otorisasi perusahaan tercatat"}
    personal_client.select_domain(Domain.OFFICE)
    r = personal_client.post("/api/decisions", json={**base, "kind": "POLICY", "office_authorization": True,
                                                     "company_reference": "surat persetujuan 7",
                                                     "allowed_providers": ["confluence"]})
    assert r.status_code == 201
    office_decision = r.json()["id"]
    req = personal_client.post("/api/decision-requests", json={"kind": "RISK_ACCEPTANCE", "title": "Risiko kantor"})
    assert req.status_code == 201
    office_snap = personal_client.get("/api/policy/snapshot").json()["active"]
    assert office_decision in {d["id"] for d in office_snap["decisions"]} and office_snap["decision_count"] == 1
    assert personal_client.get("/api/policy/office").json()["authorization_decision_id"] == office_decision

    personal_client.select_domain(Domain.PERSONAL)
    snap = personal_client.get("/api/policy/snapshot").json()["active"]
    assert snap["decisions"] == [] and snap["decision_count"] == 0  # no ids, no counts
    assert snap["content_hash"] == office_snap["content_hash"]  # same snapshot; the hash reveals no content
    office = personal_client.get("/api/policy/office").json()
    assert office["authorization_decision_id"] is None and office["applied_decision_ids"] == []
    assert office["decision_refs_hidden"] is True and office["external_worker_allowed"] is False
    assert personal_client.get(f"/api/decisions/{office_decision}").status_code == 404
    rid = req.json()["id"]
    assert personal_client.post(f"/api/decision-requests/{rid}/decline", json={"note": "bukan urusan"}).status_code == 404
    assert rid not in {i["id"] for i in personal_client.get("/api/decision-requests").json()["items"]}
    r = personal_client.post("/api/decisions", json={
        "kind": "RISK_ACCEPTANCE", "target_type": "risk", "target_id": "R-1", "auth_scope": {"risk": "R-1"},
        "rationale": "menerima risiko residual", "expires_at": "2099-01-01T00:00:00Z", "request_id": rid})
    assert r.status_code == 404  # an OFFICE request cannot be resolved from a PERSONAL session


def test_policy_read_endpoints(personal_client, db, monkeypatch, tmp_path):
    from aeos.policy import seed
    from tests.unit import mci_support

    mci_support.isolate(monkeypatch, mci_support.fake_repo(tmp_path))  # MCI not supplied (not the owner's live file)
    snap = personal_client.get("/api/policy/snapshot").json()
    assert snap["mci_status"] == "UNVERIFIED"
    seed.run(db)
    db.commit()
    snap = personal_client.get("/api/policy/snapshot").json()
    assert snap["status"] == "ACTIVE" and snap["active"]["content_hash"].startswith("sha256:")
    mci = personal_client.get("/api/policy/mci").json()
    assert mci["status"] == "UNVERIFIED" and mci["compliance"] == "NOT-PROVEN" and len(mci["risk_classes"]) == 5
    adrs = personal_client.get("/api/adrs").json()["items"]
    assert any(a["key"] == "ADR-0003" for a in adrs)
    adr = personal_client.get("/api/adrs/ADR-0003").json()
    assert "Decision" in adr["sections"] and adr["linked_decisions"][0]["kind"] == "EXCEPTION"
    personal_client.select_domain(Domain.OFFICE)  # the PERSONAL-scoped ADR-0003 decision stays in PERSONAL
    assert personal_client.get("/api/adrs/ADR-0003").json()["linked_decisions"] == []
    personal_client.select_domain(Domain.PERSONAL)
    assert personal_client.get("/api/adrs/ADR-9999").status_code == 404
    req = personal_client.post("/api/decision-requests", json={"kind": "BUDGET", "title": "Naikkan budget",
                                                               "reason": "kuota habis"})
    assert req.status_code == 201
    queue = personal_client.get("/api/decisions/queue").json()
    assert queue["counts"].get("DECISION_REQUEST") == 1
    dec = personal_client.post(f"/api/decision-requests/{req.json()['id']}/decline", json={"note": "belum perlu"})
    assert dec.status_code == 200 and dec.json()["status"] == "DECLINED"


def test_contract_files_are_in_sync_and_valid_json_schema():
    from jsonschema import Draft202012Validator

    for name, filename in contracts.SCHEMA_FILES.items():
        path = contracts.contracts_dir() / filename
        assert path.read_text(encoding="utf-8") == contracts.render(name), f"run `aeos plan schemas --write` ({filename})"
        schema = contracts.schema(name)
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"].endswith("2020-12/schema")

        def walk(node):
            if isinstance(node, dict):
                assert "$ref" not in node and "minLength" not in node and "minimum" not in node
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(schema)


def test_cli_policy_and_plan_commands_are_ascii(settings):
    from typer.testing import CliRunner

    from aeos.cli import app

    cli = CliRunner()
    for args in (["policy", "adrs", "--load"], ["policy", "snapshot", "--refresh"], ["policy", "decisions"],
                 ["plan", "schemas"]):
        res = cli.invoke(app, args)
        assert res.exit_code == 0, (args, res.output)
        assert res.output.isascii(), args
    res = cli.invoke(app, ["policy", "decisions", "--kind", "EXCEPTION"])
    assert "EXCEPTION" in res.output or "No decisions" in res.output


def test_cli_plan_create_status_and_domain_guard(settings, db, tmp_path):
    """CLI: explicit --domain (never inferred, U-05), BOM-safe prompt files (Windows editors), ASCII output,
    and bracketed DATA in titles is printed verbatim (no markup interpretation)."""
    from typer.testing import CliRunner

    from aeos.cli import app
    from tests.conftest import create_admin

    create_admin(db)
    project = make_project(db, key="cli-proj")
    prompt_file = tmp_path / "ide.md"
    prompt_file.write_bytes("﻿Aplikasi catatan pribadi dengan ekspor Markdown dan pencarian".encode())
    cli = CliRunner()
    res = cli.invoke(app, ["plan", "create", "--project", "cli-proj", "--domain", "OFFICE", "--title", "x",
                           "--prompt", "y", "--actor", "owner"])
    assert res.exit_code == 1 and "DOMAIN_MISMATCH" in res.output
    res = cli.invoke(app, ["plan", "create", "--project", "cli-proj", "--domain", "PERSONAL", "--title",
                           "Rencana [/bold] CLI", "--prompt-file", str(prompt_file), "--actor", "owner"])
    assert res.exit_code == 0, res.output
    assert res.output.isascii() and "CONTEXT_READY" in res.output
    from aeos.models.planning import Plan, PlanningContext

    db.expire_all()
    plan = db.query(Plan).filter(Plan.project_id == project.id).one()
    assert db.get(PlanningContext, plan.context_id).prompt.startswith("Aplikasi")  # BOM stripped
    res = cli.invoke(app, ["plan", "status", plan.id])
    assert res.exit_code == 0 and res.output.isascii() and "Rencana [/bold] CLI" in res.output
    res = cli.invoke(app, ["plan", "create", "--project", "cli-proj", "--domain", "PERSONAL", "--title", "x",
                           "--prompt", "y", "--actor", "nobody"])
    assert res.exit_code != 0  # the actor must be an active SUPER_ADMIN
