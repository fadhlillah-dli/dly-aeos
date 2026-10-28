"""§08 Runtime aplikasi: Python/Node/dependency compatibility, DB/migration, storage, server/frontend."""

from __future__ import annotations

import importlib.metadata
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

from aeos.core.hashing import file_sha256
from aeos.core.paths import repo_root
from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, binary_outcome, file_fingerprint, logical
from aeos.doctor.model import (
    CheckContext,
    Outcome,
    Remedy,
    aeos_cmd,
    frontend_build_command,
    os_install_command,
    venv_python,
)
from aeos.inventory import scope as scope_reader
from aeos.inventory.probes import version_tuple

MIN_PYTHON = (3, 11)
MIN_NODE = (20,)
REQUIRED_TABLES = ("users", "auth_sessions", "check_results", "component_references", "events", "audit_events")


@check("runtime.python", "RUNTIME", "Versi Python >= 3.11", component_id="python-runtime", ttl_seconds=86400,
       severity_fail="CRITICAL", observation="installation", invalidators=("binary:python",))
def runtime_python(ctx: CheckContext) -> Outcome:
    version = platform.python_version()
    evidence = {"version": version, "implementation": platform.python_implementation(),
                "executable": logical(ctx, sys.executable), "minimum": ".".join(map(str, MIN_PYTHON))}
    if sys.version_info[:2] < MIN_PYTHON:
        return Outcome("FAILED", f"Python {version} terlalu lama (minimal 3.11).", evidence=evidence,
                       component_version=version, error_code="PYTHON_TOO_OLD", remedy=Remedy(
                           operation="Pasang Python >= 3.11 dari distribusi resmi lalu buat ulang .venv",
                           command=os_install_command(windows="winget install --id Python.Python.3.13 -e",
                                                      macos="brew install python@3.13"),
                           source="python.org / package manager OS (Linux: paket resmi distro)",
                           privilege="pengguna lokal", rollback="interpreter lama tetap terpasang",
                           evidence_to_collect="python --version"))
    return Outcome("VERIFIED", f"Python {version} ({evidence['implementation']}).", evidence=evidence,
                   component_version=version, target_identity=evidence["executable"])


def _spec_ok(spec: str, version: str) -> bool | None:
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet
    except ImportError:
        return None
    try:
        return version in SpecifierSet(spec)
    except Exception:
        return None


def _deps_fp(ctx: CheckContext) -> dict[str, Any]:
    """pyproject.toml (stat) and the content hash of requirements.lock: a pulled lock change re-runs the check."""
    lock = ctx.repo / "requirements.lock"
    try:
        lock_sha: str | None = file_sha256(lock)
    except OSError:
        lock_sha = None
    return {**file_fingerprint("pyproject.toml")(ctx), "requirements_lock_sha256": lock_sha}


@check("runtime.backend_deps", "RUNTIME", "Dependensi backend terpasang", component_id="dly-aeos-control-plane",
       ttl_seconds=3600, severity_fail="CRITICAL", fingerprint=_deps_fp)
def runtime_backend_deps(ctx: CheckContext) -> Outcome:
    read, deps = scope_reader.read_pyproject(ctx.repo)
    if read.status != "READ":
        return Outcome("UNKNOWN", "pyproject.toml tidak dapat dibaca.", error_code="PYPROJECT_UNREADABLE")
    runtime = [d for d in deps if d["group"] == "runtime"]
    missing, mismatched, unchecked = [], [], []
    for d in runtime:
        try:
            v = importlib.metadata.version(d["name"])
        except importlib.metadata.PackageNotFoundError:
            missing.append(d["name"])
            continue
        ok = _spec_ok(d["spec"], v)
        if ok is False:
            mismatched.append(f"{d['name']} {v} !{d['spec']}")
        elif ok is None:
            unchecked.append(d["name"])
    evidence: dict[str, Any] = {"declared": len(runtime), "missing": missing, "mismatched": mismatched,
                                "spec_unchecked": unchecked, "environment": logical(ctx, sys.prefix)}
    if missing or mismatched:
        return Outcome("FAILED", f"Dependensi backend bermasalah: {', '.join(missing + mismatched)}.", evidence=evidence,
                       error_code="DEPENDENCIES_MISSING", remedy=Remedy(
                           operation="Pasang dependensi dari pyproject ke virtual env repo",
                           command=f"{venv_python()} -m pip install -e .", source="pyproject.toml",
                           target="<repo>/.venv", privilege="pengguna lokal",
                           rollback="pip install versi sebelumnya", evidence_to_collect="semua dependensi terpasang"))
    return Outcome("VERIFIED", f"{len(runtime)} dependensi runtime terpasang dan sesuai spesifikasi.", evidence=evidence)


