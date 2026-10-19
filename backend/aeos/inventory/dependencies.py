"""Dependency view and SBOM from the managed repository's lockfiles (§37 "Paket / image / dependency").

Core components stay in the short component list; the packages behind them are explorable here:

* ``requirements.lock`` (uv universal lock) -> ``name==version [; marker]`` with the ``# via`` parents,
  so direct vs transitive and the parent path are visible;
* ``frontend/package-lock.json`` -> every ``packages`` entry (name, version, dev, direct/transitive,
  nesting parent and the packages that require it);
* a CycloneDX 1.5 JSON SBOM generated from exactly those lockfiles (``aeos inventory sbom``), plus
  any CycloneDX files committed under ``sbom/`` (read as a declaration source).

A lockfile entry is a *declaration* of what a build links ("digunakan oleh build X", §38), never proof
of a running process. Container images are not managed by AEOS: that is reported as a coverage gap.
Reads are scoped to named files inside the repository (no machine scan) and bounded in size.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from aeos.core.hashing import sha256_text
from aeos.core.timeutil import utcnow
from aeos.inventory.scope import SourceRead, _read_text

PY_LOCK = "requirements.lock"
NPM_LOCK = "frontend/package-lock.json"
SBOM_DIR = "sbom"
PY_OWNER = "dly-aeos-control-plane"
NPM_OWNER = "control-center-frontend"
LOCK_OWNERS = {PY_OWNER: PY_LOCK, NPM_OWNER: NPM_LOCK}
IMAGES_GAP = ("Image container: coverage gap - AEOS tidak mengelola container runtime/registry; image yang mungkin "
              "dipakai tidak dipetakan.")
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;\\]+)\s*(?:;\s*(.*?))?\s*\\?\s*$")
_VIA_ONE = re.compile(r"^\s*#\s+via\s+(.+?)\s*$")
_VIA_ITEM = re.compile(r"^\s*#\s{2,}(.+?)\s*$")
ROOT_MARK = "(pyproject.toml)"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_lock(repo: Path) -> tuple[SourceRead, list[dict[str, Any]], str | None]:
    """(source, packages, sha256 of the file) from ``requirements.lock``."""
    text = _read_text(repo, PY_LOCK)
    if text is None:
        return SourceRead(PY_LOCK, "MISSING"), [], None
    packages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_via = False
    for line in text.splitlines():
        m = _PIN_RE.match(line)
        if m:
            current = {"name": _norm(m.group(1)), "version": m.group(3), "marker": m.group(4) or None, "via": [],
                       "ecosystem": "pypi"}
            packages.append(current)
            in_via = False
            continue
        if current is None:
            continue
        one = _VIA_ONE.match(line)
        if one and one.group(1).strip() not in ("", ":"):
            current["via"].append(one.group(1).strip())
            continue
        if re.match(r"^\s*#\s+via\s*$", line):
            in_via = True
            continue
        item = _VIA_ITEM.match(line)
        if in_via and item:
            current["via"].append(item.group(1).strip())
    for p in packages:
        direct = any(v.endswith(ROOT_MARK) for v in p["via"])
        p["direct"] = direct
        p["relation"] = "direct" if direct else "transitive"
        p["required_by"] = [_norm(v.split(" ")[0]) for v in p["via"] if not v.endswith(ROOT_MARK)]
        p["parent_path"] = ["dly-aeos"] if direct else (p["required_by"][:1] or [])
        p["purl"] = f"pkg:pypi/{p['name']}@{p['version']}"
    digest = "sha256:" + sha256_text(text)
    return SourceRead(PY_LOCK, "READ", len(packages), {"sha256": digest}), packages, digest


def npm_lock(repo: Path) -> tuple[SourceRead, list[dict[str, Any]], str | None]:
    """(source, packages, sha256) from ``frontend/package-lock.json`` (lockfileVersion 2/3 ``packages`` map)."""
    text = _read_text(repo, NPM_LOCK)
    if text is None:
        return SourceRead(NPM_LOCK, "MISSING"), [], None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return SourceRead(NPM_LOCK, "ERROR", detail={"error": str(exc)[:200]}), [], None
    pk = data.get("packages") or {}
    root = pk.get("") or {}
    direct = set(root.get("dependencies") or {}) | set(root.get("devDependencies") or {})
    required_by: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for path, meta in pk.items():
        if not path.startswith("node_modules/"):
            continue
        name = meta.get("name") or path.rsplit("node_modules/", 1)[-1]
        names[path] = name
        for dep in {**(meta.get("dependencies") or {}), **(meta.get("peerDependencies") or {})}:
            required_by.setdefault(dep, set()).add(name)
    packages: list[dict[str, Any]] = []
    for path, meta in pk.items():
        if not path.startswith("node_modules/"):
            continue
        name = names[path]
        nested = path.count("node_modules/") > 1
        parent = path.rsplit("/node_modules/", 1)[0].rsplit("node_modules/", 1)[-1] if nested else None
        is_direct = name in direct and not nested
        packages.append({
            "name": name, "version": meta.get("version"), "dev": bool(meta.get("dev")), "optional":
            bool(meta.get("optional")), "direct": is_direct, "relation": "direct" if is_direct else "transitive",
            "path": path, "parent_path": [parent] if parent else (["control-center-frontend"] if is_direct else []),
            "required_by": sorted(required_by.get(name, set()))[:20], "license": meta.get("license"),
            "ecosystem": "npm", "purl": f"pkg:npm/{name}@{meta.get('version')}",
        })
    packages.sort(key=lambda x: (not x["direct"], x["name"], x["path"]))
    digest = "sha256:" + sha256_text(text)
    return SourceRead(NPM_LOCK, "READ", len(packages), {"sha256": digest,
                                                         "lockfileVersion": data.get("lockfileVersion")}), \
        packages, digest


def read_sbom_files(repo: Path) -> tuple[list[SourceRead], list[dict[str, Any]]]:
    """CycloneDX JSON files committed under ``sbom/`` (declaration source kind 'sbom')."""
    reads: list[SourceRead] = []
    comps: list[dict[str, Any]] = []
    sbom_dir = repo / SBOM_DIR
    if not sbom_dir.is_dir():
        return [SourceRead(SBOM_DIR, "MISSING", detail={"gap": "belum ada SBOM CycloneDX di sbom/ (buat dengan "
                                                               "aeos inventory sbom --out sbom/aeos.cdx.json)"})], []
    for f in sorted(sbom_dir.glob("*.json")):
        text = _read_text(repo, f"{SBOM_DIR}/{f.name}")
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            reads.append(SourceRead(f"{SBOM_DIR}/{f.name}", "ERROR", detail={"error": str(exc)[:200]}))
            continue
        if data.get("bomFormat") != "CycloneDX":
            reads.append(SourceRead(f"{SBOM_DIR}/{f.name}", "ERROR", detail={"error": "bukan CycloneDX"}))
            continue
        items = [c for c in data.get("components") or [] if isinstance(c, dict)]
        comps += [{"name": c.get("name"), "version": c.get("version"), "purl": c.get("purl"),
                   "source": f"{SBOM_DIR}/{f.name}"} for c in items]
        reads.append(SourceRead(f"{SBOM_DIR}/{f.name}", "READ", len(items),
                                {"specVersion": data.get("specVersion"), "sha256": "sha256:" + sha256_text(text)}))
    return reads, comps


def owner_packages(repo: Path, component_id: str) -> tuple[SourceRead | None, list[dict[str, Any]], str | None]:
    if component_id == PY_OWNER:
        return python_lock(repo)
    if component_id == NPM_OWNER:
        return npm_lock(repo)
    return None, [], None


def dependency_page(repo: Path, component_id: str, *, page: int = 1, page_size: int = 100, q: str | None = None,
                    relation: str | None = None, observed_sha: str | None = None, build_name: str | None = None,
                    observed_at: str | None = None) -> dict[str, Any]:
    source, packages, digest = owner_packages(repo, component_id)
    if source is None:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size, "lockfile": None,
                "status": "NOT_APPLICABLE", "reason": "komponen ini tidak memiliki lockfile di managed scope",
                "coverage": {"images": IMAGES_GAP}, "source": "lockfile", "checked_at": utcnow().isoformat(),
                "mode": "LIVE"}
    needle = (q or "").strip().lower()
    rows = [p for p in packages if (not needle or needle in p["name"].lower())
            and (relation is None or p["relation"] == relation)]
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    window = rows[(page - 1) * page_size:(page - 1) * page_size + page_size]
    label = f"digunakan oleh build {build_name or component_id}"
    for p in window:
        p["used_by_build"] = {"component_id": component_id, "lockfile": source.source, "label": label}
    matches = None if observed_sha is None or digest is None else observed_sha == digest
    return {
        "items": window, "total": len(rows), "page": page, "page_size": page_size,
        "lockfile": {"path": source.source, "status": source.status, "sha256": digest,
                     "observed_sha256": observed_sha, "observed_at": observed_at,
                     "matches_last_reconcile": matches},
        "counts": {"direct": sum(1 for p in packages if p["direct"]),
                   "transitive": sum(1 for p in packages if not p["direct"]), "total": len(packages)},
        "status": "READ" if source.status == "READ" else source.status,
        "reason": None if source.status == "READ" else f"{source.source} {source.status}",
        "note": "Entri lockfile adalah deklarasi build (bukan proses aktif); relasi transitif dari lockfile.",
        "coverage": {"images": IMAGES_GAP,
                     "lockfile_changed_since_reconcile": matches is False},
        "source": source.source, "checked_at": utcnow().isoformat(), "mode": "LIVE",
    }


def cyclonedx(repo: Path, *, version: str | None = None) -> dict[str, Any]:
    """CycloneDX 1.5 JSON SBOM of the platform's two lockfiles (deterministic serial per content)."""
    py_src, py_pkgs, py_sha = python_lock(repo)
    npm_src, npm_pkgs, npm_sha = npm_lock(repo)
    components = []
    for p in py_pkgs:
        components.append({"type": "library", "bom-ref": p["purl"], "name": p["name"], "version": p["version"],
                           "purl": p["purl"], "scope": "required",
                           "properties": [{"name": "aeos:relation", "value": p["relation"]},
                                          {"name": "aeos:lockfile", "value": PY_LOCK},
                                          {"name": "aeos:used_by_build", "value": PY_OWNER}]
                           + ([{"name": "aeos:marker", "value": p["marker"]}] if p.get("marker") else [])})
    for p in npm_pkgs:
        ref = f"{p['purl']}#{p['path']}"
        components.append({"type": "library", "bom-ref": ref, "name": p["name"], "version": p["version"],
                           "purl": p["purl"], "scope": "optional" if p["dev"] else "required",
                           "properties": [{"name": "aeos:relation", "value": p["relation"]},
                                          {"name": "aeos:lockfile", "value": NPM_LOCK},
                                          {"name": "aeos:used_by_build", "value": NPM_OWNER}]})
    seed = f"{py_sha}|{npm_sha}"
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'dly-aeos-sbom:' + seed)}", "version": 1,
        "metadata": {
            "timestamp": utcnow().replace(microsecond=0).isoformat(),
            "tools": {"components": [{"type": "application", "name": "aeos inventory sbom", "version": version or "1"}]},
            "component": {"type": "application", "name": "dly-aeos", "bom-ref": "dly-aeos"},
            "properties": [{"name": "aeos:source", "value": f"{PY_LOCK} ({py_src.status}), {NPM_LOCK} "
                                                             f"({npm_src.status})"},
                           {"name": "aeos:coverage_gap", "value": IMAGES_GAP},
                           {"name": "aeos:lockfile_sha256", "value": seed}],
        },
        "components": components,
    }
