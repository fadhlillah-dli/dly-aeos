"""Dependency diff of a candidate (§39 "Metadata fetch, dependency diff, budget hitung dan probe memakai kode").

For a GitHub-hosted entry the owner names two refs (the baseline pin and the candidate pin). Code fetches
the well-known manifests and lockfiles at both refs through the public contents API (only the repo path
and the refs leave the machine, no credentials), parses them and stores a dated diff in
``ResearchEntry.upstream["dependency_diff"]``. Nothing is installed or resolved; a file that cannot be
read or parsed is reported as such (never as "no change"). Licenses of new packages are not in these
files and stay UNKNOWN.
"""

from __future__ import annotations

import base64
import json
import re
import tomllib
from typing import Any

import httpx
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.research import ResearchEntry
from aeos.research.identity import github_repo

GITHUB_API = "https://api.github.com"
# Direct manifests and lockfiles (lockfiles carry the transitive set).
MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml")
LOCKFILES = ("package-lock.json", "uv.lock", "poetry.lock", "Cargo.lock", "go.sum")
MAX_BYTES = 1_000_000
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,127}$")
_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _req_name(spec: str) -> tuple[str, str] | None:
    m = _PEP508_NAME.match(spec or "")
    if not m:
        return None
    name = m.group(1).lower().replace("_", "-")
    return name, spec[m.end():].strip()


def parse_file(path: str, text: str) -> dict[str, str]:
    """Package name -> version/spec for one manifest or lockfile. Raises ValueError when unparseable."""
    out: dict[str, str] = {}
    base = path.rsplit("/", 1)[-1]
    if base == "package.json":
        data = json.loads(text)
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for name, spec in (data.get(section) or {}).items():
                out[str(name)] = f"{spec}" if section == "dependencies" else f"{section}:{spec}"
    elif base == "package-lock.json":
        data = json.loads(text)
        for key, meta in (data.get("packages") or {}).items():
            if not key or not isinstance(meta, dict):
                continue
            name = key.split("node_modules/")[-1]
            out[name] = str(meta.get("version") or "?")
        for name, meta in (data.get("dependencies") or {}).items():  # lockfileVersion 1
            if isinstance(meta, dict):
                out.setdefault(str(name), str(meta.get("version") or "?"))
    elif base == "requirements.txt":
        for line in text.splitlines():
            s = line.split("#", 1)[0].strip()
            if not s or s.startswith("-"):
                continue
            parsed = _req_name(s)
            if parsed:
                out[parsed[0]] = parsed[1]
    elif base == "pyproject.toml":
        data = tomllib.loads(text)
        for s in (data.get("project") or {}).get("dependencies") or []:
            parsed = _req_name(str(s))
            if parsed:
                out[parsed[0]] = parsed[1]
        for group, deps in ((data.get("project") or {}).get("optional-dependencies") or {}).items():
            for s in deps or []:
                parsed = _req_name(str(s))
                if parsed:
                    out.setdefault(parsed[0], f"[{group}] {parsed[1]}")
        for name, spec in ((data.get("tool") or {}).get("poetry") or {}).get("dependencies", {}).items():
            out.setdefault(str(name).lower(), json.dumps(spec) if not isinstance(spec, str) else spec)
    elif base in ("uv.lock", "poetry.lock", "Cargo.lock"):
        data = tomllib.loads(text)
        for pkg in data.get("package") or []:
            if isinstance(pkg, dict) and pkg.get("name"):
                out[str(pkg["name"]).lower()] = str(pkg.get("version") or "?")
    elif base == "Cargo.toml":
        data = tomllib.loads(text)
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            for name, spec in (data.get(section) or {}).items():
                ver = spec if isinstance(spec, str) else str((spec or {}).get("version") or json.dumps(spec))
                out[str(name)] = ver if section == "dependencies" else f"{section}:{ver}"
    elif base == "go.mod":
        block = False
        for line in text.splitlines():
            s = line.split("//", 1)[0].strip()
            if s.startswith("require ("):
                block = True
                continue
            if block and s == ")":
                block = False
                continue
            if s.startswith("require "):
                s = s[len("require "):].strip()
            elif not block:
                continue
            parts = s.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    elif base == "go.sum":
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1].removesuffix("/go.mod")
    else:
        raise ValueError(f"unsupported file {path}")
    return out


