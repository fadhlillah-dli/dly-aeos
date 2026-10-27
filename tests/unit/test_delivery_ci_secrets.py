"""PR builds get no secrets (§31 "PR build tidak mendapat secret produksi"; §50 "PR code mencoba membaca
production secret" → secret tidak tersedia). Static guard over the platform CI workflows: no
``pull_request_target`` (which runs untrusted PR code with repository secrets), no ``secrets.*``
references, and a read-only default token. The workflows are a protected path (§09); this test only
reads them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from aeos.core.paths import repo_root

WORKFLOWS = sorted((repo_root() / ".github" / "workflows").glob("*.y*ml"))
WRITE_SCOPES = re.compile(r"\bwrite\b|write-all")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def _triggers(data: dict) -> set[str]:
    # PyYAML (YAML 1.1) parses the bare key ``on`` as boolean True.
    on = data.get("on", data.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    if isinstance(on, dict):
        return {str(k) for k in on}
    return set()


def test_platform_ci_workflows_exist():
    if not (repo_root() / ".git").exists() and not WORKFLOWS:
        pytest.skip("not running from a repository checkout")
    assert WORKFLOWS, "the platform CI workflow (.github/workflows/ci.yml) is missing"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_pr_builds_receive_no_secrets_and_a_read_only_token(path: Path):
    text = path.read_text(encoding="utf-8")
    data = _load(path)
    triggers = _triggers(data)
    assert "pull_request_target" not in triggers, "pull_request_target runs PR code with repository secrets"
    assert "workflow_run" not in triggers, "workflow_run can hand privileged context to PR artifacts"
    assert not re.search(r"\$\{\{\s*secrets\.", text), "no secrets.* reference may reach a PR build"
    assert _permission_problems(path.name, data) == []


# One explicit, minimal allow-list of job-level permission sets that hold a write scope: (workflow file, job id) ->
# the exact mapping that job must declare. Every other job and every top-level ``permissions`` stays read-only. The
# release evidence job (ADR-0122) signs a build-provenance attestation: Sigstore needs the OIDC token (id-token) and
# the attestation is stored with the repository (attestations). It writes nothing else: no contents, no releases.
JOB_PERMISSION_ALLOW_LIST: dict[tuple[str, str], dict[str, str]] = {
    ("release.yml", "release-evidence"): {"contents": "read", "actions": "read", "checks": "read",
                                          "id-token": "write", "attestations": "write"},
}


def _render(perms: object) -> str:
    return " ".join(f"{k}:{v}" for k, v in perms.items()) if isinstance(perms, dict) else str(perms)


def _permission_problems(name: str, data: dict) -> list[str]:
    problems = []
    perms = data.get("permissions")
    if perms is None:
        problems.append("the default GITHUB_TOKEN permissions must be declared (read-only)")
    elif WRITE_SCOPES.search(_render(perms)):
        problems.append(f"workflow token must be read-only, got {_render(perms)}")
    for jid, job in (data.get("jobs") or {}).items():
        job_perms = (job or {}).get("permissions")
        allowed = JOB_PERMISSION_ALLOW_LIST.get((name, jid))
        if allowed is not None:
            if job_perms != allowed:
                problems.append(f"job {jid}: permissions must be exactly {_render(allowed)}, got {_render(job_perms)}")
        elif job_perms is not None and WRITE_SCOPES.search(_render(job_perms)):
            problems.append(f"job {jid} requests write permissions: {_render(job_perms)}")
    return problems


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_full_commit_shas(path: Path):
    """§31 supply chain: actions are pinned (a moving tag could inject code into the trusted pipeline)."""
    for job in (_load(path).get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            uses = (step or {}).get("uses")
            if not uses or uses.startswith("./"):
                continue
            ref = uses.rsplit("@", 1)[-1] if "@" in uses else ""
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{uses} is not pinned to a full commit SHA"


def _action_pins_tool():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("aeos_script_action_pins_rules", repo_root() / "scripts" / "action_pins.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _yaml_action_refs(data: dict) -> set[str]:
    return {step["uses"] for job in (data.get("jobs") or {}).values() for step in (job or {}).get("steps") or []
            if (step or {}).get("uses") and not step["uses"].startswith("./")}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_pin_carries_a_version_comment_the_monthly_report_can_read(path: Path):
    """``scripts/action_pins.py --report`` (CONTRIBUTING "Developer tools") reads ``uses: owner/repo@<sha> # vX``; a pin
    without the comment would be UNKNOWN there. The tool's line reader must also agree with the parsed YAML."""
    tool = _action_pins_tool()
    pins = [p for p in tool.parse_workflow(path.read_text(encoding="utf-8"), path.name) if p.kind == "action"]
    assert not [p.name for p in pins if not p.tag], "a pin has no '# vX.Y.Z' version comment after the sha"
    in_yaml = {"/".join(spec.split("/")[:2]) + "@" + ref
               for spec, ref in (u.rsplit("@", 1) for u in _yaml_action_refs(_load(path)))}
    assert {f"{p.name}@{p.ref}" for p in pins} == in_yaml