@check("runtime.node", "RUNTIME", "Node.js >= 20 (build frontend)", component_id="nodejs-runtime", ttl_seconds=86400,
       invalidators=("binary:node",), fingerprint=binary_fingerprint("node"), observation="installation")
def runtime_node(ctx: CheckContext) -> Outcome:
    remedy = Remedy(operation="Pasang Node.js LTS (hanya untuk build Control Center)",
                    command=os_install_command(windows="winget install --id OpenJS.NodeJS.LTS -e", macos="brew install node"),
                    source="nodejs.org / package manager OS (Linux: paket resmi distro atau nodejs.org)",
                    privilege="pengguna lokal",
                    rollback="uninstall lewat package manager", evidence_to_collect="node --version >= 20")
    out = binary_outcome(ctx, name="node", override_attr=None, args=["--version"], regex=r"v?(\d+\.\d+\.\d+)",
                         missing_remedy=remedy, label="Node.js")
    if out.status == "VERIFIED" and version_tuple(out.component_version)[:1] < MIN_NODE:
        return Outcome("FAILED", f"Node.js {out.component_version} terlalu lama (minimal 20).", evidence=out.evidence,
                       component_version=out.component_version, error_code="NODE_TOO_OLD", remedy=remedy)
    return out


def _ops() -> Any:
    from aeos.operations import facade

    return facade


def _served_dist(ctx: CheckContext) -> Path | None:
    """The bundle the server serves (same order as ``aeos.api.app._frontend_dist``)."""
    for c in (ctx.repo / "frontend" / "dist", repo_root() / "backend" / "aeos" / "static"):
        if (c / "index.html").is_file():
            return c
    return None


def _dist_fp(ctx: CheckContext) -> dict[str, Any]:
    """Bundle files plus the committed frontend tree of HEAD: a git pull that changes ``frontend/`` re-runs the check,
    a backend-only commit does not."""
    dist = ctx.repo / "frontend" / "dist"
    return {"dist": ctx.runner.stat(str(dist / "index.html")) or {"present": False},
            "build_record": ctx.runner.stat(str(dist / "aeos-build.json")) or {"present": False},
            "head_frontend_tree": _ops().frontend_head_tree(ctx.repo)}


def _rebuild_remedy(evidence: str) -> Remedy:
    return Remedy(operation="Restart aeos serve (membangun ulang bundle usang sekali bila node_modules sesuai lockfile "
                            "dan frontend/ bersih), atau build ulang Control Center dari lockfile (dari root repo)",
                  command=frontend_build_command(), source="frontend/package-lock.json",
                  target="<repo>/frontend/dist", privilege="pengguna lokal",
                  rollback="build ulang dari commit sebelumnya", evidence_to_collect=evidence)


@check("runtime.frontend_dist", "RUNTIME", "Control Center sudah di-build", component_id="control-center-frontend",
       ttl_seconds=3600, severity_fail="CRITICAL", fingerprint=_dist_fp, observation="installation",
       invalidators=("repo:lockfile",))
def runtime_frontend_dist(ctx: CheckContext) -> Outcome:
    dist = _served_dist(ctx)
    if dist is None:
        return Outcome("MISSING", "Frontend belum di-build; dashboard tidak dapat dibuka (API tetap berjalan).",
                       evidence={"looked_in": ["<repo>/frontend/dist"]}, error_code="FRONTEND_NOT_BUILT",
                       remedy=Remedy(operation="Build Control Center dari lockfile (dari root repo)",
                                     command=frontend_build_command(),
                                     source="frontend/package-lock.json", target="<repo>/frontend/dist",
                                     privilege="pengguna lokal", rollback="hapus frontend/dist",
                                     evidence_to_collect="frontend/dist/index.html ada"))
    state = _ops().ui_bundle_state(ctx.settings, ctx.repo, dist)
    evidence: dict[str, Any] = {"dist": logical(ctx, dist), **{k: state.get(k) for k in (
        "basis", "bundle_frontend_tree", "head_frontend_tree", "release_id", "built_at", "clean", "reason")
        if state.get(k) is not None}}
    if state["status"] == "FRESH":
        tree = str(state.get("bundle_frontend_tree") or "")[:12]
        return Outcome("VERIFIED", f"Build Control Center tersedia dan sesuai frontend {state['basis']} ({tree}).",
                       evidence=evidence, target_identity=logical(ctx, dist))
    if state["status"] == "STALE":
        return Outcome("CONFIGURED", f"Bundle Control Center usang: {state['reason']}. Dashboard menyajikan UI lama.",
                       evidence=evidence, error_code="FRONTEND_STALE", target_identity=logical(ctx, dist),
                       remedy=_rebuild_remedy("runtime.frontend_dist VERIFIED (frontend_tree build = HEAD:frontend)"))
    return Outcome("UNKNOWN", f"Bundle Control Center UNVERIFIED: {state.get('reason')}.", evidence=evidence,
                   error_code="FRONTEND_BUILD_UNVERIFIED", target_identity=logical(ctx, dist),
                   remedy=_rebuild_remedy("frontend/dist/aeos-build.json bersih dan sama dengan HEAD:frontend"))


