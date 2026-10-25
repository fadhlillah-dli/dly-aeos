"""Runtime half of the read-only INVESTIGATION run (§19 "Read-only investigations terpisah dengan timebox").

The worker gets OpenCode with READ-ONLY permissions (read/glob/grep/list; edit/write/patch, bash, web and every
always-denied tool stay denied), the hypothesis and its timebox, and must answer with ONE JSON report in its final
message (it cannot write files). The runtime parses that report, checks every evidence reference against the
repository at the base commit (exists or not - the claim itself stays untrusted DATA), stores it as untrusted evidence
and never commits anything: any change in the worktree is a read-only violation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from aeos.core.redaction import redact, redact_text

SCHEMA_ID = "aeos-investigation/1"
READ_ONLY_ALLOW = ("read", "glob", "grep", "list")
VERDICTS = ("SUPPORTED", "REFUTED", "INCONCLUSIVE")
MAX_REFS = 30
MAX_FINDINGS = 20
REF_RE = re.compile(r"^(?P<path>[^:]+?)(?::(?P<line>\d+)(?:-\d+)?)?$")
PROMPT_RULES = (
    "- This is a READ-ONLY investigation of ONE hypothesis. You can only read and search files (read, glob, grep, "
    "list). Editing, writing, patching, shell commands and web access are disabled; any file change ends the run as "
    "a violation and nothing is committed.",
    "- Do not fix anything. Decide whether the hypothesis explains the problem and collect evidence: repository "
    "paths with line numbers that support or refute it.",
    "- Stop when the timebox ends; an unfinished investigation is INCONCLUSIVE.",
    f"- End your FINAL message with exactly one JSON object (schema {SCHEMA_ID}) and nothing after it:",
    '  {"schema_version": "1", "kind": "investigation", "hypothesis_key": "<key>", "verdict": '
    '"SUPPORTED|REFUTED|INCONCLUSIVE", "summary": "...", "findings": [{"title": "...", "detail": "..."}], '
    '"evidence_refs": ["path/to/file.py:123"], "proposed_fix": "..."}',
    "- Keep the whole report under 3000 characters (a longer final message is cut and the report is lost).",
    "- The report is untrusted data for the integrator: it never approves, grants or changes anything.",
)


def prompt_section(inv: dict[str, Any]) -> list[str]:
    hyp = inv.get("hypothesis") or {}
    return ["", f"## Read-only investigation (hypothesis {inv.get('index')}/{inv.get('of')}: {hyp.get('key')})",
            f"Hypothesis: {redact_text(str(hyp.get('statement') or ''))}",
            f"Timebox: {int(inv.get('timebox_seconds') or 0)} seconds",
            *PROMPT_RULES]


def _json_candidates(text: str) -> list[str]:
    """JSON objects in *text*, last first: a fenced block, else every top-level {...} span."""
    out = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)]
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:i + 1])
    return list(reversed(out))


def extract_report(raw_log: list[str]) -> dict[str, Any] | None:
    """The last ``aeos-investigation`` JSON object in the worker's text events (OpenCode ``--format json``)."""
    for line in reversed(raw_log):
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ev, dict) or ev.get("type") != "text":
            continue
        text = str((ev.get("part") or {}).get("text") or "")
        for cand in _json_candidates(text):
            try:
                data = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("kind") == "investigation":
                return data
    return None


def _safe_rel(path: str) -> str | None:
    p = path.replace("\\", "/").strip()
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        return None
    return p


def check_refs(refs: list[Any], repo: Path) -> list[dict[str, Any]]:
    """Each evidence reference with whether it names an existing file (and line) in the worktree at the base."""
    out: list[dict[str, Any]] = []
    root = repo.resolve()
    for raw in refs[:MAX_REFS]:
        ref = redact_text(str(raw))[:300]
        m = REF_RE.match(ref.strip())
        rel = _safe_rel(m.group("path")) if m else None
        exists = False
        if rel:
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
                if target.is_file():
                    exists = True
                    line = m.group("line") if m else None
                    if line:
                        with target.open("rb") as fh:
                            exists = int(line) <= sum(1 for _ in fh) + 1
            except (ValueError, OSError):
                exists = False
        out.append({"ref": ref, "exists": exists})
    return out


def validate(data: dict[str, Any] | None, *, hypothesis_key: str) -> list[str]:
    if data is None:
        return ["no investigation report in the final message"]
    errors: list[str] = []
    if str(data.get("schema_version")) != "1":
        errors.append("schema_version must be '1'")
    if data.get("hypothesis_key") != hypothesis_key:
        errors.append(f"hypothesis_key must be {hypothesis_key!r}")
    if data.get("verdict") not in VERDICTS:
        errors.append(f"verdict must be one of {VERDICTS}")
    if not isinstance(data.get("evidence_refs", []), list) or not isinstance(data.get("findings", []), list):
        errors.append("evidence_refs and findings must be lists")
    allowed = {"schema_version", "kind", "hypothesis_key", "verdict", "summary", "findings", "evidence_refs",
               "proposed_fix"}
    extra = sorted(set(data) - allowed)
    if extra:  # e.g. "approve": a report never carries authority
        errors.append(f"unknown fields {extra}")
    return errors


def summarise(data: dict[str, Any] | None, *, hypothesis_key: str, repo: Path) -> dict[str, Any]:
    """The run's investigation summary (stored on WorkerRun.caps.investigation_report, all untrusted DATA)."""
    errors = validate(data, hypothesis_key=hypothesis_key)
    clean = redact(data or {})
    refs = check_refs(list(clean.get("evidence_refs") or []) if isinstance(clean.get("evidence_refs"), list) else [],
                      repo)
    verdict = clean.get("verdict") if not errors else None
    return {"schema": SCHEMA_ID, "valid": not errors, "schema_errors": errors, "hypothesis_key": hypothesis_key,
            "verdict": verdict, "summary": redact_text(str(clean.get("summary") or ""))[:1000],
            "proposed_fix": redact_text(str(clean.get("proposed_fix") or ""))[:2000],
            "findings": [redact(f) for f in (clean.get("findings") or [])[:MAX_FINDINGS]] if not errors else [],
            "evidence_checked": refs,
            # evidenced = SUPPORTED and at least one reference the runtime found in the repository
            "evidenced": bool(not errors and verdict == "SUPPORTED" and any(r["exists"] for r in refs)),
            "untrusted": True}


def fingerprint_material() -> dict[str, Any]:
    """Part of the worker prompt revision (§23/§25): the investigation rules and read-only tool set."""
    return {"schema": SCHEMA_ID, "rules": list(PROMPT_RULES), "read_only_allow": list(READ_ONLY_ALLOW),
            "verdicts": list(VERDICTS)}