def test_the_version_comment_rule_bites_on_a_missing_comment_and_passes_a_near_miss():
    tool = _action_pins_tool()
    sha = "a" * 40
    bad = tool.parse_workflow(f"      - uses: acme/tool@{sha}\n      - uses: acme/other@{sha} # pinned\n", "x.yml")
    assert [p.name for p in bad if not p.tag] == ["acme/tool", "acme/other"]  # violating: red, both named
    good = tool.parse_workflow(f"      - uses: acme/tool/sub@{sha} # v1.2.3\n", "x.yml")
    assert [(p.name, p.tag) for p in good] == [("acme/tool", "v1.2.3")]  # near miss: a sub-path action with its comment


# ------------------------------------------------------------------ rules for every workflow file (one owner: here)
# A timeout on every job (the default is 360 minutes and GitHub Free has 5 account-wide macOS slots), no macOS runner
# outside ci.yml (a pull request title or lint check is platform-neutral and a macOS slot is scarce), and no
# ``${{ }}`` expression other than a matrix value inside ``run:`` (script injection: an untrusted value such as a pull
# request title or a branch name has to reach the shell through ``env:``).
MAX_JOB_MINUTES = 25
MATRIX_VALUE = re.compile(r"\s*matrix\.[A-Za-z0-9_-]+\s*")


def _timeout_problems(data: dict) -> list[str]:
    problems = []
    for jid, job in (data.get("jobs") or {}).items():
        timeout = (job or {}).get("timeout-minutes")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 < timeout <= MAX_JOB_MINUTES:
            problems.append(f"job {jid}: timeout-minutes must be an integer in 1..{MAX_JOB_MINUTES}, got {timeout!r}")
    return problems


def _macos_problems(name: str, data: dict) -> list[str]:
    if name == "ci.yml":
        return []
    return [f"job {jid}: runs on {(job or {}).get('runs-on')!r}; only ci.yml may use a macOS runner"
            for jid, job in (data.get("jobs") or {}).items() if "macos" in str((job or {}).get("runs-on")).lower()]


