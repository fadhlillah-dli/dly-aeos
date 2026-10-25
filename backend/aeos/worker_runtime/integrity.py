"""Controller-checkout integrity around every worker run (§09 Zone 1 "read-only for workers", ADR-0051).

Under the WEAK process sandbox the operating system does not stop a worker process from writing the
running controller checkout (its code, ``policy/``, the acceptance suite). Without a shell OpenCode's own
tools are confined to the worktree (``external_directory: deny``), so this is defence in depth against a
tool-permission bug: the controller's runtime files (all backend code, the served UI build, launchers,
scripts, config/contract files) and the trust-relevant protected-path list are fingerprinted before the
worker starts and compared after it exits (§09 "a worker cannot load its changes into the active
controller": any controller file would be loaded on the next restart). Any change fails and quarantines
the run, raises a HIGH audit/event, sets the host-wide **integrity hold** (``raise_hold``: worker
admission and ``CAN_RUN_WORKER`` fail closed until the owner reconciles) and is never loaded silently. An
owner editing the checkout during a run trips the check as well (fail closed). Writes to the data-dir
database are covered by the executor's trust-table tamper window (``aeos.verification.facade
.trust_table_window``) around process-sandbox runs; the container sandbox never mounts the data dir.

The controller **interpreter** is part of what the next start loads as well (re-audit 2026-10-19: the
editable-install finder/``.pth`` that maps ``aeos`` to the checkout and every installed dependency live in
the venv's site-packages, outside the checkout patterns). ``interpreter_roots`` adds them: startup hooks
(every top-level ``*.pth``, ``__editable__*`` finder, ``sitecustomize``/``usercustomize``, the platform's own
``dly_aeos-*.dist-info``/``*.egg-link`` and ``pyvenv.cfg``) are content-hashed; every other file under
site-packages and the venv's scripts directory is fingerprinted by size + modification time (cheap enough
for every run).

**Bytecode** (wave 7, W6-033): a ``.pyc`` whose header matches its source is imported INSTEAD of the source,
so the bytecode caches the next start reads are part of the snapshot - ``__pycache__`` of the checkout
(content-hashed) and of site-packages (size + mtime), and in release mode the ``PYTHONPYCACHEPREFIX`` tree
under the data dir (``sys.pycache_prefix``, content-hashed). The controller compiles lazily during a run, so
a new or changed cache file is accepted only when its code object is EQUAL to a fresh compile of its current
source (``verify_bytecode``: a separate ``python -I -S -B`` process, so a malformed planted file can never
crash or run inside the controller; byte equality is not usable - marshal output depends on reference
counts). Anything else is a finding and the cache file is deleted (neutralised: Python recompiles it from
the source). Only cache files this interpreter would read are considered (``<name>.<cache_tag>[.opt-N].pyc``).

**Owner config** (wave 7): the YAML settings file read at start (``AEOS_CONFIG`` or ``<config dir>/aeos.yaml``,
e.g. ``opencode_binary``/``claude_binary``) is content-hashed; a change during a run is a finding and the file
is restored to its pre-run bytes (removed when it did not exist before).

Residual risks stated honestly: a deliberate writer that restores size and mtime of an ordinary dependency
file or of a site-packages ``.pyc`` is not detected (content hashing every dependency on every run is not
cheap enough); pytest's assertion-rewrite cache (``*-pytest-*.pyc``) is not considered (the controller never
loads it and the verifier copies the trusted suite without bytecode); a changed controller checkout file other
than bytecode/config is reported and held, not restored (the owner restores it with git before reconciling);
only the container sandbox prevents writes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

from aeos.core.protected_paths import PLATFORM_PROTECTED_PATHS
from aeos.worker_runtime.scope import matches_any, static_prefix

# What the running controller loads besides the protected list (review 2026-10-19: a worker-written
# backend/aeos/models/*.py or frontend/dist bundle would be loaded on the next restart just the same).
CONTROLLER_RUNTIME_PATHS: tuple[str, ...] = (
    "backend/**", "frontend/dist/**", "scripts/**", "aeos", "aeos.ps1", "config/**", "contracts/**",
)
CONTROLLER_PATTERNS: tuple[str, ...] = tuple(dict.fromkeys(PLATFORM_PROTECTED_PATHS + CONTROLLER_RUNTIME_PATHS))
MAX_FILES = 20_000
MAX_FILE_BYTES = 8 * 1024 * 1024
CACHE_DIR = "__pycache__"
_SKIP_DIRS = {".git", "node_modules", ".pytest_cache", ".ruff_cache", ".mypy_cache"}

PYCACHE_PREFIX_LABEL = "pycache-prefix"
CONFIG_PREFIX = "config:"
MAX_CONFIG_COPY_BYTES = 256 * 1024
VERIFY_TIMEOUT_SECONDS = 180


def bytecode_name_ok(name: str) -> bool:
    """True for a cache file THIS interpreter's importer reads: ``<stem>.<cache_tag>.pyc`` or
    ``<stem>.<cache_tag>.opt-1|opt-2.pyc`` (another tag, pytest's ``-pytest-`` cache and the importer's
    temporary ``.pyc.<n>`` files are never imported by the controller)."""
    tag = sys.implementation.cache_tag
    if not tag or not name.endswith(".pyc"):
        return False
    parts = name[:-4].split(".")
    if len(parts) == 2:
        return bool(parts[0]) and parts[1] == tag
    if len(parts) == 3:
        return bool(parts[0]) and parts[1] == tag and parts[2] in ("opt-1", "opt-2")
    return False


def _iter_files(root: Path, patterns: tuple[str, ...]):
    seen: set[str] = set()
    for pat in patterns:
        prefix = static_prefix(pat)
        start = root / prefix if prefix else root
        if start.is_file() or start.is_symlink():
            rel = start.relative_to(root).as_posix()
            if rel not in seen and matches_any(rel, patterns, case_insensitive=True):
                seen.add(rel)
                yield rel, start
            continue
        if not start.is_dir():
            # a glob such as scripts/bootstrap.* : scan the parent directory only
            parent = start.parent if prefix else root
            if parent.is_dir() and parent != root:
                for child in parent.iterdir():
                    rel = child.relative_to(root).as_posix()
                    if child.is_file() and rel not in seen and matches_any(rel, patterns, case_insensitive=True):
                        seen.add(rel)
                        yield rel, child
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            in_cache = os.path.basename(dirpath) == CACHE_DIR
            for name in filenames:
                if in_cache and not bytecode_name_ok(name):
                    continue  # only bytecode the importer reads; never another tag / pytest's rewrite cache
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                if rel in seen or not matches_any(rel, patterns, case_insensitive=True):
                    continue
                seen.add(rel)
                yield rel, full


def _digest(path: Path) -> str:
    try:
        if path.is_symlink():
            return "symlink:" + os.readlink(path)
        if path.stat().st_size > MAX_FILE_BYTES:
            st = path.stat()
            return f"large:{st.st_size}:{st.st_mtime_ns}"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


INTERPRETER_PREFIX = "interpreter:"
MAX_INTERPRETER_FILES = 200_000
# Loaded/executed by every interpreter start (site.py) or naming where the platform package is imported from.
_STARTUP_NAMES = ("sitecustomize.py", "usercustomize.py", "pyvenv.cfg")
_PLATFORM_DISTS = ("dly_aeos", "dly-aeos", "aeos")


def interpreter_roots() -> list[tuple[str, Path]]:
    """(label, directory) of the controller interpreter's install locations the next start loads: the venv
    (or system) site-packages, the scripts/bin directory of the launchers and the venv root (pyvenv.cfg)."""
    paths = sysconfig.get_paths()
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, key in (("site-packages", "purelib"), ("platlib", "platlib"), ("scripts", "scripts")):
        raw = paths.get(key)
        if not raw:
            continue
        p = Path(raw)
        try:
            norm = str(p.resolve()).lower() if sys.platform == "win32" else str(p.resolve())
        except OSError:
            continue
        if norm in seen or not p.is_dir():
            continue
        seen.add(norm)
        out.append((label, p))
    if sys.prefix != sys.base_prefix and (Path(sys.prefix) / "pyvenv.cfg").is_file():
        out.append(("venv", Path(sys.prefix)))
    return out


def pycache_prefix_root() -> Path | None:
    """The bytecode cache tree of the RUNNING controller when ``PYTHONPYCACHEPREFIX`` / ``-X pycache_prefix`` is
    set (release mode: ``<data dir>/cache/release-pycache/<release id>``); there all bytecode is read from it."""
    prefix = getattr(sys, "pycache_prefix", None)
    return Path(prefix) if prefix else None


def config_file() -> Path:
    """The owner's YAML settings file the controller reads at start (``AEOS_CONFIG`` or the config dir)."""
    from aeos.core.config import _config_file_path

    return _config_file_path()


