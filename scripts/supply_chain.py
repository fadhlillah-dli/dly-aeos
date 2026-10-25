"""Supply-chain reports and their policy evaluation (master §31 G1 "dependency/license/SBOM as needed",
§45 security/test tools, §48 CI controls).

Offline and deterministic; nothing here contacts the network or installs anything.

* ``sbom``     CycloneDX 1.5 JSON SBOM of the pinned Python (requirements.lock) and frontend
               (frontend/package-lock.json) dependencies, plus the DLY AEOS component itself.
* ``licenses`` licence report of the Python distributions installed in this environment (their metadata)
               and of the frontend lockfile packages (the ``license`` field npm records). UNKNOWN licences
               are listed, never guessed. With ``--policy`` the report is evaluated against the owner's
               licence allow-list.
* ``evaluate`` evaluates the scanner reports CI produced (pip-audit / npm audit / bandit JSON, the licence
               report) against ``policy/supply-chain.yaml``.

The policy file holds OWNER decisions only. While it says ``status: UNDECIDED`` (or a value is null) the
evaluation is REPORT_ONLY: findings and failed/missing scanner reports are listed, exit code 0, the CI job
stays [NO-GATE]. Once the owner records values (status DECIDED) a violation, an UNKNOWN licence, a finding
of unknown severity or a missing/unreadable scanner report FAILS (exit 1); a malformed decided policy is
exit 2 (fail closed). No threshold or allow-list is ever defaulted here. See ADR-0095.

Usage: python scripts/supply_chain.py sbom [-o sbom.json]
       python scripts/supply_chain.py licenses [--policy policy/supply-chain.yaml] [-o licenses.json]
       python scripts/supply_chain.py evaluate --policy policy/supply-chain.yaml [--licenses licenses.json]
              [--pip-audit pip-audit.json] [--npm-audit npm-audit.json] [--bandit bandit.json] [-o out.json]
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
POLICY_REL = "policy/supply-chain.yaml"
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
THRESHOLDS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")
SCANNERS = {"python": ("vulnerabilities", "python"), "npm": ("vulnerabilities", "npm"), "bandit": ("sast", "bandit")}


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


def npm_licenses(lock: Path) -> list[dict[str, Any]]:
    """Licences npm recorded in the lockfile per package (``license`` field); UNKNOWN when absent, never guessed."""
    data = json.loads(lock.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for path, pkg in (data.get("packages") or {}).items():
        if not path or not isinstance(pkg, dict) or not pkg.get("version"):
            continue
        name = pkg.get("name") or path.split("node_modules/")[-1]
        lic = pkg.get("license")
        out[(name, str(pkg["version"]))] = {"name": name, "version": str(pkg["version"]), "ecosystem": "npm",
                                            "license": str(lic).strip() if isinstance(lic, str) and lic.strip()
                                            else "UNKNOWN"}
    return [out[k] for k in sorted(out)]


def license_report(policy: dict[str, Any] | None = None, root: Path = ROOT) -> dict[str, Any]:
    from importlib import metadata

    items = []
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        items.append({"name": _norm(name), "version": dist.version, "ecosystem": "pypi", "license": _license_of(dist)})
    npm_lock = root / "frontend" / "package-lock.json"
    if npm_lock.is_file():
        items += npm_licenses(npm_lock)
    items.sort(key=lambda i: (i["ecosystem"], i["name"], i["version"]))
    unknown = [i["name"] for i in items if i["license"] == "UNKNOWN"]
    report: dict[str, Any] = {"status": "REPORT_ONLY", "policy": None,
                              "policy_note": "No licence allow-list has been decided by the owner; nothing passes or "
                                             "fails yet.", "count": len(items), "unknown": unknown, "items": items}
    if policy is not None:
        section = evaluate_licenses(report, policy)
        report.update(status=section["status"], policy={"path": policy.get("path"), "status": policy["status"]},
                      policy_note=section["note"], violations=section["violations"])
    return report


# ----------------------------------------------------------------------------------------- policy evaluation
def load_policy(path: Path) -> dict[str, Any]:
    """Read the owner's supply-chain policy. Returns ``{"status", "errors", "licenses_allow", "thresholds",
    "min_confidence", "path"}``; nothing is defaulted - an undecided value stays None."""
    import yaml

    out: dict[str, Any] = {"path": str(path), "status": "UNDECIDED", "errors": [], "licenses_allow": None,
                           "thresholds": {k: None for k in SCANNERS}, "min_confidence": None, "decision_ref": None}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        out["errors"].append(f"policy file unreadable: {type(exc).__name__}")
        return out
    if not isinstance(raw, dict):
        out["errors"].append("policy file is not a mapping")
        return out
    status = str(raw.get("status") or "UNDECIDED").upper()
    if status not in ("UNDECIDED", "DECIDED"):
        out["errors"].append(f"status must be UNDECIDED or DECIDED, not {status!r}")
    out["status"] = status if status in ("UNDECIDED", "DECIDED") else "INVALID"
    out["decision_ref"] = raw.get("decision_ref")
    allow = (raw.get("licenses") or {}).get("allow")
    if allow is not None:
        if not isinstance(allow, list) or not allow or not all(isinstance(a, str) and a.strip() for a in allow):
            out["errors"].append("licenses.allow must be a non-empty list of licence identifiers (or null)")
        else:
            out["licenses_allow"] = sorted({a.strip() for a in allow})
    for scanner, (group, key) in SCANNERS.items():
        value = ((raw.get(group) or {}).get(key) or {}).get("fail_at_or_above")
        if value is None:
            continue
        if str(value).upper() not in THRESHOLDS:
            out["errors"].append(f"{group}.{key}.fail_at_or_above must be one of {THRESHOLDS} (or null)")
        else:
            out["thresholds"][scanner] = str(value).upper()
    conf = ((raw.get("sast") or {}).get("bandit") or {}).get("min_confidence")
    if conf is not None:
        if str(conf).upper() not in CONFIDENCES:
            out["errors"].append(f"sast.bandit.min_confidence must be one of {CONFIDENCES} (or null)")
        else:
            out["min_confidence"] = str(conf).upper()
    if out["status"] == "DECIDED" and not out["decision_ref"]:
        out["errors"].append("a DECIDED policy names its decision_ref (the ADR / Decision that recorded it)")
    return out


def _enforced(policy: dict[str, Any], value: Any) -> bool:
    return policy.get("status") == "DECIDED" and value is not None


_LIC_TOKENS = re.compile(r"(\(|\)|\bAND\b|\bOR\b)")


def licence_allowed(expression: str, allow: list[str]) -> bool:
    """Exact (case-insensitive) evaluation of an SPDX-style expression against the allow-list: ``A OR B`` passes when
    either is allowed, ``A AND B`` only when both are; ``X WITH Y`` must be listed as a whole. UNKNOWN or a malformed
    expression is never allowed (no guessing, no normalising of free-text names)."""
    allowed = {a.casefold() for a in allow}
    tokens = [t.strip() for t in _LIC_TOKENS.split(expression or "") if t and t.strip()]
    pos = 0

    def leaf() -> bool:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("unexpected end")
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            value = disjunction()
            if pos >= len(tokens) or tokens[pos] != ")":
                raise ValueError("unbalanced")
            pos += 1
            return value
        if tok in (")", "AND", "OR"):
            raise ValueError("operator without operand")
        return tok.casefold() != "unknown" and tok.casefold() in allowed

    def conjunction() -> bool:
        nonlocal pos
        value = leaf()
        while pos < len(tokens) and tokens[pos] == "AND":
            pos += 1
            value = leaf() and value
        return value

    def disjunction() -> bool:
        nonlocal pos
        value = conjunction()
        while pos < len(tokens) and tokens[pos] == "OR":
            pos += 1
            value = conjunction() or value
        return value

    try:
        result = disjunction()
    except ValueError:
        return False
    return result if pos == len(tokens) else False


def evaluate_licenses(report: dict[str, Any] | None, policy: dict[str, Any]) -> dict[str, Any]:
    allow = policy.get("licenses_allow")
    if report is None:
        status = "FAIL" if _enforced(policy, allow) else "REPORT_ONLY"
        return {"status": status, "violations": [], "note": "licence report missing (never counted as compliant)",
                "report": "MISSING"}
    if not _enforced(policy, allow):
        return {"status": "REPORT_ONLY", "violations": [], "report": "PRESENT",
                "note": "No licence allow-list decided by the owner (policy UNDECIDED); nothing passes or fails yet.",
                "unknown": list(report.get("unknown") or [])}
    violations = [{"name": i["name"], "version": i.get("version"), "ecosystem": i.get("ecosystem"),
                   "license": i["license"], "reason": "UNKNOWN licence" if i["license"] == "UNKNOWN"
                   else "not in licenses.allow"}
                  for i in report.get("items") or [] if not licence_allowed(i["license"], allow)]
    return {"status": "FAIL" if violations else "PASS", "violations": violations, "report": "PRESENT",
            "note": f"{len(violations)} licence(s) outside the owner allow-list or UNKNOWN"}


def _norm_severity(value: Any) -> str:
    s = str(value or "").strip().upper()
    s = {"MODERATE": "MEDIUM", "UNDEFINED": "UNKNOWN", "": "UNKNOWN"}.get(s, s)
    return s if s in SEVERITIES else "UNKNOWN"


def parse_pip_audit(data: Any) -> list[dict[str, Any]]:
    """pip-audit ``-f json``: dependencies[].vulns[]. pip-audit 2.7.3 reports no severity: UNKNOWN, never guessed."""
    if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        raise ValueError("not a pip-audit JSON report")
    out = []
    for dep in data["dependencies"]:
        for v in (dep or {}).get("vulns") or []:
            out.append({"package": dep.get("name"), "version": dep.get("version"), "id": v.get("id"),
                        "severity": _norm_severity(v.get("severity")), "fix_versions": v.get("fix_versions") or []})
    return out


def parse_npm_audit(data: Any) -> list[dict[str, Any]]:
    """``npm audit --json`` (report v2): vulnerabilities{name: {severity, via}}; severity moderate -> MEDIUM."""
    if not isinstance(data, dict) or not isinstance(data.get("vulnerabilities"), dict):
        raise ValueError("not an npm audit JSON report")
    return [{"package": name, "id": None, "severity": _norm_severity((v or {}).get("severity")),
             "range": (v or {}).get("range")} for name, v in sorted(data["vulnerabilities"].items())]


def parse_bandit(data: Any) -> list[dict[str, Any]]:
    """bandit ``-f json``: results[] with issue_severity / issue_confidence (UNDEFINED -> UNKNOWN)."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("not a bandit JSON report")
    return [{"id": r.get("test_id"), "file": r.get("filename"), "line": r.get("line_number"),
             "severity": _norm_severity(r.get("issue_severity")),
             "confidence": str(r.get("issue_confidence") or "UNKNOWN").upper()} for r in data["results"]]


