"""Human-only merge/deploy (U-09, §31; §50 "Agent mencoba direct push main/merge API"): the delivery
and verification code has no merge, deploy, force-push or branch-deletion operation, and the GitHub
adapter only issues GET requests. Static AST guard over the module sources."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import aeos.delivery
import aeos.verification

PACKAGES = [Path(aeos.delivery.__file__).parent, Path(aeos.verification.__file__).parent]
FORBIDDEN_STRINGS = re.compile(
    r"/merges?\b|/pulls/\{[^}]*\}/merge|/deployments\b|/dispatches\b|\bgh\s+pr\s+merge\b|--force\b|refs/heads/main\b"
)
FORBIDDEN_CONSTANTS = {"merge", "push", "rebase", "reset", "deploy", "PUT", "DELETE", "PATCH"}
FORBIDDEN_CALLS = {"merge_pull_request", "create_deployment", "merge", "delete_branch", "force_push"}
HTTP_WRITE_METHODS = {"post", "put", "patch", "delete"}


def _sources():
    for pkg in PACKAGES:
        for path in sorted(pkg.rglob("*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def test_no_merge_or_deploy_operations_in_delivery_and_verification():
    problems = []
    for path, tree in _sources():
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                if node.value in FORBIDDEN_CONSTANTS:
                    problems.append(f"{path.name}:{node.lineno} constant {node.value!r}")
                if FORBIDDEN_STRINGS.search(node.value):
                    problems.append(f"{path.name}:{node.lineno} string {node.value[:60]!r}")
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if name in FORBIDDEN_CALLS:
                    problems.append(f"{path.name}:{node.lineno} call {name}()")
    assert not problems, problems


def test_github_adapter_is_get_only():
    path = Path(aeos.delivery.__file__).parent / "provider.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    writes = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and (node.func.attr in HTTP_WRITE_METHODS or node.func.attr == "request")):
            writes.append(f"line {node.lineno}: .{node.func.attr}()")
    assert not writes, writes


def test_api_has_no_merge_or_deploy_route():
    from aeos.delivery.router import router as delivery_router
    from aeos.verification.router import router as verification_router

    for r in [*delivery_router.routes, *verification_router.routes]:
        path = getattr(r, "path", "")
        assert "merge" not in path and "deploy" not in path and "release" not in path, path


def test_cli_has_no_merge_or_deploy_command():
    from aeos.delivery.cli import app as delivery_cli
    from aeos.verification.cli import app as verify_cli

    for app in (delivery_cli, verify_cli):
        for cmd in app.registered_commands:
            name = cmd.name or cmd.callback.__name__
            assert "merge" not in name and "deploy" not in name, name