def _drift_fp(ctx: CheckContext) -> dict[str, Any]:
    drift = _ops().serve_code_drift(ctx.settings)
    return {k: drift.get(k) for k in ("status", "pid", "loaded_commit", "head_commit")}


@check("runtime.code_drift", "RUNTIME", "Server menjalankan kode HEAD", component_id="dly-aeos-control-plane",
       ttl_seconds=300, fingerprint=_drift_fp, observation="configuration", ok={"VERIFIED", "NOT_APPLICABLE"},
       description="aeos serve mencatat commit yang dimuat saat start (<data>/run/serve.json); git pull yang mengubah "
                   "backend/, requirements.lock atau pyproject.toml butuh restart.")
def runtime_code_drift(ctx: CheckContext) -> Outcome:
    drift = _ops().serve_code_drift(ctx.settings)
    status = drift["status"]
    evidence = {k: drift.get(k) for k in ("loaded_commit", "head_commit", "started_at", "changed", "changed_count",
                                          "reason") if drift.get(k) is not None}
    if status == "NOT_RUNNING":
        return Outcome("NOT_APPLICABLE", f"Tidak ada aeos serve (checkout) yang berjalan: {drift['reason']}.",
                       evidence=evidence)
    if status == "RELOAD":
        return Outcome("NOT_APPLICABLE", "aeos serve --reload memuat ulang kode backend sendiri.", evidence=evidence)
    if status == "UNKNOWN":
        return Outcome("UNKNOWN", f"Commit server tidak dapat dibandingkan: {drift['reason']}.", evidence=evidence,
                       error_code="CODE_DRIFT_UNKNOWN")
    loaded = str(drift.get("loaded_commit") or "")[:12]
    head = str(drift.get("head_commit") or "")[:12]
    if status == "CURRENT":
        return Outcome("VERIFIED", f"Server menjalankan commit HEAD {head}.", evidence=evidence,
                       component_version=head)
    if status == "OUTSIDE_BACKEND":
        return Outcome("VERIFIED", f"Server memuat {loaded}; HEAD {head} hanya mengubah path di luar backend "
                       "(docs/frontend), tidak perlu restart.", evidence=evidence, component_version=loaded)
    return Outcome("CONFIGURED", f"Server masih menjalankan {loaded}; HEAD mengubah {drift.get('changed_count')} file "
                   f"backend: restart aeos serve to load {head}.", evidence=evidence, error_code="CODE_DRIFT",
                   component_version=loaded,
                   remedy=Remedy(operation="Hentikan aeos serve yang berjalan (Ctrl+C) lalu jalankan lagi",
                                 command=aeos_cmd("serve"), target="aeos serve (checkout)",
                                 rollback="git checkout commit sebelumnya lalu restart",
                                 evidence_to_collect="runtime.code_drift VERIFIED"))


def _schema(db_path: Any) -> dict[str, set[str]]:
    """table -> column names, read through a read-only connection (nothing is created or changed)."""
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        out: dict[str, set[str]] = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            out[name] = {r[1] for r in con.execute(f"PRAGMA table_info({quoted})")}
        return out
    finally:
        con.close()


