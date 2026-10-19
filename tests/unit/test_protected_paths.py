"""One protected-path definition for compiler, runtime and verifier (§09, §16, §21, §25; audit 2026-10-19)."""

from __future__ import annotations

import re

import pytest

from aeos.core import protected_paths as pp
from aeos.core.paths import repo_root
from aeos.planning import dag
from aeos.verification import scope as vscope
from aeos.worker_runtime import scope as wscope


def _sample(pattern: str) -> str:
    """A concrete repository path the pattern must match (``**`` -> one directory, ``*`` -> a name)."""
    p = pattern.replace("**/", "x/").replace("/**", "/x/y.py").replace("**", "x").replace("*", "x")
    return re.sub(r"/+", "/", p).strip("/")


def test_agents_md_section_equals_the_canonical_platform_list():
    text = (repo_root() / "AGENTS.md").read_text(encoding="utf-8")
    assert pp.parse_agents_md(text) == pp.PLATFORM_PROTECTED_PATHS
    assert dag.protected_paths() == pp.PLATFORM_PROTECTED_PATHS


def test_compiler_runtime_and_verifier_share_one_definition():
    assert wscope.PLATFORM_PROTECTED_PATHS is pp.PLATFORM_PROTECTED_PATHS is vscope.PLATFORM_PROTECTED_PATHS
    assert wscope.GENERIC_PROTECTED_PATHS is pp.GENERIC_PROTECTED_PATHS is vscope.GENERIC_PROTECTED_PATHS
    assert dag.FALLBACK_PROTECTED_PATHS is pp.PLATFORM_PROTECTED_PATHS
    assert dag.GENERIC_TRUSTED_PATHS is pp.GENERIC_TRUSTED_PATHS
    # the compiler's anchored view is part of the runtime/verifier view (no path is forbidden-by-default at
    # compile time but silently accepted at run time, or the other way round)
    assert set(pp.GENERIC_TRUSTED_PATHS) <= set(pp.GENERIC_PROTECTED_PATHS)
    # CI files the earlier copies disagreed about
    for path in ("azure-pipelines.yml", ".circleci/**", ".gitlab-ci.yml", "tests/acceptance/**"):
        assert path in pp.GENERIC_TRUSTED_PATHS


@pytest.mark.parametrize("pattern", pp.platform_protected_paths())
def test_every_protected_pattern_quarantines_a_synthetic_change_in_runtime_and_verifier(pattern):
    path = _sample(pattern)
    runtime = wscope.check_scope([wscope.ChangedPath(path=path, status="M")], allowed=["**"], forbidden=[],
                                 protected=pp.platform_protected_paths())
    assert any(h["rule"] == "protected-path" for h in runtime.protected_hits), (pattern, path)
    verifier = vscope.check_scope([path], allowed=["**"], forbidden=[], protected=list(pp.platform_protected_paths()))
    assert verifier.status == "QUARANTINED", (pattern, path, verifier.to_dict()["violations"])


@pytest.mark.parametrize("path", [
    "backend/aeos/routing/service.py",       # router / routing policy (§09, §16, §21)
    "backend/aeos/worker_runtime/sandbox.py",  # tool permissions / sandbox
    "backend/aeos/doctor/capabilities.py",   # capability gates
    "backend/aeos/cost/service.py",          # budgets
    "backend/aeos/evaluation/gates.py",      # evaluator
    "backend/aeos/api/security.py",          # auth/CORS
    "backend/aeos/planning/prompts.py",      # prompts (§25 system change)
    "AGENTS.md", "CLAUDE.md", "pyproject.toml", "requirements.lock", "scripts/bootstrap.ps1",
])
def test_trust_relevant_controller_code_is_quarantined_for_self_build(path):
    res = wscope.check_scope([wscope.ChangedPath(path=path, status="M")], allowed=["**"], forbidden=[],
                             protected=pp.platform_protected_paths())
    assert res.quarantine and not res.violations


@pytest.mark.parametrize("path", [
    "infra/main.tf", "envs/prod.tfvars", "deploy/k8s/app.yaml", "k8s/rbac-admin.yaml", "iam/roles.json",
    "services/api/.env", ".env.production", "config/secrets/db.yaml", "charts/helm/values.yaml",
    "opencode.json", "opencode.jsonc", ".opencode/agent/x.md", "AGENTS.md", "CLAUDE.md", ".claude/settings.json",
    "azure-pipelines.yml", ".circleci/config.yml", ".gitlab-ci.yml", "bitbucket-pipelines.yml",
    "policy/access.rego",  # §16 'policy ... restricted area' (the verifier quarantined it before the lists merged)
])
def test_restricted_areas_of_any_managed_project_are_quarantined(path):
    """§16 Platform/DevSecOps: IaC/IAM/secret/deploy targets and §25 agent config are restricted areas."""
    res = wscope.check_scope([wscope.ChangedPath(path=path, status="M")], allowed=["**"], forbidden=[],
                             protected=pp.GENERIC_PROTECTED_PATHS)
    assert res.quarantine, path
    assert vscope.check_scope([path], allowed=["**"], forbidden=[],
                              protected=list(pp.GENERIC_PROTECTED_PATHS)).status == "QUARANTINED", path


def test_ordinary_product_code_is_not_restricted():
    res = wscope.check_scope([wscope.ChangedPath(path="src/app/main.py", status="M")], allowed=["src/**"],
                             forbidden=[], protected=pp.GENERIC_PROTECTED_PATHS)
    assert not res.quarantine
