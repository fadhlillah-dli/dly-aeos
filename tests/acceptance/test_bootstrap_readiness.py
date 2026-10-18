"""Bootstrap and readiness (§07, §08, §38). §50 rows:

* 1.1 "Dependency inti belum terpasang" -> the bootstrap CLI explains reason + remedy, never fails
  silently before the UI;
* 1.2 "Tool opsional atau Office belum ready" -> the dashboard stays open, independent features stay
  available, OFFICE is BLOCKED by default;
* 1.4 "Doctor hanya berhasil connect" -> CONNECTED is never shown as verified permission/security;
* 1.5 "Config/model/provider berubah setelah doctor" -> related checks become STALE and are retested;
* 3.5 "Probe connectivity berhasil tetapi functional check gagal" -> CONNECTED + FAILED, not READY.

Only the host (binaries, versions, GitHub HTTP) and the official ``claude`` child process are faked.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeos.core import config as config_mod
from aeos.core.enums import Domain
from aeos.core.paths import repo_root
from aeos.inventory.probes import HttpResult
from tests.acceptance.fakes import FakeClaude, ok
from tests.acceptance.world import caps_by_name, checks_by_id, seed_catalog
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

OPTIONAL_TOOL_WORDS = ("docker", "opencode", "openbao", "bao", "claude", "office", "jira", "confluence")


def _cli():
    from aeos.cli import app

    return app


# ---------------------------------------------------------------------------------------------- 1.1
def test_missing_core_dependency_is_explained_by_the_bootstrap_cli(tmp_path: Path, monkeypatch, host):
    """No server, no DB, a core Python dependency missing: `aeos doctor` names it with a copyable
    remedy, exits non-zero, prints ASCII only and creates no state as a side effect."""
    data = tmp_path / "never-created"
    monkeypatch.setenv("AEOS_DATA_DIR", str(data))
    monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "none.yaml"))
    real_version = importlib.metadata.version

    def version(name: str) -> str:
        if name.lower() == "fastapi":
            raise importlib.metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", version)
    config_mod.reset_settings_cache()
    try:
        as_json = CliRunner().invoke(_cli(), ["doctor", "--json"])
        as_text = CliRunner().invoke(_cli(), ["doctor"])
    finally:
        config_mod.reset_settings_cache()
    assert as_json.exit_code == 1, as_json.output
    payload = json.loads(as_json.output)
    by_id = {r["check_id"]: r for r in payload["results"]}
    deps = by_id["runtime.backend_deps"]
    assert deps["status"] == "FAILED" and deps["error_code"] == "DEPENDENCIES_MISSING"
    assert "fastapi" in deps["observed_evidence"]["missing"]
    assert "pip install" in deps["remediation"]["command"]
    assert deps["remediation"]["executed_by"].startswith("MANUSIA")  # proposal only, never executed
    assert "runtime.backend_deps" in payload["dashboard_critical_failures"]
    assert by_id["runtime.db"]["remediation"]["command"].endswith("db upgrade")
    assert payload["database"] == "missing" and payload["persisted"] is False
    assert not data.exists(), "the bootstrap doctor never creates state as a side effect"
    assert as_text.exit_code == 1
    assert "Remedies (copy and run yourself" in as_text.output and "EXIT 1" in as_text.output
    assert "runtime.backend_deps" in as_text.output and "Traceback" not in as_text.output
    assert as_json.output.isascii() and as_text.output.isascii()


def test_bootstrap_component_list_names_missing_tools_with_install_hints(tmp_path: Path):
    """The pre-venv bootstrap step (scripts/component_status.py) reports missing tools with the
    official install hint and never installs anything (PATH without any tool)."""
    script = Path(repo_root()) / "scripts" / "component_status.py"
    empty = tmp_path / "empty-path"
    empty.mkdir()
    env = {"PATH": str(empty), "AEOS_OPENBAO_ADDR": "http://127.0.0.1:9"}
    for k in ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP"):
        if k in os.environ:
            env[k] = os.environ[k]
    run = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True, env=env,
                         cwd=str(tmp_path), timeout=120, check=False)
    assert run.returncode == 0, run.stderr
    items = {i["name"]: i for i in json.loads(run.stdout)["items"]}
    git = items["Git"]
    assert git["status"] == "MISSING" and git["required"] is True and git["install_hint"]
    node = items["Node.js"]
    assert node["status"] == "MISSING" and node["install_hint"]
    claude = next(i for n, i in items.items() if n.startswith("Claude Code"))
    assert claude["status"] == "MISSING" and claude["install_hint"]
    text = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env,
                          cwd=str(tmp_path), timeout=120, check=False)
    assert "Nothing is installed by this script" in text.stdout
    assert "install:" in text.stdout and text.stdout.isascii()


# ---------------------------------------------------------------------------------------------- 1.2
def test_optional_tools_and_office_missing_keep_dashboard_and_independent_features(personal_client: AuthedClient,
                                                                                    db, host, vault):
    seed_catalog(db)
    run = personal_client.post("/api/doctor/run", json={})
    assert run.status_code == 200, run.text
    caps = caps_by_name(personal_client)
    dashboard = caps["CAN_OPEN_DASHBOARD"]
    blob = json.dumps(dashboard["reasons"]).lower()
    assert not any(w in blob for w in OPTIONAL_TOOL_WORDS), dashboard["reasons"]
    # tools that are missing block exactly the capabilities that depend on them, nothing else
    assert caps["CAN_RUN_WORKER"]["status"] in ("BLOCKED", "UNKNOWN")
    assert caps["CAN_REVIEW_WITH_MAX"]["status"] in ("BLOCKED", "UNKNOWN")
    assert caps["CAN_PLAN_OFFICE"]["status"] == "NOT_APPLICABLE"  # PERSONAL session
    checks = checks_by_id(personal_client)
    assert checks["containment.docker"]["status"] == "MISSING" and checks["containment.docker"]["severity"] == "LOW"
    # the dashboard and independent features keep working
    assert personal_client.get("/api/control-center/overview").status_code == 200
    created = personal_client.post("/api/projects", json={"domain": "PERSONAL", "name": "Independen"})
    assert created.status_code == 200, created.text
    assert personal_client.get("/api/inventory/components").status_code == 200
    assert personal_client.get("/api/plans").status_code == 200
    # OFFICE is blocked by default, with the reason, in an OFFICE session
    personal_client.select_domain(Domain.OFFICE)
    office = caps_by_name(personal_client)
    for cap in ("CAN_PLAN_OFFICE", "CAN_USE_OFFICE_INTEGRATIONS"):
        assert office[cap]["status"] == "BLOCKED"
        assert "OFFICE default deny" in office[cap]["reasons"][0]
    assert personal_client.get("/api/control-center/overview").status_code == 200


def test_missing_module_degrades_one_section_not_the_dashboard(personal_client: AuthedClient, db, host, vault,
                                                               absent_modules):
    absent_modules("aeos.cost.service", "aeos.policy.queue")
    body = personal_client.get("/api/control-center/overview")
    assert body.status_code == 200
    sections = body.json()["sections"]
    assert sections["cost"]["status"] == "UNKNOWN"
    assert sections["decisions"]["status"] == "UNKNOWN"
    assert sections["capabilities"]["status"] != "READY"  # never green without checks
    assert all(s["status"] != "READY" for s in sections.values())
    assert personal_client.get("/api/projects").status_code == 200
    assert personal_client.get("/api/doctor/checks").status_code == 200


# ---------------------------------------------------------------------------------------------- 1.4
def test_connected_github_probe_is_not_verified_permission(personal_client: AuthedClient, db, host, vault):
    seed_catalog(db)
    vault.put("personal/github", "gh" + "p_" + "A1b2C3d4E5" * 4, actor="owner")
    slug = config_mod.get_settings().platform_repo_slug
    host.http[f"https://api.github.com/repos/{slug}"] = HttpResult(
        200, {"id": 1, "private": True, "default_branch": "main",
              "permissions": {"admin": True, "push": True, "pull": True}})
    r = personal_client.post("/api/doctor/run", json={"group": "PROJECT_ADAPTERS", "functional": True})
    assert r.status_code == 200, r.text
    gh = checks_by_id(personal_client)["adapter.github_token"]
    assert gh["status"] == "CONNECTED", gh["summary"]
    assert gh["status"] != "VERIFIED" and "belum" in gh["summary"]  # write/enforcement not verified
    assert tuple({ref for ref, _ in vault.reads}) == ("personal/github", )  # read through a scoped purpose
    detail = personal_client.get("/api/inventory/components/github").json()
    dims = detail["lifecycle"]
    assert dims["connectivity"]["value"] == "CONNECTED"
    assert dims["functional"]["value"] != "VERIFIED"
    claim = next(c for c in detail["evidence"]["claims"] if c["claim"].startswith("Konektivitas"))
    assert "bukan bukti izin/keamanan" in claim["claim"]
    legend = personal_client.get("/api/doctor/checks").json()["status_legend"]
    assert "bukan verifikasi izin/keamanan" in legend["CONNECTED"]


# ---------------------------------------------------------------------------------------------- 3.5
def _claude_host(host) -> None:
    host.install("claude", "2.1.302 (Claude Code)\n")
    host.outputs[("claude", ("auth", "status", "--json"))] = ok(json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}))


def test_connected_but_functional_check_failed_is_not_ready(personal_client: AuthedClient, db, host, vault,
                                                            monkeypatch, settings):
    seed_catalog(db)
    _claude_host(host)
    settings.claude_binary = None  # resolve through the (fake) PATH
    fake = FakeClaude(monkeypatch, lambda argv, prompt: (1, "", "API Error: 500 internal error"))
    r = personal_client.post("/api/doctor/run", json={"group": "CLAUDE_NATIVE", "functional": True})
    assert r.status_code == 200, r.text
    assert fake.calls, "the capability test went through the bridge"
    checks = checks_by_id(personal_client)
    assert checks["claude.auth"]["status"] == "CONNECTED"
    assert checks["claude.capability_test"]["status"] == "FAILED"
    caps = caps_by_name(personal_client)
    review = caps["CAN_REVIEW_WITH_MAX"]
    assert review["status"] == "BLOCKED"
    assert any(x.startswith("claude.capability_test: FAILED") for x in review["reasons"])
    assert caps["CAN_PLAN_PERSONAL"]["status"] == "BLOCKED"
    assert all(c["status"] != "READY" for c in caps.values() if c["capability"] in
               ("CAN_REVIEW_WITH_MAX", "CAN_PLAN_PERSONAL"))
    bridge = personal_client.get("/api/inventory/components/claude-native-bridge").json()["lifecycle"]
    assert bridge["functional"]["value"] == "FAILED"
    claude = personal_client.get("/api/inventory/components/claude-code").json()["lifecycle"]
    assert claude["connectivity"]["value"] == "CONNECTED"


# ---------------------------------------------------------------------------------------------- 1.5
def test_binary_change_after_doctor_marks_checks_stale_and_recheck_runs(personal_client: AuthedClient, db, host,
                                                                         vault, settings):
    seed_catalog(db)
    _claude_host(host)
    settings.claude_binary = None
    personal_client.post("/api/doctor/run", json={"group": "CLAUDE_NATIVE"})
    before = checks_by_id(personal_client)["claude.binary"]
    assert before["status"] == "VERIFIED" and before["effective_status"] == "VERIFIED"
    host.replace_binary("claude", size=4242)  # upgraded in place after the doctor ran
    stale = checks_by_id(personal_client)["claude.binary"]
    assert stale["effective_status"] == "STALE" and stale["stale_reason"]
    review = caps_by_name(personal_client)["CAN_REVIEW_WITH_MAX"]
    assert review["status"] != "READY" and any("claude.binary: STALE" in x for x in review["reasons"])
    personal_client.post("/api/doctor/run", json={"group": "CLAUDE_NATIVE"})
    after = checks_by_id(personal_client)["claude.binary"]
    assert after["effective_status"] == "VERIFIED" and after["run_id"] != before["run_id"]
    hist = personal_client.get("/api/doctor/checks/claude.binary/history").json()["items"]
    assert [h["status"] for h in hist][:2] == ["VERIFIED", "STALE"]  # history kept, old one marked STALE


def test_route_configuration_change_invalidates_route_checks(personal_client: AuthedClient, db, host, vault):
    """Provider/route configuration change -> config:routes dependants are STALE until rechecked."""
    from tests.acceptance.world import register_free_route

    seed_catalog(db)
    register_free_route(db, "worker.acc-free")
    personal_client.post("/api/doctor/run", json={"group": "MODEL_ROUTE"})
    assert checks_by_id(personal_client)["route.registry"]["effective_status"] != "STALE"
    from aeos.routing import service as routing

    route = routing.get_route(db, "worker.acc-free")
    route.context_window = 1_000_000  # provider changed the route configuration
    assert routing.refresh_config_hash(db, route, reason="provider changed context window") is True
    db.commit()
    reg = checks_by_id(personal_client)["route.registry"]
    assert reg["effective_status"] == "STALE" and "config:routes" in (reg["stale_reason"] or "")
    personal_client.post("/api/doctor/run", json={"group": "MODEL_ROUTE"})
    assert checks_by_id(personal_client)["route.registry"]["effective_status"] != "STALE"
