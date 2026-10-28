"""A test that asserts a FIXED time bound must state its margin (LESSONS L15).

A fixed bound measures the machine, not the code. On 2026-10-28 three timing tests failed with correct code: a
200 ms wall-clock bound under load, a 0.5 s CPU bound on the Intel macOS CI runner (0.54 s there, 0.13 s on arm64)
and a bite test whose 0.3 ms runs were pure noise. This test AST-scans every ``tests/**/*.py`` file and fails on an
``assert`` that compares a measured time with a fixed number unless the assert carries a comment

    # timing-cap: <margin, at least 10 characters>

on its first line or on the line directly above it, saying how far the bound is from the normal cost (for example
"10 s vs a typical 0.2 s start; only a hang reaches it"). Prefer a relative check (both sides measured, such as a
linear-growth ratio) over a fixed bound; a relative check needs no comment.

Definitions (the scanner below is the one implementation):

* timing call: ``time.perf_counter``, ``time.monotonic``, ``time.process_time``, ``time.time`` and their ``_ns``
  variants, also imported bare (``from time import perf_counter``) or through a module alias;
* timing value: an expression containing a timing call; a name assigned in the same function (or a scope enclosing
  it) from an expression containing a timing value, to a fixed point (``best = min(best, time.process_time() - t0)``
  marks ``best``); a name bound by calling a function of the same file whose return value contains a timing value
  (a timing helper such as ``_best_of``); for tuple unpacking only the element at the position where the helper's
  returned tuple holds the timing value (``proc, elapsed = _run(...)`` marks ``elapsed``, not ``proc``);
* violation: inside an ``assert`` (any comparison nested in its test, also inside ``and``/``or``), an ordering
  comparison (``<``, ``<=``, ``>``, ``>=``) whose one side contains a timing value and the other side none. When both
  sides contain one (a ratio or a deadline) it is relative and allowed; ``while``/``if`` conditions are not asserts.

The bite fixtures are strings written to ``tmp_path``, never into ``tests/``, so the scan of the repository does not
see them.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MESSAGE = "fixed timing bound without a '# timing-cap: <margin>' comment"
MIN_MARGIN_CHARS = 10

_TIMING_ATTRS = frozenset(
    name + suffix for name in ("perf_counter", "monotonic", "process_time", "time") for suffix in ("", "_ns")
)
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FILLING_METHODS = frozenset({"append", "extend", "insert", "add", "update", "appendleft", "setdefault"})
_CAP = re.compile(r"#\s*timing-cap:(?P<margin>.*)")


@dataclass
class _Returns:
    """What a function of the file returns: a timing value as a whole, or at these tuple positions."""

    whole: bool = False
    positions: frozenset[int] = frozenset()

    def timing(self) -> bool:
        return self.whole or bool(self.positions)


@dataclass
class _File:
    time_modules: set[str] = field(default_factory=set)
    bare_calls: set[str] = field(default_factory=set)
    returns: dict[str, _Returns] = field(default_factory=dict)

    # -- what counts as a timing value ------------------------------------------------------------------------

    def _helper(self, call: ast.Call) -> _Returns | None:
        func = call.func
        if isinstance(func, ast.Name):
            return self.returns.get(func.id)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}:
            return self.returns.get(func.attr)
        return None

    def _timing_call(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in self.bare_calls
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _TIMING_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id in self.time_modules
        )

    def contains(self, expr: ast.AST, names: set[str]) -> bool:
        for node in ast.walk(expr):
            if isinstance(node, ast.Name) and node.id in names:
                return True
            if isinstance(node, ast.Call):
                if self._timing_call(node):
                    return True
                helper = self._helper(node)
                if helper is not None and helper.timing():
                    return True
        return False

    # -- names a statement marks ------------------------------------------------------------------------------

    def _target_names(self, target: ast.expr, value: ast.expr, names: set[str]) -> Iterator[str]:
        """The names an assignment of ``value`` (which holds a timing value) to ``target`` marks."""
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(target, (ast.Tuple, ast.List)) and not any(isinstance(e, ast.Starred) for e in target.elts):
            if (
                isinstance(value, (ast.Tuple, ast.List))
                and len(value.elts) == len(target.elts)
                and not any(isinstance(e, ast.Starred) for e in value.elts)
            ):
                for elt_target, elt_value in zip(target.elts, value.elts, strict=True):
                    if self.contains(elt_value, names):
                        yield from self._target_names(elt_target, elt_value, names)
                return
            helper = self._helper(value) if isinstance(value, ast.Call) else None
            if helper is not None and not helper.whole:
                for position, elt in enumerate(target.elts):
                    if position in helper.positions:
                        yield from _bound_names(elt)
                return
        yield from _bound_names(target)

    def marked(self, node: ast.AST, names: set[str]) -> Iterator[str]:
        if isinstance(node, ast.Assign):
            if self.contains(node.value, names):
                for target in node.targets:
                    yield from self._target_names(target, node.value, names)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if node.value is not None and self.contains(node.value, names):
                yield from self._target_names(node.target, node.value, names)
        elif isinstance(node, ast.NamedExpr):
            if self.contains(node.value, names):
                yield node.target.id
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            if self.contains(node.iter, names):
                yield from _bound_names(node.target)
        elif isinstance(node, ast.Call):
            # samples.append(time.perf_counter() - t0) fills ``samples`` with timing values
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _FILLING_METHODS
                and isinstance(func.value, ast.Name)
                and any(self.contains(arg, names) for arg in [*node.args, *(kw.value for kw in node.keywords)])
            ):
                yield func.value.id

    def fixed_point(self, nodes: list[ast.AST], seed: set[str]) -> set[str]:
        names = set(seed)
        changed = True
        while changed:
            changed = False
            for node in nodes:
                for name in self.marked(node, names):
                    if name not in names:
                        names.add(name)
                        changed = True
        return names


def _bound_names(target: ast.expr) -> Iterator[str]:
    """The names a target binds (``a``, ``a, *b``) or fills (``a[key]`` fills ``a``); an attribute binds none."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _bound_names(elt)
    elif isinstance(target, ast.Subscript):
        yield from _bound_names(target.value)


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node of this scope; a nested function or class is listed, its body is not."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        found.append(node)
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return found


