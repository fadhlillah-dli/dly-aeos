"""Readiness Doctor types (§08): check specs, context, outcomes, remediation proposals, results.

Every CheckResult carries the §08 fields: check ID, component version, project/domain/capability
scope, target identity, status/severity, observed evidence, last_checked_at, expiry/invalidators,
fingerprint, error code, remediation proposal and destructive/paid-write requirements. Messages and
evidence are redacted; secrets never appear.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.enums import CheckStatus
from aeos.inventory.probes import ProbeRunner

GROUPS: tuple[tuple[str, str], ...] = (
    ("HOST", "Host"),
    ("REPOSITORY", "Repository"),
    ("RUNTIME", "Runtime aplikasi"),
    ("LOCAL_AUTH", "Local auth"),
    ("CANONICAL_POLICY", "Canonical policy"),
    ("CLAUDE_NATIVE", "Claude native"),
    ("SECRET", "Secret"),
    ("WORKER", "Worker"),
    ("CONTAINMENT", "Containment"),
    ("MODEL_ROUTE", "Model route"),
    ("PROJECT_ADAPTERS", "Project adapters"),
    ("DELIVERY", "Delivery"),
    ("EVIDENCE_REVIEW", "Evidence/review"),
    ("RECOVERY", "Recovery"),
)
GROUP_IDS = tuple(g for g, _ in GROUPS)
GROUP_LABELS = dict(GROUPS)
OK_DEFAULT = frozenset({CheckStatus.VERIFIED.value})
STATUSES = frozenset(s.value for s in CheckStatus)


def aeos_cmd(args: str) -> str:
    """Launcher command for the current OS (copyable)."""
    return f".\\aeos.ps1 {args}" if sys.platform == "win32" else f"./aeos {args}"


def os_install_command(*, windows: str | None, macos: str | None, linux: str | None = None) -> str | None:
    """Copyable install command for the current OS, or None when no single official command applies
    (e.g. Linux distributions differ: the remedy then points to the official source instead of guessing)."""
    if sys.platform == "win32":
        return windows
    if sys.platform == "darwin":
        return macos
    return linux


def venv_python() -> str:
    return ".\\.venv\\Scripts\\python.exe" if sys.platform == "win32" else ".venv/bin/python"


@dataclass(frozen=True)
class Remedy:
    """A remediation PROPOSAL. AEOS never executes it (§08: no auto-install/IAM/hidden fix)."""

    operation: str
    command: str | None = None
    source: str | None = None  # official source / version reference
    target: str | None = None
    privilege: str = "pengguna lokal"
    cost: str = "tidak ada"
    rollback: str = "tidak diperlukan"
    evidence_to_collect: str = "jalankan ulang pemeriksaan ini"
    executed_by: str = "MANUSIA (salin dan jalankan sendiri)"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Outcome:
    status: str
    summary: str
    severity: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    component_version: str | None = None
    target_identity: str | None = None
    error_code: str | None = None
    remedy: Remedy | None = None
    requires_destructive: bool = False
    requires_paid_write: bool = False
    observations: list[dict[str, Any]] = field(default_factory=list)  # extra inventory observations

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid check status {self.status}")


@dataclass
class CheckContext:
    settings: Settings
    session: Session | None
    runner: ProbeRunner
    repo: Path
    now: datetime
    functional: bool = False
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    @property
    def db_exists(self) -> bool:
        return self.settings.layout.db_path.is_file()

    @property
    def probe_cwd(self) -> Path:
        sup = self.settings.layout.supervisor_workspace
        if sup.is_dir():
            return sup
        return self.data_dir if self.data_dir.is_dir() else Path(tempfile.gettempdir())


CheckFn = Callable[[CheckContext], Outcome]
FingerprintFn = Callable[[CheckContext], dict[str, Any] | None]


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    group: str
    title: str
    fn: CheckFn
    component_id: str | None = None
    ttl_seconds: int = 3600
    invalidators: tuple[str, ...] = ()
    ok_statuses: frozenset[str] = OK_DEFAULT
    needs_db: bool = False
    functional: bool = False  # opt-in heavier check (--functional); never run implicitly
    network: bool = False  # leaves loopback (only with --functional)
    severity_fail: str = "MEDIUM"
    fingerprint: FingerprintFn | None = None
    observation: str | None = None  # inventory dimension this check informs
    scope: Mapping[str, Any] = field(default_factory=dict)  # e.g. {"domain": "OFFICE"}
    description: str = ""


@dataclass
class CheckResultData:
    check_id: str
    title: str
    group: str
    component_id: str | None
    component_version: str | None
    scope: dict[str, Any]
    target_identity: str | None
    status: str
    severity: str
    summary: str
    observed_evidence: dict[str, Any]
    last_checked_at: datetime
    expires_at: datetime | None
    invalidators: list[str]
    fingerprint: str | None
    error_code: str | None
    remediation: dict[str, Any]
    requires_destructive: bool
    requires_paid_write: bool
    run_id: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "observations"}
        d["last_checked_at"] = self.last_checked_at.isoformat()
        d["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return d
