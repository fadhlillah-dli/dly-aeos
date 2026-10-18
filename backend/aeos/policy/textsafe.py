"""String-only redaction for structured policy/planning records.

``aeos.core.redaction.redact`` blanks every value whose *key* merely contains words such as
``token`` or ``authorization`` — which would destroy legitimate fields like ``office_authorization``
or ``max_output_tokens`` and break hash binding. Decisions and plan content are not credential
stores, so here every string *value* is pattern-redacted (known secret shapes + registered runtime
secrets) while keys and non-string values are kept intact.
"""

from __future__ import annotations

from typing import Any

from aeos.core.redaction import redact_text


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value
