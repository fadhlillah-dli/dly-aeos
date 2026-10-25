"""G2 controlled tests per change (§16 Backend/API "tes negatif", §31 G2, §22; W6-045/W6-104).

* **Negative tests** (``negative_tests``). The refusal tests the implementer added are found in the *syntax tree*
  of the changed test files - ``pytest.raises``/``assertRaises``, an asserted HTTP 4xx status, an asserted refusal
  code such as ``POLICY_DENIED`` - inside a test function whose lines the change added or modified (comments and
  strings outside assertions never count). Exactly those tests are then executed against the candidate head through
  the verifier runner: the check is PASS only when at least one ran and all passed. They stay implementer-authored
  (lower trust); the verifier's own cases come from ``aeos.verification.adversarial``.
* **Integration-marked tests** (``integration_tests``). Tests that need real tools are opted in by the TaskSpec
  (``verifier_policy.integration_tests: run|required``) and executed (``-m "integration and not network"``) on the
  trusted suite copy and the candidate's ``tests/integration``. A test that skips because its tool is absent is
  NOT-PROVEN, never a pass. They block the result when the TaskSpec says ``required``, when the change has ``api``
  impact, or for the Backend/API and Integration Engineer roles.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aeos.verification import scope
from aeos.verification.steps import StepContext, StepEvidence, StepResult, parse_report

TEST_FILE_PATTERNS = ("tests/**", "**/test_*.py", "**/*_test.py")
MARK_EXPR = "not integration and not network"
INTEGRATION_MARK_EXPR = "integration and not network"
INTEGRATION_ROLES = frozenset({"backend-api-engineer", "integration-engineer"})
_RAISES = frozenset({"raises", "assertRaises", "assertRaisesRegex", "assertRaisesMessage"})
_ASSERT_CALLS = frozenset({"assertEqual", "assertEquals", "assertIn", "assert_equal"})
_REFUSAL_WORDS = ("DENIED", "FORBIDDEN", "UNAUTHORIZED", "UNAUTHORISED", "UNAUTHENTICATED", "NOT_AUTHENTICATED",
                  "BLOCKED", "NOT_FOUND", "CONFLICT", "INVALID", "VALIDATION_FAILED", "REFUSED", "REJECTED",
                  "FAILED", "NOT_ALLOWED", "QUARANTINED", "READ_ONLY", "REQUIRED")
_CODE_SHAPE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
MAX_NEGATIVE_TESTS = 200


@dataclass
class NegativeTest:
    path: str  # repository-relative test file
    cls: str | None
    name: str
    kinds: list[str]

    @property
    def key(self) -> str:
        module = self.path[:-3].replace("/", ".") if self.path.endswith(".py") else self.path.replace("/", ".")
        return f"{module}.{self.cls}::{self.name}" if self.cls else f"{module}::{self.name}"

    def nodeid(self, snapshot: Path) -> str:
        base = str(snapshot.joinpath(*self.path.split("/")))
        return f"{base}::{self.cls}::{self.name}" if self.cls else f"{base}::{self.name}"


# ---------------------------------------------------------------------------------------------- detection


def _call_name(node: ast.Call) -> str:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")


def _is_4xx(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return 400 <= node.value < 500
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and node.elts:
        return all(_is_4xx(e) for e in node.elts)
    return False


def _is_status(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in ("status_code", "status")


def _is_refusal_code(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str) and bool(_CODE_SHAPE.match(node.value))
            and any(w in node.value for w in _REFUSAL_WORDS))


def _compare_kinds(node: ast.AST) -> set[str]:
    kinds: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Compare):
            operands = [sub.left, *sub.comparators]
            if any(_is_status(o) for o in operands) and any(_is_4xx(o) for o in operands):
                kinds.add("http-4xx")
            if any(_is_refusal_code(o) for o in operands):
                kinds.add("refusal-code")
    return kinds


def refusal_kinds(fn: ast.AST) -> set[str]:
    """Kinds of refusal assertion executed by one test function (syntax tree only, comments never count)."""
    kinds: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _RAISES:
                kinds.add("raises")
            elif name in _ASSERT_CALLS:
                if any(_is_status(a) for a in node.args) and any(_is_4xx(a) for a in node.args):
                    kinds.add("http-4xx")
                if any(_is_refusal_code(a) for a in node.args):
                    kinds.add("refusal-code")
        elif isinstance(node, ast.Assert):
            kinds |= _compare_kinds(node.test)
    return kinds


def _test_functions(tree: ast.Module) -> list[tuple[str | None, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out: list[tuple[str | None, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            out.append((None, node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    out.append((node.name, item))
    return out


def find_negative_tests(snapshot: Path, diff_text: str) -> list[NegativeTest]:
    """Added/modified Python test functions of the change that assert a refusal path."""
    added: dict[str, set[int]] = {}
    for al in scope.added_lines(diff_text):
        added.setdefault(al.path, set()).add(al.line)
    found: list[NegativeTest] = []
    for path in sorted(added):
        if not path.endswith(".py") or not scope.first_match(path, TEST_FILE_PATTERNS):
            continue
        name = path.rsplit("/", 1)[-1]
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        target = snapshot.joinpath(*path.split("/"))
        if not target.is_file():
            continue
        try:
            tree = ast.parse(target.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for cls, fn in _test_functions(tree):
            lines = set(range(fn.lineno, (fn.end_lineno or fn.lineno) + 1))
            if not lines & added[path]:
                continue
            kinds = refusal_kinds(fn)
            if kinds:
                found.append(NegativeTest(path=path, cls=cls, name=fn.name, kinds=sorted(kinds)))
            if len(found) >= MAX_NEGATIVE_TESTS:
                return found
    return found


# ---------------------------------------------------------------------------------------------- execution


def _matches(key: str, case_key: str) -> bool:
    return case_key == key or case_key.startswith(key + "[")


def run_negative_tests(ctx: StepContext, risky: list[str]) -> StepResult:
    """Detect and execute the change's refusal tests (TaskSpec ``negative_tests_required`` + api/auth impact)."""
    tests = find_negative_tests(ctx.snapshot, ctx.diff_text)
    label = ", ".join(risky[:5])
    if not tests:
        msg = (f"negative tests required for the changed API/auth surface ({label}): no added test asserts a "
               "refusal/error path in code (pytest.raises / 4xx status / refusal code)")
        return StepResult("negative_tests", "NOT_PROVEN", msg, not_proven=[msg], blocking=[msg])
    if ctx.ini is None:
        msg = "negative tests found but the verifier pytest configuration is missing; not executed"
        return StepResult("negative_tests", "NOT_PROVEN", msg, not_proven=[msg], blocking=[msg])
    rn = ctx.runner
    junit = ctx.workdir / "r" / "negative-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    args = ["-m", "pytest", *[t.nodeid(ctx.snapshot) for t in tests], "-c", str(ctx.ini), "--rootdir",
            str(ctx.snapshot), "--confcutdir", str(ctx.snapshot), "-p", "no:cacheprovider", "-q", "--junitxml",
            str(junit), "-m", MARK_EXPR, *ctx.basetemp("b4")]
    res = rn.python(args, pythonpath=ctx.snapshot / "backend", autoload=True, timeout=ctx.timeout)
    report, parse_error = parse_report(junit)
    rows: list[dict[str, Any]] = []
    passed = failed = missing = 0
    for t in tests:
        outcomes = [c.outcome for c in (report.cases if report else []) if _matches(t.key, c.key)]
        if not outcomes or any(o == "skipped" for o in outcomes):
            outcome, missing = ("skipped" if outcomes else "not-executed"), missing + 1
        elif any(o in ("failed", "error") for o in outcomes):
            outcome, failed = "failed", failed + 1
        else:
            outcome, passed = "passed", passed + 1
        rows.append({"test": t.key, "kinds": t.kinds, "outcome": outcome})
    content = {"suite": "candidate-negative-tests", "trust": "LOW - refusal tests authored with the candidate, "
               "executed by the verifier", "tests": rows[:200], "exit_code": res.returncode,
               "timed_out": res.timed_out, "duration_s": res.duration_s, "parse_error": parse_error,
               "output_tail": res.tail(4000)}
    ev = StepEvidence("TEST_REPORT", f"Negative tests added by the change, executed by the verifier: "
                                     f"{passed} passed, {failed} failed, {missing} not executed", content,
                      "E2", "negative_tests")
    details = {"tests": rows[:50], "risky_paths": risky[:20]}
    if res.timed_out:
        msg = f"negative tests timed out after {ctx.timeout:.0f}s"
        return StepResult("negative_tests", "FAIL", msg, details, [ev], reasons=[msg])
    if failed:
        bad = [r["test"] for r in rows if r["outcome"] == "failed"]
        msg = f"{failed} added negative/refusal test(s) failed against the head ({', '.join(bad[:5])})"
        return StepResult("negative_tests", "FAIL", msg, details, [ev], reasons=[msg])
    if missing or report is None:
        msg = (f"{missing} added negative/refusal test(s) were not executed or skipped "
               f"({parse_error or 'see report'}); refusal coverage NOT-PROVEN")
        return StepResult("negative_tests", "NOT_PROVEN", msg, details, [ev], not_proven=[msg], blocking=[msg])
    return StepResult("negative_tests", "PASS",
                      f"{passed} added negative/refusal test(s) executed and passed against the head (authored with "
                      "the candidate: lower trust)", details, [ev])


