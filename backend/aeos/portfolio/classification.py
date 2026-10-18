"""Data classification ordering shared by portfolio, intake and knowledge (§04, §24)."""

from __future__ import annotations

from aeos.core.enums import DataClassification
from aeos.core.errors import ValidationFailed

ORDER = [DataClassification.PUBLIC, DataClassification.INTERNAL, DataClassification.CONFIDENTIAL,
         DataClassification.RESTRICTED]
_RANK = {c.value: i for i, c in enumerate(ORDER)}
# §24 "Memory tidak dipakai lintas proyek untuk data sensitif".
SENSITIVE = frozenset({DataClassification.CONFIDENTIAL.value, DataClassification.RESTRICTED.value})


def normalize(value: str | None, *, default: str = DataClassification.INTERNAL.value) -> str:
    v = (value or default).strip().upper()
    if v not in _RANK:
        raise ValidationFailed(f"unknown data classification {value!r}", code="INVALID_CLASSIFICATION",
                               details={"allowed": [c.value for c in ORDER]})
    return v


def rank(value: str | None) -> int:
    return _RANK.get((value or "").upper(), _RANK[DataClassification.RESTRICTED.value])  # unknown = strictest


def highest(*values: str | None) -> str:
    present = [v.upper() for v in values if v]
    if not present:
        return DataClassification.INTERNAL.value
    if any(v not in _RANK for v in present):  # unknown labels are treated as the strictest class
        return DataClassification.RESTRICTED.value
    return max(present, key=rank)


def at_least(value: str | None, floor: str) -> str:
    """Never downgrade below *floor* (e.g. an attachment below its project's classification)."""
    v = normalize(value, default=floor)
    if rank(v) < rank(floor):
        raise ValidationFailed(f"classification {v} is lower than the required minimum {floor}",
                               code="CLASSIFICATION_TOO_LOW", details={"minimum": floor})
    return v
