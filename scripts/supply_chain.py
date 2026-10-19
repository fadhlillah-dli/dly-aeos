"""Supply-chain reports (master §31 G1 "dependency/license/SBOM as needed", §45 security/test tools).

Offline and deterministic; nothing here contacts the network or installs anything.

* ``sbom``     CycloneDX 1.5 JSON SBOM of the pinned Python (requirements.lock) and frontend
               (frontend/package-lock.json) dependencies, plus the DLY AEOS component itself.
* ``licenses`` licence report of the Python distributions installed in this environment, from their
               metadata. No licence policy (allow-list) has been decided by the owner yet, so the report
               is REPORT_ONLY: UNKNOWN licences are listed, never guessed, and nothing passes or fails.

Vulnerability data is not offline: CI runs pip-audit / npm audit as report-only steps whose results
are uploaded as artifacts (NOT-PROVEN until the owner sets thresholds). See ADR-0095.

Usage: python scripts/supply_chain.py sbom [-o sbom.json] | licenses [-o licenses.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_components(lock: Path) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = _REQ.match(line.strip())
        if not m:
            continue
        name, version = _norm(m.group(1)), m.group(2)
        seen[f"{name}@{version}"] = {"type": "library", "name": name, "version": version,
                                     "purl": f"pkg:pypi/{name}@{version}", "bom-ref": f"pkg:pypi/{name}@{version}",
                                     "properties": [{"name": "aeos:ecosystem", "value": "pypi"},
                                                    {"name": "aeos:source", "value": "requirements.lock"}]}
    return [seen[k] for k in sorted(seen)]


def npm_components(lock: Path) -> list[dict[str, Any]]:
    data = json.loads(lock.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for path, pkg in (data.get("packages") or {}).items():
        if not path or not isinstance(pkg, dict) or not pkg.get("version"):
            continue
        name = pkg.get("name") or path.split("node_modules/")[-1]
        version = str(pkg["version"])
        purl_name = name.replace("@", "%40", 1) if name.startswith("@") else name
        ref = f"pkg:npm/{purl_name}@{version}"
        out[ref] = {"type": "library", "name": name, "version": version, "purl": ref, "bom-ref": ref,
                    "scope": "excluded" if pkg.get("dev") else "required",
                    "properties": [{"name": "aeos:ecosystem", "value": "npm"},
                                   {"name": "aeos:source", "value": "frontend/package-lock.json"}]}
    return [out[k] for k in sorted(out)]


def build_sbom(root: Path = ROOT) -> dict[str, Any]:
    init = (root / "backend" / "aeos" / "__init__.py").read_text(encoding="utf-8")
    m = _VERSION.search(init)
    version = m.group(1) if m else "UNKNOWN"
    components = python_components(root / "requirements.lock")
    npm_lock = root / "frontend" / "package-lock.json"
    if npm_lock.is_file():
        components += npm_components(npm_lock)
    body = json.dumps(components, sort_keys=True)
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"dly-aeos:{version}:{body}")
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{serial}", "version": 1,
        "metadata": {"component": {"type": "application", "name": "dly-aeos", "version": version,
                                   "bom-ref": "dly-aeos"},
                     "tools": {"components": [{"type": "application", "name": "aeos-supply-chain-script",
                                               "version": "1"}]},
                     "properties": [{"name": "aeos:scope",
                                     "value": "pinned lockfile dependencies (declared, not observed installs)"}]},
        "components": components,
    }


def _license_of(dist: Any) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return str(expr).strip()
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return " OR ".join(sorted(set(classifiers)))
    lic = (meta.get("License") or "").strip()
    if lic and len(lic) <= 80 and "\n" not in lic:
        return lic
    return "UNKNOWN"


def license_report() -> dict[str, Any]:
    from importlib import metadata

    items = []
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        items.append({"name": _norm(name), "version": dist.version, "license": _license_of(dist)})
    items.sort(key=lambda i: (i["name"], i["version"]))
    unknown = [i["name"] for i in items if i["license"] == "UNKNOWN"]
    return {"status": "REPORT_ONLY", "policy": None,
            "policy_note": "No licence allow-list has been decided by the owner; nothing passes or fails yet.",
            "count": len(items), "unknown": unknown, "items": items}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", choices=["sbom", "licenses"])
    ap.add_argument("-o", "--output", help="write JSON to this file (default: stdout)")
    args = ap.parse_args(argv)
    data = build_sbom() if args.report == "sbom" else license_report()
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"{args.report}: wrote {args.output}")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
