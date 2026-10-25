"""tests/acceptance/MATRIX.md (master §50): every acceptance row is mapped exactly once, and every test the matrix
cites really exists - a renamed or deleted test cannot keep a row "PASS" on paper. Rows whose behaviour has a
dedicated test cite it (2.13 auto-update/hooks refused at skill import, 2.14 protected controller paths)."""

from __future__ import annotations

import re
from pathlib import Path

from aeos.core.paths import repo_root

ROOT = repo_root()
MATRIX = ROOT / "tests" / "acceptance" / "MATRIX.md"
# master §50: Table 1 (implementation), Table 2 (organisation/workforce), Table 3 (tools/architecture)
EXPECTED_ROWS = {"1": 40, "2": 14, "3": 28}
_ROW = re.compile(r"^\|\s*(\d+)\.(\d+)\s*\|(.*)$")
# `test_x.py::test_y[param]`, `tests/unit/test_x.py::test_y`, or `::test_y` (same file as the previous citation)
_REF = re.compile(r"`((?:tests/[\w/]+/)?test_\w+\.py)?::(test_\w+)(?:\[[^\]`]*\])?`")


def _rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    dupes: list[str] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        key = f"{m.group(1)}.{m.group(2)}"
        if key in rows:
            dupes.append(key)
        cells = [c.strip() for c in m.group(3).split("|")]
        rows[key] = cells[1] if len(cells) > 1 else ""  # the "Test id(s)" column
    assert not dupes, f"rows mapped more than once: {dupes}"
    return rows


def _refs(cell: str) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    current: Path | None = None
    for file, func in _REF.findall(cell):
        if file:
            current = ROOT / file if file.startswith("tests/") else ROOT / "tests" / "acceptance" / file
        assert current is not None, f"'::{func}' cites no file: {cell[:120]}"
        out.append((current, func))
    return out


def test_every_section_50_row_is_mapped_exactly_once():
    rows = _rows()
    counts = {t: sum(1 for k in rows if k.split(".")[0] == t) for t in EXPECTED_ROWS}
    assert counts == EXPECTED_ROWS, counts
    assert all(_refs(cell) for cell in rows.values()), [k for k, c in rows.items() if not _refs(c)]


def test_every_cited_test_exists():
    missing: list[str] = []
    sources: dict[Path, str] = {}
    for key, cell in _rows().items():
        for path, func in _refs(cell):
            if path not in sources:
                sources[path] = path.read_text(encoding="utf-8") if path.is_file() else ""
            if not re.search(rf"^\s*(?:async\s+)?def {re.escape(func)}\(", sources[path], re.M):
                missing.append(f"{key}: {path.relative_to(ROOT).as_posix()}::{func}")
    assert not missing, "MATRIX.md cites tests that do not exist:\n" + "\n".join(missing)


def test_rows_with_a_dedicated_test_cite_it():
    rows = _rows()
    cited = {k: {f"{p.relative_to(ROOT).as_posix()}::{f}" for p, f in _refs(c)} for k, c in rows.items()}
    assert "tests/unit/test_workforce_skill_import.py::test_pack_requesting_auto_update_or_hooks_is_refused" \
        in cited["2.13"], cited["2.13"]
    assert "tests/unit/test_verification_scope.py::test_capability_audit_dashboard_and_master_paths_are_protected" \
        in cited["2.14"], cited["2.14"]
