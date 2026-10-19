"""Released controller artifacts: build, promotion, rollback (§09 zones, §40, §50).

Zones (§09): the *trusted released controller* is a pinned artifact; the *candidate* is a
branch/worktree workers may edit; only an *accepted release* (merged to ``main`` by a human, then
built here) can replace the active controller through a controlled, human-only promotion.

* ``build``: ``git archive`` of a commit that is reachable from ``main`` in a clean working tree,
  written to ``<data>/releases/<version>-<sha12>.zip`` with a provenance manifest (commit, tree
  hash, artifact SHA-256, schema head, built_at, builder). Candidate code is parsed (``ast``) but
  never imported into the trusted process.
* ``promote`` (CLI only, human, audited, recorded as a PROMOTION ``Decision`` bound to the artifact
  hash): integrity check -> compatibility check (migration head vs current DB revision) ->
  pre-upgrade backup + restore test -> smoke test in a subprocess (import the app factory from the
  extracted artifact, migrate a *copy* of the database, background services disabled so queued work
  is never executed twice, ``GET /api/health`` on a temporary loopback port) -> live migration only
  when required and the server is stopped -> ``releases/ACTIVE.json``
  updated keeping the previous release. Any failure keeps the old release active (§50 "Upgrade
  controller gagal").
* ``rollback`` returns the pointer to the previous release after the same integrity/compatibility/
  smoke checks. Code rollback never claims data rollback (§40): if the database is ahead of the
  previous release, rollback is refused with the exact backup to restore.

There is deliberately no HTTP route for promote/rollback: workers and candidates cannot reach them.
"""

from __future__ import annotations

import ast
import json
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.errors import AeosError, Blocked, NotFound, ValidationFailed
from aeos.core.hashing import file_sha256
from aeos.core.paths import UnsafePathError, ensure_within, repo_root, safe_segment
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.operations._util import (
    free_loopback_port,
    minimal_child_env,
    ops_tmp_dir,
    read_json,
    refuse_worker_context,
    rmtree_robust,
    server_status,
    write_json_atomic,
)

FORMAT = "aeos-release/1"
POINTER = "ACTIVE.json"
MIGRATIONS_PREFIX = "backend/aeos/migrations/versions/"
_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
_SCHEMA_RE = re.compile(r"""^SCHEMA_VERSION\s*=\s*["']([^"']+)["']""", re.M)

SmokeRunner = Callable[..., dict[str, Any]]


# ----------------------------------------------------------------------------------------- git


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              check=False, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise Blocked("git is not installed or not on PATH", code="GIT_MISSING") from exc
    except subprocess.TimeoutExpired as exc:
        raise Blocked(f"git {args[0]} timed out", code="GIT_TIMEOUT") from exc
    if proc.returncode != 0:
        raise ValidationFailed(f"git {args[0]} failed: {redact_text(proc.stderr.strip())[:300]}", code="GIT_FAILED")
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
                              check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _main_ref(repo: Path, main_branch: str) -> str:
    for ref in (f"refs/heads/{main_branch}", f"refs/remotes/origin/{main_branch}"):
        if _git_ok(repo, "rev-parse", "--verify", "--quiet", ref):
            return ref
    raise Blocked(f"no '{main_branch}' branch found (local or origin)", code="RELEASE_NO_MAIN")


def _redacted_remote(repo: Path) -> str | None:
    if not _git_ok(repo, "remote", "get-url", "origin"):
        return None
    url = _git(repo, "remote", "get-url", "origin").strip()
    return redact_text(re.sub(r"//[^@/]+@", "//", url))  # drop any embedded credentials


# ----------------------------------------------------------------------------------------- artifact parsing


def _safe_member(name: str) -> str:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise ValidationFailed(f"unsafe archive member: {name!r}")
    return pure.as_posix()