PARSERS = {"python": parse_pip_audit, "npm": parse_npm_audit, "bandit": parse_bandit}


def evaluate_scanner(scanner: str, data: Any, policy: dict[str, Any], *, present: bool) -> dict[str, Any]:
    threshold = policy["thresholds"].get(scanner)
    enforced = _enforced(policy, threshold)
    base: dict[str, Any] = {"scanner": scanner, "threshold": threshold, "enforced": enforced}
    if not present:
        return {**base, "report": "MISSING", "findings": [], "violations": [],
                "status": "FAIL" if enforced else "REPORT_ONLY",
                "note": "scanner report missing (the scan did not run or crashed) - never counted as 'no findings'"}
    try:
        findings = PARSERS[scanner](data)
    except ValueError as exc:
        return {**base, "report": "UNREADABLE", "findings": [], "violations": [],
                "status": "FAIL" if enforced else "REPORT_ONLY", "note": f"scanner report unreadable: {exc}"}
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    if not enforced:
        return {**base, "report": "PRESENT", "findings": findings, "counts": counts, "violations": [],
                "status": "REPORT_ONLY", "note": "no threshold decided by the owner (policy UNDECIDED)"}
    floor = SEVERITIES.index(str(threshold))
    min_conf = policy.get("min_confidence") if scanner == "bandit" else None
    violations = []
    for f in findings:
        if min_conf and f.get("confidence") in CONFIDENCES and \
                CONFIDENCES.index(f["confidence"]) < CONFIDENCES.index(min_conf):
            continue
        if f["severity"] == "UNKNOWN" or SEVERITIES.index(f["severity"]) >= floor:
            violations.append(f)  # an unknown severity cannot be shown to be below the threshold: fail closed
    return {**base, "report": "PRESENT", "findings": findings, "counts": counts, "violations": violations,
            "status": "FAIL" if violations else "PASS",
            "note": f"{len(violations)} finding(s) at or above {threshold} (or of unknown severity)"}