_PLATFORM_NORMALISED = frozenset(d.replace("-", "_").lower() for d in _PLATFORM_DISTS)


def _startup_critical(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if len(parts) == 1 and (name.endswith(".pth") or name.startswith("__editable__") or name in _STARTUP_NAMES
                            or name.endswith(".egg-link")):
        return True
    top = parts[0].lower()
    if not top.endswith((".dist-info", ".egg-info")):
        return False
    # the platform's own install metadata (names where "aeos" is imported from): the dist name is the
    # segment before the first "-" (e.g. "dly_aeos-0.1.0.dist-info" -> "dly_aeos")
    dist_name = top.rsplit(".", 1)[0].split("-", 1)[0].replace("-", "_")
    return dist_name in _PLATFORM_NORMALISED


def _interpreter_files(label: str, root: Path, files: dict[str, str]) -> bool:
    """Add one interpreter root to *files*; False when the file cap was reached."""
    if label == "venv":  # only the venv marker; its Lib/ and Scripts/ are the other roots
        cfg = root / "pyvenv.cfg"
        files[f"{INTERPRETER_PREFIX}venv/pyvenv.cfg"] = _digest(cfg)
        return True
    flat = label == "scripts"  # launchers live at the top level of Scripts/ or bin/
    # (relative path prefix, directory) pairs; string paths keep this walk cheap enough for every run
    stack: list[tuple[str, str]] = [("", str(root))]
    while stack:
        prefix, current = stack.pop()
        in_cache = os.path.basename(current) == CACHE_DIR
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not flat and entry.name not in _SKIP_DIRS:
                        stack.append((f"{prefix}{entry.name}/", entry.path))
                    continue
                if in_cache and not bytecode_name_ok(entry.name):
                    continue
                rel = prefix + entry.name
                key = f"{INTERPRETER_PREFIX}{label}/{rel}"
                if not flat and _startup_critical(rel):
                    files[key] = _digest(Path(entry.path))
                else:
                    st = entry.stat(follow_symlinks=False)
                    files[key] = f"stat:{st.st_size}:{st.st_mtime_ns}"
            except (OSError, ValueError):
                continue
            if len(files) >= MAX_INTERPRETER_FILES:
                return False
    return True


def _prefix_files(prefix: Path, files: dict[str, str]) -> bool:
    """Content hashes of every bytecode file in the PYTHONPYCACHEPREFIX tree (False when the cap was reached)."""
    if not prefix.is_dir():
        return True
    for dirpath, dirnames, filenames in os.walk(prefix):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not bytecode_name_ok(name):
                continue
            full = Path(dirpath) / name
            files[f"{PYCACHE_PREFIX_LABEL}:{full.relative_to(prefix).as_posix()}"] = _digest(full)
            if len(files) >= MAX_INTERPRETER_FILES:
                return False
    return True


_UNSET: Any = object()


def snapshot(root: Path | None = None, patterns: tuple[str, ...] = CONTROLLER_PATTERNS, *,
             interpreter: bool | None = None, pycache_prefix: Path | None = _UNSET,
             config_path: Path | None = _UNSET) -> dict[str, Any]:
    """Fingerprint of the runtime and trust-relevant files of the running controller checkout and (by default
    for the real controller checkout) of the controller interpreter's site-packages/scripts, the bytecode cache
    prefix tree and the owner's config file."""
    from aeos.core.paths import repo_root

    base = (root or repo_root()).resolve()
    real = root is None
    if interpreter is None:
        interpreter = real
    if pycache_prefix is _UNSET:
        pycache_prefix = pycache_prefix_root() if real else None
    if config_path is _UNSET:
        config_path = config_file() if real else None
    files: dict[str, str] = {}
    truncated = False
    for rel, full in _iter_files(base, patterns):
        if len(files) >= MAX_FILES:
            truncated = True
            break
        files[rel] = _digest(full)
    roots: list[tuple[str, Path]] = interpreter_roots() if interpreter else []
    for label, iroot in roots:
        if not _interpreter_files(label, iroot, files):
            truncated = True
            break
    if pycache_prefix is not None and not truncated and not _prefix_files(Path(pycache_prefix), files):
        truncated = True
    config_copy: str | None = None
    if config_path is not None:
        cfg = Path(config_path)
        key = f"{CONFIG_PREFIX}{cfg.name}"
        if cfg.is_file():
            files[key] = _digest(cfg)
            try:
                raw = cfg.read_bytes()
                if len(raw) <= MAX_CONFIG_COPY_BYTES:
                    config_copy = base64.b64encode(raw).decode("ascii")
            except OSError:
                config_copy = None
        else:
            files[key] = "absent"
    return {"root": str(base), "files": files, "truncated": truncated, "patterns": list(patterns),
            "interpreter": bool(interpreter), "interpreter_roots": {label: str(p) for label, p in roots},
            "pycache_prefix": str(pycache_prefix) if pycache_prefix is not None else None,
            "config_path": str(config_path) if config_path is not None else None, "config_copy": config_copy}


# ----------------------------------------------------------------------------- bytecode verification
_VERIFY_SCRIPT = r"""
import importlib.util, json, marshal, sys
items = json.loads(sys.stdin.read())
magic = importlib.util.MAGIC_NUMBER
out = {}
for it in items:
    key = it["key"]
    try:
        with open(it["pyc"], "rb") as fh:
            data = fh.read()
    except OSError:
        out[key] = "GONE"
        continue
    if len(data) < 16 or data[:4] != magic:
        out[key] = "BAD_HEADER"
        continue
    src = it.get("source")
    try:
        with open(src, "rb") as fh:
            source = fh.read()
    except (OSError, TypeError):
        out[key] = "NO_SOURCE"
        continue
    try:
        fresh = compile(source, src, "exec", dont_inherit=True, optimize=int(it.get("opt") or 0))
    except Exception:
        out[key] = "SOURCE_DOES_NOT_COMPILE"
        continue
    try:
        code = marshal.loads(data[16:])
    except Exception:
        out[key] = "UNREADABLE"
        continue
    out[key] = "OK" if type(code) is type(fresh) and code == fresh else "MISMATCH"
sys.stdout.write(json.dumps(out))
"""
ACCEPTED_VERDICTS = frozenset({"OK", "GONE"})  # GONE: removed again, nothing left to import


def _opt_level(name: str) -> int:
    parts = name[:-4].split(".")
    return int(parts[2][4:]) if len(parts) == 3 and parts[2].startswith("opt-") and parts[2][4:].isdigit() else 0


def _source_for_cache(pyc: Path) -> Path:
    """``<dir>/__pycache__/<stem>.<tag>[.opt-N].pyc`` -> ``<dir>/<stem>.py``."""
    return pyc.parent.parent / (pyc.name.split(".", 1)[0] + ".py")


def _source_for_prefix(prefix: Path, rel: str, anchors: list[Path]) -> Path | None:
    """A prefix-tree file mirrors the source's absolute path without its drive (``importlib`` strips it):
    ``<prefix>/<path>/<stem>.<tag>.pyc`` -> ``<drive>/<path>/<stem>.py`` (drive of the checkout/interpreter)."""
    parts = rel.split("/")
    tail = [*parts[:-1], parts[-1].split(".", 1)[0] + ".py"]
    candidates: list[Path] = []
    if sys.platform == "win32":
        drives = list(dict.fromkeys(a.drive for a in anchors if a.drive))
        candidates = [Path(d + "\\", *tail) for d in drives]
    else:
        candidates = [Path("/", *tail)]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0] if candidates else None