def _literal_assignments(source: str, names: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    tree = ast.parse(source)
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id in names and value is not None:
            try:
                out[target.id] = ast.literal_eval(value)
            except ValueError:
                continue
    return out


def parse_migrations(zf: zipfile.ZipFile) -> dict[str, Any]:
    """Revision graph of the artifact's Alembic migrations, parsed statically (never imported)."""
    revisions: dict[str, list[str]] = {}
    for name in zf.namelist():
        if not name.startswith(MIGRATIONS_PREFIX) or not name.endswith(".py") or name.count("/") != 4:
            continue
        values = _literal_assignments(zf.read(name).decode("utf-8", "replace"), {"revision", "down_revision"})
        rev = values.get("revision")
        if not isinstance(rev, str):
            continue
        down = values.get("down_revision")
        if down is None:
            downs: list[str] = []
        elif isinstance(down, str):
            downs = [down]
        else:
            downs = [str(d) for d in down]
        revisions[rev] = downs
    referenced = {d for downs in revisions.values() for d in downs}
    heads = sorted(r for r in revisions if r not in referenced)
    return {"revisions": revisions, "heads": heads}


# ----------------------------------------------------------------------------------------- build


def build_release(
    settings: Settings,
    *,
    repo: Path | None = None,
    ref: str = "HEAD",
    main_branch: str = "main",
    builder: str = "cli",
    session: Session | None = None,
) -> dict[str, Any]:
    repo = (repo or repo_root()).resolve()
    if not ref or ref.startswith("-") or any(ch.isspace() for ch in ref):
        raise ValidationFailed("invalid git ref (must be a commit/ref name, not an option)", code="RELEASE_BAD_REF")
    if not _git_ok(repo, "rev-parse", "--is-inside-work-tree"):
        raise Blocked(f"{repo} is not a git working tree", code="RELEASE_NOT_A_REPO")
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=normal").splitlines()
    if dirty:
        raise Blocked("working tree is dirty; commit or stash changes before building a release",
                      code="RELEASE_DIRTY", details={"changes": [redact_text(d) for d in dirty[:20]],
                                                     "count": len(dirty)})
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    main_ref = _main_ref(repo, main_branch)
    if not _git_ok(repo, "merge-base", "--is-ancestor", commit, main_ref):
        raise Blocked(f"commit {commit[:12]} is not on {main_branch}; only human-merged main commits can be "
                      "released (§09 accepted release)", code="RELEASE_NOT_ON_MAIN",
                      details={"commit": commit, "main_ref": main_ref})
    init_src = _git(repo, "show", f"{commit}:backend/aeos/__init__.py")
    version_m = _VERSION_RE.search(init_src)
    if not version_m:
        raise ValidationFailed("cannot determine __version__ of the commit", code="RELEASE_NO_VERSION")
    version = version_m.group(1)
    schema_m = _SCHEMA_RE.search(init_src)
    tree_hash = _git(repo, "rev-parse", f"{commit}^{{tree}}").strip()
    release_id = safe_segment(f"{version}-{commit[:12]}")
    out_dir = settings.layout.released_controller
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = out_dir / f"{release_id}.zip"
    prov_path = out_dir / f"{release_id}.provenance.json"
    if artifact.is_file() and prov_path.is_file():
        existing = read_json(prov_path) or {}
        if existing.get("artifact_sha256") == file_sha256(artifact) and existing.get("commit") == commit:
            return {**existing, "already_built": True}
        raise Blocked(f"release {release_id} exists but does not match its provenance; investigate before "
                      "rebuilding", code="RELEASE_TAMPERED")
    # §09 zone 3 / §31: only a commit that passed the relevant gates is built. "Reachable from main" is not
    # proof of verification (GitHub Free cannot enforce branch protection, ADR-0063), so the build also
    # requires trusted provider-CI PASS for this exact commit, or an owner EXCEPTION decision bound to it.
    if session is None:
        raise Blocked("release acceptance cannot be checked without the metadata database; refusing to build "
                      "(fail closed)", code="RELEASE_ACCEPTANCE_UNVERIFIED", details={"commit": commit})
    acceptance = release_acceptance(session, commit)
    if acceptance["status"] != "ACCEPTED":
        raise Blocked(f"commit {commit[:12]} has no recorded acceptance: no trusted provider-CI PASS for this "
                      "exact commit and no owner EXCEPTION decision. Sync CI evidence for the commit, or record "
                      f"an owner exception: aeos ops release accept --commit {commit} --operator <you> --reason ...",
                      code="RELEASE_NOT_ACCEPTED", details=acceptance)
    tmp = out_dir / f".{release_id}.zip.tmp"
    _git(repo, "archive", "--format=zip", "-o", str(tmp), commit, timeout=300)
    tmp.replace(artifact)
    with zipfile.ZipFile(artifact) as zf:
        migrations = parse_migrations(zf)
        file_count = len(zf.namelist())
    ui = _build_ui_bundle(repo, out_dir, release_id)
    git_version = _git(repo, "--version").strip()
    provenance = {
        "format": FORMAT,
        "release_id": release_id,
        "version": version,
        "schema_version": schema_m.group(1) if schema_m else None,
        "commit": commit,
        "tree_hash": tree_hash,
        "artifact": artifact.name,
        "artifact_sha256": file_sha256(artifact),
        "size_bytes": artifact.stat().st_size,
        "file_count": file_count,
        "schema_heads": migrations["heads"],
        "schema_head": migrations["heads"][0] if len(migrations["heads"]) == 1 else None,
        "migrations": migrations["revisions"],
        "built_at": utcnow().isoformat(),
        "builder": {"actor": builder, "platform": platform.system(), "python": sys.version.split()[0],
                    "git": git_version},
        "source": {"repo_slug": settings.platform_repo_slug, "remote": _redacted_remote(repo),
                   "main_ref": main_ref, "on_main": True, "clean_tree": True},
        "acceptance": acceptance,
        "ui": ui,
    }
    write_json_atomic(prov_path, provenance)
    from aeos.audit import service as audit

    audit.record(session, actor_type="cli", actor_id=builder, action="ops.release.build",
                 target_type="controller_release", target_id=release_id,
                 details={"commit": commit, "artifact_sha256": provenance["artifact_sha256"],
                          "schema_head": provenance["schema_head"],
                          "acceptance": {k: acceptance.get(k) for k in ("basis", "evidence_id", "decision_id")}})
    return provenance


# ----------------------------------------------------------------------------------------- acceptance

ACCEPTANCE_TARGET_TYPE = "controller_commit"
ACCEPTANCE_OPERATION = "release.build"


def _ci_overall(ev: Any) -> str:
    """Overall provider-CI conclusion of a trusted CI_STATUS evidence row (every check-run counts)."""
    from aeos.delivery import facade as delivery
    from aeos.verification import facade as verification

    data = verification.read_json(ev) or {}
    status = delivery.summarize_check_runs([r for r in data.get("runs") or [] if isinstance(r, dict)])
    if data.get("truncated") and status in ("PASS", "NO_CHECKS"):
        return "NOT_PROVEN"
    return status


def release_acceptance(session: Session, commit: str) -> dict[str, Any]:
    """Acceptance basis for building *commit* (§09 zone 3, §31 "artifact built once after relevant gates").

    ACCEPTED when trusted provider-CI evidence for the exact commit of the platform project concludes PASS
    (G5 retest), or when an ACTIVE owner EXCEPTION decision is bound to that commit. Anything else -
    no evidence, FAIL, PENDING, NOT_PROVEN - is NOT_ACCEPTED (UNKNOWN never passes)."""
    from sqlalchemy import select

    from aeos.models.portfolio import Project

    checked: list[dict[str, Any]] = []
    platform_project = session.scalar(select(Project).where(Project.is_platform.is_(True))
                                      .order_by(Project.created_at.asc()).limit(1))
    if platform_project is None:
        checked.append({"basis": "provider_ci", "status": "NO_PLATFORM_PROJECT"})
    else:
        from aeos.delivery.facade import latest_ci_evidence

        ev = latest_ci_evidence(session, platform_project.id, commit)
        if ev is None:
            checked.append({"basis": "provider_ci", "status": "NO_EVIDENCE"})
        else:
            status = _ci_overall(ev)
            checked.append({"basis": "provider_ci", "status": status, "evidence_id": ev.id})
            if status == "PASS":
                return {"status": "ACCEPTED", "basis": "provider_ci", "commit": commit, "evidence_id": ev.id,
                        "ci_status": status, "checked": checked}
    from aeos.policy import facade as policy

    decision = policy.find_active_decision(session, kind="EXCEPTION", target_type=ACCEPTANCE_TARGET_TYPE,
                                           target_id=commit, target_hash=commit, operation=ACCEPTANCE_OPERATION)
    if decision is not None:
        checked.append({"basis": "owner_exception", "status": "ACTIVE", "decision_id": decision.id})
        return {"status": "ACCEPTED", "basis": "owner_exception", "commit": commit, "decision_id": decision.id,
                "checked": checked}
    checked.append({"basis": "owner_exception", "status": "NONE"})
    return {"status": "NOT_ACCEPTED", "basis": None, "commit": commit, "checked": checked}


def record_acceptance_exception(session: Session, *, commit: str, operator_user_id: str, reason: str) -> str:
    """Human-only (CLI) owner EXCEPTION letting *commit* be built without trusted CI PASS. Bound to the exact
    commit id; the reason is mandatory and recorded."""
    refuse_worker_context("release acceptance exception")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit or ""):
        raise ValidationFailed("commit must be the full lowercase commit id", code="RELEASE_BAD_REF")
    if len((reason or "").strip()) < 10:
        raise ValidationFailed("an exception needs a reason (at least 10 characters)")
    from aeos.policy import facade as policy

    decision = policy.record_decision(
        session, kind="EXCEPTION", actor_user_id=operator_user_id, target_type=ACCEPTANCE_TARGET_TYPE,
        target_id=commit, target_hash=commit, allowed_operations=[ACCEPTANCE_OPERATION],
        auth_scope={"via": "cli", "operation": ACCEPTANCE_OPERATION, "commit": commit,
                    "replaces": "trusted provider-CI PASS for this commit"},
        rationale=reason.strip())
    return decision.id


