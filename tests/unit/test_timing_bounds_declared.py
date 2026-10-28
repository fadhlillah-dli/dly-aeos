"""A test that asserts a FIXED time bound must state its margin (LESSONS L15).

A fixed bound measures the machine, not the code. On 2026-10-28 three timing tests failed with correct code: a
200 ms wall-clock bound under load, a 0.5 s CPU bound on the Intel macOS CI runner (0.54 s there, 0.13 s on arm64)
and a bite test whose 0.3 ms runs were pure noise. This test AST-scans every ``tests/**/*.py`` file and fails on an
``assert`` that compares a measured time with a fixed number unless the assert carries a comment

    # timing-cap: <margin, at least 10 characters>

on its first line or on the line directly above it, saying how far the bound is from the normal cost (for example
"10 s vs a typical 0.2 s start; only a hang reaches it"). Prefer a relative check (a measured time against a
multiple of another, such as a linear-growth ratio) over a fixed bound; a relative check needs no comment.

Definitions (the scanner below is the one implementation):

* clock: ``time.perf_counter``, ``time.monotonic``, ``time.process_time``, ``time.time`` and their ``_ns``
  variants, imported bare (``from time import perf_counter`` or ``*``) or through a module alias, called or passed
  around uncalled (``clock = time.perf_counter``, a parameter default ``clock=time.monotonic``);
* timing value: an expression that holds a clock reading. Also, to a fixed point over the whole file: a name, a
  dotted attribute (``box.took``) or a container assigned or filled from one in the same function or a scope
  enclosing it (``best = min(best, time.process_time() - t0)`` marks ``best``); an attribute that a class of the
  file sets on ``self`` from one (``t.elapsed`` after ``with Timer() as t``); the result of a function of the file
  (also a nested one, a method, a fixture parameter of that name or a ``with`` target) that returns or yields one,
  and for a returned tuple only the element at the position that holds it (``proc, elapsed = _run(...)`` marks
  ``elapsed``, not ``proc``); a parameter of a function of the file that a call passes one to
  (``_assert_fast(took)``); a result attribute or string key named like a measured time (``res.duration_s``,
  ``rr.duration_ms``, ``run["elapsed"]``: the clock was read in product code, the bound in the test is still fixed);
* violation: inside an ``assert`` (any comparison nested in its test, also inside ``and``/``or``), an ordering
  comparison (``<``, ``<=``, ``>``, ``>=``) that bounds a measured time by a constant. Either one side holds a
  timing value and the other none, unless that side is a ratio of two timing values (``large / small < 8``); or
  both sides hold one, neither is scaled by a factor nor a ratio, and one is shifted by a term that is not measured:
  ``t1 < t0 + 0.5`` and ``time.monotonic() < deadline`` with ``deadline = time.monotonic() + 5`` are a fixed bound
  on a difference. ``took <= 8 * max(small, 0.02)`` and ``t0 <= t1`` are relative. A comparison made outside the
  assert counts when the assert reads its result (``slow = [...]`` then ``assert not slow``; a helper that returns
  ``elapsed < 0.5``). ``while``/``if`` conditions (deadline polling) are not asserts.

The bite fixtures are strings written to ``tmp_path``, never into ``tests/``, so the scan of the repository does not
see them.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TypeAlias, TypeVar

ROOT = Path(__file__).resolve().parents[2]

MESSAGE = "fixed timing bound without a '# timing-cap: <margin>' comment"
MIN_MARGIN_CHARS = 10

_TIMING_ATTRS = frozenset(
    name + suffix for name in ("perf_counter", "monotonic", "process_time", "time") for suffix in ("", "_ns")
)
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# samples.append(took) and heapq.heappush(samples, took) fill ``samples`` with timing values
_FILLING_METHODS = frozenset({"append", "extend", "insert", "add", "update", "appendleft", "setdefault"})
_FILLING_FUNCTIONS = frozenset({"heappush", "heappushpop", "heapreplace", "insort", "insort_left", "insort_right"})
# builtins whose result is a count, a type or a text, never a time: len(samples) counts timing values
_NOT_A_TIME = frozenset({"len", "isinstance", "issubclass", "callable", "type", "id", "bool", "str", "repr", "format"})
# a result attribute or string key named like a measured time (res.duration_s): the clock was read in product code
_MEASURED_NAME = re.compile(r"(?:^|_)(?:duration|elapsed|latency|took)(?:_|$)")
# a file without one of these words imports no clock and reads no measured name: nothing to analyse
_MENTIONS = re.compile(r"\btime\b|duration|elapsed|latency|took")
_CAP = re.compile(r"#\s*timing-cap:(?P<margin>.*)")
_MAX_ROUNDS = 50  # the facts only grow in a finite lattice; a file needs a handful of rounds

_Function: TypeAlias = ast.FunctionDef | ast.AsyncFunctionDef
_Key = TypeVar("_Key")


# -- the shape of a timing value ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shape:
    """How a timing value is built, which decides whether a bound on it is fixed or relative.

    ``shifted``: plus or minus a term that is not measured (``deadline = time.monotonic() + 5``), so a comparison
    with another measured value still bounds their difference by a constant. ``scaled``: multiplied or divided by a
    factor that is not measured (``8 * small``). ``ratio``: one measured value divided by another
    (``large / small``), which has no unit and so no machine in it.
    """

    shifted: bool = False
    scaled: bool = False
    ratio: bool = False


_PLAIN = _Shape()


def _join(a: _Shape | None, b: _Shape | None) -> _Shape | None:
    """Either of two values (a name assigned twice, two call sites): the shape that is flagged more often."""
    if a is None:
        return b
    if b is None:
        return a
    return _Shape(a.shifted or b.shifted, a.scaled and b.scaled, a.ratio and b.ratio)


def _put(table: dict[_Key, _Shape], key: _Key, shape: _Shape | None) -> bool:
    """Join ``shape`` into ``table[key]``; whether that changed the table."""
    old = table.get(key)
    new = _join(old, shape)
    if new is None or new == old:
        return False
    table[key] = new
    return True


def _arith(op: ast.operator, left: _Shape | None, right: _Shape | None) -> _Shape | None:
    if left is None and right is None:
        return None
    if left is not None and right is not None:
        if isinstance(op, (ast.Div, ast.FloorDiv)):
            return _Shape(ratio=True)
        if isinstance(op, (ast.Add, ast.Sub)):
            return _Shape(left.shifted or right.shifted, left.scaled or right.scaled, left.ratio and right.ratio)
        return _Shape(left.shifted or right.shifted, scaled=True)
    measured = left if left is not None else right
    assert measured is not None
    if isinstance(op, (ast.Add, ast.Sub)):
        return replace(measured, shifted=True)
    return replace(measured, scaled=True)  # took * 1000, total / len(samples), n / took


def _fixed(left: _Shape | None, right: _Shape | None) -> bool:
    """Whether an ordering comparison of these two sides bounds a measured time by a constant."""
    if left is None or right is None:
        measured = left if left is not None else right
        return measured is not None and not measured.ratio
    if left.scaled or right.scaled or left.ratio or right.ratio:
        return False  # relative: a multiple of another measured value, or a ratio
    return left.shifted or right.shifted  # t1 < t0 + 0.5 is t1 - t0 < 0.5


@dataclass
class _Returns:
    """What a function of the file returns or yields: a timing value as a whole or at tuple positions, a verdict."""

    whole: _Shape | None = None
    positions: dict[int, _Shape] = field(default_factory=dict)
    verdict: bool = False  # returns the result of a fixed-bound comparison (``return elapsed < 0.5``)

    def shape(self) -> _Shape | None:
        shape = self.whole
        for position in self.positions.values():
            shape = _join(shape, position)
        return shape


def _merged(a: _Returns, b: _Returns) -> _Returns:
    positions = dict(a.positions)
    for position, shape in b.positions.items():
        _put(positions, position, shape)
    return _Returns(_join(a.whole, b.whole), positions, a.verdict or b.verdict)


# -- syntax helpers ----------------------------------------------------------------------------------------------


def _dotted(node: ast.expr) -> str | None:
    """``box.took`` for an attribute chain on a name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _keys(target: ast.expr) -> Iterator[str]:
    """The keys a target binds (``a``, ``a, *b``, ``box.took``) or fills (``a[key]`` fills ``a``)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Attribute):
        key = _dotted(target)
        if key is not None:
            yield key
    elif isinstance(target, ast.Starred):
        yield from _keys(target.value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _keys(elt)
    elif isinstance(target, ast.Subscript):
        yield from _keys(target.value)


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _decorators(function: _Function) -> set[str]:
    names = (_name_of(d.func if isinstance(d, ast.Call) else d) for d in function.decorator_list)
    return {name for name in names if name is not None}


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


def _unstarred(elts: list[ast.expr]) -> bool:
    return not any(isinstance(elt, ast.Starred) for elt in elts)


# -- the analysis of one file ------------------------------------------------------------------------------------


@dataclass
class _Scope:
    env: dict[str, _Shape]  # names and dotted attributes that hold a timing value
    visible: dict[str, _Function]  # the functions of the file a bare call reaches from here


@dataclass
class _File:
    """What one file defines, learnt in whole-file rounds until nothing changes (a call may precede its def)."""

    time_modules: set[str] = field(default_factory=set)
    bare_calls: set[str] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)
    methods: dict[str, list[_Function]] = field(default_factory=dict)
    static: set[_Function] = field(default_factory=set)
    fixtures: set[_Function] = field(default_factory=set)
    returns: dict[_Function, _Returns] = field(default_factory=dict)
    params: dict[_Function, dict[str, _Shape]] = field(default_factory=dict)
    attrs: dict[str, _Shape] = field(default_factory=dict)  # set on ``self`` by a class of the file
    globals: dict[str, _Shape] = field(default_factory=dict)  # set by a function through ``global``
    changed: bool = False

    @classmethod
    def of(cls, tree: ast.Module) -> _File:
        info = cls()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                info.time_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "time")
            elif isinstance(node, ast.ImportFrom) and node.module == "time" and node.level == 0:
                for alias in node.names:
                    if alias.name == "*":
                        info.bare_calls.update(_TIMING_ATTRS)
                    elif alias.name in _TIMING_ATTRS:
                        info.bare_calls.add(alias.asname or alias.name)
            elif isinstance(node, ast.ClassDef):
                info.classes.add(node.name)
                for method in _own_nodes(node):
                    if isinstance(method, _FUNCTIONS):
                        info.methods.setdefault(method.name, []).append(method)
                        if "staticmethod" in _decorators(method):
                            info.static.add(method)
            elif isinstance(node, _FUNCTIONS) and "fixture" in _decorators(node):
                info.fixtures.add(node)
        return info

    # -- what counts as a timing value ------------------------------------------------------------------------

    def _callees(self, call: ast.Call, scope: _Scope) -> list[tuple[_Function, int]]:
        """The functions of the file this call reaches, each with the count of leading parameters it fills itself."""
        func = call.func
        if isinstance(func, ast.Name):
            function = scope.visible.get(func.id)
            return [] if function is None else [(function, 0)]
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (func.value.id in {"self", "cls"} or func.value.id in self.classes)
        ):
            return [(m, 0 if m in self.static else 1) for m in self.methods.get(func.attr, [])]
        return []

    def _helper(self, call: ast.Call, scope: _Scope) -> _Returns | None:
        callees = self._callees(call, scope)
        if not callees:
            return None
        found = _Returns()
        for function, _ in callees:
            found = _merged(found, self.returns.get(function, _Returns()))
        return found

    def _clock(self, node: ast.expr) -> bool:
        """A clock function itself, called or not (``time.perf_counter``, a bare ``perf_counter``)."""
        if isinstance(node, ast.Name):
            return node.id in self.bare_calls
        return (
            isinstance(node, ast.Attribute)
            and node.attr in _TIMING_ATTRS
            and isinstance(node.value, ast.Name)
            and node.value.id in self.time_modules
        )

    def shape(self, node: ast.AST, scope: _Scope) -> _Shape | None:
        """How ``node`` holds a timing value, or None when it holds none."""
        if isinstance(node, ast.Name):
            if node.id in scope.env:
                return scope.env[node.id]
            if self._clock(node):
                return _PLAIN
            function = scope.visible.get(node.id)  # map(_measure, sizes) passes a timing helper
            return None if function is None else self.returns.get(function, _Returns()).shape()
        if isinstance(node, ast.Attribute):
            key = _dotted(node)
            if key is not None and key in scope.env:
                return scope.env[key]
            if self._clock(node):
                return _PLAIN
            if node.attr in self.attrs:
                return self.attrs[node.attr]
            if _MEASURED_NAME.search(node.attr):
                return _PLAIN
            return self.shape(node.value, scope)
        if isinstance(node, ast.Subscript):
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str) and _MEASURED_NAME.search(index.value):
                return _PLAIN
            if isinstance(node.value, ast.Call) and isinstance(index, ast.Constant) and isinstance(index.value, int):
                found = self._helper(node.value, scope)
                if found is not None and found.whole is None:
                    return found.positions.get(index.value)  # _run(...)[0] is the process, not the time
            return self.shape(node.value, scope)
        if isinstance(node, ast.Call):
            return self._call_shape(node, scope)
        if isinstance(node, ast.BinOp):
            return _arith(node.op, self.shape(node.left, scope), self.shape(node.right, scope))
        if isinstance(node, (ast.Compare, ast.JoinedStr, ast.Constant)):
            return None  # a truth value, a text or a literal is not a time
        if isinstance(node, ast.UnaryOp):
            return None if isinstance(node.op, ast.Not) else self.shape(node.operand, scope)
        if isinstance(node, ast.Lambda):
            return self.shape(node.body, scope)
        if isinstance(node, ast.IfExp):
            return _join(self.shape(node.body, scope), self.shape(node.orelse, scope))
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return self.shape(node.elt, scope)
        if isinstance(node, ast.DictComp):
            return _join(self.shape(node.key, scope), self.shape(node.value, scope))
        shape: _Shape | None = None
        for child in ast.iter_child_nodes(node):
            shape = _join(shape, self.shape(child, scope))
        return shape

    def _call_shape(self, call: ast.Call, scope: _Scope) -> _Shape | None:
        if isinstance(call.func, ast.Name) and call.func.id in _NOT_A_TIME:
            return None
        found = self._helper(call, scope)
        if found is not None:
            return found.shape()
        shape = self.shape(call.func, scope)
        for arg in [*call.args, *(kw.value for kw in call.keywords)]:
            shape = _join(shape, self.shape(arg, scope))
        return shape

    # -- keys a statement marks -------------------------------------------------------------------------------

    def _assigned(
        self, target: ast.expr, value: ast.expr, shape: _Shape, scope: _Scope
    ) -> Iterator[tuple[str, _Shape]]:
        """The keys an assignment of ``value`` (which holds a timing value of ``shape``) to ``target`` marks."""
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(target, (ast.Tuple, ast.List)) and _unstarred(target.elts):
            if (
                isinstance(value, (ast.Tuple, ast.List))
                and len(value.elts) == len(target.elts)
                and _unstarred(value.elts)
            ):
                for elt_target, elt_value in zip(target.elts, value.elts, strict=True):
                    elt_shape = self.shape(elt_value, scope)
                    if elt_shape is not None:
                        yield from self._assigned(elt_target, elt_value, elt_shape, scope)
                return
            found = self._helper(value, scope) if isinstance(value, ast.Call) else None
            if found is not None and found.whole is None:
                for position, elt in enumerate(target.elts):
                    if position in found.positions:
                        yield from ((key, found.positions[position]) for key in _keys(elt))
                return
        yield from ((key, shape) for key in _keys(target))

    def _filled(self, call: ast.Call, scope: _Scope) -> Iterator[tuple[str, _Shape]]:
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in _FILLING_METHODS:
            container, values = func.value, [*call.args, *(kw.value for kw in call.keywords)]
        elif _name_of(func) in _FILLING_FUNCTIONS and call.args:
            container, values = call.args[0], call.args[1:]
        else:
            return
        shape: _Shape | None = None
        for value in values:
            shape = _join(shape, self.shape(value, scope))
        if shape is not None:
            yield from ((key, shape) for key in _keys(container))

    def marked(self, node: ast.AST, scope: _Scope) -> Iterator[tuple[str, _Shape]]:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            shape = None if node.value is None else self.shape(node.value, scope)
            if shape is not None and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    yield from self._assigned(target, node.value, shape, scope)
        elif isinstance(node, ast.AugAssign):
            before, value = self.shape(node.target, scope), self.shape(node.value, scope)
            # total += took starts an accumulator: its first value is no shift
            accumulates = before is None and isinstance(node.op, (ast.Add, ast.Sub))
            shape = value if accumulates else _arith(node.op, before, value)
            if shape is not None:
                yield from ((key, shape) for key in _keys(node.target))
        elif isinstance(node, ast.NamedExpr):
            shape = self.shape(node.value, scope)
            if shape is not None:
                yield node.target.id, shape
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            shape = self.shape(node.iter, scope)
            if shape is not None:
                yield from ((key, shape) for key in _keys(node.target))
        elif isinstance(node, ast.withitem):
            shape = self.shape(node.context_expr, scope)
            if shape is not None and node.optional_vars is not None:
                yield from ((key, shape) for key in _keys(node.optional_vars))
        elif isinstance(node, ast.Call):
            yield from self._filled(node, scope)

    def fixed_point(self, nodes: list[ast.AST], scope: _Scope) -> None:
        changed = True
        while changed:
            changed = False
            for node in nodes:
                for key, shape in list(self.marked(node, scope)):
                    changed |= _put(scope.env, key, shape)

    # -- fixed bounds -----------------------------------------------------------------------------------------

    def bounds(self, expr: ast.AST, scope: _Scope, verdicts: set[str]) -> bool:
        """Whether ``expr`` makes a fixed-bound comparison, or reads the verdict of one made elsewhere."""
        for node in ast.walk(expr):
            if isinstance(node, ast.Name) and node.id in verdicts:
                return True
            if isinstance(node, ast.Call):
                found = self._helper(node, scope)
                if found is not None and found.verdict:
                    return True
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                for op, left, right in zip(node.ops, operands, operands[1:], strict=False):
                    if isinstance(op, _ORDERING) and _fixed(self.shape(left, scope), self.shape(right, scope)):
                        return True
        return False

    def _verdicts(self, nodes: list[ast.AST], scope: _Scope, seed: set[str]) -> set[str]:
        """Names that hold the result of a fixed-bound comparison made outside an assert, to a fixed point.

        ``slow = {p: v for p, v in results.items() if v["p95_ms"] > CEILING_MS}`` then ``assert not slow`` is the
        same fixed bound as ``assert v["p95_ms"] <= CEILING_MS``, only moved out of the assert.
        """
        verdicts = set(seed)
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    continue
                if node.value is None or not self.bounds(node.value, scope, verdicts):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for key in _keys(target):
                        if key not in verdicts:
                            verdicts.add(key)
                            changed = True
        return verdicts

    # -- the walk ---------------------------------------------------------------------------------------------

    def _parameters(self, function: _Function, outer: _Scope) -> dict[str, _Shape]:
        """Parameters that hold a timing value: passed one by a call of the file, defaulting to one, or a fixture."""
        args = function.args
        positional = [*args.posonlyargs, *args.args]
        seeds = dict(self.params.get(function, {}))
        defaults: list[tuple[ast.arg, ast.expr]] = list(
            zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
        )
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if default is not None:
                defaults.append((arg, default))
        for arg, default in defaults:
            _put(seeds, arg.arg, self.shape(default, outer))
        for arg in [*positional, *args.kwonlyargs]:
            fixture = outer.visible.get(arg.arg)
            if fixture is not None and fixture in self.fixtures:
                _put(seeds, arg.arg, self.returns.get(fixture, _Returns()).shape())
        return seeds

    def _learn(self, node: ast.AST, nodes: list[ast.AST], scope: _Scope, verdicts: set[str]) -> None:
        """Record what this scope tells the rest of the file: attributes, globals, returns, call arguments."""
        for key, shape in scope.env.items():
            owner, _, attr = key.rpartition(".")
            if owner.split(".")[0] in {"self", "cls"}:
                self.changed |= _put(self.attrs, attr, shape)
        if isinstance(node, _FUNCTIONS):
            for child in nodes:
                if isinstance(child, ast.Global):
                    for name in child.names:
                        self.changed |= _put(self.globals, name, scope.env.get(name))
            found = _Returns()
            for child in nodes:
                if isinstance(child, ast.Return) and child.value is not None:
                    value = child.value
                    if isinstance(value, ast.Tuple) and _unstarred(value.elts):
                        for position, elt in enumerate(value.elts):
                            _put(found.positions, position, self.shape(elt, scope))
                    else:
                        found.whole = _join(found.whole, self.shape(value, scope))
                    found.verdict = found.verdict or self.bounds(value, scope, verdicts)
                elif isinstance(child, (ast.Yield, ast.YieldFrom)) and child.value is not None:
                    found.whole = _join(found.whole, self.shape(child.value, scope))
            old = self.returns.get(node, _Returns())
            new = _merged(old, found)
            if new != old:
                self.returns[node] = new
                self.changed = True
        for child in nodes:
            if isinstance(child, ast.Call):
                for function, offset in self._callees(child, scope):
                    self._learn_call(function, offset, child, scope)

    def _learn_call(self, function: _Function, offset: int, call: ast.Call, scope: _Scope) -> None:
        args = function.args
        table = self.params.setdefault(function, {})
        for arg, value in zip([*args.posonlyargs, *args.args][offset:], call.args, strict=False):
            if isinstance(value, ast.Starred):
                break
            self.changed |= _put(table, arg.arg, self.shape(value, scope))
        by_keyword = {arg.arg for arg in [*args.args, *args.kwonlyargs]}
        for keyword in call.keywords:
            if keyword.arg is not None and keyword.arg in by_keyword:
                self.changed |= _put(table, keyword.arg, self.shape(keyword.value, scope))

    def walk(self, node: ast.AST, outer: _Scope, outer_verdicts: set[str], hits: set[int] | None) -> None:
        nodes = _own_nodes(node)
        scope = _Scope(dict(outer.env), dict(outer.visible))
        if isinstance(node, ast.Module):
            for name, shape in self.globals.items():
                _put(scope.env, name, shape)
        if isinstance(node, _FUNCTIONS):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                scope.env.pop(arg.arg, None)  # a parameter shadows the enclosing name
            for name, shape in self._parameters(node, outer).items():
                _put(scope.env, name, shape)
        if not isinstance(node, ast.ClassDef):
            scope.visible.update((child.name, child) for child in nodes if isinstance(child, _FUNCTIONS))
        self.fixed_point(nodes, scope)
        verdicts = self._verdicts(nodes, scope, outer_verdicts)
        self._learn(node, nodes, scope, verdicts)
        for child in nodes:
            if isinstance(child, ast.Assert):
                if hits is not None and self.bounds(child.test, scope, verdicts):
                    hits.add(child.lineno)
            elif isinstance(child, _SCOPES):
                self.walk(child, scope, verdicts, hits)


def _cap_comments(source: str) -> dict[int, str]:
    """Line number -> the margin stated by a '# timing-cap:' comment token on that line."""
    caps: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            match = _CAP.match(token.string)
            if match:
                caps[token.start[0]] = match.group("margin").strip()
    return caps


def scan_source(source: str, filename: str) -> list[int]:
    """Line numbers of the asserts in ``source`` that compare a timing value with a fixed bound undeclared."""
    if not _MENTIONS.search(source):
        return []
    tree = ast.parse(source, filename)
    info = _File.of(tree)
    module = _Scope({}, {})
    for _ in range(_MAX_ROUNDS):
        info.changed = False
        info.walk(tree, module, set(), None)
        if not info.changed:
            break
    else:
        raise AssertionError(f"{filename}: the timing analysis did not settle in {_MAX_ROUNDS} rounds")
    hits: set[int] = set()
    info.walk(tree, module, set(), hits)
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
    assert _run([sys.executable, "-c", "pass"])[0].returncode <= 1
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

STAR = '''\
from time import *


def test_star_import():
    t0 = perf_counter()
    assert perf_counter() - t0 < 0.5
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

SHIFTED = '''\
import time


def test_shifted_bound():
    t0 = time.perf_counter()
    sum(range(1000))
    t1 = time.perf_counter()
    assert t1 < t0 + 0.5


def test_deadline_in_an_assert():
    deadline = time.monotonic() + 0.5
    sum(range(1000))
    assert time.monotonic() < deadline, "too slow"


def test_deadline_plus_a_constant():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        break
    assert time.monotonic() < deadline + 60
'''

ATTRIBUTES = '''\
import time
from contextlib import contextmanager


class Box:
    cost = 0.0
    size = 0


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.wall = time.perf_counter() - self.t0


@contextmanager
def timer():
    box = {}
    t0 = time.perf_counter()
    yield box
    box["wall"] = time.perf_counter() - t0


def test_attribute():
    box = Box()
    t0 = time.perf_counter()
    box.cost = time.perf_counter() - t0
    assert box.size < 3
    assert box.cost < 0.5


def test_timer_class():
    with Timer() as t:
        sum(range(1000))
    assert t.wall < 0.5


def test_timer_generator():
    with timer() as t:
        sum(range(1000))
    assert t["wall"] < 0.5
'''

HELPERS = '''\
import time


def _assert_fast(spent):
    assert spent < 0.5


def _fast(spent):
    return spent < 0.5


def _grows_linearly(small, large):
    return large <= 8 * max(small, 0.02)


def _check_small(n):
    assert n < 5


def test_assert_in_a_helper():
    t0 = time.perf_counter()
    _assert_fast(time.perf_counter() - t0)
    _check_small(len("abc"))


def test_a_helper_verdict():
    t0 = time.perf_counter()
    took = time.perf_counter() - t0
    assert _fast(took)
    assert _grows_linearly(took, took)
'''

CLOCKS = '''\
import time


def _timed(fn, clock=time.monotonic):
    start = clock()
    fn()
    return clock() - start


def test_clock_alias():
    clock = time.perf_counter
    t0 = clock()
    sum(range(1000))
    assert clock() - t0 < 0.5


def test_default_clock():
    assert _timed(lambda: None) < 0.5


def test_nested_closure():
    t0 = time.perf_counter()
    sum(range(1000))
    t1 = time.perf_counter()

    def spent():
        return t1 - t0

    assert spent() < 0.5
'''

MORE = '''\
import heapq
import time

import pytest

LAST = 0.0


def _measure(n):
    t0 = time.perf_counter()
    sum(range(n))
    return time.perf_counter() - t0


class Perf:
    @staticmethod
    def measure(fn):
        t0 = time.perf_counter()
        fn()
        return time.perf_counter() - t0


def _record():
    global LAST
    t0 = time.perf_counter()
    LAST = time.perf_counter() - t0


@pytest.fixture
def stopwatch():
    t0 = time.perf_counter()
    yield lambda: time.perf_counter() - t0


def test_map():
    timings = list(map(_measure, [10, 100, 1000]))
    assert max(timings) < 0.5


def test_staticmethod():
    assert Perf.measure(lambda: sum(range(1000))) < 0.5


def test_global():
    _record()
    assert LAST < 0.5


def test_fixture(stopwatch):
    assert stopwatch() < 0.5


def test_heappush():
    samples = []
    for _ in range(3):
        t0 = time.perf_counter()
        heapq.heappush(samples, time.perf_counter() - t0)
    assert len(samples) >= 3
    assert samples[-1] < 0.5
'''

MEASURED = '''\
import sys


def test_timeout_kills(run_process):
    res = run_process([sys.executable, "-c", "pass"], timeout=3)
    assert res.returncode == 0 and res.timeout_s <= 3
    assert res.duration_s < 30


def test_probe(probe):
    rr = probe.run(timeout=1)
    assert rr.error == "TIMEOUT" and rr.duration_ms < 20_000
    report = probe.report()
    assert report["retries"] <= 3
    assert report["elapsed"] < 5
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


def test_relative_checks_and_deadline_polling():
    t0 = time.perf_counter()
    small = time.perf_counter() - t0
    t1 = time.perf_counter()
    took = time.perf_counter() - t1
    assert took <= 8 * max(small, 0.02)
    assert took / max(small, 1e-9) < 8
    assert t0 <= t1
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if time.monotonic() > deadline - 4:
            break
    assert _grows_linearly(small, took)
    assert len("abc") < 10


def test_a_count_of_timing_values_is_not_a_time():
    samples = []
    for _ in range(3):
        t0 = time.perf_counter()
        samples.append(time.perf_counter() - t0)
    assert len(samples) >= 3
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


def _findings(tmp_path: Path, name: str, text: str) -> list[str]:
    _write(tmp_path, name, text)
    return find_fixed_timing_bounds(tmp_path, tmp_path)


def test_bite_a_measured_duration_against_a_number_is_red_with_its_text(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_plain.py", PLAIN)
    assert find_fixed_timing_bounds(tmp_path, tmp_path) == [
        "tests/unit/test_plain.py:8: fixed timing bound without a '# timing-cap: <margin>' comment"
    ]


def test_bite_a_best_of_helper_returning_cpu_time_is_red(tmp_path: Path) -> None:
    assert _findings(tmp_path, "test_best_of.py", BEST_OF) == [_at("test_best_of.py", BEST_OF, "assert took < 0.5")]


def test_bite_the_timing_element_of_an_unpacked_tuple_is_red_and_its_sibling_is_not(tmp_path: Path) -> None:
    # proc.returncode and _run(...)[0].returncode are green: only the element the helper times is marked
    assert _findings(tmp_path, "test_tuple.py", TUPLE) == [_at("test_tuple.py", TUPLE, "assert elapsed < 10")]


def test_bite_a_bound_inside_and_is_red(tmp_path: Path) -> None:
    assert _findings(tmp_path, "test_boolop.py", BOOLOP) == [_at("test_boolop.py", BOOLOP, "assert calls == 1")]


def test_bite_bare_star_and_aliased_imports_are_red(tmp_path: Path) -> None:
    _write(tmp_path, "test_star.py", STAR)
    assert _findings(tmp_path, "test_bare.py", BARE_AND_ALIAS) == [
        _at("test_bare.py", BARE_AND_ALIAS, "assert pc() - t0 < 1"),
        _at("test_bare.py", BARE_AND_ALIAS, "assert _t.monotonic_ns()"),
        _at("test_star.py", STAR, "assert perf_counter()"),
    ]


def test_bite_a_bound_moved_out_of_the_assert_is_red(tmp_path: Path) -> None:
    assert _findings(tmp_path, "test_verdict.py", VERDICT) == [_at("test_verdict.py", VERDICT, "assert not slow")]


def test_bite_a_timestamp_shifted_by_a_constant_is_a_fixed_bound_even_with_clocks_on_both_sides(
    tmp_path: Path,
) -> None:
    # t1 < t0 + 0.5 is t1 - t0 < 0.5; an assert on a deadline is a fixed bound, the while loop polling it is not
    assert _findings(tmp_path, "test_shifted.py", SHIFTED) == [
        _at("test_shifted.py", SHIFTED, "assert t1 < t0 + 0.5"),
        _at("test_shifted.py", SHIFTED, '< deadline, "too slow"'),
        _at("test_shifted.py", SHIFTED, "assert time.monotonic() < deadline + 60"),
    ]


def test_bite_attributes_class_timers_and_context_manager_timers_are_red(tmp_path: Path) -> None:
    # box.size is green: only the attribute that was assigned a time is marked
    assert _findings(tmp_path, "test_attributes.py", ATTRIBUTES) == [
        _at("test_attributes.py", ATTRIBUTES, "assert box.cost < 0.5"),
        _at("test_attributes.py", ATTRIBUTES, "assert t.wall < 0.5"),
        _at("test_attributes.py", ATTRIBUTES, 'assert t["wall"] < 0.5'),
    ]


def test_bite_a_bound_inside_a_helper_or_returned_by_one_is_red(tmp_path: Path) -> None:
    # _check_small(len(...)) and the linear-growth helper stay green: their parameters get no fixed-bound time
    assert _findings(tmp_path, "test_helpers.py", HELPERS) == [
        _at("test_helpers.py", HELPERS, "assert spent < 0.5"),
        _at("test_helpers.py", HELPERS, "assert _fast(took)"),
    ]


def test_bite_clock_references_defaults_and_closures_are_red(tmp_path: Path) -> None:
    assert _findings(tmp_path, "test_clocks.py", CLOCKS) == [
        _at("test_clocks.py", CLOCKS, "assert clock() - t0 < 0.5"),
        _at("test_clocks.py", CLOCKS, "assert _timed("),
        _at("test_clocks.py", CLOCKS, "assert spent() < 0.5"),
    ]


def test_bite_function_references_static_methods_globals_fixtures_and_heaps_are_red(tmp_path: Path) -> None:
    # len(samples) counts timing values and is green
    assert _findings(tmp_path, "test_more.py", MORE) == [
        _at("test_more.py", MORE, "assert max(timings) < 0.5"),
        _at("test_more.py", MORE, "assert Perf.measure("),
        _at("test_more.py", MORE, "assert LAST < 0.5"),
        _at("test_more.py", MORE, "assert stopwatch() < 0.5"),
        _at("test_more.py", MORE, "assert samples[-1] < 0.5"),
    ]


def test_bite_a_duration_measured_in_product_code_is_red_without_any_clock_in_the_file(tmp_path: Path) -> None:
    # res.timeout_s is a configured time, not a measured one, and report["retries"] is a count: both green
    assert _findings(tmp_path, "test_measured.py", MEASURED) == [
        _at("test_measured.py", MEASURED, "assert res.duration_s < 30"),
        _at("test_measured.py", MEASURED, "rr.duration_ms < 20_000"),
        _at("test_measured.py", MEASURED, 'assert report["elapsed"] < 5'),
    ]


def test_near_misses_declared_relative_polling_helpers_and_counts_are_green(tmp_path: Path) -> None:
    # a cap, a multiple, a ratio against a constant, two ordered readings, polling, a linear-growth helper, a count
    assert _findings(tmp_path, "test_green.py", GREEN) == []


def test_bite_a_short_misplaced_or_quoted_cap_is_still_red(tmp_path: Path) -> None:
    # a margin of fewer than 10 characters (line 8), a cap two lines up (line 15), a cap inside a string (line 21)
    assert _findings(tmp_path, "test_short.py", SHORT_CAP) == [
        f"test_short.py:8: {MESSAGE}",
        f"test_short.py:15: {MESSAGE}",
        f"test_short.py:21: {MESSAGE}",
    ]