def _bytecode_location(key: str, before: dict[str, Any]) -> tuple[Path, Path | None] | None:
    """(cache file, source file) for a bytecode key of the snapshot, None when the key is not bytecode."""
    name = key.rsplit("/", 1)[-1]
    if not bytecode_name_ok(name):
        return None
    if key.startswith(f"{PYCACHE_PREFIX_LABEL}:"):
        prefix = before.get("pycache_prefix")
        if not prefix:
            return None
        rel = key[len(PYCACHE_PREFIX_LABEL) + 1:]
        anchors = [Path(before["root"]), *(Path(p) for p in (before.get("interpreter_roots") or {}).values())]
        pyc = Path(prefix) / rel
        return pyc, _source_for_prefix(Path(prefix), rel, anchors)
    if f"/{CACHE_DIR}/" not in f"/{key}":
        return None
    if key.startswith(INTERPRETER_PREFIX):
        label, _, rel = key[len(INTERPRETER_PREFIX):].partition("/")
        iroot = (before.get("interpreter_roots") or {}).get(label)
        if not iroot:
            return None
        pyc = Path(iroot) / rel
    else:
        pyc = Path(before["root"]) / key
    return pyc, _source_for_cache(pyc)


def verify_bytecode(items: list[tuple[str, Path, Path | None]], *,
                    timeout: float = VERIFY_TIMEOUT_SECONDS) -> dict[str, str]:
    """key -> verdict (``OK`` | ``GONE`` | ``MISMATCH`` | ``NO_SOURCE`` | ``BAD_HEADER`` | ``UNREADABLE`` |
    ``SOURCE_DOES_NOT_COMPILE`` | ``VERIFIER_FAILED``) for (key, cache file, source file) items: OK only when the
    cache file's code object equals a fresh compile of the current source. Runs in a separate isolated
    interpreter (``-I -S -B``: no site hooks, no bytecode writes); a crash/timeout fails closed."""
    if not items:
        return {}
    payload = [{"key": k, "pyc": str(p), "source": str(s) if s is not None else None, "opt": _opt_level(p.name)}
               for k, p, s in items]
    env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH", "WINDIR")}
    try:
        proc = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", _VERIFY_SCRIPT], input=json.dumps(payload),
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env, check=False)
        verdicts = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        verdicts = {}
    if not isinstance(verdicts, dict):
        verdicts = {}
    return {k: str(verdicts.get(k) or "VERIFIER_FAILED") for k, _p, _s in items}