# ----------------------------------------------------------------------------------------- UI bundle

UI_MEMBER_PREFIX = "frontend/dist/"


def _build_ui_bundle(repo: Path, out_dir: Path, release_id: str) -> dict[str, Any] | None:
    """Pin the Control Center bundle with the release: ``frontend/dist`` is a build output (not tracked),
    so it is zipped next to the artifact with its own SHA-256. Its correspondence to the commit is not
    provable from the build output alone and is labelled UNVERIFIED."""
    dist = repo / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        return None
    target = out_dir / f"{release_id}.ui.zip"
    tmp = out_dir / f".{release_id}.ui.zip.tmp"
    count = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(dist.rglob("*")):
            if path.is_file() and not path.is_symlink():
                zf.write(path, UI_MEMBER_PREFIX + path.relative_to(dist).as_posix())
                count += 1
    tmp.replace(target)
    return {"artifact": target.name, "sha256": file_sha256(target), "file_count": count,
            "source": "checkout frontend/dist at build time", "commit_correspondence": "UNVERIFIED"}


# ----------------------------------------------------------------------------------------- listing / pointer


def _pointer_path(settings: Settings) -> Path:
    return settings.layout.released_controller / POINTER


def read_pointer(settings: Settings) -> dict[str, Any]:
    return read_json(_pointer_path(settings)) or {"active": None, "previous": None, "history": []}


def load_provenance(settings: Settings, release_id: str) -> dict[str, Any]:
    try:
        safe_segment(release_id)
    except UnsafePathError as exc:
        raise ValidationFailed("invalid release id") from exc
    prov = read_json(settings.layout.released_controller / f"{release_id}.provenance.json")
    if prov is None or prov.get("format") != FORMAT:
        raise NotFound(f"release {release_id} not found")
    return prov


def artifact_integrity(settings: Settings, prov: dict[str, Any]) -> str:
    artifact = settings.layout.released_controller / str(prov.get("artifact", ""))
    if not artifact.is_file():
        return "MISSING"
    if file_sha256(artifact) != prov.get("artifact_sha256"):
        return "MISMATCH"
    ui = prov.get("ui")
    if isinstance(ui, dict):
        ui_zip = settings.layout.released_controller / str(ui.get("artifact", ""))
        if not ui_zip.is_file():
            return "MISSING"
        if file_sha256(ui_zip) != ui.get("sha256"):
            return "MISMATCH"
    return "OK"


def list_releases(settings: Settings) -> dict[str, Any]:
    out_dir = settings.layout.released_controller
    pointer = read_pointer(settings)
    active_id = (pointer.get("active") or {}).get("release_id")
    prev_id = (pointer.get("previous") or {}).get("release_id")
    items: list[dict[str, Any]] = []
    if out_dir.is_dir():
        for prov_file in sorted(out_dir.glob("*.provenance.json")):
            prov = read_json(prov_file)
            if not prov or prov.get("format") != FORMAT:
                continue
            rid = prov.get("release_id")
            items.append({
                "release_id": rid, "version": prov.get("version"), "commit": prov.get("commit"),
                "tree_hash": prov.get("tree_hash"), "artifact_sha256": prov.get("artifact_sha256"),
                "size_bytes": prov.get("size_bytes"), "schema_head": prov.get("schema_head"),
                "built_at": prov.get("built_at"), "builder": (prov.get("builder") or {}).get("actor"),
                "integrity": artifact_integrity(settings, prov),
                "role": "ACTIVE" if rid == active_id else "PREVIOUS" if rid == prev_id else "AVAILABLE",
            })
    items.sort(key=lambda i: i.get("built_at") or "", reverse=True)
    return {"items": items, "active": pointer.get("active"), "previous": pointer.get("previous"),
            "history": (pointer.get("history") or [])[-50:]}


# ----------------------------------------------------------------------------------------- compatibility


def db_revision(db_path: Path) -> tuple[str | None, bool]:
    """(alembic revision or None, database has application tables)."""
    if not db_path.is_file():
        return None, False
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rev = None
        if "alembic_version" in tables:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            rev = str(row[0]) if row else None
        app_tables = {t for t in tables if not t.startswith("sqlite_") and t != "alembic_version"}
        return rev, bool(app_tables)
    finally:
        conn.close()