def _schema_gaps(schema: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """(missing tables, missing columns 'table.column') of the code's models vs the database file.

    Without Alembic revisions an existing database is never altered by create_all, so a column added
    by a newer version is only visible here (honest "migration status" before the first revision)."""
    from aeos.core.db import Base, import_all_models

    import_all_models()
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for name, table in sorted(Base.metadata.tables.items()):
        if name not in schema:
            missing_tables.append(name)
            continue
        missing_columns += [f"{name}.{c.name}" for c in table.columns if c.name not in schema[name]]
    return missing_tables, missing_columns


@check("runtime.db", "RUNTIME", "Database dan status migrasi", component_id="sqlite-metadata-store",
       ttl_seconds=3600, invalidators=("db:schema",), severity_fail="CRITICAL", observation="configuration")
def runtime_db(ctx: CheckContext) -> Outcome:
    db = ctx.settings.layout.db_path
    remedy = Remedy(operation="Buat/upgrade database (backup otomatis sebelum migrasi)", command=aeos_cmd("db upgrade"),
                    source="aeos.core.migrate (Alembic)", target="<data>/aeos.sqlite3", privilege="pengguna lokal",
                    rollback="restore backup pra-upgrade dari <data>/backups", evidence_to_collect="aeos db status")
    if not db.is_file():
        return Outcome("MISSING", "Database lokal belum dibuat.", evidence={"database": "<data>/aeos.sqlite3"},
                       error_code="DB_MISSING", remedy=remedy)
    try:
        schema = _schema(db)
    except sqlite3.Error as exc:
        return Outcome("FAILED", f"Database tidak dapat dibaca ({type(exc).__name__}).", error_code="DB_UNREADABLE",
                       remedy=remedy)
    tables = set(schema)
    missing_tables = [t for t in REQUIRED_TABLES if t not in tables]
    model_missing_tables, missing_columns = _schema_gaps(schema)
    evidence: dict[str, Any] = {"database": "<data>/aeos.sqlite3", "tables": len(tables), "missing_tables": missing_tables,
                                "model_tables_missing": model_missing_tables[:50],
                                "columns_missing": missing_columns[:50]}
    if ctx.session is None:
        status: dict[str, Any] = {"current": None, "head": None}
    else:
        try:
            from aeos.core.migrate import migration_status

            status = migration_status()
        except Exception as exc:
            return Outcome("UNKNOWN", f"Status migrasi tidak dapat dibaca ({type(exc).__name__}).", evidence=evidence,
                           error_code="MIGRATION_STATUS_UNKNOWN")
    evidence.update({"current": status.get("current"), "head": status.get("head")})
    if missing_tables or model_missing_tables:
        names = missing_tables or model_missing_tables
        return Outcome("FAILED", f"Skema belum lengkap: {', '.join(names[:5])}{'...' if len(names) > 5 else ''} "
                       "tidak ada.", evidence=evidence, error_code="SCHEMA_INCOMPLETE", remedy=remedy)
    if missing_columns:
        return Outcome("FAILED", f"Skema database lebih lama dari kode: {len(missing_columns)} kolom tidak ada "
                       f"({', '.join(missing_columns[:5])}{'...' if len(missing_columns) > 5 else ''}); butuh revisi "
                       "migrasi, create_all tidak mengubah tabel yang sudah ada.", evidence=evidence,
                       error_code="SCHEMA_OUTDATED", remedy=remedy)
    head, current = status.get("head"), status.get("current")
    if head is not None and current != head:
        return Outcome("FAILED", f"Migrasi tertunda ({current} -> {head}).", evidence=evidence,
                       error_code="MIGRATION_PENDING", remedy=remedy)
    note = "skema pada head migrasi" if head else "belum ada revisi Alembic; skema dari bootstrap create_all"
    return Outcome("VERIFIED", f"Database ada; {note}.", evidence=evidence, component_version=head or "create_all",
                   target_identity="<data>/aeos.sqlite3")


@check("runtime.storage", "RUNTIME", "Tes tulis storage dan database", component_id="sqlite-metadata-store",
       ttl_seconds=3600, severity_fail="CRITICAL", observation="functional")
def runtime_storage(ctx: CheckContext) -> Outcome:
    db = ctx.settings.layout.db_path
    if not db.is_file():
        return Outcome("MISSING", "Database belum ada; tes tulis tidak dijalankan.", error_code="DB_MISSING",
                       remedy=Remedy(operation="Buat database lokal", command=aeos_cmd("db upgrade"),
                                     target="<data>/aeos.sqlite3", evidence_to_collect="tes tulis berhasil"))
    try:
        con = sqlite3.connect(str(db), timeout=5)
        try:
            con.execute("BEGIN IMMEDIATE")  # acquires the write lock: proves the DB is writable
            con.execute("CREATE TEMP TABLE aeos_doctor_probe (x INTEGER)")
            con.rollback()
        finally:
            con.close()
        probe = ctx.data_dir / ".aeos-storage-probe"
        probe.write_bytes(b"ok")
        ok = probe.read_bytes() == b"ok"
        probe.unlink()
    except (sqlite3.Error, OSError) as exc:
        return Outcome("FAILED", f"Tes tulis gagal ({type(exc).__name__}).", error_code="STORAGE_WRITE_FAILED",
                       remedy=Remedy(operation="Periksa izin/kunci file database dan data dir", target="<data>",
                                     evidence_to_collect="tes tulis berhasil"))
    if not ok:
        return Outcome("FAILED", "Isi file uji tidak sama setelah ditulis.", error_code="STORAGE_READBACK_FAILED")
    return Outcome("VERIFIED", "Tes tulis database (BEGIN IMMEDIATE + rollback) dan file data dir berhasil.",
                   evidence={"scenario": "write-lock + rollback; file write/read/delete"})
