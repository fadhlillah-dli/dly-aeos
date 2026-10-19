"""CI gates (master §11 testing incl. type checking, §31 G1/G3, §45 security/test tools, §06 table).

The workflow itself runs on GitHub; these tests pin its structure (what each gate runs) and the local
tooling it calls, so a removed gate or a weakened scan fails here first."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aeos_script_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_g1_runs_lint_types_and_scans_and_g3_runs_browser_tests():
    assert "python -m ruff check backend tests scripts" in CI
    assert "python -m mypy" in CI  # §11 type checking is a gate, not only configured
    assert "python scripts/secret_scan.py" in CI
    assert "tests/e2e/test_ui_smoke.py" in CI and "playwright install --with-deps chromium" in CI
    assert "npx tsc -b --noEmit" in CI and "npm test" in CI


def test_supply_chain_reports_are_produced_and_labelled_report_only():
    for needle in ("scripts/supply_chain.py sbom", "scripts/supply_chain.py licenses", "pip_audit", "npm audit",
                   "bandit"):
        assert needle in CI, needle
    job = CI.split("supply-chain:", 1)[1]
    assert "[NO-GATE]" in job.splitlines()[1]  # report-only: never counted as a gate pass
    assert "pip-audit==" in job and "bandit==" in job  # tools pinned to exact versions


def test_every_job_names_its_gates_explicitly():
    from aeos.delivery.provider import classify_check

    names = re.findall(r"^\s{4}name:\s*(.+)$", CI, flags=re.M)
    assert names
    for name in names:
        assert re.search(r"\[(G[0-8](,G[0-8])*|NO-GATE)\]", name), name
    assert classify_check("backend [G1,G2] (ubuntu-latest)") == ["G1", "G2"]
    assert classify_check("supply-chain report [NO-GATE] (SBOM)") == []
    assert classify_check("frontend [G1,G3]") == ["G1", "G3"]
    assert "G1" in classify_check("lint")  # untagged provider checks keep the name heuristics


def test_mypy_baseline_only_shrinks():
    """The ratcheted baseline may lose entries (a module got fixed) but never gain one."""
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = data["tool"]["mypy"].get("overrides") or []
    exempt = {m for o in overrides if o.get("ignore_errors") for m in o.get("module", [])}
    baseline_2026_10_19 = {
        "aeos.broker.git_broker", "aeos.broker.grants", "aeos.broker.model_proxy", "aeos.cost.service",
        "aeos.delivery.pr_bundle", "aeos.docs_engine.renderer", "aeos.doctor.capabilities",
        "aeos.doctor.checks.delivery", "aeos.doctor.checks.evidence", "aeos.doctor.checks.secret", "aeos.doctor.cli",
        "aeos.doctor.views", "aeos.inventory.drift", "aeos.inventory.manifest", "aeos.inventory.snapshots",
        "aeos.inventory.views", "aeos.planning.dag", "aeos.policy.decisions", "aeos.policy.queue",
        "aeos.policy.router", "aeos.policy.snapshot", "aeos.proposals.review", "aeos.publisher.jobs",
        "aeos.publisher.targets", "aeos.routing.catalog", "aeos.routing.service", "aeos.worker_runtime.executor",
        "aeos.worker_runtime.opencode", "aeos.worker_runtime.processes", "aeos.worker_runtime.sandbox",
        "aeos.workforce.demo", "aeos.workforce.loop", "aeos.workforce.scheduler",
    }
    assert exempt <= baseline_2026_10_19, sorted(exempt - baseline_2026_10_19)


def test_pii_scan_flags_personal_data_and_tenant_hosts_in_templates():
    scan = _script("secret_scan")
    assert scan.PII_SCOPES == ("blueprints/", "contracts/", "docs/")
    found = scan.pii_findings("blueprints/x.md", "owner: budi" + "@" + "corp-mail.co.id\nhp 0812-3456-7890\n"
                              "wiki https://acme-corp.atlassian.net/wiki\n")
    assert any("EMAIL" in f for f in found) and any("PHONE" in f for f in found)
    assert any("tenant/company host" in f for f in found)
    assert scan.pii_findings("docs/y.md", "contact someone@example.com at https://your-tenant.atlassian.net") == []
    assert scan.pii_findings("contracts/z.json", '{"id": "289f8778-6cba-415a-8323-2390263811d2"}') == []


def test_sbom_is_cyclonedx_and_deterministic():
    sc = _script("supply_chain")
    a, b = sc.build_sbom(), sc.build_sbom()
    assert a == b and a["bomFormat"] == "CycloneDX" and a["specVersion"] == "1.5"
    purls = {c["purl"] for c in a["components"]}
    assert any(p.startswith("pkg:pypi/fastapi@") for p in purls)
    assert any(p.startswith("pkg:npm/react@") for p in purls)
    assert a["metadata"]["component"]["name"] == "dly-aeos"


def test_licence_report_never_guesses_and_is_report_only():
    sc = _script("supply_chain")
    rep = sc.license_report()
    assert rep["status"] == "REPORT_ONLY" and rep["policy"] is None
    by_name = {i["name"]: i for i in rep["items"]}
    assert "fastapi" in by_name and by_name["fastapi"]["license"] != ""
    assert set(rep["unknown"]) == {i["name"] for i in rep["items"] if i["license"] == "UNKNOWN"}


@pytest.mark.parametrize("tool", ["IaC", "DAST", "load"])
def test_not_applicable_security_tools_are_recorded_with_a_reason(tool):
    adr = (ROOT / "docs" / "decisions" / "ADR-0095-ci-gates-supply-chain-and-security-tooling.md").read_text(
        encoding="utf-8")
    assert tool in adr and "NOT_APPLICABLE" in adr


def test_sbom_cli_writes_json(tmp_path: Path):
    sc = _script("supply_chain")
    out = tmp_path / "sbom.json"
    assert sc.main(["sbom", "-o", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["components"]