def _neutralise_file(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return not path.exists()
    except OSError:
        return False


def _restore_config(before: dict[str, Any]) -> str:
    path = before.get("config_path")
    if not path:
        return "not restored (no config path)"
    cfg = Path(path)
    was_absent = (before.get("files") or {}).get(f"{CONFIG_PREFIX}{cfg.name}") == "absent"
    try:
        if was_absent:
            cfg.unlink(missing_ok=True)
            return "removed (did not exist before the run)"
        copy = before.get("config_copy")
        if copy is None:
            return "NOT restored (no pre-run copy kept)"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_bytes(base64.b64decode(copy))
        return "restored to the pre-run content"
    except (OSError, ValueError) as exc:
        return f"NOT restored ({type(exc).__name__})"


def compare(before: dict[str, Any] | None) -> list[dict[str, str]]:
    """Changed/added/removed controller files since ``before`` (empty = untouched).

    Bytecode caches: a removed cache file is harmless (recompiled from source); an added/changed one is a
    finding only when its code differs from a fresh compile of its current source - and it is then deleted.
    The owner config file is restored to its pre-run bytes on any change (``detail`` says what was done)."""
    if not before:
        return []
    after = snapshot(Path(before["root"]), tuple(before.get("patterns") or CONTROLLER_PATTERNS),
                     interpreter=bool(before.get("interpreter")),
                     pycache_prefix=Path(before["pycache_prefix"]) if before.get("pycache_prefix") else None,
                     config_path=Path(before["config_path"]) if before.get("config_path") else None)
    old, new = before.get("files") or {}, after["files"]
    out: list[dict[str, str]] = []
    if before.get("truncated") or after.get("truncated"):
        # review wave 7: a capped snapshot compared only part of the controller - UNKNOWN is never a pass
        out.append({"path": "*", "change": "incomplete",
                    "detail": f"more than {MAX_FILES} checkout / {MAX_INTERPRETER_FILES} interpreter files: not every "
                              "controller file was compared (fail closed)"})
    pending: list[tuple[str, Path, Path | None, str]] = []
    for rel in sorted(set(old) | set(new)):
        if rel in new and rel in old and old[rel] == new[rel]:
            continue
        change = "removed" if rel not in new else "added" if rel not in old else "modified"
        loc = _bytecode_location(rel, before)
        if loc is not None:
            if change != "removed":
                pending.append((rel, loc[0], loc[1], change))
            continue
        item = {"path": rel, "change": change}
        if rel.startswith(CONFIG_PREFIX):
            item["change"] = ("added" if old.get(rel) == "absent" else "removed" if new.get(rel) == "absent"
                              else change)
            item["detail"] = "owner config file changed while the worker ran; " + _restore_config(before)
        out.append(item)
    if pending:
        verdicts = verify_bytecode([(k, p, s) for k, p, s, _c in pending])
        for key, pyc, _src, change in pending:
            verdict = verdicts.get(key, "VERIFIER_FAILED")
            if verdict in ACCEPTED_VERDICTS:
                continue
            removed = _neutralise_file(pyc)
            out.append({"path": key, "change": change,
                        "detail": f"bytecode not equal to a fresh compile of its source ({verdict}); cache file "
                                  + ("removed (recompiled from source on next import)" if removed
                                     else "could NOT be removed")})
    return sorted(out, key=lambda c: c["path"])


# ----------------------------------------------------------------------------- integrity hold (fail closed)
HOLD_KEY = "worker_runtime.integrity_hold"
HOLD_TARGET_TYPE = "worker_integrity_hold"
HOLD_RECONCILE_KIND = "RISK_ACCEPTANCE"
MAX_HOLD_FINDINGS = 100


def _hold_row(session: Any) -> Any:
    from aeos.models.identity import SystemSetting

    return session.get(SystemSetting, HOLD_KEY)


def reconcile_command(hold_id: str) -> str:
    """The owner's reconciliation of exactly this hold. The domain is the owner's explicit choice (AGENTS.md
    invariant 1: never suggested or defaulted); the hold is host-wide, so either domain's decision lifts it."""
    return (f"aeos policy decide --kind {HOLD_RECONCILE_KIND} --target-type {HOLD_TARGET_TYPE} --target-id {hold_id} "
            "--as <SUPER_ADMIN> --domain <PERSONAL|OFFICE> --days 1 --rationale \"<what you checked and restored>\"")


def domain_view(hold: dict[str, Any], domain: str | None) -> dict[str, Any]:
    """The hold as a ``domain``-scoped view may show it (AGENTS.md invariant 9: another domain's run id, project
    and findings never reach this domain's views). The hold itself still applies to every domain (fail closed); only
    the details of findings raised by another domain's run/verification are replaced by their count."""
    if domain is None:
        return dict(hold)
    findings = list(hold.get("findings") or [])
    visible = [f for f in findings if f.get("domain") in (None, "", domain)]
    own_subject = hold.get("domain") in (None, "", domain)
    out = {**hold, "findings": visible, "other_domain_findings": len(findings) - len(visible)}
    if not own_subject:
        out.update(subject_id=None, project_id=None, subject_in_other_domain=True)
    return out


def raise_hold(session: Any, *, source: str, subject_id: str, findings: list[dict[str, Any]],
               project_id: str | None = None, domain: str | None = None) -> dict[str, Any]:
    """Record (or widen) the host-wide integrity hold after a controller/config/bytecode/DB tamper finding.
    Every new finding gets a NEW hold id, so a reconciliation recorded for an earlier hold never clears it.
    Does not commit. Worker admission (``sandbox.sandbox_readiness``) stays BLOCKED until the owner reconciles."""
    import uuid

    from aeos.audit import service as audit
    from aeos.core.redaction import redact
    from aeos.core.timeutil import utcnow
    from aeos.events import service as events
    from aeos.models.identity import SystemSetting

    row = _hold_row(session)
    previous = row.value if row is not None and isinstance(row.value, dict) else None
    carried: list[dict[str, Any]] = []
    if previous and hold_status(session, row=row) is not None:  # still unreconciled: keep its findings
        carried = list(previous.get("findings") or [])
    clean: list[dict[str, Any]] = redact([{**{k: str(v)[:300] for k, v in f.items()}, "source": source,
                                           "subject_id": subject_id, "domain": domain} for f in findings])
    hold_id = str(uuid.uuid4())
    value: dict[str, Any] = {"id": hold_id, "status": "ACTIVE", "detected_at": utcnow().isoformat(), "source": source,
                             "subject_id": subject_id, "domain": domain, "project_id": project_id,
                             "findings": [*carried, *clean][-MAX_HOLD_FINDINGS:],
                             "previous_id": (previous or {}).get("id")}
    if row is None:
        session.add(SystemSetting(key=HOLD_KEY, value=value))
    else:
        row.value = value
        row.version = (row.version or 1) + 1
    audit.record(session, actor_type="service", actor_id="worker_runtime.integrity", action="worker_runtime.integrity_hold",
                 result="DENIED", target_type="worker_integrity_hold", target_id=hold_id, project_id=project_id,
                 domain=domain, reason="controller/config/bytecode/DB tamper finding: worker admission held until the "
                 "owner reconciles (fail closed)", details={"source": source, "subject_id": subject_id,
                                                           "findings": clean[:50]})
    events.emit(session, source="worker_runtime", kind="worker_runtime.alert", entity_type="worker_integrity_hold",
                entity_id=hold_id, project_id=project_id, domain=domain,
                payload={"severity": "HIGH", "code": "INTEGRITY_HOLD", "source": source, "subject_id": subject_id,
                         "findings": len(clean)})
    session.flush()
    return value


def hold_status(session: Any, *, row: Any = _UNSET) -> dict[str, Any] | None:
    """The ACTIVE (unreconciled) integrity hold, or None."""
    row = _hold_row(session) if row is _UNSET else row
    hold = row.value if row is not None and isinstance(row.value, dict) else None
    if not hold or hold.get("status") != "ACTIVE" or not hold.get("id"):
        return None
    if _reconciled_by(session, hold) is not None:
        return None
    return hold


def _reconciled_by(session: Any, hold: dict[str, Any]) -> Any:
    """The human decision that reconciles exactly this hold (kind RISK_ACCEPTANCE, target worker_integrity_hold:
    <hold id>, recorded by a HUMAN after the hold was raised). An expired decision still counts (it was valid
    when recorded); an invalidated/superseded one does not."""
    from datetime import datetime

    from sqlalchemy import select

    from aeos.core.timeutil import ensure_aware
    from aeos.models.decisions import Decision

    try:
        detected = ensure_aware(datetime.fromisoformat(str(hold.get("detected_at"))))
    except ValueError:
        return None
    rows = session.scalars(select(Decision).where(
        Decision.kind == HOLD_RECONCILE_KIND, Decision.target_type == HOLD_TARGET_TYPE,
        Decision.target_id == str(hold["id"]), Decision.status.in_(("ACTIVE", "EXPIRED")))
        .order_by(Decision.created_at.desc()))
    for d in rows:
        created = ensure_aware(d.created_at)
        if d.actor_kind == "HUMAN" and created is not None and detected is not None and created >= detected:
            return d
    return None


def hold_view(session: Any, *, domain: str | None = None) -> dict[str, Any]:
    """Status of the integrity hold for status pages/CLI: ACTIVE (with findings + remedy) or CLEAR. With
    ``domain`` the details are scoped to that domain (``domain_view``)."""
    row = _hold_row(session)
    hold = row.value if row is not None and isinstance(row.value, dict) else None
    if not hold:
        return {"status": "CLEAR", "hold": None}
    active = hold_status(session, row=row)
    if active is None:
        d = _reconciled_by(session, hold)
        brief = domain_view({k: hold.get(k) for k in ("id", "detected_at", "source", "subject_id", "domain")}, domain)
        brief.pop("findings", None)
        brief.pop("other_domain_findings", None)
        return {"status": "CLEAR", "hold": brief, "reconciled_by_decision": d.id if d is not None else None}
    return {"status": "ACTIVE", "hold": domain_view(active, domain), "remedy": reconcile_command(str(active["id"]))}
