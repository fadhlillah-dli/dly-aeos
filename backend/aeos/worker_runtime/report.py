"""Structured worker message ``aeos-report`` (§19 "pesan membawa task/run/version refs, facts/findings,
evidence refs, blocker dan requested decision"; §21 "worker berhenti dan mengirim blocker/decision request").

The worker writes ``.aeos/report.json`` in its worktree; the runtime removes it from the candidate,
validates it against ``WORKER_REPORT_SCHEMA`` (published at ``contracts/worker/aeos-report.schema.json``)
and treats it as untrusted DATA: it is redacted, size-capped, stored as worker-origin evidence (E0), and
a blocker / requested decision becomes an OPEN ``DecisionRequest`` (requested_by_kind AGENT) in the single
human decision queue. A request never grants anything; the task stops BLOCKED (``WORKER_BLOCKER``) until
the owner acts. Refs the worker claims (task/run/plan version) are compared with the runtime's own and a
mismatch is reported, never trusted.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from aeos.core.redaction import redact, redact_text

SCHEMA_ID = "aeos.worker.report.v1"
BLOCKER_CODES = ("SCOPE_GROWTH", "CONTRACT_UNMET", "MISSING_INPUT", "NEEDS_DECISION", "OTHER")
DECISION_KINDS = ("POLICY", "RISK_ACCEPTANCE", "EXCEPTION", "BUDGET")
_STR = {"type": "string", "maxLength": 2000}
_SHORT = {"type": "string", "maxLength": 200}

WORKER_REPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SCHEMA_ID,
    "title": "aeos.worker.report",
    "description": "Structured message from a worker to the AEOS control plane (untrusted DATA).",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "status"],
    "properties": {
        "schema_version": {"const": "1"},
        "task_key": _SHORT,
        "run_id": _SHORT,
        "plan_version_hash": _SHORT,
        "status": {"enum": ["COMPLETED", "BLOCKED"]},
        "summary": _STR,
        "findings": {"type": "array", "maxItems": 50, "items": {
            "type": "object", "additionalProperties": False, "required": ["title"],
            "properties": {"title": _SHORT, "detail": _STR, "evidence_refs": {
                "type": "array", "maxItems": 20, "items": _SHORT}}}},
        "evidence_refs": {"type": "array", "maxItems": 50, "items": _SHORT},
        "blocker": {"type": ["object", "null"], "additionalProperties": False, "required": ["code", "detail"],
                    "properties": {"code": {"enum": list(BLOCKER_CODES)}, "detail": _STR}},
        "requested_decision": {"type": ["object", "null"], "additionalProperties": False,
                               "required": ["kind", "title", "reason"],
                               "properties": {"kind": {"enum": list(DECISION_KINDS)}, "title": _SHORT, "reason": _STR,
                                              "requested_operations": {"type": "array", "maxItems": 10,
                                                                       "items": _SHORT}}},
    },
}
_VALIDATOR = Draft202012Validator(WORKER_REPORT_SCHEMA)


def validate(data: Any) -> list[str]:
    """Schema errors (empty = valid)."""
    return sorted(f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message[:200]}"
                  for e in _VALIDATOR.iter_errors(data))[:20]


def is_blocker(report: dict[str, Any] | None) -> bool:
    return bool(report) and (report.get("status") == "BLOCKED" or bool(report.get("blocker"))
                             or bool(report.get("requested_decision")))


def ref_mismatches(report: dict[str, Any], *, task_key: str, run_id: str, plan_version_hash: str | None) -> list[str]:
    out = []
    for key, actual in (("task_key", task_key), ("run_id", run_id), ("plan_version_hash", plan_version_hash)):
        claimed = report.get(key)
        if claimed and actual and str(claimed) != str(actual):
            out.append(f"{key}: worker claimed {str(claimed)[:80]!r}, runtime has {str(actual)[:80]!r}")
    return out


def sanitized(report: dict[str, Any]) -> dict[str, Any]:
    """Redacted copy for evidence / decision-request details (secrets never persisted)."""
    return redact(report)


def decision_request_fields(report: dict[str, Any], *, task_key: str) -> dict[str, Any]:
    """kind/title/reason/operations of the DecisionRequest a blocker report opens (all redacted)."""
    req = report.get("requested_decision") or {}
    blocker = report.get("blocker") or {}
    kind = req.get("kind") if req.get("kind") in DECISION_KINDS else "POLICY"
    title = req.get("title") or f"Worker blocker ({blocker.get('code') or 'BLOCKED'})"
    reason = req.get("reason") or blocker.get("detail") or report.get("summary") or "worker reported a blocker"
    return {"kind": kind, "title": redact_text(f"{task_key}: {title}")[:256], "reason": redact_text(str(reason))[:2000],
            "requested_operations": [redact_text(str(o))[:64] for o in req.get("requested_operations") or []][:10]}
