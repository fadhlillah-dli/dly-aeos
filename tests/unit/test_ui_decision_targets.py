"""§41 "Keputusan terkait" (W6-113): every page links the decision register filtered on the target_type of its own
records (pinned per page in frontend/src/pages/pageProvenance.test.ts). This side checks that each target_type the
UI filters on is one the backend actually records decisions/records under - a typo or an invented type would show
an always-empty register while looking like "no related decisions"."""

from __future__ import annotations

import re

from aeos.core.paths import repo_root

ROOT = repo_root()
UI_FILTER = re.compile(r"/decisions\?target_type=([A-Za-z0-9_.-]+)")
# how the backend names a target type: a keyword/field (target_type="x", "target_type": "x") or a TARGET constant
BACKEND_TARGET = re.compile(
    r"""(?:target_type\s*=\s*|"target_type"\s*:\s*|\b[A-Z_]*TARGET[A-Z_]*\s*(?::\s*str\s*)?=\s*)"([a-z0-9_.-]+)\"""")


def _ui_target_types() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted((ROOT / "frontend" / "src").rglob("*.tsx")):
        if ".test." in path.name or "__fixtures__" in path.parts:
            continue
        for t in UI_FILTER.findall(path.read_text(encoding="utf-8")):
            out.setdefault(t, set()).add(path.relative_to(ROOT).as_posix())
    return out


def _backend_target_types() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "backend" / "aeos").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        found |= set(BACKEND_TARGET.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_ui_decision_filter_names_a_backend_target_type():
    ui = _ui_target_types()
    assert len(ui) >= 10, f"only {len(ui)} target_types found in the pages: {sorted(ui)}"
    backend = _backend_target_types()
    unknown = {t: sorted(pages) for t, pages in ui.items() if t not in backend}
    assert not unknown, f"'Keputusan terkait' filters on target_types the backend never records: {unknown}"


def test_the_backend_scan_is_not_vacuous():
    backend = _backend_target_types()
    assert {"change_proposal", "plan_version", "model_route", "provider_write", "office_policy"} <= backend
    assert "no_such_target_type" not in backend
