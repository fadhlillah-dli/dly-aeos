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
    # fixed modules never return to the exemption (cleaned 2026-10-19 by K4)
    fixed = {m for m in baseline_2026_10_19 if m.startswith(("aeos.docs_engine.", "aeos.doctor.", "aeos.inventory."))}
    fixed |= {"aeos.policy.decisions", "aeos.policy.router", "aeos.policy.snapshot", "aeos.publisher.targets",
              "aeos.routing.catalog", "aeos.routing.service", "aeos.worker_runtime.processes",
              "aeos.workforce.demo", "aeos.workforce.loop"}
    assert len(fixed) == 20 and fixed <= baseline_2026_10_19 and not (exempt & fixed), sorted(exempt & fixed)
    assert len(exempt) <= 13


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


# ------------------------------------------------------------------ W6-106 / W6-135: CI controls for the scans
def _workflow() -> dict:
    import yaml

    return yaml.safe_load(CI)


def test_every_teed_report_step_runs_with_pipefail():
    """GitHub's default `bash -e {0}` has no pipefail: `tool | tee` reported success even when the tool crashed.
    `shell: bash` runs `bash --noprofile --norc -eo pipefail {0}`."""
    teed = []
    for job_id, job in _workflow()["jobs"].items():
        default_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell")
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if "| tee" in run:
                teed.append(job_id)
                assert (step.get("shell") or default_shell) == "bash", (job_id, step.get("name"))
    assert "supply-chain" in teed and "browser-e2e" in teed


def test_supply_chain_job_evaluates_the_owner_policy_and_hard_codes_no_threshold():
    job = _workflow()["jobs"]["supply-chain"]
    runs = "\n".join(s.get("run") or "" for s in job["steps"])
    assert "scripts/supply_chain.py evaluate --policy policy/supply-chain.yaml" in runs
    for report in ("pip-audit.json", "npm-audit.json", "bandit.json", "licenses.json"):
        assert report in runs, report
    assert "--audit-level" not in runs and " -ll" not in runs, "severity filtering is the owner's threshold"
    evaluation = next(s for s in job["steps"] if "evaluate --policy" in (s.get("run") or ""))
    assert not evaluation.get("continue-on-error"), "once decided, a violation fails the job"
    assert "[NO-GATE]" in job["name"]  # until the owner decides (policy UNDECIDED)


SAMPLE_PIP = {"dependencies": [{"name": "jinja2", "version": "3.1.0", "vulns": [
    {"id": "PYSEC-X", "fix_versions": ["3.1.6"], "aliases": [], "description": "x"}]},
    {"name": "fastapi", "version": "0.1", "vulns": []}], "fixes": []}
SAMPLE_NPM = {"auditReportVersion": 2, "vulnerabilities": {
    "esbuild": {"name": "esbuild", "severity": "moderate", "range": "<0.25"},
    "vite": {"name": "vite", "severity": "high", "range": "<6"}}, "metadata": {}}
SAMPLE_BANDIT = {"results": [
    {"test_id": "B603", "filename": "a.py", "line_number": 1, "issue_severity": "LOW", "issue_confidence": "HIGH"},
    {"test_id": "B608", "filename": "b.py", "line_number": 2, "issue_severity": "MEDIUM", "issue_confidence": "LOW"}],
    "errors": []}


def _reports(tmp_path: Path, **override) -> dict:
    files = {"pip_audit": SAMPLE_PIP, "npm_audit": SAMPLE_NPM, "bandit": SAMPLE_BANDIT,
             "licenses": {"items": [{"name": "fastapi", "license": "MIT"}], "unknown": []}} | override
    out = {}
    for key, data in files.items():
        if data is None:
            out[key] = str(tmp_path / f"{key}-missing.json")
            continue
        p = tmp_path / f"{key}.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        out[key] = str(p)
    return out


