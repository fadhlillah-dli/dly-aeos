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
# The required status checks of the ruleset on main (GitHub Actions app). The ruleset is owner configuration; these
# names must keep existing, each exactly once, and must only pass when everything they stand for passed.
REQUIRED_CHECKS = (
    *(f"backend [G1,G2] ({o})" for o in HOST_OSES),
    *(f"bootstrap [G2] ({o})" for o in HOST_OSES),
    "browser e2e + a11y smoke [G3]",
    "frontend [G1,G3]",
)


def test_every_required_check_of_the_main_ruleset_is_reported_by_exactly_one_job():
    names = [n for job in _workflow()["jobs"].values() for n in _check_names(job)]
    for required in REQUIRED_CHECKS:
        assert names.count(required) == 1, (required, names.count(required))


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


def test_g1_static_checks_run_once_in_their_own_job_that_every_backend_aggregator_needs():
    jobs = _workflow()["jobs"]
    job = jobs[STATIC_JOB]
    assert job["runs-on"] == "ubuntu-latest" and "[G1]" in job["name"] and "if" not in job
    assert "continue-on-error" not in job and "strategy" not in job
    static = [s for s in job["steps"] if str(s.get("name", "")).startswith("G1 static")]
    assert [s["run"].split("\n")[0] for s in static] == ["python -m ruff check backend tests scripts",
                                                        "python -m mypy --platform darwin",
                                                        "python scripts/secret_scan.py"]
    mypy = static[1]["run"].splitlines()
    assert mypy == ["python -m mypy --platform darwin", "python -m mypy --platform win32"]
    for step in static:  # never skipped, never allowed to fail
        assert "if" not in step and "continue-on-error" not in step, step["name"]
    for shard in _shard_jobs().values():  # the tests themselves never depend on a gate tag
        for step in shard["steps"]:
            if "pytest" in (step.get("run") or ""):
                assert "if" not in step
    for os_label in HOST_OSES:
        assert STATIC_JOB in _aggregator(os_label)[1]["needs"]


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


def test_every_job_has_a_timeout_so_a_hung_runner_cannot_hold_a_macos_slot_for_six_hours():
    """The default is 360 minutes; GitHub Free has 5 account-wide macOS slots."""
    caps = {"static-checks": 15, "backend-shards-": 25, "backend-verifier-": 20, "frontend": 15, "browser-e2e": 20,
            "bootstrap": 15, "supply-chain": 20}
    for jid, job in _workflow()["jobs"].items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and not isinstance(timeout, bool) and 0 < timeout <= 25, (jid, timeout)
        expected = next((v for k, v in caps.items() if jid.startswith(k)), 5)  # the rest are the aggregators
        assert timeout == expected, (jid, timeout)


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
