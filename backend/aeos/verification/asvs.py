"""OWASP ASVS trace of the control plane (master §46 "requirements selected per application/risk and version-pinned;
evidence = a test and trace per relevant control"; review W6-130).

Read-only projection of two reviewed contract files: the complete ASVS 4.0.3 catalog
(``contracts/security/asvs-4.0.3-catalog.yaml``) and the control plane's selection
(``contracts/security/asvs-selection.yaml``). ``tests/unit/test_asvs_trace.py`` keeps the selection complete (every
requirement selected or excluded with a reason) and every cited test id existing.

Evidence honesty: ``TESTED`` means the trace names existing tests that cover the control - whether they pass on the
current commit is CI gate evidence (G1/G2), not something this view claims; ``NOT_PROVEN`` is never a pass. The
selection is for the control plane only: applications AEOS builds have no ASVS level until the owner decides one per
data classification/risk (``per_project`` stays UNDECIDED; no level is invented here).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aeos.core.hashing import file_sha256
from aeos.core.paths import repo_root

SELECTION_REL = "contracts/security/asvs-selection.yaml"
CATALOG_REL = "contracts/security/asvs-4.0.3-catalog.yaml"
STATUSES = ("TESTED", "NOT_PROVEN", "NOT_SELECTED", "DELETED")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def trace(root: Path | None = None) -> dict[str, Any]:
    """Every catalog requirement with its disposition, per-chapter counts and the file identities."""
    base = root or repo_root()
    sel_path, cat_path = base / SELECTION_REL, base / CATALOG_REL
    selection, catalog = _load(sel_path), _load(cat_path)
    deleted: dict[str, str] = dict(catalog.get("deleted") or {})
    selected = {str(c["id"]): c for c in selection.get("selected") or [] if isinstance(c, dict) and c.get("id")}
    excluded: dict[str, str] = {}
    for entry in selection.get("not_selected") or []:
        for rid in [*(entry.get("ids") or []), *([entry["id"]] if entry.get("id") else [])]:
            excluded[str(rid)] = str(entry.get("reason") or "")
    items: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    for chapter, body in (catalog.get("chapters") or {}).items():
        counts = dict.fromkeys(STATUSES, 0) | {"UNLISTED": 0}
        for rid, title in (body.get("requirements") or {}).items():
            item: dict[str, Any] = {"id": rid, "chapter": chapter, "title": str(title)}
            if rid in deleted:
                item.update(status="DELETED", reason=f"deleted in ASVS 4.0.3 ({deleted[rid]})")
            elif rid in selected:
                c = selected[rid]
                item.update(status=str(c.get("status") or "NOT_PROVEN"), tests=list(c.get("tests") or []),
                            note=c.get("note"), selected_title=c.get("title"))
                if item["status"] not in ("TESTED", "NOT_PROVEN") or (item["status"] == "TESTED"
                                                                       and not item["tests"]):
                    item["status"] = "NOT_PROVEN"  # never shown as met without named tests
            elif rid in excluded:
                item.update(status="NOT_SELECTED", reason=excluded[rid])
            else:  # the trace test forbids this; shown as NOT_PROVEN, never dropped
                item.update(status="NOT_PROVEN", note="not listed in the selection (trace incomplete)")
                counts["UNLISTED"] += 1
            counts[item["status"]] += 1
            items.append(item)
        chapters.append({"chapter": chapter, "name": body.get("name"), **{k.lower(): v for k, v in counts.items()}})
    totals = {s.lower(): sum(1 for i in items if i["status"] == s) for s in STATUSES}
    return {
        "asvs_version": str(selection.get("asvs_version") or ""),
        "catalog_version": str(catalog.get("asvs_version") or ""),
        "application": selection.get("application"),
        "items": items,
        "chapters": chapters,
        "totals": totals,
        "selection_sha256": file_sha256(sel_path) if sel_path.is_file() else None,
        "catalog_sha256": file_sha256(cat_path) if cat_path.is_file() else None,
        "per_project": {"status": "UNDECIDED",
                        "reason": "the ASVS level of applications AEOS builds (per data classification/risk) is an "
                                  "owner decision that is not recorded; no per-project selection is claimed"},
        "evidence_note": "TESTED = the trace names existing tests that cover the control; a passing run on the "
                         "current commit is CI gate evidence (G1/G2), not claimed here. NOT_PROVEN is never a pass.",
    }
