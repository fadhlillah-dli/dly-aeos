"""Personal-data minimisation for stored traces, evidence, audit and exports (§50 "Trace berisi
secret/PII", §24 memory never stores sensitive data, §35 transcripts not opened by default).

Detects common personal identifiers and replaces them with typed placeholders. Detection is
deliberately conservative to avoid mangling versions, dates, hashes and ids:

* e-mail addresses;
* phone numbers written with a ``+`` country code, or Indonesian local numbers starting with ``0``
  (9-15 digits in total, separators allowed);
* Indonesian NIK (16 digits with a plausible province code and birth date);
* payment card numbers (13-19 digits, separators allowed) that pass the Luhn check.

This is minimisation, not a guarantee: free-text names and addresses are not detected. Callers that
must not keep personal data at all apply their own retention policy on top.
"""

from __future__ import annotations

import re

PII_EMAIL = "[PII:EMAIL]"
PII_PHONE = "[PII:PHONE]"
PII_NIK = "[PII:NIK]"
PII_CARD = "[PII:CARD]"

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}\b")
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){1,5}(?:[\s.-]?\d{2,4})(?![\w])")
# Digit-run detectors must never start or end inside an identifier: a run glued to a letter, digit or
# a hyphenated group (UUIDs, hex digests, ULIDs, "run-1234...") is part of an id, not personal data.
# Without this guard the digit tail of ~0.4% of uuid4 values was replaced by [PII:CARD], corrupting
# operation/decision/publication ids in the audit trail (§29 interlock binding); with digit-only boundaries
# ~0.5% of random sha256 digests contained a Luhn-valid 13-19 digit run (evidence/commit/config hashes).
_ID_BEFORE = r"(?<![0-9A-Za-z_-])"
_ID_AFTER = r"(?!-?[0-9A-Za-z_])"
_PHONE_LOCAL = re.compile(_ID_BEFORE + r"(?<!\.)0[1-9]\d{1,3}(?:[\s.-]?\d{3,4}){1,2}[\s.-]?\d{2,5}(?![\w.])(?!-\w)")
_NIK = re.compile(_ID_BEFORE + r"\d{16}" + _ID_AFTER)
_CARD = re.compile(_ID_BEFORE + r"(?:\d[ -]?){12,18}\d" + _ID_AFTER)
# Canonical identifier shapes that are never personal data (used for mapping values under id keys).
_ID_SHAPE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|(?:sha256:)?[0-9a-fA-F]*[a-fA-F][0-9a-fA-F]*"
)


def is_identifier(value: str) -> bool:
    """True when ``value`` is exactly a UUID or a hex digest with at least one hex letter (so a bare
    16-digit NIK or card number under a key named ``*_id`` is still treated as personal data)."""
    return bool(value) and len(value) >= 8 and _ID_SHAPE.fullmatch(value) is not None


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _phone(m: re.Match[str]) -> str:
    n = len(_digits(m.group(0)))
    return PII_PHONE if 9 <= n <= 15 else m.group(0)


def _plausible_nik(d: str) -> bool:
    province = int(d[0:2])
    day, month = int(d[6:8]), int(d[8:10])
    if day > 40:  # women: day + 40
        day -= 40
    return 11 <= province <= 94 and 1 <= day <= 31 and 1 <= month <= 12


def _nik(m: re.Match[str]) -> str:
    return PII_NIK if _plausible_nik(m.group(0)) else m.group(0)


def _luhn(d: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(d)):
        v = int(ch)
        if i % 2:
            v *= 2
            if v > 9:
                v -= 9
        total += v
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    d = _digits(m.group(0))
    return PII_CARD if 13 <= len(d) <= 19 and _luhn(d) else m.group(0)


# Identifiers that must never be "minimised": UUIDs (record ids, e.g. a publication operation id in an audit
# row) and long hex digests. Their digit runs can look like a Luhn-valid card or a local phone number by chance.
# (Never next to "@": a hex-looking e-mail local part or domain is still an e-mail address.)
_PROTECTED = re.compile(r"(?<![0-9A-Fa-f@])(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                        r"[0-9A-Fa-f]{12}|[0-9A-Fa-f]{32,})(?![0-9A-Fa-f@])")


def _redact_plain(text: str) -> str:
    text = _EMAIL.sub(PII_EMAIL, text)
    text = _CARD.sub(_card, text)
    text = _NIK.sub(_nik, text)
    text = _PHONE_INTL.sub(_phone, text)
    text = _PHONE_LOCAL.sub(_phone, text)
    return text


def redact_pii(text: str) -> str:
    if not text or is_identifier(text):
        return text
    out: list[str] = []
    pos = 0
    for m in _PROTECTED.finditer(text):
        out.append(_redact_plain(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_redact_plain(text[pos:]))
    return "".join(out)


def contains_pii(text: str) -> bool:
    return redact_pii(text) != text