def _run_interpolation_problems(data: dict) -> list[str]:
    problems = []
    for jid, job in (data.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            for expr in re.findall(r"\$\{\{(.*?)\}\}", str((step or {}).get("run", "")), flags=re.S):
                if not MATRIX_VALUE.fullmatch(expr):
                    problems.append(f"job {jid}, step {(step or {}).get('name')}: run: interpolates {{{{{expr}}}}}; "
                                    "pass it through env:")
    return problems


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_a_timeout_no_macos_outside_ci_and_no_expression_in_run(path: Path):
    data = _load(path)
    assert _timeout_problems(data) == []
    assert _macos_problems(path.name, data) == []
    assert _run_interpolation_problems(data) == []


def test_the_every_workflow_rules_bite_on_a_violation_and_pass_a_near_miss():
    import copy

    ok = {"jobs": {"a": {"runs-on": "ubuntu-latest", "timeout-minutes": 5,
                         "steps": [{"name": "s", "run": "echo ${{ matrix.os }} \"$PR_TITLE\"",
                                    "env": {"PR_TITLE": "${{ github.event.pull_request.title }}"}}]}}}
    assert _timeout_problems(ok) == [] and _macos_problems("pr-title.yml", ok) == []
    assert _run_interpolation_problems(ok) == []  # near miss: a matrix value in run:, the title in env:

    for value in (None, 0, 26, "5", True):
        bad = copy.deepcopy(ok)
        bad["jobs"]["a"]["timeout-minutes"] = value
        assert _timeout_problems(bad) == [
            f"job a: timeout-minutes must be an integer in 1..25, got {value!r}"], value
    missing = copy.deepcopy(ok)
    del missing["jobs"]["a"]["timeout-minutes"]
    assert "job a: timeout-minutes must be an integer" in _timeout_problems(missing)[0]
    edge = copy.deepcopy(ok)
    edge["jobs"]["a"]["timeout-minutes"] = 25
    assert _timeout_problems(edge) == []

    mac = copy.deepcopy(ok)
    mac["jobs"]["a"]["runs-on"] = "macos-26-intel"
    assert _macos_problems("pr-title.yml", mac) == [
        "job a: runs on 'macos-26-intel'; only ci.yml may use a macOS runner"]
    assert _macos_problems("ci.yml", mac) == []  # ci.yml is the one place for macOS runners

    for expr in ("github.event.pull_request.title", "github.head_ref", "matrix.os == 'x'", "matrix.a.b"):
        injected = copy.deepcopy(ok)
        injected["jobs"]["a"]["steps"][0]["run"] = f"echo '${{{{ {expr} }}}}'"
        (problem,) = _run_interpolation_problems(injected)
        assert problem == f"job a, step s: run: interpolates {{{{ {expr} }}}}; pass it through env:", expr


# ------------------------------------------------------------------ release.yml (ADR-0122; its rules are owned here)
# A pushed tag vX.Y.Z builds release evidence and uploads it; it never publishes a release. The checks below pin the
# shape that makes that safe: tags only, one ubuntu job with a timeout, the release check and a GREEN CI verdict for the
# tagged commit before anything is built, no step that may fail softly, and the tag reaching the shell only as the
# runner's GITHUB_REF_NAME. Reuse of ci.yml's pins and SBOM command is checked in tests/unit/test_ci_gates.py.
RELEASE = "release.yml"
RELEASE_JOB = "release-evidence"
RELEASE_CHECK_RUN = 'python scripts/release_check.py --tag "$GITHUB_REF_NAME"'
CI_STATUS_RUN = re.compile(r'python scripts/ci_status\.py --sha "\$GITHUB_SHA" --repo "\$GITHUB_REPOSITORY" '
                           r"--required-only --wait --deadline (\d+)")
# (label, predicate) in the order the steps must run.
RELEASE_STEPS = (
    ("release check", lambda s: s.get("run") == RELEASE_CHECK_RUN),
    ("CI status", lambda s: bool(CI_STATUS_RUN.fullmatch(str(s.get("run", ""))))),
    ("git archive", lambda s: "git archive --format=zip" in str(s.get("run", ""))),
    ("SBOM", lambda s: "scripts/supply_chain.py sbom -o dist/" in str(s.get("run", ""))),
    ("SHA256SUMS", lambda s: "sha256sum" in str(s.get("run", ""))),
    ("attestation", lambda s: str(s.get("uses", "")).startswith("actions/attest-build-provenance@")),
    ("upload", lambda s: str(s.get("uses", "")).startswith("actions/upload-artifact@")),
)


def _release_problems(data: dict) -> list[str]:
    problems = []
    on = data.get("on", data.get(True))
    if on != {"push": {"tags": ["v*.*.*"]}}:
        problems.append(f"on: must be a push of tags ['v*.*.*'] only, got {on!r}")
    jobs = data.get("jobs") or {}
    if list(jobs) != [RELEASE_JOB]:
        return problems + [f"jobs must be exactly ['{RELEASE_JOB}'], got {list(jobs)}"]
    job = jobs[RELEASE_JOB] or {}
    for key, want in (("name", "release evidence [NO-GATE]"), ("runs-on", "ubuntu-latest"), ("timeout-minutes", 20)):
        if job.get(key) != want:
            problems.append(f"job {RELEASE_JOB}: {key} must be {want!r}, got {job.get(key)!r}")
    steps = job.get("steps") or []
    for where, block in ((f"job {RELEASE_JOB}", job), *((f"step {s.get('name') or s.get('uses')}", s) for s in steps)):
        for key in ("if", "continue-on-error"):
            if key in block:
                problems.append(f"{where}: '{key}' could let the job go on past a failed check")
    found = [(label, next((i for i, s in enumerate(steps) if pred(s)), None)) for label, pred in RELEASE_STEPS]
    problems += [f"step '{label}' is missing" for label, i in found if i is None]
    present = [(label, i) for label, i in found if i is not None]
    problems += [f"step '{a}' must run before '{b}'" for (a, i), (b, j) in zip(present, present[1:]) if not i < j]
    for step in steps:
        match = CI_STATUS_RUN.fullmatch(str(step.get("run", "")))
        if match:
            if step.get("env") != {"GH_TOKEN": "${{ github.token }}"}:
                problems.append(f"CI status: env must be exactly GH_TOKEN from github.token, got {step.get('env')!r}")
            if not 0 < int(match.group(1)) < (job.get("timeout-minutes") or 0):
                problems.append("CI status: --deadline must end before the job's timeout-minutes")
        uses = str(step.get("uses", ""))
        if uses.startswith("actions/checkout@") and (step.get("with") or {}).get("fetch-depth") != 0:
            problems.append("checkout: fetch-depth must be 0 (release_check.py needs origin/main)")
        if uses.startswith("actions/attest-build-provenance@") and step.get("with") != {"subject-path": "dist/*"}:
            problems.append(f"attestation: with must be exactly subject-path dist/*, got {step.get('with')!r}")
        if uses.startswith("actions/upload-artifact@"):
            given = step.get("with") or {}
            if given.get("path") != "dist/" or given.get("retention-days") != 90:
                problems.append(f"upload: path dist/ with retention-days 90, got {given!r}")
    return problems


def _release_workflow() -> dict:
    path = repo_root() / ".github" / "workflows" / RELEASE
    if not path.is_file():
        pytest.skip("not running from a repository checkout")
    return _load(path)


def test_release_workflow_builds_evidence_only_after_the_release_check_and_a_green_ci_verdict():
    data = _release_workflow()
    assert _release_problems(data) == []
    assert _permission_problems(RELEASE, data) == []
    assert _macos_problems(RELEASE, data) == [] and _run_interpolation_problems(data) == []
    assert data["permissions"] == {"contents": "read"}  # write scopes only at job level, only the allow-listed two


def test_the_release_rules_bite_on_a_branch_trigger_a_write_scope_macos_and_an_interpolated_tag():
    import copy

    real = _release_workflow()
    job = lambda d: d["jobs"][RELEASE_JOB]  # noqa: E731

    def mutated(edit) -> dict:
        doc = copy.deepcopy(real)
        edit(doc)
        return doc

    branches = mutated(lambda d: d.update({"on": {"push": {"tags": ["v*.*.*"], "branches": ["main"]}}}))
    assert _release_problems(branches) == [
        "on: must be a push of tags ['v*.*.*'] only, got {'push': {'tags': ['v*.*.*'], 'branches': ['main']}}"]

    contents = mutated(lambda d: job(d)["permissions"].update({"contents": "write"}))
    (problem,) = _permission_problems(RELEASE, contents)
    assert problem.startswith("job release-evidence: permissions must be exactly contents:read actions:read") \
        and problem.endswith("got contents:write actions:read checks:read id-token:write attestations:write")
    top = mutated(lambda d: d.update({"permissions": {"contents": "write"}}))
    assert _permission_problems(RELEASE, top) == ["workflow token must be read-only, got contents:write"]
    other = mutated(lambda d: d["jobs"].update({"extra": {"permissions": {"id-token": "write"}}}))
    assert "job extra requests write permissions: id-token:write" in _permission_problems(RELEASE, other)
    elsewhere = _permission_problems("pr-title.yml", real)  # the allow-list is keyed on the file too
    assert elsewhere == ["job release-evidence requests write permissions: contents:read actions:read checks:read "
                         "id-token:write attestations:write"]

    mac = mutated(lambda d: job(d).update({"runs-on": "macos-latest"}))
    assert _macos_problems(RELEASE, mac) == ["job release-evidence: runs on 'macos-latest'; only ci.yml may use a macOS runner"]
    assert "job release-evidence: runs-on must be 'ubuntu-latest', got 'macos-latest'" in _release_problems(mac)

    def interpolate(d: dict) -> None:
        step = next(s for s in job(d)["steps"] if s.get("run") == RELEASE_CHECK_RUN)
        step["run"] = 'python scripts/release_check.py --tag "${{ github.ref_name }}"'

    injected = mutated(interpolate)
    assert _run_interpolation_problems(injected) == [
        "job release-evidence, step Release check of the tagged commit: run: interpolates {{ github.ref_name }}; "
        "pass it through env:"]
    assert "step 'release check' is missing" in _release_problems(injected)

    def late_check(d: dict) -> None:
        steps = job(d)["steps"]
        steps.append(steps.pop(next(i for i, s in enumerate(steps) if CI_STATUS_RUN.fullmatch(str(s.get("run", ""))))))

    assert "step 'CI status' must run before 'git archive'" in _release_problems(mutated(late_check))
    soft = mutated(lambda d: job(d)["steps"][3].update({"continue-on-error": True}))
    assert any("'continue-on-error' could let the job go on" in p for p in _release_problems(soft))
    long_wait = mutated(lambda d: job(d)["steps"][3].update(run=job(d)["steps"][3]["run"].replace("15", "30")))
    assert _release_problems(long_wait) == ["CI status: --deadline must end before the job's timeout-minutes"]
