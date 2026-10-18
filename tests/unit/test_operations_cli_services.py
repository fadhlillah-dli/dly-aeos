"""`aeos ops` CLI, service inventory, runbook index and retirement checklist (master §10, §33).

CLI output is ASCII-only; the SLI command requires an explicit domain (U-05); retirement is a
read-only checklist that never deletes anything (§33 "Tidak ada agent yang menghapus data").
"""

from __future__ import annotations

import httpx
import respx
from typer.testing import CliRunner

from aeos.core.db import session_factory
from aeos.core.enums import Domain
from aeos.models.portfolio import Project, ProjectIntegration
from aeos.models.routing import CapabilityGrant
from aeos.operations import services
from tests.unit.test_operations_fakes import fake_verification

runner = CliRunner()


def _cli():
    from aeos.cli import app

    return app


def test_cli_backup_create_list_restore_test(settings, monkeypatch):
    fake_verification(monkeypatch)
    app = _cli()
    r = runner.invoke(app, ["ops", "backup", "create", "--label", "cli"])
    assert r.exit_code == 0, r.output
    assert "Backup created:" in r.output and "NOT_RESTORE_TESTED" in r.output
    r = runner.invoke(app, ["ops", "backup", "list"])
    assert r.exit_code == 0 and "NOT-PROVEN" in r.output
    bid = r.output.split()[0]
    r = runner.invoke(app, ["ops", "backup", "verify", bid])
    assert r.exit_code == 0 and "OK" in r.output
    r = runner.invoke(app, ["ops", "backup", "restore-test", bid])
    assert r.exit_code == 0 and "PASSED" in r.output, r.output
    r = runner.invoke(app, ["ops", "backup", "restore", bid], input="wrong-id\n")
    assert r.exit_code == 1 and "nothing changed" in r.output
    assert all(ord(ch) < 128 for ch in r.output)


def test_cli_backup_create_if_exists_without_db(tmp_path, monkeypatch):
    from aeos.core import config as config_mod

    monkeypatch.setenv("AEOS_DATA_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "none.yaml"))
    config_mod.reset_settings_cache()
    try:
        r = runner.invoke(_cli(), ["ops", "backup", "create", "--if-exists"])
        assert r.exit_code == 0 and "nothing to back up" in r.output
    finally:
        config_mod.reset_settings_cache()


def test_cli_sli_requires_domain_and_prints(settings):
    app = _cli()
    r = runner.invoke(app, ["ops", "sli"])
    assert r.exit_code != 0
    r = runner.invoke(app, ["ops", "sli", "--domain", "PERSONAL"])
    assert r.exit_code == 0, r.output
    assert "queue_age_seconds" in r.output and "NO_TARGET" in r.output
    r = runner.invoke(app, ["ops", "sli", "target", "queue_age_seconds", "--max", "600"])
    assert r.exit_code == 0 and "600" in r.output
    r = runner.invoke(app, ["ops", "sli", "--domain", "PERSONAL", "--raise-alerts", "--json"])
    assert r.exit_code == 0 and "restore_untested" in r.output


def test_cli_release_list_and_promote_requires_operator(settings):
    app = _cli()
    r = runner.invoke(app, ["ops", "release", "list"])
    assert r.exit_code == 0 and "No releases built yet" in r.output
    r = runner.invoke(app, ["ops", "release", "promote", "x"])
    assert r.exit_code != 0  # --operator is mandatory (human decision)


def test_cli_emergency_stop_requires_confirmation(settings):
    r = runner.invoke(_cli(), ["ops", "emergency-stop", "--reason", "drill now"], input="no\n")
    assert r.exit_code == 1 and "nothing changed" in r.output


@respx.mock
def test_service_inventory_openbao_connected_not_verified(settings):
    respx.get("http://127.0.0.1:8200/v1/sys/health").mock(
        return_value=httpx.Response(200, json={"initialized": True, "sealed": True, "version": "2.4.0"}))
    inv = services.service_inventory(settings)
    by = {i["service"]: i for i in inv["items"]}
    assert by["openbao"]["status"] == "CONNECTED" and by["openbao"]["sealed"] is True
    assert "not a functional verification" in by["openbao"]["detail"]
    assert by["aeos.operations.loop"]["status"] in ("NOT_REGISTERED", "CONFIGURED", "RUNNING")


@respx.mock
def test_service_inventory_openbao_unreachable(settings):
    respx.get("http://127.0.0.1:8200/v1/sys/health").mock(side_effect=httpx.ConnectError("refused"))
    by = {i["service"]: i for i in services.service_inventory(settings)["items"]}
    assert by["openbao"]["status"] == "UNREACHABLE"


def test_runbook_index_lists_required_runbooks():
    ids = {r["id"] for r in services.runbooks()}
    required = {"restart-recovery", "credential-rotation", "queue-drain", "cancel-runaway-run",
                "publication-reconciliation", "backup-restore", "model-rollback", "incident-containment",
                "openbao-unseal-recovery", "controller-upgrade-rollback"}
    assert required <= ids
    assert all(r["trigger"] for r in services.runbooks())


def test_retirement_checklist_is_read_only(personal_client, db, settings):
    p = Project(project_key="alpha", name="Alpha", domain="PERSONAL")
    db.add(p)
    db.flush()
    db.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="github", purpose="code",
                              credential_ref="personal/github"))
    from datetime import timedelta

    from aeos.core.timeutil import utcnow

    db.add(CapabilityGrant(token_hash="h", run_id="r", project_id=p.id, domain="PERSONAL", kind="GIT_BRANCH",
                           expires_at=utcnow() + timedelta(hours=1)))
    db.commit()
    body = personal_client.get(f"/api/ops/retirement/{p.id}").json()
    items = {i["key"]: i for i in body["items"]}
    assert items["credentials"]["status"] == "BLOCKING" and "credentials" in body["blocking"]
    assert items["final_verification"]["status"] == "PENDING_OWNER"
    assert items["legal_hold"]["status"] == "UNKNOWN"
    s = session_factory()()
    try:
        assert s.get(Project, p.id) is not None and s.query(CapabilityGrant).count() == 1  # nothing changed
    finally:
        s.close()
    office = Project(project_key="omega", name="Omega", domain="OFFICE")
    db.add(office)
    db.commit()
    assert personal_client.get(f"/api/ops/retirement/{office.id}").status_code == 404


def test_services_and_runbooks_api(personal_client):
    assert personal_client.get("/api/ops/services?probe=false").json()["items"][0]["service"] == "control-plane-api"
    assert personal_client.get("/api/ops/runbooks").json()["items"]


def test_domain_enum_cli_rejects_invalid(settings):
    r = runner.invoke(_cli(), ["ops", "sli", "--domain", "CORPORATE"])
    assert r.exit_code != 0
    assert Domain("PERSONAL")