# ---------------------------------------------------------------------------------------------- integration


def integration_mode(policy: dict[str, Any]) -> str:
    raw = str(policy.get("integration_tests") or "").strip().lower()
    return raw if raw in ("run", "required") else ""


def run_integration_tests(ctx: StepContext, suites: list[str]) -> StepResult | None:
    """Integration-marked tests opted in by the TaskSpec; None when not opted in (the verifier states that)."""
    mode = integration_mode(ctx.policy)
    if not mode:
        return None
    blocking = mode == "required" or "api" in ctx.categories or ctx.role in INTEGRATION_ROLES
    why = ("TaskSpec integration_tests=required" if mode == "required" else
           "api impact" if "api" in ctx.categories else
           f"role {ctx.role}" if ctx.role in INTEGRATION_ROLES else "")
    if ctx.ini is None:
        msg = f"integration-marked tests ({mode}) not run: the verifier pytest configuration is missing"
        return StepResult("integration_tests", "NOT_PROVEN", msg, not_proven=[msg], blocking=[msg] if blocking else [])
    rn = ctx.runner
    runs: list[dict[str, Any]] = []
    targets: list[tuple[str, Path, Path]] = []
    if ctx.trusted_copy is not None and (ctx.trusted_copy / "tests" / "acceptance").is_dir():
        targets.append(("trusted-acceptance", ctx.trusted_copy / "tests" / "acceptance", ctx.trusted_copy))
    cand_dir = ctx.snapshot / "tests" / "integration"
    if "integration" in suites and cand_dir.is_dir():
        targets.append(("candidate-integration", cand_dir, ctx.snapshot))
    passed = failed = skipped = 0
    skip_reasons: list[str] = []
    for label, path, root in targets:
        junit = ctx.workdir / "r" / f"integration-{label}.xml"
        junit.parent.mkdir(parents=True, exist_ok=True)
        args = ["-m", "pytest", str(path), "-c", str(ctx.ini), "--rootdir", str(root), "--confcutdir", str(root),
                "-p", "no:cacheprovider", "-q", "-rs", "--junitxml", str(junit), "-m", INTEGRATION_MARK_EXPR,
                *ctx.basetemp("b5" if label.startswith("trusted") else "b6")]
        res = rn.python(args, pythonpath=ctx.snapshot / "backend", autoload=label.startswith("candidate"),
                        timeout=ctx.timeout)
        report, parse_error = parse_report(junit)
        entry: dict[str, Any] = {"suite": label, "exit_code": res.returncode, "timed_out": res.timed_out,
                                 "duration_s": res.duration_s, "parse_error": parse_error,
                                 "report": report.to_dict() if report else None, "output_tail": res.tail(3000)}
        runs.append(entry)
        if res.timed_out:
            failed += 1
            continue
        if report is None:
            if res.returncode != 5:  # 5 = nothing selected
                skip_reasons.append(f"{label}: no readable report ({parse_error})")
            continue
        passed += report.passed
        failed += report.failures + report.errors
        skipped += report.skipped
        skip_reasons += [f"{c.key}: {c.message[:120]}" for c in report.cases if c.outcome == "skipped"][:20]
    content = {"suite": "integration-marked", "mode": mode, "blocking": blocking, "blocking_because": why,
               "mark_expr": INTEGRATION_MARK_EXPR, "runs": runs}
    ev = StepEvidence("TEST_REPORT", f"Integration-marked tests ({mode}): {passed} passed, {failed} failed, "
                                     f"{skipped} skipped", content, "E2", "integration_tests")
    details = {"passed": passed, "failed": failed, "skipped": skipped, "mode": mode, "blocking": blocking,
               "skipped_tests": skip_reasons[:20]}
    if failed:
        msg = f"integration-marked tests: {failed} failed"
        return StepResult("integration_tests", "FAIL", msg, details, [ev], reasons=[msg])
    if passed and not skipped and not skip_reasons:
        return StepResult("integration_tests", "PASS", f"{passed} integration-marked test(s) passed with real tools",
                          details, [ev])
    msg = (f"integration-marked tests ({mode}) not proven: {passed} passed, {skipped} skipped (tool absent on this "
           f"host)" + (f"; {skip_reasons[0]}" if skip_reasons else "") +
           (f"; blocking ({why})" if blocking else "; non-blocking"))
    return StepResult("integration_tests", "NOT_PROVEN", msg, details, [ev], not_proven=[msg],
                      blocking=[msg] if blocking else [])
