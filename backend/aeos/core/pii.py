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
_PHONE_LOCAL = re.compile(r"(?<![\w.])0[1-9]\d{1,3}(?:[\s.-]?\d{3,4}){1,2}[\s.-]?\d{2,5}(?![\w.])")
_NIK = re.compile(r"(?<!\d)\d{16}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


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


def redact_pii(text: str) -> str:
    if not text:
        return text
    text = _EMAIL.sub(PII_EMAIL, text)
    text = _CARD.sub(_card, text)
    text = _NIK.sub(_nik, text)
    text = _PHONE_INTL.sub(_phone, text)
    text = _PHONE_LOCAL.sub(_phone, text)
    return text


def contains_pii(text: str) -> bool:
    return redact_pii(text) != text
