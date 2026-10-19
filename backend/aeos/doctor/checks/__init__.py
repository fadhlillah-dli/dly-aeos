"""Readiness Doctor check registry (§08 groups). Checks are computed from code/probes, never from
model narration; a check that cannot decide returns UNKNOWN (never a pass)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from aeos.doctor.model import GROUP_IDS, OK_DEFAULT, CheckContext, CheckFn, CheckSpec, FingerprintFn

REGISTRY: dict[str, CheckSpec] = {}

_MODULES = (
    "host", "repository", "runtime", "local_auth", "policy", "claude", "secret", "worker", "containment",
    "model_route", "adapters", "delivery", "evidence", "recovery",
)

# §34 evidence ladder per check (level without --functional, level with --functional). E1 = static read of
# files/config/records, E2 = controlled self-test on synthetic input, E3 = runtime probe of the real component
# on this host/target. A check not listed is E1 (conservative: never claims runtime evidence by default).
EVIDENCE_LEVELS: dict[str, tuple[str, str | None]] = {
    "host.platform": ("E3", None), "host.resource_budget": ("E3", None), "host.disk": ("E3", None),
    "host.clock": ("E1", "E3"), "host.port": ("E3", None),
    "host.data_dir": ("E3", None), "host.vscode": ("E3", None),
    "runtime.python": ("E3", None), "runtime.backend_deps": ("E3", None), "runtime.node": ("E3", None),
    "runtime.db": ("E3", None), "runtime.storage": ("E3", None),
    "auth.default_credentials": ("E2", None), "auth.http_guard": ("E2", None),
    "claude.binary": ("E3", None), "claude.auth": ("E3", None), "claude.env": ("E3", None),
    "claude.extra_billing": ("E0", None), "claude.quota": ("E0", None), "claude.capability_test": ("E3", None),
    "secret.keyring": ("E3", None), "secret.openbao_binary": ("E3", None), "secret.openbao_health": ("E3", None),
    "secret.openbao_auth": ("E3", None), "secret.status": ("E3", None),
    "worker.opencode": ("E3", None), "worker.contract": ("E2", None),
    "containment.report": ("E1", "E2"), "containment.docker": ("E3", None), "containment.protected_paths": ("E2", None),
    "adapter.github_token": ("E1", "E3"), "adapter.project_repo": ("E1", "E3"),
    "adapter.project_drive_root": ("E1", "E3"), "delivery.branch_protection": ("E1", "E3"),
    "evidence.dir_permissions": ("E3", None), "evidence.trace_redaction": ("E2", None),
}


def check(
    check_id: str, group: str, title: str, *, component_id: str | None = None, ttl_seconds: int = 3600,
    invalidators: tuple[str, ...] = (), ok: frozenset[str] | set[str] | None = None, needs_db: bool = False,
    functional: bool = False, network: bool = False, severity_fail: str = "MEDIUM",
    fingerprint: FingerprintFn | None = None, observation: str | None = None,
    scope: dict[str, Any] | None = None, description: str = "", level: str | None = None,
    functional_level: str | None = None, targets: Callable[[CheckContext], list[dict[str, Any]]] | None = None,
) -> Callable[[CheckFn], CheckFn]:
    if group not in GROUP_IDS:
        raise ValueError(f"unknown doctor group {group}")
    default_level, default_functional = EVIDENCE_LEVELS.get(check_id, ("E1", None))

    def deco(fn: CheckFn) -> CheckFn:
        if check_id in REGISTRY:
            raise ValueError(f"duplicate check id {check_id}")
        REGISTRY[check_id] = CheckSpec(
            check_id=check_id, group=group, title=title, fn=fn, component_id=component_id,
            ttl_seconds=ttl_seconds, invalidators=tuple(invalidators),
            ok_statuses=frozenset(ok) if ok is not None else OK_DEFAULT, needs_db=needs_db,
            functional=functional, network=network, severity_fail=severity_fail, fingerprint=fingerprint,
            observation=observation, scope=dict(scope or {}), description=description,
            evidence_level=level or default_level, functional_level=functional_level or default_functional,
            targets=targets,
        )
        return fn

    return deco


def load_all() -> dict[str, CheckSpec]:
    for name in _MODULES:
        importlib.import_module(f"aeos.doctor.checks.{name}")
    return REGISTRY


def ordered_specs() -> list[CheckSpec]:
    load_all()
    return sorted(REGISTRY.values(), key=lambda s: (GROUP_IDS.index(s.group), s.check_id))


__all__ = ["EVIDENCE_LEVELS", "REGISTRY", "CheckContext", "CheckSpec", "check", "load_all", "ordered_specs"]