def _read_imports(tree: ast.Module, info: _File) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            info.time_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time" and node.level == 0:
            info.bare_calls.update(alias.asname or alias.name for alias in node.names if alias.name in _TIMING_ATTRS)


def _read_returns(tree: ast.Module, info: _File) -> None:
    """Which functions of the file return a timing value, to a fixed point (a helper may call another)."""
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    changed = True
    while changed:
        changed = False
        for function in functions:
            nodes = _own_nodes(function)
            names = info.fixed_point(nodes, set())
            whole = False
            positions: set[int] = set()
            for node in nodes:
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                if isinstance(node.value, ast.Tuple):
                    positions.update(i for i, elt in enumerate(node.value.elts) if info.contains(elt, names))
                elif info.contains(node.value, names):
                    whole = True
            old = info.returns.get(function.name, _Returns())
            new = _Returns(old.whole or whole, old.positions | frozenset(positions))
            if new != old:
                info.returns[function.name] = new
                changed = True


def _cap_comments(source: str) -> dict[int, str]:
    """Line number -> the margin stated by a '# timing-cap:' comment token on that line."""
    caps: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            match = _CAP.match(token.string)
            if match:
                caps[token.start[0]] = match.group("margin").strip()
    return caps


def _fixed_bound(test: ast.expr, info: _File, names: set[str]) -> bool:
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, operands, operands[1:], strict=False):
            if isinstance(op, _ORDERING) and info.contains(left, names) != info.contains(right, names):
                return True
    return False


def _verdicts(nodes: list[ast.AST], info: _File, names: set[str], seed: set[str]) -> set[str]:
    """Names that hold the result of a fixed-bound comparison made outside an assert, to a fixed point.

    ``slow = {p: v for p, v in results.items() if v["p95_ms"] > CEILING_MS}`` then ``assert not slow`` is the
    same fixed bound as ``assert v["p95_ms"] <= CEILING_MS``, only moved out of the assert.
    """
    verdicts = set(seed)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                if node.value is None:
                    continue
                uses_verdict = any(isinstance(n, ast.Name) and n.id in verdicts for n in ast.walk(node.value))
                if not uses_verdict and not _fixed_bound(node.value, info, names):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in _bound_names(target):
                        if name not in verdicts:
                            verdicts.add(name)
                            changed = True
    return verdicts


