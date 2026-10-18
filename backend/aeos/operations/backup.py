"""Control-plane backup, verification, restore test and live restore (§26, §33, §50).

A backup is a directory ``<data>/backups/<backup_id>/`` containing

* ``aeos.sqlite3`` — consistent online copy made with SQLite's backup API (safe with WAL); login
  sessions are removed from the copy;
* ``personal.zip`` / ``office.zip`` — evidence, attachments and publication files, one archive per
  domain partition so Office material never shares an archive with Personal material (§04);
* ``manifest.json`` — files, sizes, SHA-256, row counts, schema revision, app version, exclusions;
* ``results.json`` — append-only log of hash verifications and restore tests.

Excluded by design (§26, §33 "OpenBao recovery material ... dipisah"): OpenBao data and unseal/root
material, OS keyring entries, native Claude login state, worker sandboxes, logs and any file whose
name or content matches a credential pattern (listed in the manifest, value never recorded).

A backup is **never** reported as ready before a restore test succeeded: the test restores the
copy into an isolated temp directory, runs ``PRAGMA integrity_check``, compares row counts, opens
every table with the application models and re-hashes every extracted file. Only then is a
``RESTORE_TEST`` evidence artifact collected through ``aeos.verification.facade`` (origin
``restore-test``). A failed or corrupt backup produces no evidence (§50 "Restore backup belum
pernah diuji -> Tidak mengklaim recovery siap").

Live restore is CLI-only, refuses while the server is running (unless ``--force-maintenance``),
requires a restore-tested backup and takes a pre-restore backup first.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import platform
import shutil
import sqlite3
import sys
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from aeos import SCHEMA_VERSION, __version__
from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.errors import Blocked, NotFound, ValidationFailed
from aeos.core.hashing import file_sha256
from aeos.core.ids import new_id, slugify
from aeos.core.paths import UnsafePathError, ensure_within, safe_segment
from aeos.core.redaction import contains_secret, redact_text
from aeos.core.timeutil import utcnow
from aeos.operations._util import (
    ops_tmp_dir,
    read_json,
    refuse_worker_context,
    rmtree_robust,
    server_status,
    write_json_atomic,
)

FORMAT = "aeos-backup/1"
MANIFEST = "manifest.json"
RESULTS = "results.json"
DB_FILE = "aeos.sqlite3"
PARTIAL_SUFFIX = ".partial"
ARCHIVED_SUBDIRS = ("evidence", "attachments", "publications")
SCRUBBED_TABLES = ("auth_sessions",)
TEXT_SCAN_MAX_BYTES = 5 * 1024 * 1024

EXCLUDED_BY_DESIGN = [
    "OpenBao data directory, unseal keys, root tokens and recovery material (separate recovery path, §26/§33)",
    "OS keyring / credential-store entries",
    "Claude Code native login state (managed by the official client, §12)",
    "worker sandboxes (task-data), supervisor workspace, manual Claude packages, logs and temp files",
    "server-side login sessions (auth_sessions rows are deleted from the database copy)",
    "files whose name or content matches a credential pattern (listed below without values)",
]

SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "client_secret*.json", "credentials*.json",
    "oauth-token*.json", "token*.json", "id_rsa*", "id_ecdsa*", "id_ed25519*", "*.kdbx",
)

READINESS_TESTED = "RESTORE_TESTED"
READINESS_NOT_TESTED = "NOT_RESTORE_TESTED"
READINESS_FAILED = "RESTORE_TEST_FAILED"
READINESS_UNRECORDED = "RESTORE_TEST_UNRECORDED"
READINESS_INCOMPLETE = "INCOMPLETE"


# ----------------------------------------------------------------------------------------- helpers


def _sha256_file(path: Path) -> str:
    return file_sha256(path)


def _backup_dir(settings: Settings, backup_id: str) -> Path:
    try:
        safe_segment(backup_id)
    except UnsafePathError as exc:
        raise ValidationFailed("invalid backup id") from exc
    path = settings.layout.backups / backup_id
    if not (path / MANIFEST).is_file():
        raise NotFound(f"backup {backup_id} not found")
    return path


def load_manifest(settings: Settings, backup_id: str) -> dict[str, Any]:
    manifest = read_json(_backup_dir(settings, backup_id) / MANIFEST)
    if manifest is None or manifest.get("format") != FORMAT:
        raise ValidationFailed(f"backup {backup_id} has an unreadable or unknown manifest")
    return manifest


def _load_results(path: Path) -> dict[str, Any]:
    return read_json(path / RESULTS) or {"verifications": [], "restore_tests": []}


def _append_result(path: Path, key: str, entry: dict[str, Any]) -> None:
    results = _load_results(path)
    results.setdefault(key, []).append(entry)
    write_json_atomic(path / RESULTS, results)


def _is_secret_name(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_FILE_PATTERNS)


def _file_contains_secret(path: Path) -> bool:
    try:
        if path.stat().st_size > TEXT_SCAN_MAX_BYTES:
            return False
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False  # binary or unreadable: not text-scannable (recorded as included, not scanned)
    return contains_secret(text)


PLATFORM_PARTITION = "PLATFORM"


def partition_root(settings: Settings, partition: str) -> Path:
    """Root directory of a backup partition: a domain partition or the platform-level partition."""
    if partition == PLATFORM_PARTITION:
        return settings.layout.root
    return settings.layout.domain_root(Domain(partition))


def _partition_bases(settings: Settings, partition: str) -> list[Path]:
    root = partition_root(settings, partition)
    if partition == PLATFORM_PARTITION:
        # Platform-level evidence (no project, e.g. restore tests) if the collector stores it here.
        return [root / "evidence"]
    bases = [root / "evidence"]
    projects = root / "projects"
    if projects.is_dir():
        for proj in sorted(projects.iterdir()):
            if proj.is_symlink() or not proj.is_dir():
                continue
            bases.extend(proj / sub for sub in ARCHIVED_SUBDIRS)
    return bases


def _iter_partition_files(settings: Settings, partition: str) -> Iterator[tuple[Path, str]]:
    """Evidence/attachment/publication files of one partition — never symlinks, never anything
    outside the partition root."""
    root = partition_root(settings, partition)
    for base in _partition_bases(settings, partition):
        if base.is_symlink() or not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
            for fname in sorted(filenames):
                p = Path(dirpath) / fname
                if p.is_symlink() or not p.is_file():
                    continue
                try:
                    ensure_within(root, p)
                except UnsafePathError:
                    continue
                yield p, p.relative_to(root).as_posix()


PARTITIONS = (Domain.PERSONAL.value, Domain.OFFICE.value, PLATFORM_PARTITION)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {t: int(conn.execute(f"SELECT count(*) FROM {_ident(t)}").fetchone()[0])  # noqa: S608 - quoted identifier from sqlite_master
                for t in tables}
    finally:
        conn.close()


def _schema_revision(db_path: Path) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()
        if not row:
            return None
        rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(rev[0]) if rev else None
    finally:
        conn.close()


def _online_copy(src_path: Path, dest_path: Path) -> None:
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # single self-contained file, no -wal/-shm
            for table in SCRUBBED_TABLES:
                exists = dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                                     (table,)).fetchone()
                if exists:
                    dst.execute(f"DELETE FROM {_ident(table)}")  # noqa: S608 - constant table name, quoted
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _safe_member(name: str) -> str:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise ValidationFailed(f"unsafe archive member: {name!r}")
    return pure.as_posix()


# ----------------------------------------------------------------------------------------- create


def create_backup(
    settings: Settings,
    *,
    label: str = "manual",
    actor: str = "cli",
    session: Session | None = None,
    actor_type: str = "cli",
) -> dict[str, Any]:
    """Create a consistent backup. Records audit/event in *session* when given (caller commits)."""
    layout = settings.layout
    if not layout.db_path.is_file():
        raise Blocked("no control-plane database exists yet; nothing to back up", code="NO_DATABASE")
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{stamp}-{slugify(label, 24)}-{new_id()[:8]}"
    safe_segment(backup_id)
    layout.backups.mkdir(parents=True, exist_ok=True)
    work = layout.backups / (backup_id + PARTIAL_SUFFIX)
    work.mkdir(parents=False, exist_ok=False)
    try:
        db_copy = work / DB_FILE
        _online_copy(layout.db_path, db_copy)
        archives: list[dict[str, Any]] = []
        excluded: list[dict[str, str]] = []
        for partition in PARTITIONS:
            entries: list[dict[str, Any]] = []
            archive_name = f"{partition.lower()}.zip"
            archive_path = work / archive_name
            zf: zipfile.ZipFile | None = None
            try:
                for path, rel in _iter_partition_files(settings, partition):
                    if _is_secret_name(path.name):
                        excluded.append({"domain": partition, "path": rel, "reason": "credential-like file name"})
                        continue
                    if _file_contains_secret(path):
                        excluded.append({"domain": partition, "path": rel, "reason": "credential pattern in content"})
                        continue
                    if zf is None:
                        zf = zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED)
                    zf.write(path, arcname=rel)
                    entries.append({"path": rel, "size": path.stat().st_size, "sha256": _sha256_file(path)})
            finally:
                if zf is not None:
                    zf.close()
            if entries:
                archives.append({
                    "file": archive_name, "domain": partition, "file_count": len(entries),
                    "size": archive_path.stat().st_size, "sha256": _sha256_file(archive_path), "files": entries,
                })
        manifest: dict[str, Any] = {
            "format": FORMAT,
            "backup_id": backup_id,
            "label": label,
            "created_at": utcnow().isoformat(),
            "created_by": actor,
            "app_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "schema_revision": _schema_revision(db_copy),
            "platform": {"system": platform.system(), "python": sys.version.split()[0]},
            "database": {
                "file": DB_FILE,
                "size": db_copy.stat().st_size,
                "sha256": _sha256_file(db_copy),
                "row_counts": _row_counts(db_copy),
                "scrubbed_tables": list(SCRUBBED_TABLES),
            },
            "archives": archives,
            "excluded_by_design": EXCLUDED_BY_DESIGN,
            "excluded_files": excluded,
            "notice": "Not restore-tested at creation time. Run `aeos ops backup restore-test <id>` "
                      "before relying on this backup (§33).",
        }
        write_json_atomic(work / MANIFEST, manifest)
        manifest_sha = _sha256_file(work / MANIFEST)
        write_json_atomic(work / RESULTS, {"backup_id": backup_id, "manifest_sha256": manifest_sha,
                                           "verifications": [], "restore_tests": []})
        final = layout.backups / backup_id
        os.replace(work, final)
    except BaseException:
        rmtree_robust(work)
        raise
    summary = summarize(settings, backup_id)
    if session is not None:
        from aeos.audit import service as audit
        from aeos.events import service as events

        audit.record(session, actor_type=actor_type, actor_id=actor, action="ops.backup.create",
                     target_type="backup", target_id=backup_id,
                     details={"label": label, "manifest_sha256": manifest_sha,
                              "db_sha256": manifest["database"]["sha256"],
                              "excluded_files": len(excluded)})
        events.emit(session, source="operations", kind="ops.backup.created", entity_type="backup",
                    entity_id=backup_id, payload={"label": label, "readiness": summary["readiness"]})
    return summary


# ----------------------------------------------------------------------------------------- verify


def verify_backup(settings: Settings, backup_id: str, *, record: bool = True) -> dict[str, Any]:
    """Hash verification only. A verified hash is NOT a restore test (§33)."""
    path = _backup_dir(settings, backup_id)
    problems: list[str] = []
    checked = 0
    results = _load_results(path)
    manifest_sha = _sha256_file(path / MANIFEST)
    if results.get("manifest_sha256") and results["manifest_sha256"] != manifest_sha:
        problems.append("manifest.json changed after creation (hash mismatch)")
    manifest = read_json(path / MANIFEST) or {}
    if manifest.get("format") != FORMAT:
        problems.append("manifest format unknown")
    db_meta = manifest.get("database") or {}
    db_file = path / str(db_meta.get("file", DB_FILE))
    if not db_file.is_file():
        problems.append("database copy missing")
    elif _sha256_file(db_file) != db_meta.get("sha256"):
        problems.append("database copy hash mismatch")
    else:
        checked += 1
    for arch in manifest.get("archives") or []:
        apath = path / str(arch.get("file", ""))
        if not apath.is_file():
            problems.append(f"archive {arch.get('file')} missing")
            continue
        if _sha256_file(apath) != arch.get("sha256"):
            problems.append(f"archive {arch.get('file')} hash mismatch")
            continue
        try:
            with zipfile.ZipFile(apath) as zf:
                names = set(zf.namelist())
                expected = {e["path"] for e in arch.get("files") or []}
                if names != expected:
                    problems.append(f"archive {arch.get('file')} members differ from manifest")
                for entry in arch.get("files") or []:
                    if entry["path"] not in names:
                        continue
                    h = hashlib.sha256()
                    with zf.open(entry["path"]) as fh:
                        while block := fh.read(1 << 20):
                            h.update(block)
                    if "sha256:" + h.hexdigest() != entry.get("sha256"):
                        problems.append(f"file hash mismatch in {arch.get('file')}")
                    else:
                        checked += 1
        except zipfile.BadZipFile:
            problems.append(f"archive {arch.get('file')} is not a valid zip")
    result = {"backup_id": backup_id, "ok": not problems, "checked_at": utcnow().isoformat(),
              "files_checked": checked, "problems": problems, "kind": "HASH_VERIFY",
              "notice": "hash verification is not a restore test"}
    if record:
        _append_result(path, "verifications", result)
    return result


# ----------------------------------------------------------------------------------------- restore test


def _integrity_check(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    finally:
        conn.close()


def _open_with_models(db_path: Path) -> dict[str, Any]:
    """Open every mapped table with the application models (SELECT all model columns)."""
    from aeos.core.db import Base, import_all_models

    import_all_models()
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", poolclass=NullPool, future=True)
    checked: list[str] = []
    missing: list[str] = []
    errors: list[str] = []
    try:
        with engine.connect() as conn:
            existing = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in Base.metadata.sorted_tables:
                if table.name not in existing:
                    missing.append(table.name)
                    continue
                try:
                    conn.execute(select(*table.columns).limit(1)).fetchall()
                    checked.append(table.name)
                except Exception as exc:  # incompatible columns -> restore not usable by this app
                    errors.append(f"{table.name}: {type(exc).__name__}")
    finally:
        engine.dispose()
    return {"tables_checked": len(checked), "missing_tables": missing, "errors": errors}


def _extract_and_verify(backup_path: Path, manifest: dict[str, Any], dest: Path) -> dict[str, Any]:
    verified = 0
    problems: list[str] = []
    for arch in manifest.get("archives") or []:
        domain_dir = dest / str(arch.get("domain", "unknown")).lower()
        domain_dir.mkdir(parents=True, exist_ok=True)
        expected = {e["path"]: e for e in arch.get("files") or []}
        with zipfile.ZipFile(backup_path / str(arch["file"])) as zf:
            for info in zf.infolist():
                name = _safe_member(info.filename)
                target = ensure_within(domain_dir, domain_dir / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                entry = expected.get(name)
                if entry is None:
                    problems.append("unexpected archive member")
                elif _sha256_file(target) != entry["sha256"]:
                    problems.append("extracted file hash mismatch")
                else:
                    verified += 1
    return {"files_verified": verified, "problems": problems}


def restore_test(
    settings: Settings,
    backup_id: str,
    *,
    session: Session | None = None,
    actor: str = "cli",
    actor_type: str = "cli",
) -> dict[str, Any]:
    """Restore into an isolated temp directory and prove it is usable. Evidence only on success."""
    path = _backup_dir(settings, backup_id)
    started = utcnow()
    problems: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {}
    hash_check = verify_backup(settings, backup_id, record=True)
    if not hash_check["ok"]:
        problems.extend(hash_check["problems"])
    manifest = read_json(path / MANIFEST) or {}
    if not problems:
        tmp = ops_tmp_dir(settings, "restore-test-")
        try:
            data = tmp / "data"
            data.mkdir()
            db_copy = data / DB_FILE
            shutil.copy2(path / DB_FILE, db_copy)
            integrity = _integrity_check(db_copy)
            stats["integrity_check"] = "ok" if integrity == ["ok"] else "failed"
            if integrity != ["ok"]:
                problems.append(f"PRAGMA integrity_check reported {len(integrity)} problem(s)")
            expected_counts = (manifest.get("database") or {}).get("row_counts") or {}
            counts = _row_counts(db_copy)
            mismatched = sorted(t for t, n in expected_counts.items() if counts.get(t) != n)
            if mismatched:
                problems.append(f"row counts differ from manifest for {len(mismatched)} table(s)")
            stats["tables"] = len(counts)
            stats["rows_total"] = sum(counts.values())
            models = _open_with_models(db_copy)
            stats["model_tables_checked"] = models["tables_checked"]
            if models["errors"]:
                problems.append(f"{len(models['errors'])} table(s) cannot be read with the current models")
                stats["model_errors"] = models["errors"]
            if models["missing_tables"]:
                warnings.append(f"{len(models['missing_tables'])} model table(s) absent in backup (created on "
                                "next `aeos db upgrade`)")
            files = _extract_and_verify(path, manifest, data)
            stats["files_verified"] = files["files_verified"]
            problems.extend(files["problems"])
            expected_files = sum(int(a.get("file_count", 0)) for a in manifest.get("archives") or [])
            if files["files_verified"] != expected_files:
                problems.append("not every archived file could be restored and verified")
        except (sqlite3.DatabaseError, OSError, zipfile.BadZipFile, ValidationFailed, UnsafePathError) as exc:
            problems.append(f"restore failed: {type(exc).__name__}: {redact_text(str(exc))[:200]}")
        finally:
            rmtree_robust(tmp)
    status = "PASSED" if not problems else "FAILED"
    evidence_id: str | None = None
    evidence_note = ""
    if status == "PASSED":
        if session is None:
            status = "PASSED_UNRECORDED"
            evidence_note = "no database session available to record evidence"
        else:
            try:
                from aeos.verification import facade as verification

                ev = verification.collect_evidence(
                    session,
                    kind="RESTORE_TEST",
                    origin="restore-test",
                    collector_identity="aeos.operations.backup.restore_test",
                    claim=(f"Backup {backup_id} restored into an isolated temp directory: integrity_check ok, "
                           f"row counts match manifest, all model tables readable, "
                           f"{stats.get('files_verified', 0)} archived file(s) hash-verified."),
                    content={"backup_id": backup_id, "manifest_sha256": _sha256_file(path / MANIFEST),
                             "db_sha256": (manifest.get("database") or {}).get("sha256"),
                             "stats": stats, "warnings": warnings, "started_at": started.isoformat()},
                    evidence_level="E2",
                    subject_type="backup",
                    subject_id=backup_id,
                    environment={"app_version": __version__, "schema_version": SCHEMA_VERSION,
                                 "schema_revision": manifest.get("schema_revision"),
                                 "platform": platform.system(), "python": sys.version.split()[0]},
                    classification="INTERNAL",
                    mode="LIVE",
                )
                evidence_id = ev.id
            except ImportError as exc:
                status = "PASSED_UNRECORDED"
                evidence_note = f"verification module unavailable ({exc.name or exc}); evidence not recorded"
            except Exception as exc:
                status = "PASSED_UNRECORDED"
                evidence_note = f"evidence collection failed: {type(exc).__name__}: {redact_text(str(exc))[:200]}"
    result = {
        "backup_id": backup_id,
        "kind": "RESTORE_TEST",
        "status": status,
        "started_at": started.isoformat(),
        "finished_at": utcnow().isoformat(),
        "problems": problems,
        "warnings": warnings,
        "stats": stats,
        "evidence_id": evidence_id,
        "evidence_note": evidence_note,
    }
    _append_result(path, "restore_tests", result)
    if session is not None:
        from aeos.audit import service as audit
        from aeos.events import service as events

        audit.record(session, actor_type=actor_type, actor_id=actor, action="ops.backup.restore_test",
                     result="SUCCESS" if status == "PASSED" else "FAILED", target_type="backup",
                     target_id=backup_id, reason="; ".join(problems)[:500] or evidence_note,
                     details={"status": status, "evidence_id": evidence_id, "stats": stats})
        events.emit(session, source="operations", kind="ops.backup.restore_tested", entity_type="backup",
                    entity_id=backup_id, payload={"status": status, "evidence_id": evidence_id})
    return result


# ----------------------------------------------------------------------------------------- listing


def readiness_of(results: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    tests = results.get("restore_tests") or []
    if not tests:
        return READINESS_NOT_TESTED, None
    last = tests[-1]
    if last.get("status") == "PASSED" and last.get("evidence_id"):
        verifications = results.get("verifications") or []
        later_bad = [v for v in verifications if not v.get("ok") and v.get("checked_at", "") > last.get("finished_at", "")]
        return (READINESS_FAILED if later_bad else READINESS_TESTED), last
    if last.get("status") == "PASSED_UNRECORDED":
        return READINESS_UNRECORDED, last
    return READINESS_FAILED, last


def summarize(settings: Settings, backup_id: str, *, domain: Domain | None = None) -> dict[str, Any]:
    """API-safe summary: no file names; per-archive stats only for *domain* (§35 scope)."""
    path = _backup_dir(settings, backup_id)
    manifest = read_json(path / MANIFEST) or {}
    results = _load_results(path)
    readiness, last_test = readiness_of(results)
    verifications = results.get("verifications") or []
    archives = manifest.get("archives") or []

    def _visible(part: Any) -> bool:
        return domain is None or part in (domain.value, PLATFORM_PARTITION)

    visible = [a for a in archives if _visible(a.get("domain"))]
    size = sum(p.stat().st_size for p in path.iterdir() if p.is_file())
    return {
        "backup_id": backup_id,
        "label": manifest.get("label"),
        "created_at": manifest.get("created_at"),
        "created_by": manifest.get("created_by"),
        "app_version": manifest.get("app_version"),
        "schema_version": manifest.get("schema_version"),
        "schema_revision": manifest.get("schema_revision"),
        "size_bytes": size,
        "db_sha256": (manifest.get("database") or {}).get("sha256"),
        "manifest_sha256": results.get("manifest_sha256"),
        "archives": [{"domain": a.get("domain"), "file_count": a.get("file_count"), "size": a.get("size"),
                      "sha256": a.get("sha256")} for a in visible],
        "excluded_file_count": len([e for e in manifest.get("excluded_files") or [] if _visible(e.get("domain"))]),
        "excluded_by_design": manifest.get("excluded_by_design") or [],
        "readiness": readiness,
        "ready": readiness == READINESS_TESTED,
        "last_verification": verifications[-1] if verifications else None,
        "last_restore_test": last_test,
        "restore_test_count": len(results.get("restore_tests") or []),
    }


def list_backups(
    settings: Settings, *, domain: Domain | None = None, retention: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    root = settings.layout.backups
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        if entry.name.endswith(PARTIAL_SUFFIX):
            items.append({"backup_id": entry.name, "readiness": READINESS_INCOMPLETE, "ready": False,
                          "note": "incomplete backup directory (creation interrupted); never restorable"})
            continue
        if not (entry / MANIFEST).is_file():
            continue
        try:
            items.append(summarize(settings, entry.name, domain=domain))
        except (NotFound, ValidationFailed, OSError):
            continue
    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    _apply_retention(items, retention)
    return items


def _apply_retention(items: list[dict[str, Any]], retention: dict[str, Any] | None) -> None:
    """Retention listing only — nothing is ever deleted automatically (§33)."""
    complete = [i for i in items if i.get("readiness") != READINESS_INCOMPLETE]
    newest_ready = next((i for i in complete if i.get("ready")), None)
    if not retention:
        for i in items:
            i["retention"] = "NO_POLICY"
        return
    keep_last = int(retention.get("keep_last") or 0)
    keep_days = int(retention.get("keep_days") or 0)
    now = utcnow()
    for idx, i in enumerate(complete):
        keep = idx < keep_last if keep_last else True
        created = i.get("created_at")
        if keep_days and created:
            age_days = (now - datetime.fromisoformat(created)).days
            keep = keep or age_days <= keep_days
        if i is newest_ready:
            keep = True  # never suggest deleting the newest restore-tested recovery point
        i["retention"] = "KEEP" if keep else "EXPIRED_BY_POLICY (manual deletion only)"
    for i in items:
        i.setdefault("retention", "KEEP")


def latest_ready(settings: Settings) -> dict[str, Any] | None:
    return next((b for b in list_backups(settings) if b.get("ready")), None)


# ----------------------------------------------------------------------------------------- live restore


def restore_live(
    settings: Settings,
    backup_id: str,
    *,
    actor: str,
    force_maintenance: bool = False,
    server_check: Callable[[Settings], dict[str, Any]] = server_status,
) -> dict[str, Any]:
    """Replace the live database and partition files with a restore-tested backup (CLI only).

    The caller must not hold an open session on the live database. Newer evidence/attachment files
    that are not part of the backup are kept (evidence is append-only; nothing is deleted).
    """
    refuse_worker_context("live restore")
    path = _backup_dir(settings, backup_id)
    srv = server_check(settings)
    if srv.get("running") and not force_maintenance:
        raise Blocked(
            f"the control plane appears to be running on {settings.host}:{settings.port}; stop it first "
            "or pass --force-maintenance",
            code="SERVER_RUNNING", details={"server": srv},
        )
    readiness, _last = readiness_of(_load_results(path))
    if readiness != READINESS_TESTED:
        raise Blocked(f"backup {backup_id} has not passed a recorded restore test ({readiness}); run "
                      f"`aeos ops backup restore-test {backup_id}` first", code="RESTORE_NOT_TESTED")
    hash_check = verify_backup(settings, backup_id, record=True)
    if not hash_check["ok"]:
        raise Blocked("backup failed hash verification; refusing to restore", code="BACKUP_CORRUPT",
                      details={"problems": hash_check["problems"]})
    manifest = load_manifest(settings, backup_id)

    from aeos.core.db import get_engine, init_engine, session_scope

    pre: dict[str, Any] | None = None
    raw_copy: str | None = None
    if settings.layout.db_path.is_file():
        try:
            pre = create_backup(settings, label="pre-restore", actor=actor)
        except (sqlite3.DatabaseError, Blocked, OSError):
            # Damaged live database: keep a raw byte copy (db + wal/shm) instead of losing it.
            raw_dir = settings.layout.backups / f"raw-pre-restore-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
            raw_dir.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                src_file = Path(str(settings.layout.db_path) + suffix)
                if src_file.is_file():
                    shutil.copy2(src_file, raw_dir / src_file.name)
            raw_copy = raw_dir.name
    get_engine().dispose()
    live = settings.layout.db_path
    live.parent.mkdir(parents=True, exist_ok=True)
    if srv.get("running"):
        # Forced maintenance with a live server: write through SQLite's locking (backup API) so the
        # running process sees the restored content instead of keeping an unlinked old file open.
        src = sqlite3.connect(path / DB_FILE)
        try:
            dst = sqlite3.connect(live, timeout=10)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
    else:
        staged = live.with_name(f".restore-{new_id()[:8]}.sqlite3")
        shutil.copy2(path / DB_FILE, staged)
        for suffix in ("-wal", "-shm"):
            Path(str(live) + suffix).unlink(missing_ok=True)
        os.replace(staged, live)
    restored_files = 0
    for arch in manifest.get("archives") or []:
        root = partition_root(settings, str(arch["domain"]))
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path / str(arch["file"])) as zf:
            for info in zf.infolist():
                name = _safe_member(info.filename)
                target = ensure_within(root, root / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, target.open("wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                restored_files += 1
    init_engine(settings)
    from aeos.core.migrate import upgrade_database

    migration = upgrade_database()
    report = {
        "backup_id": backup_id,
        "status": "RESTORED",
        "pre_restore_backup_id": pre["backup_id"] if pre else None,
        "pre_restore_raw_copy": raw_copy,
        "forced": bool(force_maintenance and srv.get("running")),
        "restored_files": restored_files,
        "migration": {k: str(v) for k, v in migration.items()},
        "finished_at": utcnow().isoformat(),
        "notice": "Files newer than the backup were kept (nothing deleted). Login sessions were not "
                  "restored; sign in again.",
    }
    with session_scope() as s:
        from aeos.audit import service as audit
        from aeos.events import service as events

        audit.record(s, actor_type="cli", actor_id=actor, action="ops.backup.restore", target_type="backup",
                     target_id=backup_id, details=report)
        events.emit(s, source="operations", kind="ops.backup.restored", entity_type="backup",
                    entity_id=backup_id, payload={"pre_restore_backup_id": report["pre_restore_backup_id"]})
    return report
