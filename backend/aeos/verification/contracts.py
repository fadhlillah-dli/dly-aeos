"""Contract-impact checks (§16 Backend/API "contract", Solution Architect "Perubahan kontrak berdampak tinggi masuk
keputusan/review", §22 "API contract ... disepakati", §31 G3 "contract"; W6-045).

``contract_diff`` compares the change's contracts with the recorded base commit, by the verifier itself:

* **facade signatures** (``**/facade.py``, AGENTS.md "frozen cross-module contracts - keep signatures stable"):
  parsed from the base and head sources (syntax tree; no code runs). Removed public names, removed parameters,
  new required parameters, lost defaults or removed dataclass fields are BREAKING; added names and added optional
  parameters are compatible;
* **OpenAPI**: for the DLY AEOS layout the base and head apps generate their documents (candidate code, through the
  verifier runner); a changed ``openapi*.json|yaml`` file is parsed statically. Removed operations, newly required
  parameters and newly required request bodies are BREAKING; response schemas are not analysed (stated);
* other contract formats (proto, GraphQL, JSON schema, AsyncAPI) are listed as changed and NOT analysed.

A breaking change is never decided by the verifier: it is a blocking NOT-PROVEN that needs the human-approved
TaskSpec to say ``verifier_policy.contract_breaking: approved`` (then it is stated, non-blocking).

``contract_tests`` selects the trusted tests that exercise a changed facade (trusted test sources referencing the
module) and reports their executed outcome from the trusted run.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aeos.verification import scope
from aeos.verification.steps import (
    StepContext,
    StepEvidence,
    StepResult,
    last_json_line,
    outcomes_for,
    pack_text,
)

OPENAPI_FILE = re.compile(r"(^|/)(openapi|swagger)[\w.-]*\.(json|ya?ml)$", re.IGNORECASE)
FACADE_FILE = re.compile(r"(^|/)facade\.py$")
_PATH_PARAM = re.compile(r"\{[^}]+\}")
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


# ------------------------------------------------------------------------------------------------ facades


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # posonly | pos | vararg | kwonly | varkw
    has_default: bool


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Param]:
    a = fn.args
    out: list[Param] = []
    positional = [*a.posonlyargs, *a.args]
    first_default = len(positional) - len(a.defaults)
    for i, arg in enumerate(positional):
        out.append(Param(arg.arg, "posonly" if i < len(a.posonlyargs) else "pos", i >= first_default))
    if a.vararg is not None:
        out.append(Param(a.vararg.arg, "vararg", True))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=False):
        out.append(Param(arg.arg, "kwonly", default is not None))
    if a.kwarg is not None:
        out.append(Param(a.kwarg.arg, "varkw", True))
    return out


def public_surface(source: str) -> dict[str, Any]:
    """Public names of a facade module: functions (params), classes (methods, fields), constants."""
    surface: dict[str, Any] = {}
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            surface[node.name] = {"kind": "function", "params": _params(node)}
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            surface[node.name] = {"kind": "class"}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        not item.name.startswith("_") or item.name == "__init__"):
                    surface[f"{node.name}.{item.name}"] = {"kind": "method", "params": _params(item)}
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    surface[f"{node.name}.{item.target.id}"] = {"kind": "field", "has_default": item.value is not None}
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    surface[t.id] = {"kind": "constant"}
    return surface


def compare_params(old: list[Param], new: list[Param]) -> list[str]:
    """Why calls valid against *old* may fail against *new* (empty: compatible)."""
    problems: list[str] = []
    old_pos = [p for p in old if p.kind in ("posonly", "pos")]
    new_pos = [p for p in new if p.kind in ("posonly", "pos")]
    new_by_name = {p.name: p for p in new}
    has_varkw = any(p.kind == "varkw" for p in new)
    has_vararg = any(p.kind == "vararg" for p in new)
    for i, p in enumerate(old_pos):
        if i >= len(new_pos):
            if not has_vararg:
                problems.append(f"positional parameter '{p.name}' removed")
            continue
        if new_pos[i].name != p.name and p.kind == "pos":
            problems.append(f"positional parameter {i} renamed/reordered '{p.name}' -> '{new_pos[i].name}'")
        if p.has_default and not new_pos[i].has_default:
            problems.append(f"parameter '{new_pos[i].name}' lost its default")
    for p in new_pos[len(old_pos):]:
        if not p.has_default:
            problems.append(f"new required positional parameter '{p.name}'")
    for p in old:
        if p.kind == "kwonly":
            q = new_by_name.get(p.name)
            if q is None or q.kind in ("posonly", "vararg"):
                if not has_varkw:
                    problems.append(f"keyword parameter '{p.name}' removed")
            elif p.has_default and not q.has_default:
                problems.append(f"keyword parameter '{p.name}' lost its default")
        if p.kind == "vararg" and not has_vararg:
            problems.append(f"*{p.name} removed")
        if p.kind == "varkw" and not has_varkw:
            problems.append(f"**{p.name} removed")
    old_names = {p.name for p in old}
    for p in new:
        if p.kind == "kwonly" and not p.has_default and p.name not in old_names:
            problems.append(f"new required keyword parameter '{p.name}'")
    return problems


def compare_surfaces(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    breaking: list[str] = []
    for name, spec in sorted(old.items()):
        cur = new.get(name)
        if cur is None:
            breaking.append(f"{name}: removed")
            continue
        if spec["kind"] in ("function", "method") and cur.get("kind") in ("function", "method"):
            breaking += [f"{name}: {p}" for p in compare_params(spec["params"], cur["params"])]
        elif spec["kind"] != cur.get("kind"):
            breaking.append(f"{name}: {spec['kind']} became {cur.get('kind')}")
    added = sorted(n for n in new if n not in old)
    new_required_fields = [f"{n}: new field without default" for n in added
                           if new[n].get("kind") == "field" and not new[n].get("has_default")
                           and n.split(".", 1)[0] in old]
    return {"breaking": breaking + new_required_fields, "added": added}


# ------------------------------------------------------------------------------------------------ OpenAPI


def openapi_operations(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ops: dict[str, dict[str, Any]] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = [p for p in item.get("parameters") or [] if isinstance(p, dict)]
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            params = [*shared, *[p for p in op.get("parameters") or [] if isinstance(p, dict)]]
            key = f"{method.lower()} {_PATH_PARAM.sub('{}', str(path))}"
            body = op.get("requestBody") if isinstance(op.get("requestBody"), dict) else None
            ops[key] = {
                "required": sorted(f"{p.get('in')}:{p.get('name')}" for p in params
                                   if p.get("required") and p.get("in") != "path"),
                "params": sorted(f"{p.get('in')}:{p.get('name')}" for p in params),
                "body_required": bool(body and body.get("required")),
            }
    return ops


def compare_openapi(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    a, b = openapi_operations(old), openapi_operations(new)
    breaking = [f"{k}: operation removed" for k in sorted(set(a) - set(b))]
    for k in sorted(set(a) & set(b)):
        newly = sorted(set(b[k]["required"]) - set(a[k]["required"]))
        if newly:
            breaking.append(f"{k}: newly required parameter(s) {', '.join(newly)}")
        if b[k]["body_required"] and not a[k]["body_required"]:
            breaking.append(f"{k}: request body newly required")
    return {"breaking": breaking, "added": sorted(set(b) - set(a)), "operations_base": len(a),
            "operations_head": len(b)}


def _load_doc(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            import yaml

            data = yaml.safe_load(text)
    except Exception:  # unparsable document: reported as not analysable
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------------------------------------ steps


def _generated_openapi(ctx: StepContext, tree: Path, name: str) -> tuple[dict[str, Any] | None, str]:
    rn = ctx.runner
    out = ctx.workdir / "r" / f"openapi-{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    res = rn.python(["-c", pack_text("openapi_dump.py"), rn.to_container(str(out))], pythonpath=tree / "backend",
                    autoload=False, timeout=min(ctx.timeout, 300.0))
    status = last_json_line(res.stdout)
    if not status.get("ok") or not out.is_file():
        return None, str(status.get("error") or f"no document (exit {res.returncode}, timed out {res.timed_out})")
    return _load_doc(out), ""


def run_contract_diff(ctx: StepContext) -> StepResult | None:
    contract_paths = list(ctx.categories.get("contract", []))
    api_paths = list(ctx.categories.get("api", []))
    if not contract_paths and not api_paths:
        return None
    base = ctx.base_tree()
    findings: dict[str, Any] = {}
    breaking: list[str] = []
    stated: list[str] = []
    analysed = 0
    for path in sorted(p for p in contract_paths if FACADE_FILE.search(p)):
        head_file = ctx.snapshot.joinpath(*path.split("/"))
        base_file = base.joinpath(*path.split("/")) if base is not None else None
        try:
            new = public_surface(head_file.read_text(encoding="utf-8")) if head_file.is_file() else {}
            old = (public_surface(base_file.read_text(encoding="utf-8"))
                   if base_file is not None and base_file.is_file() else None)
        except (SyntaxError, ValueError, OSError) as exc:
            breaking.append(f"{path}: facade not parseable ({type(exc).__name__})")
            continue
        if old is None:
            if base is None:
                stated.append(f"{path}: base commit not materialised; facade diff NOT-PROVEN")
                continue
            findings[path] = {"added": sorted(new), "breaking": [], "note": "new facade"}
            analysed += 1
            continue
        diff = compare_surfaces(old, new)
        findings[path] = diff
        breaking += [f"{path}: {b}" for b in diff["breaking"]]
        analysed += 1
    platform_api = (ctx.snapshot / "backend" / "aeos" / "api" / "app.py").is_file()
    if platform_api and (api_paths or any(p.startswith("backend/aeos/api/") for p in contract_paths)
                         or any(FACADE_FILE.search(p) for p in contract_paths)):
        head_doc, head_err = _generated_openapi(ctx, ctx.snapshot, "head")
        base_doc, base_err = (_generated_openapi(ctx, base, "base") if base is not None
                              and (base / "backend" / "aeos" / "api" / "app.py").is_file() else (None, "no base app"))
        if head_doc is None and base_doc is not None:
            breaking.append(f"OpenAPI: the head app no longer produces its document ({head_err})")
        elif head_doc is None or base_doc is None:
            stated.append(f"OpenAPI diff NOT-PROVEN: {head_err or base_err}")
        else:
            diff = compare_openapi(base_doc, head_doc)
            findings["openapi (generated)"] = diff
            breaking += [f"OpenAPI {b}" for b in diff["breaking"]]
            analysed += 1
    for path in sorted(p for p in contract_paths if OPENAPI_FILE.search(p)):
        head_file = ctx.snapshot.joinpath(*path.split("/"))
        base_file = base.joinpath(*path.split("/")) if base is not None else None
        new_doc = _load_doc(head_file) if head_file.is_file() else {}
        old_doc = _load_doc(base_file) if base_file is not None and base_file.is_file() else {}
        if new_doc is None or old_doc is None or (base is None):
            stated.append(f"{path}: OpenAPI document not parseable or base missing; diff NOT-PROVEN")
            continue
        diff = compare_openapi(old_doc, new_doc)
        findings[path] = diff
        breaking += [f"{path}: {b}" for b in diff["breaking"]]
        analysed += 1
    other = [p for p in contract_paths if not FACADE_FILE.search(p) and not OPENAPI_FILE.search(p)]
    if other:
        stated.append(f"contract file(s) changed but not analysed for breaking changes (format not supported by "
                      f"the verifier): {', '.join(other[:5])}")
    approved = str(ctx.policy.get("contract_breaking") or "").strip().lower() == "approved"
    content = {"suite": "contract-diff", "base_commit_tree": "materialised" if base is not None else "missing",
               "findings": findings, "breaking": breaking[:200], "not_analysed": stated,
               "breaking_approved_by_taskspec": approved,
               "note": "response schemas and runtime behaviour are not analysed by the diff"}
    ev = StepEvidence("STATIC_ANALYSIS", f"Contract diff against the base: {len(breaking)} breaking change(s)",
                      content, "E1", "contract_diff")
    details = {"breaking": breaking[:50], "analysed": analysed, "not_analysed": stated[:10]}
    if breaking:
        msg = (f"breaking contract change(s): {'; '.join(breaking[:3])} - a high-impact contract change needs an "
               "owner/architecture decision (§16); ")
        if approved:
            msg += "stated (TaskSpec verifier_policy.contract_breaking=approved)"
            return StepResult("contract_diff", "NOT_PROVEN", msg, details, [ev], not_proven=[msg, *stated])
        msg += ("blocking until the approved TaskSpec sets verifier_policy.contract_breaking=approved (the planner's "
                "TaskSpec contract v1 cannot carry that approval yet: a planner-compiled task stays blocked)")
        return StepResult("contract_diff", "NOT_PROVEN", msg, details, [ev], not_proven=[msg, *stated], blocking=[msg])
    if not analysed:
        msg = stated[0] if stated else (
            f"api/contract change ({', '.join((contract_paths + api_paths)[:3])}): no facade, OpenAPI document or "
            "generated app document to diff; contract compatibility NOT-PROVEN")
        return StepResult("contract_diff", "NOT_PROVEN", msg, details, [ev], not_proven=stated or [msg])
    return StepResult("contract_diff", "PASS", f"{analysed} contract surface(s) compatible with the base "
                                               "(additive or unchanged)", details, [ev], not_proven=stated)


def _module_of(path: str) -> str | None:
    """``backend/aeos/x/facade.py`` -> ``aeos.x.facade`` (platform layout)."""
    if not path.startswith("backend/") or not path.endswith(".py"):
        return None
    return path[len("backend/"):-3].replace("/", ".")


def run_contract_tests(ctx: StepContext) -> StepResult | None:
    facades = [p for p in ctx.categories.get("contract", []) if FACADE_FILE.search(p)]
    if not facades:
        return None
    if ctx.trusted_copy is None:
        return None
    sources = sorted((ctx.trusted_copy / "tests").rglob("test_*.py"))
    rows: list[dict[str, Any]] = []
    stated: list[str] = []
    reasons: list[str] = []
    for path in facades:
        module = _module_of(path)
        if module is None:
            stated.append(f"{path}: not a platform-layout module; trusted contract tests not selected")
            continue
        pkg, _, leaf = module.rpartition(".")
        needles = (module, f"from {pkg} import {leaf}", f"from {pkg} import ({leaf}")
        selected = []
        for src in sources:
            try:
                text = src.read_text(encoding="utf-8")
            except OSError:
                continue
            if any(n in text for n in needles):
                rel = src.relative_to(ctx.trusted_copy).as_posix()
                selected.append(rel[:-3].replace("/", "."))
        counts = outcomes_for(ctx.trusted_cases, [f"{m}::" for m in selected] + [f"{m}." for m in selected])
        rows.append({"contract": path, "trusted_modules": selected, **counts})
        if not selected or not counts["passed"]:
            stated.append(f"{path}: no trusted test exercises this contract; consumer compatibility NOT-PROVEN")
        if counts["failed"]:
            reasons.append(f"trusted contract tests of {path} failed ({counts['failed']})")
    status = "FAIL" if reasons else ("NOT_PROVEN" if stated else "PASS")
    summary = (reasons[0] if reasons else stated[0] if stated else
               f"trusted tests exercising {len(facades)} changed contract(s) passed")
    return StepResult("contract_tests", status, summary, {"contracts": rows}, reasons=reasons, not_proven=stated)


def changed_contract_kind(path: str) -> str | None:
    if FACADE_FILE.search(path):
        return "facade"
    if OPENAPI_FILE.search(path):
        return "openapi"
    return "other" if scope.first_match(path, scope.IMPACT_CATEGORIES["contract"], case_insensitive=True) else None