def compatibility(settings: Settings, prov: dict[str, Any]) -> dict[str, Any]:
    current, has_tables = db_revision(settings.layout.db_path)
    heads = list(prov.get("schema_heads") or [])
    revisions = set((prov.get("migrations") or {}).keys())
    head = heads[0] if len(heads) == 1 else None
    base = {"current_revision": current, "release_head": head, "release_heads": heads}
    if len(heads) > 1:
        return {**base, "compatible": False, "action": "none",
                "reason": "release has multiple migration heads (ambiguous schema)"}
    if not has_tables:
        return {**base, "compatible": True, "action": "initialize", "reason": "no existing database tables"}
    if current is None:
        if head is None:
            return {**base, "compatible": True, "action": "none",
                    "reason": "database and release both use the create_all baseline (no revisions)"}
        return {**base, "compatible": True, "action": "stamp",
                "reason": "database has no revision stamp; upgrade will stamp it at the release head",
                "warning": "verify the schema matches the release head (dev create_all database)"}
    if head is None:
        return {**base, "compatible": False, "action": "none",
                "reason": f"database is at revision {current} but the release has no migrations (older release)"}
    if current == head:
        return {**base, "compatible": True, "action": "none", "reason": "database already at release head"}
    if current in revisions:
        return {**base, "compatible": True, "action": "upgrade",
                "reason": f"forward migration {current} -> {head}"}
    return {**base, "compatible": False, "action": "none",
            "reason": f"database revision {current} is unknown to the release (database is newer); downgrade "
                      "is not supported"}


# ----------------------------------------------------------------------------------------- extraction + subprocess


def extract_release(settings: Settings, prov: dict[str, Any]) -> Path:
    release_id = str(prov["release_id"])
    target = settings.layout.released_controller / release_id
    marker = target / ".aeos-extracted"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == prov.get("artifact_sha256"):
        return target
    if target.exists():
        rmtree_robust(target)
    target.mkdir(parents=True)
    for zip_path in _release_zips(settings, prov):
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = _safe_member(info.filename)
                dest = ensure_within(target, target / name)
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    marker.write_text(str(prov.get("artifact_sha256")), encoding="utf-8")
    return target


def _release_zips(settings: Settings, prov: dict[str, Any]) -> list[Path]:
    zips = [settings.layout.released_controller / str(prov["artifact"])]
    ui = prov.get("ui")
    if isinstance(ui, dict) and ui.get("artifact"):
        zips.append(settings.layout.released_controller / str(ui["artifact"]))
    return zips


_IGNORED_EXTRACTED = {"__pycache__", ".aeos-extracted"}


def verify_extracted(settings: Settings, prov: dict[str, Any], release_dir: Path) -> list[str]:
    """Compare the extracted tree with the verified artifact byte-for-byte (SHA-256 per member) and refuse
    files that are not in the artifact (a planted module). Returns the problems (empty = intact)."""
    import hashlib

    problems: list[str] = []
    expected: set[str] = set()
    for zip_path in _release_zips(settings, prov):
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = _safe_member(info.filename)
                expected.add(name)
                on_disk = release_dir / name
                if not on_disk.is_file() or on_disk.is_symlink():
                    problems.append(f"missing: {name}")
                    continue
                with zf.open(info) as src:
                    want = hashlib.sha256(src.read()).hexdigest()
                if hashlib.sha256(on_disk.read_bytes()).hexdigest() != want:
                    problems.append(f"modified: {name}")
    for path in release_dir.rglob("*"):
        rel = path.relative_to(release_dir)
        if any(part in _IGNORED_EXTRACTED for part in rel.parts) or path.is_dir():
            continue
        if rel.as_posix() not in expected:
            problems.append(f"unexpected: {rel.as_posix()}")
    return problems[:50]


def prepare_active_release(settings: Settings) -> dict[str, Any]:
    """Resolve, verify and extract the ACTIVE release so it can be run as the controller (§09 zones).

    Refuses (``Blocked``) when there is no active release, when the artifact (or UI bundle) does not match
    its provenance, when the pointer and provenance disagree, or when the extracted tree cannot be made to
    match the artifact exactly."""
    pointer = read_pointer(settings)
    active = pointer.get("active") or {}
    if not active.get("release_id"):
        raise Blocked("no release has been promoted yet (releases/ACTIVE.json has no active release)",
                      code="NO_ACTIVE_RELEASE")
    prov = _verified_prov(settings, str(active["release_id"]))
    if active.get("artifact_sha256") != prov.get("artifact_sha256") or active.get("commit") != prov.get("commit"):
        raise Blocked("the ACTIVE pointer does not match the release provenance; refusing to start",
                      code="RELEASE_INTEGRITY", details={"release_id": active["release_id"]})
    release_dir = extract_release(settings, prov)
    problems = verify_extracted(settings, prov, release_dir)
    if problems:  # re-extract once from the verified artifact, then require an exact match
        rmtree_robust(release_dir)
        release_dir = extract_release(settings, prov)
        problems = verify_extracted(settings, prov, release_dir)
    if problems:
        raise Blocked("the extracted release does not match its verified artifact; refusing to start",
                      code="RELEASE_INTEGRITY", details={"release_id": active["release_id"], "problems": problems})
    return {"release_id": prov["release_id"], "version": prov.get("version"), "commit": prov.get("commit"),
            "artifact_sha256": prov.get("artifact_sha256"), "dir": release_dir, "ui": prov.get("ui"),
            "acceptance": prov.get("acceptance")}


_SERVE_SCRIPT = r"""
import json, sys, pathlib
release_root = pathlib.Path(sys.argv[1]).resolve()
import aeos
if release_root not in pathlib.Path(aeos.__file__).resolve().parents:
    print(json.dumps({"ok": False, "error": "imported aeos from outside the release artifact"})); sys.exit(3)
import uvicorn
from aeos.core.config import get_settings
from aeos.core.db import init_engine
from aeos.core.migrate import upgrade_database
from aeos.api.app import create_app
settings = get_settings()
init_engine(settings)
upgrade_database()
uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
"""


