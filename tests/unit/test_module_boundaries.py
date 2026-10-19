"""Module boundaries and the table registry (master §10 "modules are code and contract boundaries", §13
"no duplicate subsystems/tables; extend equivalent records and reference canonical IDs").

Both are ratchets over contracts/architecture/module-boundaries.json: existing crossings are the recorded
baseline; a NEW cross-module import of another module's internals, or a NEW table without a §13 concept,
fails here until it goes through a facade / is mapped (scripts/module_boundaries.py --update after review).
"""

from __future__ import annotations

import importlib.util
import json
import sys

from aeos.core.paths import repo_root

ROOT = repo_root()


def _script():
    spec = importlib.util.spec_from_file_location("aeos_module_boundaries", ROOT / "scripts" / "module_boundaries.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _baseline() -> dict:
    return json.loads((ROOT / "contracts" / "architecture" / "module-boundaries.json").read_text(encoding="utf-8"))


def test_no_new_cross_module_internal_imports():
    mb = _script()
    new = sorted(mb.crossings() - set(_baseline()["crossings"]))
    assert not new, ("new cross-module imports bypass a facade (use aeos.<module>.facade, or review and record "
                     f"them with scripts/module_boundaries.py --update): {new}")


def test_detector_sees_a_planted_crossing(tmp_path, monkeypatch):
    mb = _script()
    pkg = tmp_path / "aeos"
    (pkg / "alpha").mkdir(parents=True)
    (pkg / "beta").mkdir()
    (pkg / "beta" / "internals.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "beta" / "facade.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "alpha" / "service.py").write_text(
        "from aeos.beta import facade\nfrom aeos.beta.internals import X\nfrom aeos.core.db import Base\n",
        encoding="utf-8")
    monkeypatch.setattr(mb, "PKG", pkg)
    assert mb.crossings() == {"alpha -> beta.internals"}  # facade and shared layers are allowed


def test_every_table_is_mapped_to_one_concept():
    mb = _script()
    mapped = _baseline()["tables"]
    current = mb.tables()
    unmapped = sorted(t for t in current if t not in mapped or mapped[t].startswith("UNMAPPED"))
    assert not unmapped, f"new tables need a §13 concept (one concept = one table): {unmapped}"
    concepts = [c.split(" (")[0] for c in mapped.values()]
    dupes = sorted({c for c in concepts if concepts.count(c) > 1})
    assert not dupes, f"two tables claim the same concept: {dupes}"


def test_baseline_only_lists_crossings_that_still_exist():
    """The ratchet only shrinks: a crossing that was routed through a facade must leave the baseline, so it
    cannot silently come back (review finding: crossings introduced together with the baseline were recorded
    as pre-existing)."""
    mb = _script()
    stale = sorted(set(_baseline()["crossings"]) - mb.crossings())
    assert not stale, f"remove fixed crossings from the baseline (scripts/module_boundaries.py --update): {stale}"