def _read_json(path: str | None) -> tuple[bool, Any]:
    if not path:
        return False, None
    p = Path(path)
    if not p.is_file():
        return False, None
    try:
        return True, json.loads(p.read_text(encoding="utf-8", errors="replace") or "null")
    except json.JSONDecodeError:
        return True, "<unreadable>"


def evaluate(policy: dict[str, Any], *, licenses: str | None = None, pip_audit: str | None = None,
             npm_audit: str | None = None, bandit: str | None = None) -> dict[str, Any]:
    """The whole supply-chain evaluation. ``mode`` is REPORT_ONLY until the owner decided at least one value."""
    sections: dict[str, Any] = {}
    present, lic = _read_json(licenses)
    sections["licenses"] = evaluate_licenses(lic if present and isinstance(lic, dict) else None, policy)
    for scanner, path in (("python", pip_audit), ("npm", npm_audit), ("bandit", bandit)):
        ok, data = _read_json(path)
        sections[scanner] = evaluate_scanner(scanner, data, policy, present=ok)
    statuses = [s["status"] for s in sections.values()]
    decided = policy.get("status") == "DECIDED" and any(s != "REPORT_ONLY" for s in statuses)
    if policy.get("errors") and policy.get("status") != "UNDECIDED":
        overall = "POLICY_INVALID"
    elif not decided:
        overall = "REPORT_ONLY"
    else:
        overall = "FAIL" if "FAIL" in statuses else "PASS"
    return {"mode": "ENFORCED" if decided else "REPORT_ONLY", "overall": overall,
            "policy": {k: policy.get(k) for k in ("path", "status", "decision_ref", "errors", "licenses_allow",
                                                  "thresholds", "min_confidence")},
            "sections": sections,
            "note": None if decided else "policy UNDECIDED: reports are listed, nothing passes or fails (owner "
                                         "decision pending, ADR-0095)"}


