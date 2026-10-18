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


def check(
    check_id: str, group: str, title: str, *, component_id: str | None = None, ttl_seconds: int = 3600,
    invalidators: tuple[str, ...] = (), ok: frozenset[str] | set[str] | None = None, needs_db: bool = False,
    functional: bool = False, network: bool = False, severity_fail: str = "MEDIUM",
    fingerprint: FingerprintFn | None = None, observation: str | None = None,
    scope: dict[str, Any] | None = None, description: str = "",
) -> Callable[[CheckFn], CheckFn]:
    if group not in GROUP_IDS:
        raise ValueError(f"unknown doctor group {group}")

    def deco(fn: CheckFn) -> CheckFn:
        if check_id in REGISTRY:
            raise ValueError(f"duplicate check id {check_id}")
        REGISTRY[check_id] = CheckSpec(
            check_id=check_id, group=group, title=title, fn=fn, component_id=component_id,
            ttl_seconds=ttl_seconds, invalidators=tuple(invalidators),
            ok_statuses=frozenset(ok) if ok is not None else OK_DEFAULT, needs_db=needs_db,
            functional=functional, network=network, severity_fail=severity_fail, fingerprint=fingerprint,
            observation=observation, scope=dict(scope or {}), description=description,
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


__all__ = ["REGISTRY", "CheckContext", "CheckSpec", "check", "load_all", "ordered_specs"]
