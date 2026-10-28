"""One source of truth for protected/restricted repository paths (§09, §16, §21, §25).

The compiler (``aeos.planning.dag``), the worker runtime (``aeos.worker_runtime.scope``) and the
independent verifier (``aeos.verification.scope``) all import these lists, so a path that one of them
quarantines can never slip past another (audit 2026-10-19: the three copies had drifted apart).

* ``PLATFORM_PROTECTED_PATHS`` - the self-build list of AGENTS.md "Protected paths": the trusted
  controller's policy, verifier, broker, identity, acceptance suite, pipeline, manifest and
  migrations, plus the trust-relevant controller code the master names for special review (router,
  tool permissions/sandbox, capability gates, budgets, evaluator, auth/CORS/config, admission,
  secrets, prompts/instructions). A candidate touching them is quarantined for special review.
* ``GENERIC_TRUSTED_PATHS`` - anchored trusted pipeline / grader / agent-configuration paths of ANY
  managed project. The planner forbids them by default; runtime and verifier quarantine them.
* ``GENERIC_RESTRICTED_AREAS`` - restricted areas of any project (§16 Platform/DevSecOps: policy,
  IaC, IAM, secrets, deploy targets; mostly unanchored). Matched against concrete changed paths only (runtime and
  verifier quarantine); never used for the planner's conservative prefix-overlap test, where a
  leading ``**`` would overlap every task.
* ``GENERIC_PROTECTED_PATHS`` = trusted paths + restricted areas (runtime and verifier view).
"""

from __future__ import annotations

import re

PLATFORM_PROTECTED_PATHS: tuple[str, ...] = (
    # AGENTS.md core list (trusted controller, §09)
    "policy/**",
    "backend/aeos/policy/**",
    "backend/aeos/verification/**",
    "backend/aeos/broker/**",
    "backend/aeos/identity/**",
    "tests/acceptance/**",
    ".github/workflows/**",
    "install-manifest/**",
    "backend/aeos/migrations/**",
    # §09 trust-relevant controller code: router, tool permissions/sandbox, capability gates, budgets,
    # evaluator, admission, auth/CORS/config, secrets, supervisor bridge
    "backend/aeos/routing/**",
    "backend/aeos/worker_runtime/**",
    "backend/aeos/doctor/**",
    "backend/aeos/cost/**",
    "backend/aeos/evaluation/**",
    "backend/aeos/workforce/**",
    "backend/aeos/api/**",
    "backend/aeos/core/**",
    "backend/aeos/secrets/**",
    "backend/aeos/claude_bridge/**",
    # the audit/event trail and the dashboard projections (§50 T2 #2.14: a candidate must not be able to
    # change what the active audit/dashboard/controller reports about it)
    "backend/aeos/audit/**",
    "backend/aeos/events/**",
    "backend/aeos/control_center/**",
    # the authoritative design (U-23: the master changes only with traceable approval) and §25
    # prompt/instruction/tool changes are system changes
    "docs/architecture/**",
    "backend/aeos/planning/prompts.py",
    "AGENTS.md",
    "CLAUDE.md",
    # bootstrap and the dependency set the controller is built from (§07, §38)
    "scripts/bootstrap.*",
    "pyproject.toml",
    "requirements.lock",
)

GENERIC_TRUSTED_PATHS: tuple[str, ...] = (
    # §21 grader / held-out tests / trusted pipeline (the AEOS ``policy/`` tree is a platform path: in a
    # managed project ``policy/`` is that project's own code)
    "tests/acceptance/**",
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "bitbucket-pipelines.yml",
    "azure-pipelines.yml",
    ".circleci/**",
    # §25 repository-level agent configuration / instructions (OpenCode project config, rules)
    "opencode.json",
    "opencode.jsonc",
    ".opencode/**",
    "AGENTS.md",
    "CLAUDE.md",
)

GENERIC_RESTRICTED_AREAS: tuple[str, ...] = (
    # §16 restricted areas: policy / IaC / IAM / secrets / deploy targets of any managed project. The
    # project's top-level ``policy/`` tree was quarantined by the verifier before the lists were shared
    # (review 2026-10-19: keep it - sharing one list must never drop a protection).
    "policy/**",
    "**/*.tf",
    "**/*.tfvars",
    "**/iam/**",
    "**/rbac*.yaml",
    "**/rbac*.yml",
    "**/.env",
    "**/.env.*",
    "**/secrets/**",
    "**/deploy/**",
    "**/k8s/**",
    "**/kubernetes/**",
    "**/helm/**",
    "**/.claude/**",
    # owner rule R44 (ADR-0132): attributes decide what a diff shows (``* -diff`` hides a change from the reviewer);
    # the verifier reads none of them, and a candidate that changes one goes to special review
    "**/.gitattributes",
)

GENERIC_PROTECTED_PATHS: tuple[str, ...] = GENERIC_TRUSTED_PATHS + GENERIC_RESTRICTED_AREAS


def platform_protected_paths() -> tuple[str, ...]:
    """Everything a self-build candidate is quarantined for: the platform list plus the generic view."""
    return tuple(dict.fromkeys(PLATFORM_PROTECTED_PATHS + GENERIC_PROTECTED_PATHS))


_SECTION = re.compile(r"^## Protected paths.*?$(.*?)(?=^## |\Z)", flags=re.M | re.S)
_TOKEN = re.compile(r"`([^`\s]+)`")


def parse_agents_md(text: str) -> tuple[str, ...]:
    """Backticked path patterns of the first paragraph of the AGENTS.md "Protected paths" section (the
    list itself; later paragraphs explain it). Empty when the section is absent."""
    m = _SECTION.search(text)
    if not m:
        return ()
    paragraphs = [p for p in re.split(r"\n\s*\n", m.group(1).replace("\r\n", "\n")) if p.strip()]
    return tuple(dict.fromkeys(_TOKEN.findall(paragraphs[0]))) if paragraphs else ()
