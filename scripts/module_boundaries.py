"""Module boundary + table registry ratchet (master §10 "modules are code and contract boundaries", §13 "no
duplicate subsystems/tables; extend equivalent records").

* Cross-module imports: module A may use module B through ``aeos.B.facade`` (or the shared layers
  ``core``, ``models``, ``audit``, ``events``, ``api``). Every other ``aeos.B.<internal>`` import from A is a
  boundary crossing. The crossings that existed when the check was introduced are the baseline; a NEW
  crossing fails ``tests/unit/test_module_boundaries.py`` until it goes through a facade or is reviewed and
  added here with ``--update``.
* Tables: every table of ``Base.metadata`` is listed with the §13 concept it implements; a new table fails
  the test until it is mapped to a concept (one concept = one table, AGENTS.md).

Usage: python scripts/module_boundaries.py [--update]
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "backend" / "aeos"
BASELINE = ROOT / "contracts" / "architecture" / "module-boundaries.json"
SHARED = {"core", "models", "audit", "events", "api", "migrations", "cli", "__main__", "__init__"}


def _module_of(path: Path) -> str:
    rel = path.relative_to(PKG)
    return rel.parts[0].removesuffix(".py") if rel.parts else ""


def crossings() -> set[str]:
    out: set[str] = set()
    for path in PKG.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        src_mod = _module_of(path)
        if src_mod in SHARED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module == "aeos" or node.module.count(".") == 1:
                    # "from aeos.x import y": y may itself be a submodule (e.g. "from aeos.broker import grants")
                    names = [f"{node.module}.{a.name}" for a in node.names]
                else:
                    names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                parts = name.split(".")
                if len(parts) < 3 or parts[0] != "aeos":
                    continue
                target_mod, sub = parts[1], parts[2]
                if target_mod == src_mod or target_mod in SHARED or sub == "facade":
                    continue
                if not (PKG / target_mod / f"{sub}.py").is_file() and not (PKG / target_mod / sub).is_dir():
                    continue  # a symbol imported from the package __init__, not a submodule
                out.add(f"{src_mod} -> {target_mod}.{sub}")
    return out


def tables() -> list[str]:
    sys.path.insert(0, str(ROOT / "backend"))
    from aeos.core.db import Base, import_all_models

    import_all_models()
    return sorted(Base.metadata.tables)


def main(argv: list[str]) -> int:
    data = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.is_file() else {}
    if "--update" in argv:
        concepts = data.get("tables", {})
        data["crossings"] = sorted(crossings())
        data["tables"] = {t: concepts.get(t, "UNMAPPED - add the §13 concept") for t in tables()}
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {BASELINE.relative_to(ROOT)}: {len(data['crossings'])} crossings, {len(data['tables'])} tables")
        return 0
    new = sorted(crossings() - set(data.get("crossings", [])))
    print("\n".join(new) or "no new boundary crossings")
    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