def release_serve_command(settings: Settings, prepared: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """argv + environment that run the verified release as the control plane. ``PYTHONPATH`` names only
    the release, and byte-code is cached outside the release tree so a planted ``.pyc`` is never read."""
    import os

    release_dir = Path(prepared["dir"])
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["PYTHONPATH"] = str(release_dir / "backend")
    env["PYTHONPYCACHEPREFIX"] = str(settings.layout.root / "cache" / "release-pycache" / str(prepared["release_id"]))
    env["AEOS_DATA_DIR"] = str(settings.layout.root)
    # -P: never prepend the current directory (normally the editable checkout) to sys.path, so no module of
    # the checkout can shadow the release's own dependencies; only PYTHONPATH + site-packages are used.
    return [sys.executable, "-P", "-c", _SERVE_SCRIPT, str(release_dir)], env


def _checkout_commit(root: Path) -> tuple[str | None, bool | None]:
    if not (root / ".git").exists():
        return None, None
    try:
        head = _git(root, "rev-parse", "HEAD").strip()
        dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no").strip())
    except AeosError:
        return None, None
    return head, dirty


def code_identity(settings: Settings, code_root: Path | None = None) -> dict[str, Any]:
    """Identity of the code this process runs: ``release`` when AEOS is imported from an extracted release
    under the data dir, otherwise ``checkout`` (the editable source tree, which ``git pull`` or a local edit
    can change without any promotion)."""
    root = (code_root or repo_root()).resolve()
    releases_dir = settings.layout.released_controller.resolve()
    if releases_dir in root.parents:
        release_id = root.name
        try:
            prov = load_provenance(settings, release_id)
        except AeosError:
            prov = {}
        return {"mode": "release", "release_id": release_id, "commit": prov.get("commit"), "dirty": False}
    commit, dirty = _checkout_commit(root)
    return {"mode": "checkout", "release_id": None, "commit": commit, "dirty": dirty}


def running_controller(settings: Settings, code_root: Path | None = None,
                       identity: dict[str, Any] | None = None) -> dict[str, Any]:
    """Which code is running as the controller, compared with the ACTIVE release (§09 zones)."""
    active = (read_pointer(settings).get("active") or {})
    out: dict[str, Any] = {"active_release_id": active.get("release_id"), "active_commit": active.get("commit"),
                           "active_config_hash": active.get("config_hash")}
    out.update(identity or code_identity(settings, code_root))
    out.setdefault("source", "this process")
    if not active.get("release_id"):
        out["matches_active"] = None
        out["status"] = "NO_ACTIVE_RELEASE"
    elif out["mode"] == "release" and out["release_id"] == active.get("release_id"):
        out["matches_active"] = True
        out["status"] = "ACTIVE_RELEASE"
    else:
        out["matches_active"] = False
        out["status"] = "NOT_ACTIVE_RELEASE"
    return out


def probe_server_controller(settings: Settings) -> dict[str, Any] | None:
    """The ``controller`` block of the control plane running on host:port, or None when none answers."""
    import httpx

    try:
        r = httpx.get(f"{settings.base_url}/api/health", timeout=2.0)
        block = r.json().get("controller") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return block if isinstance(block, dict) else None


def controller_report(settings: Settings, session: Session | None = None,
                      probe: Callable[[Settings], dict[str, Any] | None] | None = None) -> dict[str, Any]:
    """What runs as the controller: the running server's own report when one answers on host:port,
    otherwise this process. Adds the runtime-config drift against the hash recorded at promotion."""
    served = (probe or probe_server_controller)(settings)
    if served is not None and served.get("mode") in ("release", "checkout"):
        report = {**served, "source": "running server"}
    else:
        report = running_controller(settings)
    active = read_pointer(settings).get("active") or {}
    recorded = active.get("config_hash")
    current = runtime_config(settings, session)["config_hash"]
    report["config_hash"] = current
    report["config_drift"] = None if not recorded else recorded != current
    return report


_SMOKE_SCRIPT = r"""
import json, sys, threading, time, pathlib
release_root = pathlib.Path(sys.argv[1]).resolve()
timeout = float(sys.argv[2])
out = {"ok": False}
try:
    import aeos
    loc = pathlib.Path(aeos.__file__).resolve()
    out["aeos_file_inside_release"] = release_root in loc.parents
    out["version"] = getattr(aeos, "__version__", None)
    if not out["aeos_file_inside_release"]:
        out["error"] = "imported aeos from outside the release artifact"
        print(json.dumps(out)); sys.exit(3)
    from aeos.core.config import get_settings
    from aeos.core.db import init_engine
    from aeos.core.migrate import upgrade_database
    settings = get_settings()
    init_engine(settings)
    out["migration"] = {k: str(v) for k, v in upgrade_database().items()}
    import httpx, uvicorn
    # The smoke instance runs against a COPY of the live database: background services (scheduler
    # loop, publication outbox worker, ...) must never start here, otherwise queued work could be
    # executed twice against real external targets. Clear the hook list in place before the app
    # lifespan reads it, and refuse to continue if that cannot be guaranteed (fail closed).
    import aeos.api.routers as _routers
    _hooks = getattr(_routers, "LIFECYCLE_HOOKS", [])
    out["lifecycle_hooks_disabled"] = list(_hooks)
    if isinstance(_hooks, list):
        _hooks.clear()
    import aeos.api.app as _appmod
    from aeos.api.app import create_app
    if list(getattr(_appmod, "LIFECYCLE_HOOKS", [])) or list(getattr(_routers, "LIFECYCLE_HOOKS", [])):
        out["error"] = "background services could not be disabled for the smoke test"
        print(json.dumps(out, default=str)); sys.exit(5)
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=settings.port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{settings.port}/api/health", timeout=2.0)
            try:
                body = r.json()
            except ValueError:
                body = {}
            out["health"] = {"status_code": r.status_code, "body": body}
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}"
            time.sleep(0.25)
    h = out.get("health") or {}
    health_ok = h.get("status_code") == 200 and (h.get("body") or {}).get("status") == "ok"
    functional = {"ok": False}
    if health_ok:
        # Functional smoke (§09 "health/function smoke tests"): a temporary SUPER_ADMIN exists only in this
        # COPY of the database; log in, choose PERSONAL explicitly, run the RUNTIME doctor group and read
        # capabilities and projects through the release's own API.
        try:
            import secrets as _secrets
            from aeos.core.db import session_scope
            from aeos.core.enums import Role
            from aeos.identity import service as _identity
            username = "smoke-" + _secrets.token_hex(4)
            password = "Sm0ke!" + _secrets.token_urlsafe(24)
            with session_scope() as _s:
                _identity.create_user(_s, username=username, password=password, role=Role.SUPER_ADMIN)
            base = f"http://127.0.0.1:{settings.port}"
            with httpx.Client(base_url=base, timeout=60.0, headers={"Origin": base}) as c:
                steps = {}
                r = c.post("/api/auth/login", json={"username": username, "password": password})
                steps["login"] = r.status_code
                csrf = (r.json() or {}).get("csrf_token", "") if r.status_code == 200 else ""
                hdr = {"X-CSRF-Token": csrf}
                r = c.post("/api/session/context", json={"domain": "PERSONAL"}, headers=hdr)
                steps["domain_select"] = r.status_code
                r = c.post("/api/doctor/run", json={"group": "RUNTIME"}, headers=hdr)
                steps["doctor_run"] = r.status_code
                run_items = (r.json() or {}).get("items") if r.status_code == 200 else None
                r = c.get("/api/doctor/capabilities")
                steps["capabilities"] = r.status_code
                caps = (r.json() or {}).get("items") if r.status_code == 200 else None
                r = c.get("/api/projects")
                steps["projects"] = r.status_code
                projects = (r.json() or {}).get("items") if r.status_code == 200 else None
            functional = {"steps": steps, "ok": all(v == 200 for v in steps.values()) and isinstance(run_items, list)
                          and isinstance(caps, list) and isinstance(projects, list)}
        except Exception as exc:
            functional = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    out["functional"] = functional
    server.should_exit = True
    thread.join(timeout=15)
    out["ok"] = health_ok and bool(functional.get("ok"))
    if not health_ok:
        out["error"] = "health check failed" + (f" ({last_error})" if last_error and not h else "")
    elif not out["ok"]:
        out["error"] = "functional smoke failed: " + json.dumps(functional, default=str)[:300]
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"[:500]
print(json.dumps(out, default=str))
sys.exit(0 if out["ok"] else 4)
"""

_MIGRATE_SCRIPT = r"""
import json, sys, pathlib
release_root = pathlib.Path(sys.argv[1]).resolve()
import aeos
if release_root not in pathlib.Path(aeos.__file__).resolve().parents:
    print(json.dumps({"ok": False, "error": "imported aeos from outside the release artifact"})); sys.exit(3)
from aeos.core.config import get_settings
from aeos.core.db import init_engine
from aeos.core.migrate import upgrade_database
init_engine(get_settings())
print(json.dumps({"ok": True, "migration": {k: str(v) for k, v in upgrade_database().items()}}))
"""


def _run_release_python(release_dir: Path, script: str, args: list[str], env_extra: dict[str, str],
                        cwd: Path, timeout: float) -> dict[str, Any]:
    env = minimal_child_env({"PYTHONPATH": str(release_dir / "backend"), **env_extra})
    try:
        proc = subprocess.run([sys.executable, "-c", script, str(release_dir), *args], cwd=str(cwd), env=env,
                              capture_output=True, text=True, timeout=timeout, check=False,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout:.0f}s"}
    last = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.strip().startswith("{")), "")
    try:
        data = json.loads(last) if last else {}
    except ValueError:
        data = {}
    if not data:
        data = {"ok": False, "error": "no result from release process"}
    if data.get("error"):
        data["error"] = redact_text(str(data["error"]))[:500]  # child output is untrusted text
    data["exit_code"] = proc.returncode
    if proc.returncode != 0 and proc.stderr:
        data["stderr_tail"] = redact_text(proc.stderr.strip()[-800:])
    data["ok"] = bool(data.get("ok")) and proc.returncode == 0
    return data


