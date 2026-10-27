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
    perms = data.get("permissions")
    assert perms is not None, "the default GITHUB_TOKEN permissions must be declared (read-only)"
    rendered = perms if isinstance(perms, str) else " ".join(f"{k}:{v}" for k, v in perms.items())
    assert not WRITE_SCOPES.search(str(rendered)), f"workflow token must be read-only, got {rendered}"
    for name, job in (data.get("jobs") or {}).items():
        job_perms = (job or {}).get("permissions")
        if job_perms is not None:
            rendered = job_perms if isinstance(job_perms, str) else " ".join(f"{k}:{v}" for k, v in job_perms.items())
            assert not WRITE_SCOPES.search(str(rendered)), f"job {name} requests write permissions"


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
