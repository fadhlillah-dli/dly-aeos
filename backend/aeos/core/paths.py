"""OS-aware data locations (master §06).

Runtime state — database, attachments, traces, worker output, evidence, Office data — lives in a
data directory that is separate from the tracked source tree. Paths are derived from OS/profile
configuration, never from the repository cwd or an unvalidated project name.

Defaults:
  Windows: %LOCALAPPDATA%\\DLY-AEOS
  macOS:   ~/Library/Application Support/DLY-AEOS
  Linux:   $XDG_DATA_HOME/DLY-AEOS (fallback ~/.local/share/DLY-AEOS)
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import platformdirs

from aeos.core.enums import Domain

APP_NAME = "DLY-AEOS"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UnsafePathError(ValueError):
    """Raised when a path segment or resolved path would escape its managed root."""


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False, roaming=False))


def default_config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=False))


def safe_segment(value: str) -> str:
    """Validate a single path segment (project key, run id...). Rejects traversal and separators."""
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value) or value in {".", ".."}:
        raise UnsafePathError(f"unsafe path segment: {value!r}")
    return value


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve *candidate* (following symlinks) and require it to stay inside *root*."""
    root_r = root.resolve()
    cand_r = candidate.resolve()
    try:
        cand_r.relative_to(root_r)
    except ValueError as exc:
        raise UnsafePathError(f"path escapes managed root: {candidate}") from exc
    return cand_r


@dataclass(frozen=True)
class DataLayout:
    """All managed runtime locations under one data root."""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "aeos.sqlite3"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def supervisor_workspace(self) -> Path:
        """Trusted Claude supervisor workspace (§12) — never a worker or repo directory."""
        return self.root / "supervisor"

    @property
    def released_controller(self) -> Path:
        """Released controller artifacts (§09 trusted seed / accepted release)."""
        return self.root / "releases"

    @property
    def manual_packages(self) -> Path:
        """§12 fallback: planning/review packages the user runs in official Claude Code."""
        return self.root / "manual-claude"

    @property
    def canonical_policy(self) -> Path:
        """§02/§06 owner-supplied canonical policy sources (MCI_V2), kept on this machine only: the platform
        repository is public and never holds MCI content ("MCI tidak dimasukkan tanpa hak")."""
        return self.root / "policy" / "canonical"

    def domain_root(self, domain: Domain) -> Path:
        """Personal and Office partitions never share a directory (§04 evidence/memory/cache)."""
        return self.root / domain.value.lower()

    def project_root(self, domain: Domain, project_key: str) -> Path:
        return self.domain_root(domain) / "projects" / safe_segment(project_key)

    def attachments(self, domain: Domain, project_key: str) -> Path:
        return self.project_root(domain, project_key) / "attachments"

    def evidence(self, domain: Domain, project_key: str) -> Path:
        return self.project_root(domain, project_key) / "evidence"

    def task_data(self, domain: Domain, project_key: str) -> Path:
        """Worker sandboxes/worktrees (§06) — never committed to the platform repo."""
        return self.project_root(domain, project_key) / "task-data"

    def publications(self, domain: Domain, project_key: str) -> Path:
        return self.project_root(domain, project_key) / "publications"

    def ensure(self) -> None:
        """Create the layout private to the OS user: 0700 on POSIX, so other users cannot read the database or the
        PERSONAL/OFFICE partitions. A directory left open by an older bootstrap is tightened; one that cannot be
        changed stays visible as FAILED in the Doctor check evidence.dir_permissions. Windows uses ACLs instead."""
        for p in (
            self.root,
            self.backups,
            self.logs,
            self.supervisor_workspace,
            self.released_controller,
            self.manual_packages,
            self.domain_root(Domain.PERSONAL),
            self.domain_root(Domain.OFFICE),
        ):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
            if sys.platform != "win32" and p.stat().st_mode & 0o077:
                try:
                    p.chmod(0o700)
                except OSError:
                    pass


def repo_root() -> Path:
    """Root of the platform source checkout this controller runs from."""
    return Path(__file__).resolve().parents[3]