def run_smoke(settings: Settings, release_dir: Path, *, db_source: Path | None, timeout: float = 90) -> dict[str, Any]:
    """Start the release's app factory in a subprocess against a COPY of the database on a temporary
    loopback port and require ``GET /api/health`` -> 200/ok."""
    tmp = ops_tmp_dir(settings, "smoke-")
    try:
        data_dir = tmp / "data"
        data_dir.mkdir()
        if db_source is not None and db_source.is_file():
            shutil.copy2(db_source, data_dir / "aeos.sqlite3")
        port = free_loopback_port()
        env = {"AEOS_DATA_DIR": str(data_dir), "AEOS_CONFIG": str(tmp / "no-config.yaml"),
               "AEOS_PORT": str(port), "AEOS_HOST": "127.0.0.1", "AEOS_ENVIRONMENT": "smoke"}
        result = _run_release_python(release_dir, _SMOKE_SCRIPT, [str(max(5.0, timeout - 15))], env, tmp,
                                     timeout)
        result["port"] = port
        return result
    finally:
        try:
            rmtree_robust(tmp)
        except OSError:
            pass


def _run_live_migration(settings: Settings, release_dir: Path, timeout: float = 300) -> dict[str, Any]:
    env = {"AEOS_DATA_DIR": str(settings.layout.root), "AEOS_CONFIG": str(settings.layout.root / "no-config.yaml")}
    return _run_release_python(release_dir, _MIGRATE_SCRIPT, [], env, settings.layout.root, timeout)


# ----------------------------------------------------------------------------------------- promote / rollback


def runtime_config(settings: Settings, session: Session | None) -> dict[str, Any]:
    """Hash of the non-secret runtime configuration plus the active PolicySnapshot (§09 "promotion uses the
    artifact/config hash"). Settings never hold secrets (core/config.py), so the dump is safe to hash."""
    from aeos.core.hashing import content_hash

    settings_dump = settings.model_dump(mode="json")
    policy_hash = None
    if session is not None:
        from sqlalchemy import select

        from aeos.models.decisions import PolicySnapshot

        snap = session.scalar(select(PolicySnapshot).where(PolicySnapshot.active.is_(True))
                              .order_by(PolicySnapshot.created_at.desc()).limit(1))
        policy_hash = snap.content_hash if snap is not None else None
    settings_hash = content_hash(settings_dump)
    return {"config_hash": content_hash({"settings": settings_hash, "policy_snapshot": policy_hash}),
            "settings_hash": settings_hash, "policy_snapshot_hash": policy_hash}