def _policy(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "supply-chain.yaml"
    p.write_text(body, encoding="utf-8")
    return p


DECIDED = """status: DECIDED
decision_ref: ADR-0095
licenses: {allow: [MIT, Apache-2.0]}
vulnerabilities:
  python: {fail_at_or_above: HIGH}
  npm: {fail_at_or_above: HIGH}
sast:
  bandit: {fail_at_or_above: MEDIUM, min_confidence: MEDIUM}
"""


def test_the_repository_policy_is_undecided_and_invents_no_value():
    sc = _script("supply_chain")
    pol = sc.load_policy(ROOT / "policy" / "supply-chain.yaml")
    assert pol["status"] == "UNDECIDED" and not pol["errors"]
    assert pol["licenses_allow"] is None and pol["min_confidence"] is None
    assert set(pol["thresholds"].values()) == {None}


def test_undecided_policy_is_report_only_even_with_findings_and_crashed_scans(tmp_path: Path):
    sc = _script("supply_chain")
    pol = sc.load_policy(ROOT / "policy" / "supply-chain.yaml")
    res = sc.evaluate(pol, **_reports(tmp_path, bandit=None, npm_audit="{not json"))
    assert res["mode"] == "REPORT_ONLY" and res["overall"] == "REPORT_ONLY"
    assert res["sections"]["bandit"]["report"] == "MISSING", "a crashed scan is never 'no findings'"
    assert res["sections"]["npm"]["report"] == "UNREADABLE"
    assert res["sections"]["python"]["counts"] == {"UNKNOWN": 1}  # pip-audit reports no severity: never guessed
    out = tmp_path / "eval.json"
    args = ["evaluate", "--policy", str(ROOT / "policy" / "supply-chain.yaml"), "-o", str(out)]
    for key, flag in (("pip_audit", "--pip-audit"), ("npm_audit", "--npm-audit"), ("bandit", "--bandit")):
        args += [flag, _reports(tmp_path)[key]]
    assert sc.main(args) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["mode"] == "REPORT_ONLY"


def test_the_step_summary_lists_what_was_found_not_only_counts(tmp_path: Path):
    """The scanner output no longer goes to the step summary directly (JSON for the evaluator), so the evaluation
    summary must name each finding - a report-only run is useless if it only says 'counts={UNKNOWN: 1}'."""
    sc = _script("supply_chain")
    res = sc.evaluate(sc.load_policy(ROOT / "policy" / "supply-chain.yaml"), **_reports(tmp_path))
    text = "\n".join(sc.summary_lines(res))
    assert "jinja2 3.1.0 PYSEC-X severity=UNKNOWN" in text
    assert "vite <6 severity=HIGH" in text and "esbuild <0.25 severity=MEDIUM" in text
    assert "B608 b.py:2 severity=MEDIUM confidence=LOW" in text


def test_decided_policy_fails_on_violations_unknown_severity_and_missing_reports(tmp_path: Path):
    sc = _script("supply_chain")
    pol = sc.load_policy(_policy(tmp_path, DECIDED))
    assert not pol["errors"] and pol["thresholds"] == {"python": "HIGH", "npm": "HIGH", "bandit": "MEDIUM"}
    res = sc.evaluate(pol, **_reports(tmp_path))
    assert res["mode"] == "ENFORCED" and res["overall"] == "FAIL"
    s = res["sections"]
    assert [v["package"] for v in s["npm"]["violations"]] == ["vite"]  # moderate esbuild is below HIGH
    assert s["python"]["status"] == "FAIL", "an UNKNOWN severity cannot be shown below the threshold"
    assert s["bandit"]["status"] == "PASS"  # LOW < MEDIUM; the MEDIUM one is below min_confidence
    assert s["licenses"]["status"] == "PASS"
    clean = sc.evaluate(pol, **_reports(tmp_path, pip_audit={"dependencies": []},
                                        npm_audit={"vulnerabilities": {}, "metadata": {}}))
    assert clean["overall"] == "PASS"
    missing = sc.evaluate(pol, **_reports(tmp_path, pip_audit={"dependencies": []}, npm_audit=None))
    assert missing["overall"] == "FAIL" and missing["sections"]["npm"]["report"] == "MISSING"
    args = ["evaluate", "--policy", str(_policy(tmp_path, DECIDED))]
    for key, flag in (("pip_audit", "--pip-audit"), ("npm_audit", "--npm-audit"), ("bandit", "--bandit"),
                      ("licenses", "--licenses")):
        args += [flag, _reports(tmp_path)[key]]
    assert sc.main(args) == 1


def test_decided_licence_allow_list_fails_unknown_and_unlisted_licences(tmp_path: Path):
    sc = _script("supply_chain")
    allow = ["MIT", "Apache-2.0"]
    assert sc.licence_allowed("MIT", allow) and sc.licence_allowed("apache-2.0", allow)
    assert sc.licence_allowed("MIT OR GPL-3.0-only", allow) and not sc.licence_allowed("MIT AND GPL-3.0-only", allow)
    assert sc.licence_allowed("(MIT OR BSD-3-Clause) AND Apache-2.0", allow)
    assert not sc.licence_allowed("UNKNOWN", allow) and not sc.licence_allowed("MIT License", allow)  # never mapped
    assert not sc.licence_allowed("(MIT", allow) and not sc.licence_allowed("", allow)
    pol = sc.load_policy(_policy(tmp_path, DECIDED))
    rep = {"items": [{"name": "a", "license": "MIT"}, {"name": "b", "license": "UNKNOWN"},
                     {"name": "c", "license": "GPL-3.0-only"}], "unknown": ["b"]}
    sec = sc.evaluate_licenses(rep, pol)
    assert sec["status"] == "FAIL" and {v["name"]: v["reason"] for v in sec["violations"]} == {
        "b": "UNKNOWN licence", "c": "not in licenses.allow"}
    live = sc.license_report(pol)
    assert live["status"] in ("PASS", "FAIL") and live["policy"]["status"] == "DECIDED"
    assert sc.license_report(sc.load_policy(ROOT / "policy" / "supply-chain.yaml"))["status"] == "REPORT_ONLY"


def test_a_malformed_decided_policy_fails_closed(tmp_path: Path):
    sc = _script("supply_chain")
    bad = sc.load_policy(_policy(tmp_path, "status: DECIDED\nvulnerabilities: {python: {fail_at_or_above: SOMEWHAT}}\n"))
    assert bad["errors"] and sc.evaluate(bad, **_reports(tmp_path))["overall"] == "POLICY_INVALID"
    assert sc.main(["evaluate", "--policy", str(_policy(tmp_path, "status: DECIDED\n"))]) == 2  # no decision_ref
    assert any("decision_ref" in e for e in sc.load_policy(_policy(tmp_path, "status: DECIDED\n"))["errors"])


@pytest.mark.parametrize("body", [None, "status: [DECIDED\n", "- just\n- a list\n"])
def test_a_missing_or_unreadable_policy_file_fails_closed_never_undecided(tmp_path: Path, body):
    """A decided policy that got deleted or broken must not quietly turn the gate back into a report."""
    sc = _script("supply_chain")
    path = tmp_path / "supply-chain.yaml" if body is None else _policy(tmp_path, body)
    pol = sc.load_policy(path)
    assert pol["status"] == "INVALID" and pol["errors"]
    assert sc.evaluate(pol, **_reports(tmp_path))["overall"] == "POLICY_INVALID"
    assert sc.main(["evaluate", "--policy", str(path)]) == 2
    assert sc.main(["licenses", "--policy", str(path), "-o", str(tmp_path / "lic.json")]) == 2


def test_several_licence_classifiers_are_never_read_as_alternatives():
    """Classifiers do not say whether several licences are alternatives: every one must be allowed (no lenient OR)."""
    sc = _script("supply_chain")

    class Meta:
        def get(self, key, default=None):
            return None

        def get_all(self, key):
            return ["License :: OSI Approved :: MIT License", "License :: OSI Approved :: GNU General Public License v3"]

    lic = sc._license_of(type("Dist", (), {"metadata": Meta()})())
    assert lic == "GNU General Public License v3 AND MIT License"
    assert not sc.licence_allowed(lic, ["MIT License"]) and sc.licence_allowed(lic, ["MIT License",
                                                                                     "GNU General Public License v3"])


# ------------------------------------------------------------------ W6-126: §45 tool licences + restore tests
def _adr_rows() -> dict[str, list[str]]:
    adr = (ROOT / "docs" / "decisions" / "ADR-0095-ci-gates-supply-chain-and-security-tooling.md").read_text(
        encoding="utf-8")
    table = [ln for ln in adr.splitlines() if ln.startswith("| ")]
    header = [c.strip() for c in table[0].strip("|").split("|")]
    rows = {}
    for ln in table[1:]:  # the |---| separator does not start with "| "
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) == len(header):
            rows[cells[0]] = cells
    return {"__header__": header, **rows}