def _scan_scope(scope: ast.AST, seed: set[str], verdict_seed: set[str], info: _File, hits: set[int]) -> None:
    nodes = _own_nodes(scope)
    names = info.fixed_point(nodes, seed)
    verdicts = _verdicts(nodes, info, names, verdict_seed)
    for node in nodes:
        if isinstance(node, ast.Assert) and (
            _fixed_bound(node.test, info, names)
            or any(isinstance(n, ast.Name) and n.id in verdicts for n in ast.walk(node.test))
        ):
            hits.add(node.lineno)
        elif isinstance(node, _SCOPES):
            _scan_scope(node, names, verdicts, info, hits)


def scan_source(source: str, filename: str) -> list[int]:
    """Line numbers of the asserts in ``source`` that compare a timing value with a fixed bound undeclared."""
    if "time" not in source:
        return []  # no timing call can be spelled without the time module
    tree = ast.parse(source, filename)
    info = _File()
    _read_imports(tree, info)
    if not info.time_modules and not info.bare_calls:
        return []
    _read_returns(tree, info)
    hits: set[int] = set()
    _scan_scope(tree, set(), set(), info, hits)
    caps = _cap_comments(source)
    declared = {
        line for line in hits if any(len(caps.get(n, "")) >= MIN_MARGIN_CHARS for n in (line, line - 1))
    }
    return sorted(hits - declared)


def find_fixed_timing_bounds(directory: Path, relative_to: Path) -> list[str]:
    """Every undeclared fixed timing bound in the ``*.py`` files under ``directory``, as '<path>:<line>: <msg>'."""
    findings: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(relative_to).as_posix()
        findings.extend(f"{rel}:{line}: {MESSAGE}" for line in scan_source(source, rel))
    return findings


# -- the repository ----------------------------------------------------------------------------------------------


def test_every_fixed_timing_bound_in_tests_states_its_margin() -> None:
    assert (ROOT / "tests" / "unit" / Path(__file__).name).is_file()  # the scan really covers this tree
    findings = find_fixed_timing_bounds(ROOT / "tests", ROOT)
    assert findings == [], "\n".join(findings)


# -- bite tests: fixtures are written to tmp_path, never into tests/ ---------------------------------------------

PLAIN = '''\
import time


def test_work_is_fast():
    t0 = time.perf_counter()
    sum(range(1000))
    took = time.perf_counter() - t0
    assert took < 0.5
'''

BEST_OF = '''\
import time


def _best_of(fn, runs=5):
    best = float("inf")
    for _ in range(runs):
        t0 = time.process_time()
        fn()
        best = min(best, time.process_time() - t0)
    return best


def test_cpu_is_low():
    took = _best_of(lambda: sum(range(1000)))
    assert took < 0.5
'''

TUPLE = '''\
import subprocess
import sys
import time


def _run(args):
    started = time.monotonic()
    proc = subprocess.run(args, check=False)
    return proc, time.monotonic() - started


def test_hook_is_fast():
    proc, elapsed = _run([sys.executable, "-c", "pass"])
    assert proc.returncode == 0
    assert proc.returncode <= 1
    assert elapsed < 10
'''

BOOLOP = '''\
import time


def test_no_grace():
    calls = 1
    start = time.monotonic()
    assert calls == 1 and time.monotonic() - start < 1.0
'''

BARE_AND_ALIAS = '''\
import time as _t
from time import perf_counter as pc


def test_bare():
    t0 = pc()
    assert pc() - t0 < 1


def test_alias():
    t0 = _t.monotonic_ns()
    assert _t.monotonic_ns() - t0 <= 10**9
'''

VERDICT = '''\
import time

CEILING_MS = 5000.0


def test_latency():
    timings = []
    for _ in range(3):
        t0 = time.perf_counter()
        timings.append((time.perf_counter() - t0) * 1000.0)
    slow = [t for t in timings if t > CEILING_MS]
    assert not slow
'''