def _record_decision(session: Session, *, operator_user_id: str, prov: dict[str, Any], operation: str,
                     reason: str, config: dict[str, Any] | None = None) -> str:
    from aeos.policy import facade as policy

    decision = policy.record_decision(
        session,
        kind="PROMOTION",
        actor_user_id=operator_user_id,
        target_type="controller_release",
        target_id=str(prov["release_id"]),
        target_version=str(prov.get("version")),
        target_hash=str(prov["artifact_sha256"]),
        allowed_operations=[operation],
        auth_scope={"via": "cli", "operation": operation, "commit": prov.get("commit"),
                    "ui_sha256": (prov.get("ui") or {}).get("sha256"),
                    "acceptance_basis": (prov.get("acceptance") or {}).get("basis"),
                    "config_hash": (config or {}).get("config_hash")},
        rationale=reason,
    )
    return decision.id


def _alert(session: Session, *, key: str, title: str, detail: dict[str, Any]) -> None:
    from aeos.operations import alerts

    alerts.raise_alert(session, dedup_key=key, kind="release_failure", severity="HIGH", title=title,
                       detail=detail, domain=None, source="release", auto_resolve=False)


def _verified_prov(settings: Settings, release_id: str) -> dict[str, Any]:
    prov = load_provenance(settings, release_id)
    integrity = artifact_integrity(settings, prov)
    if integrity != "OK":
        raise Blocked(f"release artifact integrity {integrity}; refusing", code="RELEASE_INTEGRITY",
                      details={"release_id": release_id, "integrity": integrity})
    return prov


def _pre_change_backup(settings: Settings, session: Session, *, label: str, actor: str) -> dict[str, Any] | None:
    from aeos.operations import backup

    if not settings.layout.db_path.is_file():
        return None
    created = backup.create_backup(settings, label=label, actor=actor, session=session)
    test = backup.restore_test(settings, created["backup_id"], session=session, actor=actor)
    session.commit()
    if test["status"] not in ("PASSED", "PASSED_UNRECORDED"):
        platform_view = backup.scope_result(test, None, platform_only=True) or {}
        raise Blocked("pre-upgrade backup failed its restore test; refusing to continue (details: "
                      f"aeos ops backup list / results.json of {created['backup_id']})",
                      code="PRE_UPGRADE_BACKUP_FAILED", details={"backup_id": created["backup_id"],
                                                                  "problems": platform_view.get("problems", [])})
    return {"backup_id": created["backup_id"], "restore_test": test["status"], "evidence_id": test["evidence_id"]}


def promote(
    settings: Settings,
    release_id: str,
    *,
    session: Session,
    operator_user_id: str,
    actor_label: str = "cli",
    reason: str = "",
    force_maintenance: bool = False,
    smoke_runner: SmokeRunner = run_smoke,
    server_check: Callable[[Settings], dict[str, Any]] = server_status,
    smoke_timeout: float = 90,
) -> dict[str, Any]:
    """Human-only promotion of a built release. Commits *session* at checkpoints (documented)."""
    refuse_worker_context("controller promotion")
    prov = _verified_prov(settings, release_id)
    return _transition(settings, session, action="promote", target_prov=prov, operator_user_id=operator_user_id,
                       actor_label=actor_label, reason=reason, force_maintenance=force_maintenance,
                       smoke_runner=smoke_runner, server_check=server_check, smoke_timeout=smoke_timeout)


def rollback(
    settings: Settings,
    *,
    session: Session,
    operator_user_id: str,
    actor_label: str = "cli",
    reason: str = "",
    force_maintenance: bool = False,
    smoke_runner: SmokeRunner = run_smoke,
    server_check: Callable[[Settings], dict[str, Any]] = server_status,
    smoke_timeout: float = 90,
) -> dict[str, Any]:
    """Return the active pointer to the previous release (code rollback only, never data)."""
    refuse_worker_context("controller rollback")
    pointer = read_pointer(settings)
    previous = pointer.get("previous")
    if not previous:
        raise Blocked("there is no previous release to roll back to", code="NO_PREVIOUS_RELEASE")
    prov = _verified_prov(settings, str(previous["release_id"]))
    return _transition(settings, session, action="rollback", target_prov=prov, operator_user_id=operator_user_id,
                       actor_label=actor_label, reason=reason, force_maintenance=force_maintenance,
                       smoke_runner=smoke_runner, server_check=server_check, smoke_timeout=smoke_timeout)


