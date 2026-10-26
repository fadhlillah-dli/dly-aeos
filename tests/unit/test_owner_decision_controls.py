"""Owner decisions that are recordable in the product (no hand-made API call, no build change).

* Authenticated read-only screenshot capture: an owner POLICY choice (preset ``screenshot_authenticated_capture``,
  target ``screenshot_capture/PERSONAL``, ``details.authenticated_capture`` ALLOWED|DENIED + ``details.scope``), read
  by the collector status through the policy facade. UNDECIDED until decided; the collector never logs in whatever
  the decision says (this build has no authenticated capture path).
* Role-evaluation threshold: ``aeos work role-eval-threshold`` (password) and the current value on
  ``GET /api/workforce/roles``.
* ``worker.route_behaviour`` threshold: ``aeos policy route-threshold`` records the Control Center form's shape.
* ``aeos policy decisions`` prints decision ids (the argument of ``aeos policy revoke``).
"""

from __future__ import annotations

from sqlalchemy import select
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.models.audit import AuditEvent
from aeos.models.decisions import Decision
from tests.conftest import ADMIN_PW, VIEWER_PW, AuthedClient, create_admin, create_viewer

runner = CliRunner()
PRESET = "screenshot_authenticated_capture"


def _capture(client: AuthedClient) -> dict:
    return client.get("/api/docs/visuals/status").json()["screenshot_collector"]["authenticated_capture"]


def _body(**over) -> dict:
    return {"kind": "POLICY", "policy_preset": PRESET, "auth_scope": {"kolektor": "screenshot"},
            "rationale": "rute Control Center boleh ditangkap read-only", **over}


def _cli():
    from aeos.cli import app

    return app


# ---------------------------------------------------------------- authenticated screenshot capture


def test_screenshot_capture_decision_is_undecided_until_the_owner_records_it(personal_client: AuthedClient, db):
    cap = _capture(personal_client)
    assert (cap["status"], cap["decision_id"], cap["capture_mode"]) == ("UNDECIDED", None, "UNAUTHENTICATED")
    assert cap["authenticated_capture_built"] is False and "aeos policy decide" in cap["record_hint"]
    shape = personal_client.get("/api/decisions/shapes").json()["policy_presets"][PRESET]
    assert (shape["target_type"], shape["target_id"], shape["choices"], shape["expiry_required"]) == (
        "screenshot_capture", "PERSONAL", ["ALLOWED", "DENIED"], False)
    # no default choice, and ALLOWED is refused without the pages it covers
    none = personal_client.post("/api/decisions", json=_body())
    assert none.status_code == 422 and none.json()["error"]["code"] == "POLICY_PRESET_DETAILS_INVALID"
    bad = personal_client.post("/api/decisions", json=_body(details={"authenticated_capture": "MAYBE"}))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "POLICY_PRESET_DETAILS_INVALID"
    unscoped = personal_client.post("/api/decisions", json=_body(details={"authenticated_capture": "ALLOWED"}))
    assert unscoped.status_code == 422 and unscoped.json()["error"]["code"] == "POLICY_PRESET_SCOPE_REQUIRED"
    assert _capture(personal_client)["status"] == "UNDECIDED"

    r = personal_client.post("/api/decisions", json=_body(details={"authenticated_capture": "allowed",
                                                                    "scope": " rute Control Center, hanya-baca "}))
    assert r.status_code == 201, r.text
    d = r.json()
    assert (d["target_type"], d["target_id"]) == ("screenshot_capture", "PERSONAL")
    assert d["details"]["authenticated_capture"] == "ALLOWED" and d["details"]["scope"] == "rute Control Center, hanya-baca"
    assert d["expires_at"] is None and "attestation" not in d["details"]
    cap = _capture(personal_client)
    assert (cap["status"], cap["decision_id"], cap["scope"]) == ("ALLOWED", d["id"], "rute Control Center, hanya-baca")
    # ALLOWED is the owner's choice, not a capability: the collector still does not log in (fail closed)
    assert cap["capture_mode"] == "UNAUTHENTICATED" and "NOT_BUILT" in cap["note"]

    # DENIED supersedes the earlier choice (same preset target)
    denied = personal_client.post("/api/decisions", json=_body(details={"authenticated_capture": "DENIED"}))
    assert denied.status_code == 201, denied.text
    db.expire_all()
    assert db.get(Decision, d["id"]).status == "SUPERSEDED"
    cap = _capture(personal_client)
    assert (cap["status"], cap["decision_id"], cap["scope"]) == ("DENIED", denied.json()["id"], None)

    # revoking it returns to UNDECIDED
    rv = personal_client.post(f"/api/decisions/{denied.json()['id']}/revoke", json={"reason": "putuskan ulang"})
    assert rv.status_code == 200, rv.text
    assert _capture(personal_client)["status"] == "UNDECIDED"


