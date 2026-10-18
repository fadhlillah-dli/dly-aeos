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