def summary_lines(result: dict[str, Any]) -> list[str]:
    lines = [f"Supply-chain policy: mode={result['mode']} overall={result['overall']} "
             f"(policy {result['policy']['status']})"]
    for name, s in result["sections"].items():
        counts = s.get("counts")
        lines.append(f"- {name}: {s['status']} report={s.get('report')} "
                     f"violations={len(s.get('violations') or [])}" + (f" counts={counts}" if counts else "")
                     + (f" note={s['note']}" if s.get("note") else ""))
    for err in result["policy"].get("errors") or []:
        lines.append(f"- policy error: {err}")
    return lines


def _write(text: str, output: str | None, label: str) -> None:
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
        print(f"{label}: wrote {output}")
    else:
        sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", choices=["sbom", "licenses", "evaluate"])
    ap.add_argument("-o", "--output", help="write JSON to this file (default: stdout)")
    ap.add_argument("--policy", help=f"owner policy file (e.g. {POLICY_REL})")
    ap.add_argument("--licenses", help="licence report JSON (evaluate)")
    ap.add_argument("--pip-audit", dest="pip_audit", help="pip-audit -f json report (evaluate)")
    ap.add_argument("--npm-audit", dest="npm_audit", help="npm audit --json report (evaluate)")
    ap.add_argument("--bandit", help="bandit -f json report (evaluate)")
    args = ap.parse_args(argv)
    policy = load_policy(Path(args.policy)) if args.policy else None
    if args.report == "sbom":
        _write(json.dumps(build_sbom(), indent=2, sort_keys=True, ensure_ascii=True), args.output, "sbom")
        return 0
    if args.report == "licenses":
        rep = license_report(policy)
        _write(json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=True), args.output, "licenses")
        if policy is not None and policy["errors"] and policy["status"] != "UNDECIDED":
            return 2
        return 1 if rep["status"] == "FAIL" else 0
    if policy is None:
        ap.error("evaluate needs --policy")
    result = evaluate(policy, licenses=args.licenses, pip_audit=args.pip_audit, npm_audit=args.npm_audit,
                      bandit=args.bandit)
    _write(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True), args.output, "evaluate")
    print("\n".join(summary_lines(result)))
    return {"POLICY_INVALID": 2, "FAIL": 1}.get(result["overall"], 0)


if __name__ == "__main__":
    raise SystemExit(main())
