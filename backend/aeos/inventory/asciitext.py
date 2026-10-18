"""ASCII-only rendering for CLI output (AGENTS.md: legacy Windows consoles)."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

# Code points spelled numerically so the source stays ASCII and unambiguous.
_REPLACEMENTS = {
    chr(0x2014): "-",  # em dash
    chr(0x2013): "-",  # en dash
    chr(0x2026): "...",
    chr(0x00A7): "S",  # section sign
    chr(0x2260): "!=",
    chr(0x2192): "->",
    chr(0x201C): '"',
    chr(0x201D): '"',
    chr(0x2018): "'",
    chr(0x2019): "'",
    chr(0x2022): "*",
    chr(0x2265): ">=",
    chr(0x2264): "<=",
}


def ascii_safe(text: object) -> str:
    s = "" if text is None else str(text)
    for k, v in _REPLACEMENTS.items():
        s = s.replace(k, v)
    s = unicodedata.normalize("NFKD", s)
    return s.encode("ascii", "replace").decode("ascii")


def table(headers: Sequence[str], rows: Sequence[Sequence[object]], *, max_width: int = 60) -> str:
    """Plain ASCII table with clipped cells."""
    cells = [[ascii_safe(c) for c in headers]] + [[ascii_safe(c) for c in r] for r in rows]
    clipped = [[c if len(c) <= max_width else c[: max_width - 3] + "..." for c in row] for row in cells]
    widths = [max(len(row[i]) for row in clipped) for i in range(len(headers))]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    lines = [sep, "| " + " | ".join(clipped[0][i].ljust(widths[i]) for i in range(len(headers))) + " |", sep]
    for row in clipped[1:]:
        lines.append("| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))) + " |")
    lines.append(sep)
    return "\n".join(lines)