GREEN = '''\
import time


def _grows_linearly(small, large):
    return large <= 8 * max(small, 0.02)


def test_declared_above():
    t0 = time.perf_counter()
    took = time.perf_counter() - t0
    # timing-cap: 20x the measured 0.5 s; only a hang reaches it
    assert took < 10


def test_declared_on_the_line():
    t0 = time.perf_counter()
    assert time.perf_counter() - t0 < 10  # timing-cap: 20x the measured 0.5 s; only a hang reaches it


def test_ratio_and_deadline():
    t0 = time.perf_counter()
    small = time.perf_counter() - t0
    t1 = time.perf_counter()
    took = time.perf_counter() - t1
    assert took <= 8 * max(small, 0.02)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if time.monotonic() > deadline - 4:
            break
    assert time.monotonic() < deadline + 60
    assert _grows_linearly(small, took)
    assert len("abc") < 10
'''

SHORT_CAP = '''\
import time


def test_short_cap():
    t0 = time.perf_counter()
    took = time.perf_counter() - t0
    # timing-cap: 10x
    assert took < 0.5


def test_cap_two_lines_up():
    t0 = time.perf_counter()
    # timing-cap: 20x the measured 0.5 s; only a hang reaches it
    took = time.perf_counter() - t0
    assert took < 0.5


def test_cap_in_a_string():
    t0 = time.perf_counter()
    note = "# timing-cap: 20x the measured 0.5 s; only a hang reaches it"
    assert time.perf_counter() - t0 < 0.5, note
'''


def _write(directory: Path, name: str, text: str) -> None:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _at(name: str, text: str, needle: str) -> str:
    """The finding the scanner must report for the (single) line of ``text`` that contains ``needle``."""
    [line] = [i for i, row in enumerate(text.splitlines(), start=1) if needle in row]
    return f"{name}:{line}: {MESSAGE}"


def test_bite_a_measured_duration_against_a_number_is_red_with_its_text(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_plain.py", PLAIN)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [
        "tests/unit/test_plain.py:8: fixed timing bound without a '# timing-cap: <margin>' comment"
    ]


def test_bite_a_best_of_helper_returning_cpu_time_is_red(tmp_path: Path) -> None:
    _write(tmp_path, "test_best_of.py", BEST_OF)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [_at("test_best_of.py", BEST_OF, "assert took < 0.5")]


def test_bite_the_timing_element_of_an_unpacked_tuple_is_red_and_its_sibling_is_not(tmp_path: Path) -> None:
    _write(tmp_path, "test_tuple.py", TUPLE)
    # proc.returncode == 0 and proc.returncode <= 1 are green: only the element the helper times is marked
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [_at("test_tuple.py", TUPLE, "assert elapsed < 10")]


def test_bite_a_bound_inside_and_is_red(tmp_path: Path) -> None:
    _write(tmp_path, "test_boolop.py", BOOLOP)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [_at("test_boolop.py", BOOLOP, "assert calls == 1")]


def test_bite_bare_imports_and_module_aliases_are_red(tmp_path: Path) -> None:
    _write(tmp_path, "test_bare.py", BARE_AND_ALIAS)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [
        _at("test_bare.py", BARE_AND_ALIAS, "assert pc() - t0 < 1"),
        _at("test_bare.py", BARE_AND_ALIAS, "assert _t.monotonic_ns()"),
    ]


def test_bite_a_bound_moved_out_of_the_assert_is_red(tmp_path: Path) -> None:
    _write(tmp_path, "test_verdict.py", VERDICT)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [_at("test_verdict.py", VERDICT, "assert not slow")]


def test_near_misses_declared_relative_polling_and_helpers_are_green(tmp_path: Path) -> None:
    _write(tmp_path, "test_green.py", GREEN)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == []


def test_bite_a_short_misplaced_or_quoted_cap_is_still_red(tmp_path: Path) -> None:
    _write(tmp_path, "test_short.py", SHORT_CAP)
    # a margin of fewer than 10 characters (line 8), a cap two lines up (line 15), a cap inside a string (line 21)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [
        f"test_short.py:8: {MESSAGE}",
        f"test_short.py:15: {MESSAGE}",
        f"test_short.py:21: {MESSAGE}",
    ]
