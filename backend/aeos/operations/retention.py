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


EXPIRED_LABEL = "Kedaluwarsa menurut kebijakan (disposal manual saja)"


def marker(policy: dict[str, Any], data_class: str, domain: str | None, created_at: datetime | None, *,
           legal_hold: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Per-record retention marker shown next to audit/evidence records (§35). A platform record (no domain)
    has no domain rule and is RETAINED; a legal hold is never expired. AEOS never deletes anything: an
    expired record is only *marked* for manual disposal."""
    rule = resolve(policy, data_class, domain) if domain in DOMAINS else {"rule": "RETAIN", "retain_days": None}
    expired = is_expired(rule, created_at, legal_hold=legal_hold, now=now)
    return {"rule": rule["rule"], "retain_days": rule["retain_days"], "legal_hold": legal_hold,
            "expired_by_policy": expired, "disposal": "MANUAL_ONLY" if expired else "NONE",
            "label": EXPIRED_LABEL if expired else ("Legal hold: tidak pernah didisposal" if legal_hold
                                                    else "Disimpan" if rule["retain_days"] is None
                                                    else f"Disimpan (aturan {rule['retain_days']} hari)")}


def legal_hold_project_ids(session: Any) -> set[str]:
    """Projects under an active retirement legal hold: their records are never expired (§33)."""
    from sqlalchemy import select

    from aeos.models.operations_ext import OpsRetirement

    return {r.project_id for r in session.scalars(select(OpsRetirement))
            if isinstance(r.legal_hold, dict) and r.legal_hold.get("active")}


def evidence_legal_hold(retention_label: str | None, project_id: str | None, held_projects: set[str]) -> bool:
    """EvidenceArtifact.retention_policy ``legal-hold`` (collector label) or a held project blocks expiry."""
    return retention_label == "legal-hold" or (project_id is not None and project_id in held_projects)


def applied_counts(session: Any, policy: dict[str, Any], domain: str, *, now: datetime | None = None
                   ) -> dict[str, dict[str, Any]]:
    """How the rule applies to the stored audit/evidence records of *domain*: record count and how many are
    expired by policy (only ever > 0 for a decided rule; nothing is deleted)."""
    from sqlalchemy import func, select

    from aeos.models.audit import AuditEvent
    from aeos.models.evidence import EvidenceArtifact

    now = now or utcnow()
    held = legal_hold_project_ids(session)
    out: dict[str, dict[str, Any]] = {}
    for cls, model, at_col in (("audit_events", AuditEvent, AuditEvent.at),
                               ("evidence", EvidenceArtifact, EvidenceArtifact.created_at)):
        total = session.scalar(select(func.count()).select_from(model).where(model.domain == domain)) or 0
        rule = resolve(policy, cls, domain)
        expired = 0
        if rule["retain_days"] is not None:
            cutoff = now - timedelta(days=int(rule["retain_days"]))
            q = select(model).where(model.domain == domain, at_col <= cutoff)
            for row in session.scalars(q):
                hold = (evidence_legal_hold(row.retention_policy, row.project_id, held) if cls == "evidence"
                        else row.project_id in held)
                expired += 0 if hold else 1
        out[cls] = {"records": int(total), "expired_by_policy": expired, "evaluated": True}
    return out
