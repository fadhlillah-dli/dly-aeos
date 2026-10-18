"""Trusted acceptance suite identity (§09, §19, §21).

The trusted suite is read from the **trusted controller checkout** (``aeos.core.paths.repo_root()``),
never from a candidate workspace. Its identity is the hash of every suite file (plus the shared
``tests/__init__.py`` / ``tests/conftest.py`` fixtures it reuses). The verifier records this hash in
every piece of evidence so a changed grader is visible and invalidates acceptance (config hash).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash, file_sha256
from aeos.core.ids import new_id
from aeos.core.paths import UnsafePathError, ensure_within, repo_root

SUITE_DIR = ("tests", "acceptance")
SUPPORT_FILES = ("tests/__init__.py", "tests/conftest.py")
_IGNORED_SUFFIXES = {".pyc", ".pyo"}
# Project (non-platform) suites: copied by the controller into its own data dir, bound to an owner
# POLICY decision on the suite hash (integration amendment, ADR-0060).
PROJECT_SUITES_DIR = "trusted-suites"
SUITE_TARGET_TYPE = "trusted_suite"
PROJECT_SUITE_EXTRA = ("pyproject.toml",)  # trusted lint configuration travels with the suite


@dataclass(frozen=True)
class TrustedSuite:
    """Where the grader for a project comes from — never from a candidate workspace."""

    root: Path
    kind: str  # CONTROLLER (platform: trusted controller checkout) | PROJECT (registered copy)
    decision_id: str | None = None
    source_commit: str | None = None


def trusted_controller_root() -> Path:
    """Root of the trusted (released/seed) controller checkout. Tests may monkeypatch this."""
    return repo_root()


def trusted_suite_files(root: Path | None = None) -> list[tuple[str, Path]]:
    root = (root or trusted_controller_root()).resolve()
    suite_dir = root.joinpath(*SUITE_DIR)
    if not suite_dir.is_dir():
        raise Blocked("trusted acceptance suite not found in the controller checkout",
                      code="NO_TRUSTED_SUITE", details={"expected": "/".join(SUITE_DIR)})
    files: list[tuple[str, Path]] = []
    for rel in SUPPORT_FILES:
        p = root / rel
        if p.is_file():
            files.append((rel, p))
    for p in sorted(suite_dir.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in _IGNORED_SUFFIXES:
            continue
        files.append((p.relative_to(root).as_posix(), p))
    return files


def hash_files(files: list[tuple[str, Path]]) -> str:
    return content_hash(sorted([rel, file_sha256(p)] for rel, p in files))


def trusted_suite_hash(root: Path | None = None) -> str:
    return hash_files(trusted_suite_files(root))


def copy_trusted_suite(dest: Path, root: Path | None = None) -> tuple[list[tuple[str, Path]], str]:
    """Copy the trusted suite into a verifier-owned directory; return (copied files, hash of copies)."""
    copied: list[tuple[str, Path]] = []
    for rel, src in trusted_suite_files(root):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copied.append((rel, target))
    # Make sure package markers exist so `tests.conftest` stays importable from the copy.
    for pkg in ("tests", "tests/acceptance"):
        marker = dest / pkg / "__init__.py"
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")
    return copied, hash_files(copied)


# ----------------------------------------------------------------------------- project suites
# A managed project's grader must also live outside the candidate (§09, §19, §21 "grader/held-out
# tests" are read-only for workers). The owner registers it from a commit of the project's own
# repository (normally the reviewed default branch); the controller copies exactly that tree's
# ``tests/acceptance`` (+ shared ``tests/__init__.py``/``tests/conftest.py`` and the ``pyproject.toml``
# lint configuration) into its data dir and binds an owner POLICY decision to the suite hash. A copy
# that no longer matches the bound hash is refused (fail closed).


def project_suites_root() -> Path:
    from aeos.core.config import get_settings

    return get_settings().layout.root / PROJECT_SUITES_DIR


def _registration(session: Any, project_id: str) -> Any:
    """Newest ACTIVE, unexpired, human POLICY decision registering a suite for *project_id*."""
    from sqlalchemy import select

    from aeos.core.timeutil import is_expired
    from aeos.models.decisions import Decision

    rows = session.scalars(select(Decision).where(
        Decision.kind == "POLICY", Decision.target_type == SUITE_TARGET_TYPE, Decision.target_id == project_id,
        Decision.status == "ACTIVE").order_by(Decision.created_at.desc()))
    for d in rows:
        if (d.actor_kind or "HUMAN") == "HUMAN" and not is_expired(d.expires_at):
            return d
    return None


def resolve_trusted_suite(session: Any, project: Any) -> TrustedSuite:
    """The trusted suite of *project*: the controller checkout for the platform project, otherwise the
    owner-registered copy whose files still hash to the registration decision. Raises ``Blocked``."""
    if getattr(project, "is_platform", False):
        return TrustedSuite(root=trusted_controller_root().resolve(), kind="CONTROLLER")
    d = _registration(session, project.id)
    if d is None:
        raise Blocked("no trusted acceptance suite is registered outside the candidate for this project",
                      code="NO_TRUSTED_SUITE")
    base = project_suites_root() / project.id
    try:
        root = ensure_within(base, base / str((d.details or {}).get("dir") or "-"))
    except UnsafePathError as exc:
        raise Blocked("registered trusted suite location is invalid", code="NO_TRUSTED_SUITE") from exc
    if not root.joinpath(*SUITE_DIR).is_dir():
        raise Blocked("registered trusted suite files are missing", code="TRUSTED_SUITE_MISSING")
    if trusted_suite_hash(root) != d.target_hash:
        raise Blocked("registered trusted suite changed after the owner's registration decision (tamper)",
                      code="TRUSTED_SUITE_TAMPERED")
    return TrustedSuite(root=root.resolve(), kind="PROJECT", decision_id=d.id,
                        source_commit=(d.details or {}).get("source_commit"))


def has_registration(session: Any, project_id: str) -> bool:
    return _registration(session, project_id) is not None


def register_project_suite(session: Any, project: Any, *, actor_user_id: str, rationale: str,
                           commit: str | None = None) -> dict[str, Any]:
    """Owner registers the project's trusted suite from *commit* (default: the repository's default
    branch head) of its registered local repository. Read-only git plumbing; caller commits."""
    from sqlalchemy import select

    from aeos.audit import service as audit
    from aeos.models.portfolio import RepositoryReference
    from aeos.verification.procs import GitError, GitReader, SnapshotError, valid_sha

    if getattr(project, "is_platform", False):
        raise ValidationFailed("the platform project's trusted suite is the controller checkout; nothing to register")
    if not actor_user_id:
        raise ValidationFailed("a human owner must register the trusted suite")
    if len((rationale or "").strip()) < 5:
        raise ValidationFailed("a rationale (min 5 characters) is required")
    ref = session.scalar(select(RepositoryReference).where(RepositoryReference.project_id == project.id,
                                                           RepositoryReference.local_path_ref.is_not(None)))
    if ref is None or not ref.local_path_ref:
        raise NotFound("project has no registered local repository to read the suite from")
    dest_name = new_id().split("-")[0]
    dest = project_suites_root() / project.id / dest_name
    if dest.exists():
        raise Conflict("suite directory collision; retry", code="SUITE_DIR_EXISTS")
    try:
        git = GitReader(Path(ref.local_path_ref))
        if not git.is_repository():
            raise Blocked("registered local repository is not a git repository", code="REPOSITORY_INVALID")
        rev = commit or ref.default_branch or "HEAD"
        sha = git.resolve_commit(rev) if rev == "HEAD" or valid_sha(rev) else git.branch_head(rev)
        with tempfile.TemporaryDirectory(prefix="aeos-suite-") as td:
            tree = Path(td) / "t"
            tree.mkdir()
            git.export_tree(sha, tree, max_bytes=256 * 1024 * 1024)
            if not tree.joinpath(*SUITE_DIR).is_dir():
                raise Blocked(f"commit {sha[:12]} has no tests/acceptance directory", code="NO_TRUSTED_SUITE")
            for rel, src in trusted_suite_files(tree):
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
            for extra in PROJECT_SUITE_EXTRA:
                if (tree / extra).is_file():
                    shutil.copyfile(tree / extra, dest / extra)
    except (GitError, SnapshotError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise Blocked(f"suite could not be read from the repository: {exc}", code="REPOSITORY_UNREADABLE") from exc
    suite_hash = trusted_suite_hash(dest)
    files = len(trusted_suite_files(dest))
    previous = _registration(session, project.id)
    try:
        from aeos.policy import facade as policy

        decision = policy.record_decision(
            session, kind="POLICY", actor_user_id=actor_user_id, target_type=SUITE_TARGET_TYPE, target_id=project.id,
            target_hash=suite_hash, allowed_operations=["verify"],
            auth_scope={"project_id": project.id, "suite": "/".join(SUITE_DIR), "source_commit": sha},
            project_id=project.id, domain=project.domain, rationale=rationale,
            details={"dir": dest_name, "source_commit": sha, "files": files, "repository": ref.full_name})
    except ImportError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise Blocked("decision registry unavailable; suite not registered (fail closed)",
                      code="DECISION_REGISTRY_UNAVAILABLE") from exc
    if previous is not None and previous.id != decision.id:
        try:
            from aeos.policy import decisions as registry

            registry.supersede_decision(session, previous, new_decision_id=decision.id,
                                        reason="new trusted suite registered", actor_type="user",
                                        actor_id=actor_user_id)
        except ImportError:
            pass
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="verification.suite.register",
                 target_type="project", target_id=project.id, project_id=project.id, domain=project.domain,
                 reason=rationale, details={"suite_hash": suite_hash, "source_commit": sha, "files": files,
                                            "decision_id": decision.id})
    return {"project_id": project.id, "suite_hash": suite_hash, "source_commit": sha, "files": files,
            "decision_id": decision.id, "kind": "PROJECT"}


def suite_status(session: Any, project: Any) -> dict[str, Any]:
    try:
        ts = resolve_trusted_suite(session, project)
    except Blocked as exc:
        return {"project_id": project.id, "status": "BLOCKED", "code": exc.code, "reason": exc.message}
    return {"project_id": project.id, "status": "REGISTERED" if ts.kind == "PROJECT" else "CONTROLLER",
            "kind": ts.kind, "suite_hash": trusted_suite_hash(ts.root), "decision_id": ts.decision_id,
            "source_commit": ts.source_commit}
