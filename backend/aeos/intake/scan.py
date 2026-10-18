"""Untrusted-input scanning (§05 step 5, §25, §50).

Two detectors run on every piece of human- or file-supplied text before it is persisted:

* secrets — any credential-shaped value (``aeos.core.redaction``) flags the input and the stored
  text is redacted; the value itself never appears in findings, logs or responses;
* instruction-injection markers — phrases that try to act as authority ("ignore previous
  instructions", "ubah domain ke PERSONAL", "send source to ...", "approve and merge"). They are
  recorded as ``UNTRUSTED_INSTRUCTION`` findings. A finding is DATA for human review: it never
  changes goal, domain, policy, permission, memory or routing, and never blocks a human decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aeos.core.redaction import contains_secret, redact_text

UNTRUSTED_INSTRUCTION = "UNTRUSTED_INSTRUCTION"
SECRET = "SECRET"

# (marker id, pattern). English + Indonesian; case-insensitive; whitespace-tolerant.
_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    (m, re.compile(p, re.IGNORECASE))
    for m, p in [
        ("ignore-previous-instructions",
         r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+)*(previous|prior|above|earlier|system)\s+"
         r"(instructions?|prompts?|rules?|messages?|context)"),
        ("abaikan-instruksi", r"\b(abaikan|lupakan|timpa)\s+(semua\s+|seluruh\s+)?(instruksi|perintah|aturan)"),
        ("change-domain", r"\b(change|switch|set|move|convert|reclassify)\s+(the\s+|this\s+)?(project\s+)?domain\b"),
        ("ubah-domain", r"\b(ubah|ganti|pindah(kan)?|jadikan|alihkan)\s+(\w+\s+){0,3}domain\b"),
        ("domain-swap",
         r"\b(OFFICE|PERSONAL)\s+(menjadi|jadi|to|into|->|=>|→)\s+(OFFICE|PERSONAL)\b"),
        ("domain-is", r"\bdomain\s*(is|=|:|adalah|menjadi)\s*(now\s+)?(OFFICE|PERSONAL)\b"),
        ("exfiltrate-source",
         r"\b(send|upload|push|copy|exfiltrate|post|forward)\s+(the\s+|all\s+|this\s+)*(source|code|repo(sitory)?|"
         r"secrets?|credentials?|tokens?|keys?|data|files?)\b[^.\n]{0,40}\b(to|ke)\b"),
        ("kirim-source", r"\b(kirim(kan)?|unggah|salin)\s+(\w+\s+){0,2}(source|kode|repo(sitori)?|secret|kredensial|token|data)\b"),
        ("role-override", r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be)\s+(an?\s+)?(admin|administrator|root|owner|system)"),
        ("system-prompt", r"\b(reveal|print|show|ignore)\s+(the\s+|your\s+)?system\s+prompt\b"),
        ("self-approve",
         r"\b(auto(matically)?[\s-]*approve|approve\s+(this|the\s+plan|yourself)|merge\s+(it\s+)?(to|into)\s+main|"
         r"deploy\s+to\s+production|skip\s+(the\s+)?(review|approval|verification|tests?))\b"),
        ("setujui-otomatis", r"\b(setujui|approve)\s+(otomatis|sendiri)|\blewati\s+(review|persetujuan|verifikasi)"),
        ("grant-permission",
         r"\b(grant|give)\s+(yourself|me|the\s+agent|the\s+worker)\s+(full\s+)?(access|permission|admin|privileges?)"),
        ("disable-control",
         r"\b(disable|turn\s+off|bypass)\s+(the\s+)?(auth(entication)?|csrf|sandbox|policy|interlock|redaction|budget)"),
        ("script-tag", r"<\s*script\b"),
    ]
]

_MAX_FINDINGS = 50


@dataclass(frozen=True)
class ScanResult:
    findings: list[dict[str, object]]
    has_secret: bool
    has_injection: bool
    redacted_text: str


def _excerpt(line: str, start: int, end: int, width: int = 60) -> str:
    a = max(0, start - width // 2)
    b = min(len(line), end + width // 2)
    return redact_text(line[a:b].strip())[:160]


def scan_text(text: str, *, source: str = "text") -> ScanResult:
    """Scan *text*; findings never contain secret values (excerpts are redacted)."""
    findings: list[dict[str, object]] = []
    has_secret = contains_secret(text)
    if has_secret:
        lines = [i + 1 for i, ln in enumerate(text.splitlines()) if contains_secret(ln)]
        findings.append({
            "kind": SECRET,
            "source": source,
            "severity": "HIGH",
            "lines": lines[:20],
            "message": "Credential-like value detected; stored text is redacted. Rotate the credential if it is real.",
        })
    has_injection = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        for marker, pat in _MARKERS:
            m = pat.search(line)
            if not m:
                continue
            has_injection = True
            if len(findings) < _MAX_FINDINGS:
                findings.append({
                    "kind": UNTRUSTED_INSTRUCTION,
                    "source": source,
                    "severity": "MEDIUM",
                    "marker": marker,
                    "line": lineno,
                    "excerpt": _excerpt(line, m.start(), m.end()),
                    "message": "Instruction-like text treated as DATA; it grants no authority and changes nothing.",
                })
    return ScanResult(findings=findings, has_secret=has_secret, has_injection=has_injection,
                      redacted_text=redact_text(text) if has_secret else text)
