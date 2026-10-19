"""Known security advisories for a registry entry (§40 "Source verification ... known security advisories").

``check_upstream`` stores the published GitHub repository advisories (public, unauthenticated) under
``ResearchEntry.upstream["advisories"]`` with the date they were read. This module only interprets
that stored record: whether a pinned version falls inside a vulnerable range. A range or version that
cannot be parsed is UNKNOWN, never "not affected".
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


def summary(upstream: dict[str, Any] | None, pin: str | None) -> dict[str, Any]:
    """Interpretation of the stored advisory record for the pinned/candidate version."""
    rec = (upstream or {}).get("advisories")
    if not isinstance(rec, dict):
        return {"status": "NOT_CHECKED", "affected": [], "unknown": [], "count": 0, "checked_at": None,
                "note": "Advisory keamanan belum dicek (Check Upstream untuk repo GitHub)."}
    base = {"checked_at": rec.get("checked_at"), "source": rec.get("source")}
    if rec.get("status") != "OK":
        return base | {"status": "ERROR", "affected": [], "unknown": [], "count": 0,
                       "note": f"Advisory tidak terbaca ({rec.get('status')}); status UNKNOWN."}
    items = [i for i in rec.get("items") or [] if isinstance(i, dict) and not i.get("withdrawn_at")]
    affected: list[str] = []
    unknown: list[str] = []
    for item in items:
        gid = str(item.get("ghsa_id") or item.get("id") or "?")
        vulns = [x for x in item.get("vulnerabilities") or [] if isinstance(x, dict)]
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
    return base | {"status": status, "affected": affected, "unknown": unknown, "count": len(items),
                   "note": {"AFFECTED": "Versi kandidat berada dalam rentang rentan advisory yang dipublikasikan.",
                            "UNKNOWN": "Rentang/versi tidak dapat dievaluasi; dampak advisory UNKNOWN.",
                            "NOT_AFFECTED": "Advisory yang dipublikasikan tidak mencakup versi ini.",
                            "NONE": "Tidak ada advisory terpublikasi pada saat dicek."}[status]}