def diff(before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = [{"name": n, "from": before[n], "to": after[n]} for n in sorted(set(before) & set(after))
               if before[n] != after[n]]
    return {"added": [{"name": n, "to": after[n]} for n in added], "removed": [{"name": n, "from": before[n]} for n in removed],
            "changed": changed}


def _fetch(client: httpx.Client, owner: str, repo: str, path: str, ref: str) -> tuple[str, str | None]:
    """("OK", text) | ("ABSENT", None) | ("TOO_LARGE"/"ERROR"/"RATE_LIMITED", None)."""
    resp = client.get(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
    if resp.status_code == 404:
        return "ABSENT", None
    if resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"):
        return "RATE_LIMITED", None
    if resp.status_code != 200:
        return "ERROR", None
    try:
        data = resp.json()
    except ValueError:
        return "ERROR", None
    if not isinstance(data, dict) or data.get("type") not in (None, "file"):
        return "ERROR", None
    if int(data.get("size") or 0) > MAX_BYTES or data.get("encoding") != "base64":
        return "TOO_LARGE", None
    try:
        return "OK", base64.b64decode(str(data.get("content") or "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return "ERROR", None


def _client() -> httpx.Client:
    from aeos import __version__

    return httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
                        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                 "User-Agent": f"DLY-AEOS-research/{__version__}"})


def dependency_diff(session: Session, entry: ResearchEntry, *, base_ref: str, head_ref: str, actor_type: str,
                    actor_id: str, domain: str | None, client: httpx.Client | None = None) -> dict[str, Any]:
    if entry.submitted_domain == Domain.OFFICE.value or domain == Domain.OFFICE.value:
        raise PolicyDenied("OFFICE context: public upstream lookup is not authorised", code="OFFICE_RESEARCH_DENIED")
    gh = github_repo(entry.identity or "")
    if gh is None or not entry.urls:
        raise Blocked("dependency diff supports GitHub-hosted entries with a canonical URL only",
                      code="UPSTREAM_CHECK_NOT_SUPPORTED")
    for label, ref in (("base_ref", base_ref), ("head_ref", head_ref)):
        if not _REF.match(ref or "") or ".." in ref:
            raise ValidationFailed(f"{label} must be a tag/branch/commit name (no '..', max 128 characters)")
    owner, repo = gh
    own = client is None
    http = client or _client()
    manifests: list[dict[str, Any]] = []
    problems = 0
    try:
        for path in (*MANIFESTS, *LOCKFILES):
            try:
                sa, ta = _fetch(http, owner, repo, path, base_ref)
                sb, tb = _fetch(http, owner, repo, path, head_ref)
            except httpx.HTTPError as exc:
                manifests.append({"path": path, "status": "ERROR", "reason": redact_text(type(exc).__name__)})
                problems += 1
                continue
            if sa == "ABSENT" and sb == "ABSENT":
                continue
            if sa not in ("OK", "ABSENT") or sb not in ("OK", "ABSENT"):
                manifests.append({"path": path, "status": "UNREADABLE", "base": sa, "head": sb})
                problems += 1
                continue
            try:
                before = parse_file(path, ta) if ta is not None else {}
                after = parse_file(path, tb) if tb is not None else {}
            except (ValueError, tomllib.TOMLDecodeError) as exc:
                manifests.append({"path": path, "status": "UNPARSEABLE", "reason": redact_text(str(exc))[:200]})
                problems += 1
                continue
            d = diff(before, after)
            status = ("ADDED" if sa == "ABSENT" else "REMOVED" if sb == "ABSENT" else
                      "CHANGED" if (d["added"] or d["removed"] or d["changed"]) else "UNCHANGED")
            manifests.append({"path": path, "status": status, "lockfile": path in LOCKFILES,
                              "counts": {k: len(v) for k, v in d.items()},
                              **{k: v[:200] for k, v in d.items()}})
    finally:
        if own:
            http.close()
    now = utcnow().isoformat()
    result = {
        "base": base_ref, "head": head_ref, "checked_at": now,
        "source": "GitHub contents API (public, unauthenticated)",
        "status": "OK" if manifests and not problems else ("PARTIAL" if manifests else "NO_MANIFEST"),
        "manifests": manifests,
        "transitive": ("dari lockfile" if any(m.get("lockfile") and m["status"] in ("CHANGED", "UNCHANGED", "ADDED")
                                              for m in manifests) else "UNKNOWN - tidak ada lockfile terbaca"),
        "licenses": "UNKNOWN - lisensi paket baru tidak tercantum pada manifest/lockfile",
        "note": "Diff dihitung oleh kode dari metadata publik; tidak ada instalasi atau resolusi dependency.",
    }
    entry.upstream = {**(entry.upstream or {}), "dependency_diff": result}
    entry.history = [*(entry.history or []), {"at": now, "event": "DEPENDENCY_DIFF", "status": result["status"],
                                               "from": base_ref, "to": head_ref}]
    session.flush()
    audit.record(session, actor_type=actor_type, actor_id=actor_id, action="research.dependency.diff",
                 target_type="research_entry", target_id=entry.id, domain=domain,
                 details={"identity": entry.identity, "base": base_ref, "head": head_ref, "status": result["status"],
                          "installed": False})
    events.emit(session, source="research", kind="research.dependency.diffed", entity_type="research_entry",
                entity_id=entry.id, domain=entry.submitted_domain, payload={"status": result["status"],
                                                                             "base": base_ref, "head": head_ref})
    return result