def _transition(
    settings: Settings,
    session: Session,
    *,
    action: str,
    target_prov: dict[str, Any],
    operator_user_id: str,
    actor_label: str,
    reason: str,
    force_maintenance: bool,
    smoke_runner: SmokeRunner,
    server_check: Callable[[Settings], dict[str, Any]],
    smoke_timeout: float,
) -> dict[str, Any]:
    from aeos.audit import service as audit
    from aeos.events import service as events

    release_id = str(target_prov["release_id"])
    pointer = read_pointer(settings)
    old_active = pointer.get("active")
    report: dict[str, Any] = {"action": action, "release_id": release_id, "status": "FAILED",
                              "active_before": (old_active or {}).get("release_id"), "steps": []}

    def fail(step: str, why: str, **extra: Any) -> dict[str, Any]:
        report["steps"].append({"step": step, "status": "FAILED", "reason": why, **extra})
        report["reason"] = why
        report["active_after"] = (read_pointer(settings).get("active") or {}).get("release_id")
        audit.record(session, actor_type="cli", actor_id=actor_label, action=f"ops.release.{action}",
                     result="FAILED", target_type="controller_release", target_id=release_id, reason=why,
                     details={"steps": report["steps"]})
        _alert(session, key=f"release:{action}:{release_id}", title=f"Controller {action} failed: {release_id}",
               detail={"reason": why, "active": report["active_after"]})
        events.emit(session, source="operations", kind=f"ops.release.{action}_failed",
                    entity_type="controller_release", entity_id=release_id, payload={"reason": why})
        if report.get("decision_id"):
            try:
                from aeos.policy import facade as policy

                policy.invalidate_decisions(session, target_type="controller_release", target_id=release_id,
                                            reason=f"{action} failed: {why}"[:500])
            except Exception as exc:  # best effort; the failure itself is already audited
                report["decision_invalidation"] = f"not invalidated: {type(exc).__name__}"
        session.commit()
        return report

    if action == "promote" and (old_active or {}).get("release_id") == release_id:
        raise Blocked(f"release {release_id} is already active", code="RELEASE_ALREADY_ACTIVE")
    # Acceptance basis recorded at build time (§09 zone 3); older artifacts built before acceptance gating
    # show UNKNOWN, never ACCEPTED.
    report["acceptance"] = target_prov.get("acceptance") or {"status": "UNKNOWN",
                                                             "reason": "built before acceptance gating"}
    config = runtime_config(settings, session)
    report["config"] = config

    # 1. human decision bound to the exact artifact hash (fail closed if it cannot be recorded)
    try:
        report["decision_id"] = _record_decision(session, operator_user_id=operator_user_id, prov=target_prov,
                                                 operation=action, reason=reason or f"{action} {release_id}",
                                                 config=config)
        session.commit()
        report["steps"].append({"step": "decision", "status": "SUCCEEDED", "decision_id": report["decision_id"]})
    except ImportError as exc:
        session.rollback()
        return fail("decision", f"policy module unavailable ({exc.name or exc}); a PROMOTION decision cannot be "
                                "recorded, so the operation is refused (fail closed)")
    except AeosError as exc:
        session.rollback()
        return fail("decision", f"decision refused: {exc.message}")
    except Exception as exc:  # e.g. an incompatible policy implementation: never proceed without AUTH
        session.rollback()
        return fail("decision", f"a PROMOTION decision could not be recorded ({type(exc).__name__}); the "
                                "operation is refused (fail closed)")

    # 2. compatibility (migration head vs current database revision)
    compat = compatibility(settings, target_prov)
    report["compatibility"] = compat
    if not compat["compatible"]:
        remedy = ""
        if action == "rollback":
            hist = [h for h in pointer.get("history") or [] if h.get("release_id") == (old_active or {}).get("release_id")
                    and h.get("pre_backup_id")]
            if hist:
                remedy = (f" Restore the pre-promote backup first: aeos ops backup restore {hist[-1]['pre_backup_id']}"
                          " (code rollback does not roll back data, §40).")
        return fail("compatibility", compat["reason"] + remedy)
    report["steps"].append({"step": "compatibility", "status": "SUCCEEDED", "action": compat["action"]})

    # 3. pre-upgrade backup with restore test
    try:
        pre = _pre_change_backup(settings, session, label=f"pre-{action}", actor=actor_label)
    except AeosError as exc:
        session.rollback()
        return fail("pre_backup", exc.message, details=exc.details)
    report["pre_backup"] = pre
    report["steps"].append({"step": "pre_backup", "status": "SUCCEEDED" if pre else "SKIPPED",
                            **({"backup_id": pre["backup_id"]} if pre else {"reason": "no database yet"})})

    # 4. smoke test the release in a subprocess (copy of the database, temporary loopback port)
    try:
        release_dir = extract_release(settings, target_prov)
    except (OSError, ValidationFailed, UnsafePathError, zipfile.BadZipFile) as exc:
        return fail("extract", f"cannot extract release: {type(exc).__name__}")
    db_source = settings.layout.backups / pre["backup_id"] / "aeos.sqlite3" if pre else None
    smoke = smoke_runner(settings, release_dir, db_source=db_source, timeout=smoke_timeout)
    report["smoke"] = {k: smoke.get(k) for k in ("ok", "error", "health", "functional", "migration", "version",
                                                 "exit_code", "stderr_tail", "lifecycle_hooks_disabled")}
    if not smoke.get("ok"):
        return fail("smoke", f"smoke test failed: {smoke.get('error') or 'unknown error'}; previous release stays active")
    if smoke.get("version") and smoke.get("version") != target_prov.get("version"):
        return fail("smoke", "smoke test reported a different version than the provenance manifest")
    report["steps"].append({"step": "smoke", "status": "SUCCEEDED"})

    # 5. live migration only when required (server must be stopped)
    if compat["action"] in ("upgrade", "stamp"):
        srv = server_check(settings)
        if srv.get("running") and not force_maintenance:
            return fail("live_migration", "database migration required but the control plane is running; stop it or "
                                          "pass --force-maintenance", server=srv)
        session.commit()
        session.close()
        mig = _run_live_migration(settings, release_dir)
        if not mig.get("ok"):
            return fail("live_migration", f"live migration failed: {mig.get('error') or mig.get('stderr_tail')}; "
                                          f"restore backup {pre['backup_id'] if pre else '(none)'} if needed")
        report["steps"].append({"step": "live_migration", "status": "SUCCEEDED", "result": mig.get("migration")})
    else:
        report["steps"].append({"step": "live_migration", "status": "SKIPPED", "reason": "no schema change"})

    # 6. move the pointer, keeping the previous release
    now = utcnow().isoformat()
    entry = {"release_id": release_id, "version": target_prov.get("version"), "commit": target_prov.get("commit"),
             "artifact_sha256": target_prov.get("artifact_sha256"), "activated_at": now,
             "activated_by": actor_label, "decision_id": report.get("decision_id"),
             "pre_backup_id": pre["backup_id"] if pre else None, "config_hash": config["config_hash"],
             "settings_hash": config["settings_hash"], "policy_snapshot_hash": config["policy_snapshot_hash"],
             "acceptance_basis": report["acceptance"].get("basis")}
    history = list(pointer.get("history") or [])
    history.append({"action": action, "at": now, "by": actor_label, **entry,
                    "from_release_id": (old_active or {}).get("release_id")})
    new_pointer = {"active": entry, "previous": old_active, "updated_at": now, "history": history[-200:]}
    write_json_atomic(_pointer_path(settings), new_pointer)
    report["status"] = "SUCCEEDED"
    report["active_after"] = release_id
    report["previous_after"] = (old_active or {}).get("release_id")
    report["steps"].append({"step": "pointer", "status": "SUCCEEDED"})
    report["notice"] = ("The active pointer identifies the trusted controller release. Restart the control plane "
                        "(aeos serve runs the verified active release) to run it; code rollback never rolls back "
                        "data (§40).")
    audit.record(session, actor_type="cli", actor_id=actor_label, action=f"ops.release.{action}",
                 target_type="controller_release", target_id=release_id,
                 details={"steps": report["steps"], "previous": report["previous_after"]})
    events.emit(session, source="operations", kind=f"ops.release.{action}d" if action == "promote" else
                "ops.release.rolled_back", entity_type="controller_release", entity_id=release_id,
                payload={"previous": report["previous_after"]})
    session.commit()
    return report
