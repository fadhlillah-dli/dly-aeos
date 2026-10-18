"""Auditable inventory reconciliation (§38 "Rekonsiliasi yang dapat diaudit").

1. Take the desired manifest (loading it first if the DB has none) for the selected scope.
2. Run the allowed, non-LLM, read-only probes: version commands with timeouts, loopback health URLs,
   scoped repository/lockfile/config reads. No machine-wide scans; no functional write tests.
3. Store observations with source identity, timestamp, scope, resolved version, a redacted
   operation reference and an expiry.
4. Compare desired vs configured vs observed -> drift findings; desired state is never rewritten.
5. Persist events and projections. A failing probe yields UNKNOWN/FAILED, never green.
6. Remedies are proposals (Doctor/UI); nothing is installed, upgraded, disabled or adopted here.

A scoped refresh probes one component and resolves only that component's findings; every
component keeps its own status/time (partial reconciliation never claims the whole inventory).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import platform
import sqlite3
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.config import LOOPBACK_HOSTS, Settings
from aeos.core.errors import Conflict, NotFound
from aeos.core.hashing import content_hash
from aeos.core.ids import slugify
from aeos.core.paths import repo_root
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.inventory import scope as scope_reader
from aeos.inventory.drift import compute_drift, persist_drift
from aeos.inventory.manifest import MANIFEST_REL, apply_manifest, current_manifest
from aeos.inventory.observations import UNREGISTERED_PREFIX, component_domain, record_observation
from aeos.inventory.probes import (
    PROVIDER_ENV_PREFIXES,
    ProbeRefused,
    ProbeRunner,
    clean_output,
    get_runner,
    github_slug_from_remote,
    is_loopback_url,
    logical_location,
    parse_version,
    scrubbed_env,
    strip_url_credentials,
    version_tuple,
)
from aeos.models.inventory import ComponentObservation, ComponentReference
from aeos.models.inventory_ext import ReconciliationRun

OBSERVER = "reconciler"
CORE_REQUIREMENTS = frozenset({"REQUIRED", "OPTIONAL"})
DEFAULT_TTL = 3600
DECLARATION_TTL = 86400

# Managed-scope sources and the registered component that owns what they declare.
SOURCE_OWNERS = {
    "python": "dly-aeos-control-plane",
    "npm": "control-center-frontend",
    "actions": "github-actions",
    "vscode": "vscode",
}


@dataclass
class ReconcileContext:
    settings: Settings
    runner: ProbeRunner
    repo: Path
    session: Session
    now: datetime = field(default_factory=utcnow)

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    @property
    def probe_cwd(self) -> Path:
        """Probes never run from a worker/repository directory (§12)."""
        sup = self.settings.layout.supervisor_workspace
        if sup.is_dir():
            return sup
        if self.data_dir.is_dir():
            return self.data_dir
        return Path(tempfile.gettempdir())

    def logical(self, path: str | None) -> str | None:
        return logical_location(path, repo=self.repo, data_dir=self.data_dir)


Spec = dict[str, Any]  # kwargs for record_observation (minus session/component/observer)


def _obs(dimension: str, status: str, *, version: str | None = None, identity: str | None = None,
         ttl: int = DEFAULT_TTL, **detail: Any) -> Spec:
    return {"dimension": dimension, "status": status, "observed_version": version, "observed_identity": identity,
            "ttl_seconds": ttl, "detail": {k: v for k, v in detail.items() if v is not None}}


# ---------------------------------------------------------------- per-type probes


def _probe_binary(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    timeout = float(probe.get("timeout_seconds") or 10)
    env = scrubbed_env(ctx.runner.environ(), prefixes=probe.get("scrub_env_prefixes") or PROVIDER_ENV_PREFIXES)
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for b in probe.get("binaries") or []:
        name = b["name"]
        override = b.get("settings_override")
        configured = getattr(ctx.settings, override, None) if override else None
        path: str | None
        lookup = "settings" if configured else "PATH"
        if configured:
            path = str(configured) if ctx.runner.stat(str(configured)) else None
        else:
            path = ctx.runner.which(name)
        if not path:
            missing.append(name)
            continue
        argv = [path, *b.get("version_args", [])]
        rr = ctx.runner.run(argv, timeout=timeout, env=env, cwd=ctx.probe_cwd)
        version = None
        if rr.returncode == 0 and rr.error is None:
            version = parse_version(f"{rr.stdout}\n{rr.stderr}", b.get("version_regex"))
        found.append({
            "name": name, "lookup": lookup, "location": ctx.logical(path), "version": version,
            "exit_code": rr.returncode, "error": rr.error,
            "output": clean_output(rr.stdout or rr.stderr, 160) if version is None else None,
            "operation_ref": " ".join([name, *b.get("version_args", [])]),
        })
    specs: list[Spec] = []
    if missing:
        specs.append(_obs("installation", "NOT_INSTALLED", ttl=ttl, missing=missing,
                          operation_ref="which " + " ".join(missing), lookup="PATH/settings", found=found or None))
    elif found:
        versions = [f"{f['name']} {f['version']}" if len(found) > 1 else f["version"] for f in found]
        version = "; ".join(v for v in versions if v) or None
        specs.append(_obs(
            "installation", "INSTALLED", version=version, ttl=ttl,
            location=found[0]["location"], operation_ref=found[0]["operation_ref"], binaries=found,
            version_status="RESOLVED" if all(f["version"] for f in found) else "UNKNOWN",
            distribution_verified=False,
        ))
    health = probe.get("health")
    if health:
        specs.extend(_probe_health(ctx, health, timeout, ttl=min(ttl, 900)))
    return specs


def _probe_health(ctx: ReconcileContext, health: dict[str, Any], timeout: float, *, ttl: int) -> list[Spec]:
    base = str(getattr(ctx.settings, health["settings_url"], "") or "").rstrip("/")
    url = base + health["path"]
    if not base:
        return [_obs("configuration", "MISSING", ttl=ttl, setting=health["settings_url"])]
    if not is_loopback_url(url):
        return [
            _obs("configuration", "INVALID", ttl=ttl, setting=health["settings_url"],
                 reason="endpoint bukan loopback; V1 hanya mem-probe 127.0.0.1/localhost"),
            _obs("connectivity", "UNKNOWN", ttl=ttl, reason="probe non-loopback ditolak"),
        ]
    specs = [_obs("configuration", "CONFIGURED", ttl=ttl, setting=health["settings_url"], endpoint="loopback")]
    try:
        hr = ctx.runner.http_get(url, timeout=min(timeout, 5.0))
    except ProbeRefused as exc:
        return [*specs, _obs("connectivity", "UNKNOWN", ttl=ttl, reason=str(exc)[:120])]
    op = f"GET {health['path']} (loopback)"
    if hr.status is None:
        return [*specs, _obs("connectivity", "UNREACHABLE", ttl=ttl, error=hr.error, operation_ref=op)]
    detail: dict[str, Any] = {"http_status": hr.status, "operation_ref": op}
    if health.get("expect") == "openbao":
        body = hr.body if isinstance(hr.body, dict) else {}
        detail.update({k: body.get(k) for k in ("initialized", "sealed", "standby") if k in body})
        detail["server_version"] = body.get("version")
        # Something answered; whether it is OpenBao is only confirmed by the health schema.
        detail["identity_confirmed"] = "initialized" in body and "sealed" in body
    # Reaching the endpoint is CONNECTED; it says nothing about auth/policy/function (§08, §38).
    return [*specs, _obs("connectivity", "CONNECTED", ttl=ttl, **detail)]


def _probe_python_runtime(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    version = platform.python_version()
    minimum = probe.get("min_version") or "3.11"
    meets = version_tuple(version) >= version_tuple(minimum)
    return [_obs("installation", "INSTALLED", version=version, ttl=int(probe.get("ttl_seconds") or DEFAULT_TTL),
                 location=ctx.logical(sys.executable), implementation=platform.python_implementation(),
                 min_version=minimum, meets_minimum=meets, operation_ref="python --version (running interpreter)")]


def _dist_version(dist: str | None) -> str | None:
    if not dist:
        return None
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _probe_python_module(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    module = probe.get("module") or probe.get("distribution")
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    try:
        spec = importlib.util.find_spec(str(module))
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return [_obs("installation", "NOT_INSTALLED", ttl=ttl, module=module,
                     operation_ref=f"importlib.find_spec({module})", environment="running interpreter")]
    version = _dist_version(probe.get("distribution"))
    return [_obs("installation", "INSTALLED", version=version, ttl=ttl, module=module,
                 location=ctx.logical(spec.origin) if spec.origin else None,
                 operation_ref=f"importlib.metadata.version({probe.get('distribution') or module})",
                 version_status="RESOLVED" if version else "UNKNOWN", environment="running interpreter")]


def _module_is_placeholder(spec: Any) -> bool:
    origin = getattr(spec, "origin", None)
    if not origin or not str(origin).endswith(".py"):
        return False
    try:
        head = Path(origin).read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return False
    return "PLACEHOLDER" in head


def _probe_platform_module(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    package = str(probe["package"])
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    try:
        pkg_spec = importlib.util.find_spec(package)
    except (ImportError, ValueError):
        pkg_spec = None
    if pkg_spec is None:
        return [_obs("installation", "NOT_INSTALLED", ttl=ttl, package=package, implemented="ABSENT",
                     reason="paket modul belum ada di source", operation_ref=f"find_spec({package})")]
    routes = 0
    try:
        router_mod = importlib.import_module(f"{package}.router")
        routes = len(getattr(getattr(router_mod, "router", None), "routes", []) or [])
    except Exception:  # module without router or failing import: counted as 0 routes
        routes = 0
    present: list[str] = []
    placeholders: list[str] = []
    absent: list[str] = []
    for m in probe.get("impl_modules") or []:
        try:
            spec = importlib.util.find_spec(m)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            absent.append(m)
        elif _module_is_placeholder(spec):
            placeholders.append(m)
        else:
            present.append(m)
    impl = probe.get("impl_modules") or []
    if present or routes > 0:
        implemented = "FULL" if not absent and not placeholders and (routes > 0 or impl) else "PARTIAL"
        status = "INSTALLED"
    else:
        implemented = "SKELETON"
        status = "NOT_INSTALLED"
    return [_obs("installation", status, ttl=ttl, package=package, implemented=implemented, api_routes=routes,
                 impl_present=present, impl_missing=absent, impl_placeholder=placeholders,
                 operation_ref=f"find_spec({package}.*); router routes",
                 reason=None if status == "INSTALLED" else "hanya kerangka modul (belum ada implementasi)")]


def _probe_npm_package(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    pkg = str(probe["package"])
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    version = scope_reader.npm_installed_version(ctx.repo, pkg)
    if version is None:
        return [_obs("installation", "NOT_INSTALLED", ttl=ttl, package=pkg,
                     reason="frontend/node_modules belum dipasang (npm ci)",
                     operation_ref=f"read frontend/node_modules/{pkg}/package.json")]
    return [_obs("installation", "INSTALLED", version=version, ttl=ttl, package=pkg,
                 location=f"<repo>/frontend/node_modules/{pkg}",
                 operation_ref=f"read frontend/node_modules/{pkg}/package.json")]


def _probe_repo_file(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    rel = str(probe["path"])
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    path = ctx.repo / rel
    if not path.is_file():
        return [_obs("installation", "NOT_INSTALLED", ttl=ttl, path=f"<repo>/{rel}", operation_ref=f"stat {rel}")]
    specs = [_obs("installation", "INSTALLED", ttl=ttl, location=f"<repo>/{rel}", operation_ref=f"stat {rel}")]
    if rel == MANIFEST_REL:
        specs.extend(_manifest_config(ctx, path, ttl))
    return specs


def _manifest_config(ctx: ReconcileContext, path: Path, ttl: int) -> list[Spec]:
    rec = current_manifest(ctx.session)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        file_hash = content_hash(data)
    except (OSError, yaml.YAMLError) as exc:
        return [_obs("configuration", "INVALID", ttl=ttl, error=type(exc).__name__)]
    if rec is None:
        return [_obs("configuration", "MISSING", ttl=ttl, reason="manifest belum dimuat (aeos seed)", file_hash=file_hash)]
    if rec.content_hash != file_hash:
        return [_obs("configuration", "DRIFTED", version=file_hash[:19], ttl=ttl, aspect="manifest_hash",
                     loaded_hash=rec.content_hash, file_hash=file_hash,
                     reason="manifest berubah sejak dimuat; jalankan aeos seed setelah review")]
    return [_obs("configuration", "CONFIGURED", version=file_hash[:19], ttl=ttl, loaded_hash=rec.content_hash)]


def _probe_git_repo(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    timeout = float(probe.get("timeout_seconds") or 10)
    git = ctx.runner.which("git")
    if not git:
        return [_obs("installation", "UNKNOWN", ttl=ttl, reason="git tidak ditemukan; repo tidak dapat dibaca")]
    base = [git, "-C", str(ctx.repo)]

    def run(*args: str) -> str | None:
        rr = ctx.runner.run([*base, *args], timeout=timeout, cwd=ctx.repo)
        return rr.stdout.strip() if rr.ok else None

    top = run("rev-parse", "--show-toplevel")
    if top is None:
        return [_obs("installation", "NOT_INSTALLED", ttl=ttl, reason="bukan git working tree",
                     operation_ref="git rev-parse --show-toplevel")]
    head = run("rev-parse", "HEAD")
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    porcelain = run("status", "--porcelain")
    remote = run("remote", "get-url", "origin")
    slug = github_slug_from_remote(remote) if remote else None
    identity = f"github:{slug}" if slug else (strip_url_credentials(remote) if remote else None)
    specs = [_obs("installation", "INSTALLED", version=(head or "")[:12] or None, identity=identity, ttl=ttl,
                  location="<repo>", branch=branch, head=head,
                  dirty_files=len(porcelain.splitlines()) if porcelain is not None else None,
                  operation_ref="git rev-parse/status/remote (read-only)")]
    expected = ctx.settings.platform_repo_slug
    if remote is None:
        specs.append(_obs("configuration", "MISSING", ttl=ttl, aspect="identity", reason="remote origin belum ada",
                          expected=expected))
    elif slug and slug.lower() == expected.lower():
        specs.append(_obs("configuration", "CONFIGURED", identity=identity, ttl=ttl, aspect="identity",
                          expected=expected))
    else:
        specs.append(_obs("configuration", "DRIFTED", identity=identity, ttl=ttl, aspect="identity",
                          expected=expected, reason="remote origin tidak sama dengan platform_repo_slug"))
    return specs


def _probe_sqlite(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    db = ctx.settings.layout.db_path
    specs = [_obs("installation", "INSTALLED", version=sqlite3.sqlite_version, ttl=ttl,
                  operation_ref="sqlite3.sqlite_version", library="python sqlite3")]
    if db.is_file():
        specs.append(_obs("configuration", "CONFIGURED", ttl=ttl, location=ctx.logical(str(db)),
                          size_bytes=db.stat().st_size))
    else:
        specs.append(_obs("configuration", "MISSING", ttl=ttl, reason="database belum dibuat (aeos db upgrade)"))
    return specs


def _probe_keyring(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    try:
        import keyring

        kr = keyring.get_keyring()
    except Exception as exc:
        return [_obs("installation", "UNKNOWN", ttl=ttl, error=type(exc).__name__)]
    name = f"{type(kr).__module__}.{type(kr).__name__}"
    usable = not any(x in name.lower() for x in ("fail", "null"))
    return [_obs("installation", "INSTALLED" if usable else "NOT_INSTALLED", ttl=ttl, backend=name,
                 operation_ref="keyring.get_keyring() (tanpa membaca secret)",
                 reason=None if usable else "tidak ada backend keyring yang dapat dipakai")]


def _probe_data_dir(ctx: ReconcileContext, comp: ComponentReference, probe: dict[str, Any]) -> list[Spec]:
    ttl = int(probe.get("ttl_seconds") or DEFAULT_TTL)
    rel = str(probe["path"])
    path = ctx.data_dir / rel
    if not path.is_dir():
        return [_obs("installation", "NOT_INSTALLED", ttl=ttl, location=f"<data>/{rel}", reason="direktori belum ada")]
    entries = sum(1 for _ in path.iterdir())
    if probe.get("require_entries") and entries == 0:
        return [_obs("installation", "NOT_INSTALLED", ttl=ttl, location=f"<data>/{rel}", entries=0,
                     reason="direktori kosong")]
    return [_obs("installation", "INSTALLED", ttl=ttl, location=f"<data>/{rel}", entries=entries)]


PROBES = {
    "binary": _probe_binary,
    "python_runtime": _probe_python_runtime,
    "python_module": _probe_python_module,
    "python_package": _probe_python_module,
    "platform_module": _probe_platform_module,
    "npm_package": _probe_npm_package,
    "repo_file": _probe_repo_file,
    "git_repo": _probe_git_repo,
    "sqlite": _probe_sqlite,
    "keyring_backend": _probe_keyring,
    "data_dir": _probe_data_dir,
}


def probe_component(ctx: ReconcileContext, comp: ComponentReference) -> list[Spec]:
    probe = comp.probe or {}
    fn = PROBES.get(str(probe.get("type")))
    if fn is None:
        return []
    return fn(ctx, comp, probe)


def is_probeable(comp: ComponentReference) -> bool:
    return str((comp.probe or {}).get("type") or "none") in PROBES


def core_targets(components: Iterable[ComponentReference]) -> list[ComponentReference]:
    return [c for c in components
            if c.tombstoned_at is None and c.requirement in CORE_REQUIREMENTS and is_probeable(c)]


# ---------------------------------------------------------------- managed-scope sources


def _match_index(components: Iterable[ComponentReference], key: str) -> dict[str, str]:
    idx: dict[str, str] = {}
    for c in components:
        for name in ((c.details or {}).get("discovery") or {}).get(key) or []:
            idx[str(name).lower()] = c.component_id
    return idx


def _read_sources(ctx: ReconcileContext, comps: dict[str, ComponentReference],
                  owners: set[str] | None = None) -> tuple[list[dict[str, Any]], list[tuple[str, Spec]]]:
    """Return (coverage sources, [(component_id, spec)]) for declarations and unregistered findings."""
    out: list[tuple[str, Spec]] = []
    sources: list[dict[str, Any]] = []
    want = owners  # None = all owners

    def owned(kind: str) -> bool:
        return want is None or SOURCE_OWNERS[kind] in want

    if owned("python"):
        read, deps = scope_reader.read_pyproject(ctx.repo)
        lock = scope_reader.read_python_lock(ctx.repo)
        sources += [read.as_dict(), lock.as_dict()]
        if deps:
            idx = _match_index(comps.values(), "python")
            packages = []
            for d in deps:
                installed = _dist_version(d["name"])
                entry = {**d, "installed": installed}
                packages.append(entry)
                target = idx.get(d["name"])
                if target and target in comps:
                    out.append((target, _obs("declaration", "DECLARED", version=installed, ttl=DECLARATION_TTL,
                                             source="pyproject.toml", package=d["name"], spec=d["spec"],
                                             group=d["group"])))
            out.append((SOURCE_OWNERS["python"], _obs(
                "declaration", "DECLARED", ttl=DECLARATION_TTL, source="pyproject.toml", packages=packages,
                lockfile="requirements.lock" if lock.status == "READ" else None,
                lockfile_status=lock.status, environment=ctx.logical(sys.prefix),
            )))
    if owned("npm"):
        read, direct, transitive = scope_reader.read_package_lock(ctx.repo)
        sources.append(read.as_dict())
        if read.status == "READ":
            idx = _match_index(comps.values(), "npm")
            for d in direct:
                target = idx.get(d["name"].lower())
                if target and target in comps:
                    out.append((target, _obs("declaration", "DECLARED", version=d.get("resolved"),
                                             ttl=DECLARATION_TTL, source="frontend/package-lock.json",
                                             package=d["name"], spec=d["spec"], group=d["group"])))
            out.append((SOURCE_OWNERS["npm"], _obs(
                "declaration", "DECLARED", ttl=DECLARATION_TTL, source="frontend/package-lock.json",
                packages=direct, transitive_packages=transitive,
            )))
    if owned("actions"):
        read, actions = scope_reader.read_workflows(ctx.repo)
        sources.append(read.as_dict())
        if read.status == "READ":
            out.append((SOURCE_OWNERS["actions"], _obs(
                "declaration", "DECLARED", ttl=DECLARATION_TTL, source=".github/workflows", actions=actions,
                unpinned=[a["uses"] for a in actions if not a["pinned_sha"]],
            )))
    if owned("vscode"):
        read, recs = scope_reader.read_vscode_extensions(ctx.repo)
        sources.append(read.as_dict())
        if read.status == "READ":
            out.append((SOURCE_OWNERS["vscode"], _obs("declaration", "DECLARED", ttl=DECLARATION_TTL,
                                                      source=".vscode/extensions.json", recommendations=recs)))
    if want is None:
        reads, servers = scope_reader.read_mcp_configs(ctx.repo)
        sources += [r.as_dict() for r in reads]
        idx = _match_index(comps.values(), "mcp")
        for s in servers:
            target = idx.get(s["name"].lower())
            if target and target in comps:
                out.append((target, _obs("declaration", "DECLARED", ttl=DECLARATION_TTL, source=s["file"],
                                         mcp_server=s["name"], command=s.get("command"))))
            else:
                uid = f"{UNREGISTERED_PREFIX}mcp:{slugify(s['name'], 60)}"
                out.append((uid, _obs("discovery", "UNREGISTERED", ttl=DECLARATION_TTL, kind="mcp_server",
                                      name=s["name"], source=s["file"], command=s.get("command"),
                                      note="ditemukan di managed scope tanpa registrasi; tidak diadopsi/diberi akses")))
    return sources, out


def _config_specs(ctx: ReconcileContext, comps: dict[str, ComponentReference]) -> list[tuple[str, Spec]]:
    """Effective non-secret settings of the running controller (CONFIG lens inputs)."""
    s = ctx.settings
    out: list[tuple[str, Spec]] = []
    if "dly-aeos-control-plane" in comps:
        ok = s.host in LOOPBACK_HOSTS
        out.append(("dly-aeos-control-plane", _obs(
            "configuration", "CONFIGURED" if ok else "INVALID", ttl=DEFAULT_TTL, host=s.host, port=s.port,
            dev_mode=s.dev_mode, environment=s.environment, source="settings (non-secret)",
        )))
    return out


# ---------------------------------------------------------------- change detection / invalidation

IDENTITY_DIMENSIONS = ("installation", "configuration")


def _previous_own(session: Session, component_id: str, dimension: str) -> ComponentObservation | None:
    """Latest earlier observation of this dimension by the reconciler itself (other observers report
    versions in their own format, so they are not compared, which avoids spurious invalidations)."""
    return session.scalar(
        select(ComponentObservation)
        .where(ComponentObservation.component_id == component_id, ComponentObservation.dimension == dimension,
               ComponentObservation.observer == OBSERVER)
        .order_by(ComponentObservation.observed_at.desc()).limit(1)
    )


def identity_change(session: Session, component_id: str, specs: list[Spec]) -> dict[str, Any] | None:
    """What changed between the previous reconciler observation and *specs* (status, resolved version
    or identity of the installation/configuration dimensions); None when nothing relevant changed or
    there is no earlier observation to compare with."""
    changes: list[dict[str, Any]] = []
    for dim in IDENTITY_DIMENSIONS:
        new = next((s for s in specs if s["dimension"] == dim), None)
        if new is None:
            continue
        prev = _previous_own(session, component_id, dim)
        if prev is None:
            continue
        for field_name, before, after in (
            ("status", prev.status, new["status"]),
            ("version", prev.observed_version, new.get("observed_version")),
            ("identity", prev.observed_identity, new.get("observed_identity")),
        ):
            if field_name != "status" and (before is None or after is None):
                continue  # UNKNOWN on either side is not evidence of a change
            if before != after:
                changes.append({"dimension": dim, "field": field_name, "before": before, "after": after})
    return {"changes": changes} if changes else None


def invalidators_for(comp: ComponentReference, change: dict[str, Any]) -> list[str]:
    """Doctor invalidators affected by *change* — only the checks that really depend on it (§38: do not
    freeze capabilities that do not depend on the change).

    * repository: its resolved version is the HEAD commit, so a new commit only invalidates
      HEAD-dependent checks (``repo:head``); an origin change the identity checks (``repo:origin``);
    * binary probes: ``binary:<name>`` for each probed executable (e.g. ``binary:claude`` marks
      claude.binary/claude.auth/claude.capability_test, but not the environment check);
    * anything else: ``component:<id>`` (every check about that component).
    """
    if comp.kind == "repository":
        return sorted({"repo:origin" if c["field"] == "identity" else "repo:head" for c in change["changes"]})
    probe = comp.probe or {}
    if probe.get("type") == "binary" and probe.get("binaries"):
        return sorted({f"binary:{b['name']}" for b in probe["binaries"] if b.get("name")})
    return [f"component:{comp.component_id}"]


def _invalidate_component(session: Session, comp: ComponentReference, change: dict[str, Any]) -> dict[str, Any]:
    """Mark the affected readiness checks STALE through the doctor facade and announce the change."""
    from aeos.events import service as events

    summary = "; ".join(f"{c['dimension']}.{c['field']}: {c['before']} -> {c['after']}" for c in change["changes"])
    reason = redact_text(f"rekonsiliasi melihat perubahan ({summary})")[:200]
    invalidators = invalidators_for(comp, change)
    n = 0
    try:
        from aeos.doctor import facade as doctor

        for inv in invalidators:
            n += doctor.invalidate(session, invalidator=inv, reason=reason)
    except (ImportError, ModuleNotFoundError):  # doctor absent: nothing was fresh to begin with
        n = 0
    events.emit(session, source="inventory", kind="inventory.component.changed", entity_type="component",
                entity_id=comp.component_id, domain=component_domain(session, comp.component_id),
                payload={"changes": change["changes"], "invalidators": invalidators, "checks_marked_stale": n})
    return {**change, "invalidators": invalidators, "checks_marked_stale": n}


# ---------------------------------------------------------------- entry point


def _ensure_desired(session: Session, actor: str) -> str:
    rec = current_manifest(session)
    has_components = session.scalar(select(ComponentReference.component_id).limit(1)) is not None
    if rec is None or not has_components:
        result = apply_manifest(session, actor=actor)
        return str(result["content_hash"])
    return rec.content_hash


def reconcile(
    session: Session, settings: Settings, *, component_id: str | None = None, runner: ProbeRunner | None = None,
    actor: str = "system", trigger: str = "cli", repo: Path | None = None,
) -> ReconciliationRun:
    """Run one auditable reconciliation pass. The caller commits."""
    from aeos.events import service as events

    manifest_hash = _ensure_desired(session, actor)
    comps = {c.component_id: c for c in session.scalars(select(ComponentReference))}
    if component_id is not None:
        comp = comps.get(component_id)
        if comp is None:
            raise NotFound(f"component {component_id} is not registered")
        if comp.tombstoned_at is not None:
            raise Conflict(f"component {component_id} is tombstoned; history only", code="COMPONENT_TOMBSTONED")
        targets = [comp]
        scope: dict[str, Any] = {"component_id": component_id}
    else:
        targets = core_targets(comps.values())
        scope = {"set": "core", "requirements": sorted(CORE_REQUIREMENTS)}
    ctx = ReconcileContext(settings=settings, runner=runner or get_runner(), repo=repo or repo_root(), session=session)
    run = ReconciliationRun(trigger=trigger, actor=actor, scope=scope, status="RUNNING", manifest_hash=manifest_hash,
                            started_at=ctx.now)
    session.add(run)
    session.flush()

    component_status: dict[str, Any] = {}
    invalidated: dict[str, Any] = {}
    recorded = 0
    for comp in targets:
        cid = comp.component_id
        at = utcnow().isoformat()
        if not is_probeable(comp):
            component_status[cid] = {"status": "SKIPPED", "at": at,
                                     "reason": str((comp.probe or {}).get("reason") or "tanpa probe")}
            continue
        try:
            specs = probe_component(ctx, comp)
            change = identity_change(session, cid, specs)
            if change:
                # §08/§38: a changed binary/version/identity makes the affected readiness checks STALE
                # before the capability is used again. Done before recording, so only the older
                # observations are expired.
                invalidated[cid] = _invalidate_component(session, comp, change)
            for spec in specs:
                record_observation(session, component_id=cid, observer=OBSERVER, scope={"run_id": run.id}, **spec)
                recorded += 1
            unknown = any(s["status"] in ("UNKNOWN", "UNREACHABLE", "INVALID") for s in specs)
            component_status[cid] = {"status": "UNKNOWN" if unknown else "OK", "at": at, "observations": len(specs)}
        except Exception as exc:  # a broken probe never turns green (§38 step 5)
            record_observation(session, component_id=cid, dimension="installation", status="UNKNOWN",
                               observer=OBSERVER, ttl_seconds=DEFAULT_TTL, scope={"run_id": run.id},
                               detail={"error": type(exc).__name__, "message": redact_text(str(exc))[:200]})
            recorded += 1
            component_status[cid] = {"status": "FAILED", "at": at, "reason": type(exc).__name__}

    owners = None if component_id is None else {component_id} & set(SOURCE_OWNERS.values())
    sources: list[dict[str, Any]] = []
    if owners is None or owners:
        sources, extra = _read_sources(ctx, comps, owners)
        if component_id is None:
            extra += _config_specs(ctx, comps)
        for cid, spec in extra:
            record_observation(session, component_id=cid, observer=OBSERVER, scope={"run_id": run.id}, **spec)
            recorded += 1

    evaluated = None if component_id is None else {component_id}
    items = compute_drift(session, evaluated=evaluated)
    drift_summary = persist_drift(session, items, run_id=run.id, evaluated=evaluated)

    statuses = [v["status"] for v in component_status.values()]
    if statuses and all(s == "FAILED" for s in statuses):
        run.status = "FAILED"
    elif any(s in ("FAILED", "UNKNOWN") for s in statuses):
        run.status = "PARTIAL"
    else:
        run.status = "COMPLETED"
    run.component_status = component_status
    run.sources = sources
    run.observations_recorded = recorded
    run.summary = {
        "probed": sum(1 for s in statuses if s != "SKIPPED"),
        "skipped": statuses.count("SKIPPED"),
        "failed": statuses.count("FAILED"),
        "unknown": statuses.count("UNKNOWN"),
        "drift": drift_summary,
        "invalidated": invalidated,
    }
    run.finished_at = utcnow()
    session.flush()
    events.emit(
        session, source="inventory", kind="inventory.reconcile.completed", entity_type="reconciliation_run",
        entity_id=run.id, payload={"status": run.status, "scope": scope, "summary": run.summary},
    )
    return run


def latest_run(session: Session, *, full_only: bool = False) -> ReconciliationRun | None:
    q = select(ReconciliationRun).where(ReconciliationRun.finished_at.is_not(None))
    runs = list(session.scalars(q.order_by(ReconciliationRun.started_at.desc()).limit(50)))
    for r in runs:
        if not full_only or (r.scope or {}).get("set") == "core":
            return r
    return None


def run_view(run: ReconciliationRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id, "status": run.status, "trigger": run.trigger, "actor": run.actor, "scope": run.scope,
        "started_at": run.started_at.isoformat(), "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "summary": run.summary, "component_status": run.component_status, "sources": run.sources,
        "observations_recorded": run.observations_recorded, "manifest_hash": run.manifest_hash,
    }
