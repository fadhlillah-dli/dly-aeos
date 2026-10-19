"""Retention policy resolution (master §35 audit/evidence retention, §33 retirement data disposal).

The policy is ``policy/retention.yaml`` (owner decisions; a protected path). A data class x domain
without a decided ``retain_days`` is RETAIN: nothing is ever reported as expired, and AEOS never
deletes anything - expiry only marks records as "expired by policy (manual disposal only)". A legal
hold blocks disposal regardless of the rule. An unreadable or malformed policy is UNAVAILABLE and
resolves to RETAIN as well (fail closed: no disposal).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from aeos.core.paths import repo_root
from aeos.core.timeutil import ensure_aware, utcnow

DATA_CLASSES = ("audit_events", "evidence", "attachments", "external_references", "backups")
DOMAINS = ("PERSONAL", "OFFICE")


def policy_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / "policy" / "retention.yaml"


def load_policy(root: Path | None = None) -> dict[str, Any]:
    path = policy_path(root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {"status": "UNAVAILABLE", "reason": type(exc).__name__, "rules": {}, "source": "policy/retention.yaml"}
    if not isinstance(data, dict) or not isinstance(data.get("data_classes"), dict):
        return {"status": "UNAVAILABLE", "reason": "malformed", "rules": {}, "source": "policy/retention.yaml"}
    rules: dict[str, dict[str, int | None]] = {}
    for cls, per_domain in data["data_classes"].items():
        if not isinstance(per_domain, dict):
            continue
        for dom, rule in per_domain.items():
            days = rule.get("retain_days") if isinstance(rule, dict) else None
            ok = isinstance(days, int) and not isinstance(days, bool) and days > 0
            rules.setdefault(str(cls), {})[str(dom).upper()] = days if ok else None
    decided = any(v is not None for per in rules.values() for v in per.values())
    return {"status": "DECIDED" if decided else str(data.get("status") or "OWNER_DECISION_REQUIRED"),
            "rules": rules, "legal_hold_blocks_disposal": bool(data.get("legal_hold_blocks_disposal", True)),
            "source": "policy/retention.yaml", "version": data.get("version")}


def resolve(policy: dict[str, Any], data_class: str, domain: str) -> dict[str, Any]:
    days = (policy.get("rules") or {}).get(data_class, {}).get(domain)
    if days is None:
        return {"data_class": data_class, "domain": domain, "rule": "RETAIN", "retain_days": None,
                "label": "Belum ada aturan retensi (keputusan pemilik); data disimpan, tidak ada disposal."}
    return {"data_class": data_class, "domain": domain, "rule": "RETAIN_DAYS", "retain_days": days,
            "label": f"Simpan {days} hari; setelah itu kedaluwarsa menurut kebijakan (disposal manual saja)."}


def is_expired(rule: dict[str, Any], created_at: datetime | None, *, legal_hold: bool = False,
               now: datetime | None = None) -> bool:
    """True only for a decided rule, a known creation time, no legal hold and an elapsed period."""
    if legal_hold or rule.get("retain_days") is None or created_at is None:
        return False
    return ensure_aware(created_at) + timedelta(days=int(rule["retain_days"])) <= (now or utcnow())
