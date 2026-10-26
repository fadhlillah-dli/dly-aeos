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


# 2026-10-19 (K4): 137 -> 132 after identity got a facade; wave 7: 131 (worker runtime takes context packs only
# through knowledge.facade). 2026-10-26 (W6-037): -> 111 after the consumers of
# names the facades already export (identity Principal/verify_password/bootstrap_complete, docs_engine for the
# publisher, policy office_policy, knowledge, verification read_json, proposals create_proposal) were moved onto the
# facades. Wave 7 integration: 110 (both reductions together; the merged clusters' new imports of facade names go
# through the facades). Lower this number when crossings are removed; raising it means a reviewed change ADDS
# internal crossings, which this ratchet exists to prevent.
MAX_CROSSINGS = 110

# A module alias used only for names its facade exports, kept on the internal module for a stated reason.
FACADE_ALIAS_EXCEPTIONS = {
    # the facade's opencode_status() probes the configured binary only; the catalog observes an explicit binary
    "routing/catalog.py: worker_runtime.opencode",
}


def _facade_exports(module: str) -> set[str]:
    """Names aeos.<module>.facade offers: its __all__, else what it defines itself (not what it merely imports)."""
    import ast

    path = ROOT / "backend" / "aeos" / module / "facade.py"
    if not path.is_file():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}  # type: ignore[attr-defined]
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            defined |= {t.id for t in targets if isinstance(t, ast.Name)}
    return {n for n in defined if not n.startswith("_")}


def test_no_crossing_imports_what_the_target_facade_already_exports():
    """W6-037: a facade only makes a module a contract boundary when its consumers use it. Importing a name from
    another module's internals that its facade exports (or importing an internal module only for such names) is
    a crossing that must go through the facade instead."""
    import ast

    mb = _script()
    pkg = mb.PKG
    exports: dict[str, set[str]] = {}
    found: list[str] = []
    for path in sorted(pkg.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(pkg).as_posix()
        src = rel.split("/")[0].removesuffix(".py")
        if src in mb.SHARED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module and node.level == 0):
                continue
            parts = node.module.split(".")
            if parts[0] != "aeos" or len(parts) < 2 or parts[1] in (src, *mb.SHARED):
                continue
            target = parts[1]
            ex = exports.setdefault(target, _facade_exports(target))
            if len(parts) >= 3 and parts[2] != "facade":
                hit = sorted(a.name for a in node.names if a.name in ex)
                if hit:
                    found.append(f"{rel}:{node.lineno} from {node.module} import {', '.join(hit)}")
            elif len(parts) == 2:
                for a in node.names:
                    if a.name != "facade" and (pkg / target / f"{a.name}.py").is_file():
                        aliases[a.asname or a.name] = (target, a.name)
        used: dict[str, set[str]] = {k: set() for k in aliases}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in used:
                used[node.value.id].add(node.attr)
        for alias, (target, sub) in aliases.items():
            if used[alias] and used[alias] <= exports[target] and f"{rel}: {target}.{sub}" not in FACADE_ALIAS_EXCEPTIONS:
                found.append(f"{rel}: aeos.{target}.{sub} used only for facade names {sorted(used[alias])}")
    assert not found, "use aeos.<module>.facade for these names:\n" + "\n".join(found)


def test_the_crossing_baseline_never_grows():
    assert len(_baseline()["crossings"]) <= MAX_CROSSINGS


def test_every_control_plane_module_with_cross_module_consumers_has_a_facade():
    """§10: identity, docs_engine and control_center (formerly without one) publish a frozen contract."""
    for mod in ("identity", "docs_engine", "control_center"):
        assert (ROOT / "backend" / "aeos" / mod / "facade.py").is_file(), mod
    targets = {c.split(" -> ")[1].split(".")[0] for c in _baseline()["crossings"]}
    missing = sorted(t for t in targets if not (ROOT / "backend" / "aeos" / t / "facade.py").is_file())
    assert not missing, f"modules other modules reach into, without a facade: {missing}"


def test_facades_import_cleanly():
    import importlib

    for mod in ("identity", "docs_engine", "control_center"):
        importlib.import_module(f"aeos.{mod}.facade")