def test_adr_0095_records_each_selected_tools_licence_from_package_metadata():
    rows = _adr_rows()
    header = rows["__header__"]
    assert "Licence (source)" in header
    col = header.index("Licence (source)")
    for need, cells in rows.items():
        if need == "__header__":
            continue
        assert cells[col], need  # every row states a licence, UNKNOWN or NOT_APPLICABLE - never left blank
    lic = {k: v[col] for k, v in rows.items() if k != "__header__"}
    assert "MIT" in lic["Lint"] and "Apache-2.0" in lic["Lint"]  # ruff MIT, typescript Apache-2.0
    assert "MIT" in lic["Type check"] and "MIT" in lic["Controlled tests"]
    assert "Apache Software License" in lic["Dependency vulnerabilities"]  # pip-audit: classifier, no SPDX field
    assert "Apache" in lic["SAST"] and "Apache-2.0" in lic["Browser + basic a11y"]


def test_installed_tool_licences_match_what_the_adr_records():
    """The ADR's metadata claims for tools present in this environment are checked against the metadata itself."""
    from importlib import metadata

    rows = _adr_rows()
    col = rows["__header__"].index("Licence (source)")
    for dist, need in (("ruff", "Lint"), ("mypy", "Type check"), ("pytest", "Controlled tests"),
                       ("playwright", "Browser + basic a11y")):
        try:
            expr = metadata.distribution(dist).metadata.get("License-Expression")
        except metadata.PackageNotFoundError:
            continue
        assert expr and f"{dist} {metadata.version(dist)}: {expr}" in rows[need][col], (dist, rows[need][col])


def test_adr_0095_lists_the_restore_tests():
    rows = _adr_rows()
    restore = rows.get("Restore test")
    assert restore is not None
    assert "tests/unit/test_operations_backup.py" in restore[1] and "G2" in restore[2]
    assert (ROOT / "tests" / "unit" / "test_operations_backup.py").is_file()