def test_screenshot_capture_decision_is_personal_only_and_its_key_is_refused_elsewhere(personal_client: AuthedClient,
                                                                                        db):
    other_id = personal_client.post("/api/decisions", json={
        "kind": "POLICY", "target_type": "screenshot_capture", "target_id": "OFFICE", "auth_scope": {"a": "b"},
        "rationale": "an office decision on the platform target", "details": {"authenticated_capture": "ALLOWED"}})
    assert other_id.status_code == 422 and other_id.json()["error"]["code"] == "POLICY_PRESET_MISMATCH"
    stray = personal_client.post("/api/decisions", json={
        "kind": "POLICY", "target_type": "repository", "target_id": "x", "auth_scope": {"a": "b"},
        "rationale": "a key read only on its preset target", "details": {"authenticated_capture": "ALLOWED"}})
    assert stray.status_code == 422 and stray.json()["error"]["code"] == "POLICY_PRESET_MISMATCH"
    ok = personal_client.post("/api/decisions", json=_body(details={"authenticated_capture": "ALLOWED",
                                                                     "scope": "rute Control Center"}))
    assert ok.status_code == 201
    personal_client.select_domain(Domain.OFFICE)
    office = personal_client.post("/api/decisions", json=_body(details={"authenticated_capture": "DENIED"}))
    assert office.status_code == 422 and office.json()["error"]["code"] == "POLICY_PRESET_DOMAIN_DENIED"
    # an OFFICE session never sees the PERSONAL choice as its own: UNDECIDED (fail closed)
    cap = _capture(personal_client)
    assert (cap["status"], cap["decision_id"]) == ("UNDECIDED", None)


def test_a_viewer_cannot_record_the_screenshot_capture_decision(client: AuthedClient, db):
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/decisions", json=_body(details={"authenticated_capture": "DENIED"}))
    assert r.status_code == 403
    assert _capture(client)["status"] == "UNDECIDED"


