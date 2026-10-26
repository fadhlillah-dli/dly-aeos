"""Exact money primitives of the cost module (§42): 6-dp ``Decimal``, never NaN/inf.

Split out of ``aeos.cost.service`` (which re-exports them) so that ``aeos.cost.subscription`` can use them without
importing the service module (ADR-0080 amendment 2026-10-26).
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, Overflow
from typing import Any

USD_Q = Decimal("0.000001")
ZERO = Decimal("0")


def money(value: Any) -> Decimal | None:
    """Exact 6-dp Decimal. Rejects NaN/inf (an untrusted number is not a budget number) and any amount the 6-dp
    quantization cannot represent (e.g. 1e25 needs more digits than the decimal context has): always ``ValueError``,
    never a ``decimal`` arithmetic error escaping to a caller that only expects ``ValueError``."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError("amount must be finite")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid amount {value!r}") from exc
    if not d.is_finite():
        raise ValueError("amount must be finite")
    try:
        return d.quantize(USD_Q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, Overflow) as exc:
        raise ValueError(f"amount {value!r} is out of range") from exc


def money_out(value: Any) -> float | None:
    d = money(value)
    return None if d is None else float(d)
