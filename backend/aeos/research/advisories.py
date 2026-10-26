"""Known security advisories for a registry entry (§40 "Source verification ... known security advisories").

``check_upstream`` stores the published GitHub repository advisories (public, unauthenticated) under
``ResearchEntry.upstream["advisories"]`` with the date they were read. This module only interprets
that stored record: whether a pinned version falls inside a vulnerable range. A range or version that
cannot be parsed is UNKNOWN, never "not affected".

A repository can publish several packages with their own version lines (a monorepo: ``langgraph`` 1.x next to
``langgraph-checkpoint`` 4.x). The candidate's package is the one named in the pin (``langgraph==1.2.14``) or the
repository name. Advisories that only name other packages are skipped, but only when at least one advisory of the
repository names the candidate's package (so the naming scheme is known); otherwise every advisory counts.
"""

from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"\d+")
_CMP = re.compile(r"^\s*(>=|<=|>|<|=|==)?\s*v?([0-9][^\s,]*)\s*$")


def parse_version(raw: str | None) -> tuple[int, ...] | None:
    """``v1.4.0`` / ``1.4`` / ``1.4.0-rc1`` -> (1, 4, 0). Pre-release suffixes are ignored (conservative:
    ``1.4.0-rc1`` compares as ``1.4.0``). Anything else (a digest, a branch name) is None."""
    s = (raw or "").strip().lower()
    if s.startswith("v"):
        s = s[1:]
    if not s or not s[0].isdigit():
        return None
    core = re.split(r"[-+ ]", s, maxsplit=1)[0]
    parts = core.split(".")
    if not all(p.isdigit() for p in parts if p) or len(parts) > 4:
        return None
    nums = tuple(int(p) for p in parts if p)
    return nums + (0,) * (3 - len(nums)) if len(nums) < 3 else nums


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a2, b2 = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a2 > b2) - (a2 < b2)


def affects(vulnerable_range: str | None, version: str | None) -> bool | None:
    """True/False when the GitHub range (``>= 1.0, < 1.2.3`` / ``< 2.0`` / ``= 1.2.3``) can be evaluated
    for *version*; None (UNKNOWN) otherwise."""
    v = parse_version(version)
    if v is None or not (vulnerable_range or "").strip():
        return None
    for clause in str(vulnerable_range).split(","):
        m = _CMP.match(clause)
        if not m:
            return None
        op, bound_raw = m.group(1) or "=", m.group(2)
        bound = parse_version(bound_raw)
        if bound is None:
            return None
        c = _cmp(v, bound)
        ok = {">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0, "=": c == 0, "==": c == 0}[op]
        if not ok:
            return False
    return True


_PIN_PACKAGE = re.compile(r"^\s*([A-Za-z0-9_.\-/@]+?)\s*(?:==|@)\s*(v?[0-9][^\s]*)\s*$")


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _names(name: str) -> set[str]:
    """``github.com/org/tool`` -> {"github.com/org/tool", "tool"}; ``@scope/pkg`` -> {"@scope/pkg", "pkg"}."""
    n = _norm(name)
    return {n, n.rsplit("/", 1)[-1]}


def split_pin(pin: str | None) -> tuple[str | None, str | None]:
    """``langgraph==1.2.14`` / ``pkg@1.2.14`` -> ("langgraph", "1.2.14"); a bare version -> (None, version)."""
    m = _PIN_PACKAGE.match(pin or "")
    if m and not m.group(1)[0].isdigit():
        return m.group(1), m.group(2)
    return None, pin


def summary(upstream: dict[str, Any] | None, pin: str | None, package: str | None = None) -> dict[str, Any]:
    """Interpretation of the stored advisory record for the pinned/candidate version. *package* (e.g. the repository
    name) narrows a multi-package repository to the candidate's package; a package in the pin wins."""
    rec = (upstream or {}).get("advisories")
    if not isinstance(rec, dict):
        return {"status": "NOT_CHECKED", "affected": [], "unknown": [], "count": 0, "checked_at": None,
                "note": "Advisory keamanan belum dicek (Check Upstream untuk repo GitHub)."}
    base = {"checked_at": rec.get("checked_at"), "source": rec.get("source")}
    if rec.get("status") != "OK":
        return base | {"status": "ERROR", "affected": [], "unknown": [], "count": 0,
                       "note": f"Advisory tidak terbaca ({rec.get('status')}); status UNKNOWN."}
    items = [i for i in rec.get("items") or [] if isinstance(i, dict) and not i.get("withdrawn_at")]
    pin_package, pin = split_pin(pin)
    target = pin_package or package
    want = _names(target) if target else set()
    named = [str(v.get("package")) for i in items for v in i.get("vulnerabilities") or []
             if isinstance(v, dict) and v.get("package")]
    narrow = bool(want) and any(_names(n) & want for n in named)
    affected: list[str] = []
    unknown: list[str] = []
    skipped: list[str] = []
    for item in items:
        gid = str(item.get("ghsa_id") or item.get("id") or "?")
        vulns = [x for x in item.get("vulnerabilities") or [] if isinstance(x, dict)]
        if narrow:
            mine = [x for x in vulns if not x.get("package") or _names(str(x["package"])) & want]
            if vulns and not mine:
                skipped.append(gid)
                continue
            vulns = mine
        results = [affects(x.get("vulnerable_version_range"), pin) for x in vulns]
        if any(r is True for r in results):
            affected.append(gid)
        elif not results or any(r is None for r in results):
            unknown.append(gid)
    if affected:
        status = "AFFECTED"
    elif unknown:
        status = "UNKNOWN"
    elif items:
        status = "NOT_AFFECTED"
    else:
        status = "NONE"
    if narrow:
        base = base | {"package": sorted(want)[0] if len(want) == 1 else sorted(want), "other_packages_skipped": skipped}
    return base | {"status": status, "affected": affected, "unknown": unknown, "count": len(items) - len(skipped),
                   "note": {"AFFECTED": "Versi kandidat berada dalam rentang rentan advisory yang dipublikasikan.",
                            "UNKNOWN": "Rentang/versi tidak dapat dievaluasi; dampak advisory UNKNOWN.",
                            "NOT_AFFECTED": "Advisory yang dipublikasikan tidak mencakup versi ini.",
                            "NONE": "Tidak ada advisory terpublikasi pada saat dicek."}[status]}