def test_cli_records_the_screenshot_capture_choice(settings, db):
    from aeos.policy import facade as policy

    create_admin(db)
    base = ["policy", "decide", "--kind", "POLICY", "--as", "owner", "--domain", "PERSONAL",
            "--rationale", "rute Control Center boleh ditangkap read-only"]
    unscoped = runner.invoke(_cli(), [*base, "--authenticated-capture", "ALLOWED"], input=ADMIN_PW + "\n")
    assert unscoped.exit_code == 1 and "POLICY_PRESET_SCOPE_REQUIRED" in unscoped.output
    ok = runner.invoke(_cli(), [*base, "--authenticated-capture", "allowed", "--capture-scope",
                                "Control Center routes, read-only"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert ok.output.isascii() and "screenshot_capture:PERSONAL" in ok.output
    assert "authenticated_capture=ALLOWED" in ok.output and "stays unauthenticated" in ok.output
    db.expire_all()
    got = policy.authenticated_screenshot_capture(db, "PERSONAL")
    assert (got["status"], got["scope"]) == ("ALLOWED", "Control Center routes, read-only")
    wrong = runner.invoke(_cli(), [*base, "--authenticated-capture", "DENIED"], input="not-the-password\n")
    assert wrong.exit_code != 0
    db.expire_all()
    assert policy.authenticated_screenshot_capture(db, "PERSONAL")["status"] == "ALLOWED"


# ---------------------------------------------------------------- role-evaluation threshold


def test_roles_view_states_the_role_evaluation_threshold_and_the_cli_sets_it(personal_client: AuthedClient, db):
    from aeos.workforce import scheduler

    before = personal_client.get("/api/workforce/roles").json()["role_eval_threshold"]
    assert (before["required_from"], before["source"], before["requires_evaluation"]) == ("R4", "DEFAULT", ["R4"])
    wrong = runner.invoke(_cli(), ["work", "role-eval-threshold", "R2", "--as", "owner", "--rationale", "tighten"],
                          input="not-the-password\n")
    assert wrong.exit_code != 0 and "password check failed" in wrong.output
    bad = runner.invoke(_cli(), ["work", "role-eval-threshold", "R9", "--as", "owner", "--rationale", "tighten"],
                        input=ADMIN_PW + "\n")
    assert bad.exit_code != 0
    db.expire_all()
    assert scheduler.role_eval_threshold(db)["source"] == "DEFAULT"
    ok = runner.invoke(_cli(), ["work", "role-eval-threshold", "r2", "--as", "owner", "--rationale",
                                "tighten before the R2 pilot"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert ok.output.isascii() and "R4 -> R2" in ok.output and "R2, R3, R4" in ok.output
    after = personal_client.get("/api/workforce/roles").json()["role_eval_threshold"]
    assert (after["required_from"], after["source"], after["requires_evaluation"]) == (
        "R2", "OWNER_SETTING", ["R2", "R3", "R4"])
    db.expire_all()
    ev = db.scalar(select(AuditEvent).where(AuditEvent.action == "workforce.role_eval_threshold"))
    assert ev is not None and ev.actor_id.startswith("user:")


# ---------------------------------------------------------------- worker.route_behaviour threshold


def test_cli_route_threshold_records_the_shape_the_doctor_check_reads(settings, db):
    from aeos.doctor.checks import worker as worker_checks
    from aeos.policy import catalog

    assert worker_checks.ROUTE_BEHAVIOUR_TARGET == catalog.TARGET_ROUTE_BEHAVIOUR
    create_admin(db)
    base = ["policy", "route-threshold", "--as", "owner", "--domain", "PERSONAL",
            "--rationale", "pilot: at most 5% of samples may fail"]
    out_of_range = runner.invoke(_cli(), [*base, "--max-failure-rate", "5"], input=ADMIN_PW + "\n")
    assert out_of_range.exit_code != 0
    assert worker_checks._threshold(db) == (None, None)
    ok = runner.invoke(_cli(), [*base, "--max-failure-rate", "0.05"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert ok.output.isascii() and "max_failure_rate = 0.05 (5.0%)" in ok.output
    db.expire_all()
    rate, decision_id = worker_checks._threshold(db)
    d = db.get(Decision, decision_id)
    assert rate == 0.05 and d is not None and d.actor_kind == "HUMAN" and d.domain == "PERSONAL"
    assert (d.target_type, d.target_id, d.details["max_failure_rate"]) == (
        "worker_route_behaviour", "tool_schema_failures", 0.05)


def test_the_route_threshold_target_refuses_an_unreadable_value(personal_client: AuthedClient):
    for rate in (None, "5%", 1.5, True):
        r = personal_client.post("/api/decisions", json={
            "kind": "POLICY", "target_type": "worker_route_behaviour", "target_id": "tool_schema_failures",
            "rationale": "a threshold the check could not read", "auth_scope": {"threshold": "x"},
            "details": {"max_failure_rate": rate}})
        assert r.status_code == 422 and r.json()["error"]["code"] == "ROUTE_BEHAVIOUR_THRESHOLD_INVALID", rate


# ---------------------------------------------------------------- decision ids in the CLI list


def test_cli_decision_list_prints_the_ids_revoke_takes(personal_client: AuthedClient, db):
    r = personal_client.post("/api/decisions", json=_body(details={"authenticated_capture": "DENIED"}))
    assert r.status_code == 201
    listed = runner.invoke(_cli(), ["policy", "decisions", "--target-type", "screenshot_capture"])
    assert listed.exit_code == 0, listed.output
    assert listed.output.isascii() and listed.output.startswith(r.json()["id"] + " ")
    revoked = runner.invoke(_cli(), ["policy", "revoke", r.json()["id"], "--as", "owner", "--reason", "decide anew"],
                            input=ADMIN_PW + "\n")
    assert revoked.exit_code == 0, revoked.output
    assert "INVALIDATED" in runner.invoke(_cli(), ["policy", "decisions", "--target-type", "screenshot_capture"]).output
