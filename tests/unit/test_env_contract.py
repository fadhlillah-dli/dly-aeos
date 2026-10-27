"""Environment contract: every ``AEOS_*`` variable the code reads is documented, and every documented one is read.

Derived from the code (no import, no execution), with evidence per name:

* Python under ``backend/`` and ``scripts/``, with ``ast``: ``os.environ["X"]``, ``os.environ.get/pop/setdefault``,
  ``os.getenv``, ``environ.get``, ``"X" in os.environ`` (a module-level ``NAME = "AEOS_..."`` constant is resolved)
  and ``$env:AEOS_X`` inside a Python string (PowerShell code that Python hands to a child).
* The ``Settings`` fields of ``backend/aeos/core/config.py``: ``env_prefix`` + upper-cased field name, or the
  ``alias`` / ``validation_alias`` (which bypasses the prefix, as pydantic-settings does).
* Shell and PowerShell scripts under ``scripts/`` (``$AEOS_X``, ``${AEOS_X...}``, ``$env:AEOS_X``). A PowerShell
  assignment ``$env:AEOS_X = ...`` and a comment line are not reads.
* Python source embedded in a string constant (the verifier's pytest plugin in ``verification/seal.py``): a string
  that mentions ``AEOS_`` and ``environ`` or ``getenv`` and parses as Python is read with the same rules.

Out of scope (a read in one of these forms is NOT seen, so such a variable needs its row written by hand): a key
that is not a string literal, a module-level name or a loop variable over a literal sequence (``os.environ.get(
prefix + "X")``, an f-string); a mapping that aliases the environment (``env = os.environ`` then ``env.get``);
embedded code that is not valid Python on its own, or that is not in a string constant; a read through a library
(``psutil``'s ``Process.environ()``); shell variables other than ``$AEOS_X``, ``${AEOS_X...}`` and ``$env:AEOS_X``.

Every name read must have a row in the "Environment variables" table of ``docs/OPERATIONS.md`` (name, source, read by,
default, purpose) whose "read by" cell names each reader (``Settings`` or the file path) and whose "source" cell
matches the readers (``Settings field``, ``direct read``, ``script``, joined with `` + ``), or an entry in ``EXEMPT``
with a reason. Every operation that calls ``refuse_worker_context`` must be named in that section too, and be passed as a string literal (a non-literal name is a problem). A row that nothing reads and an exemption that matches nothing fail as stale. Names only: a value
is never documented here. A key that is a loop variable over a module-level tuple of names
(``[k for k in WORKER_CONTEXT_ENV if os.environ.get(k)]``) reads every name of the tuple. Writes
(``os.environ["X"] = ...``, an ``AEOS_`` key of an ``env`` dict) are collected as evidence but do not need a row: a
variable that is only written is a hand-off to a child process, and is documented only if some reader
(``Settings``) takes it from the owner too. A marker that this process only writes into a child's environment
needs no row either, even when another process reads it through ``psutil``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeGuard

from aeos.core.paths import repo_root

ROOT = repo_root()
DOC = "docs/OPERATIONS.md"
SECTION = "Environment variables"
CONFIG = "backend/aeos/core/config.py"

# name -> reason it is read by the code but deliberately not in the owner-facing table.
EXEMPT: dict[str, str] = {
    "AEOS_ACL_PATH": "internal hand-off: backend/aeos/doctor/checks/evidence.py passes a path to its own PowerShell "
                     "child through the environment; the owner never sets it",
    "AEOS_VSEAL_NONCE": "internal hand-off: backend/aeos/verification/seal.py gives the per-run nonce to its own pytest "
                        "plugin (embedded string source) through the environment; set by the controller, never by the "
                        "owner",
    "AEOS_VSEAL_KEYFILE": "internal hand-off: backend/aeos/verification/seal.py gives the path of the per-run key file "
                          "to its own pytest plugin (embedded string source); set by the controller, never by the "
                          "owner",
}

NAME = re.compile(r"AEOS_[A-Z0-9_]+")
PS_ENV = re.compile(r"\$env:(AEOS_[A-Z0-9_]+)\b(?!\s*=(?!=))", re.IGNORECASE)
SH_VAR = re.compile(r"\$\{?(AEOS_[A-Z0-9_]+)")
SHELL_SUFFIXES = {".sh", ".ps1", ".cmd", ".bat"}
SKIP_DIRS = {"node_modules", ".venv", "__pycache__", ".git"}
SETTINGS_LABEL = "Settings"


@dataclass
class Usage:
    reads: dict[str, set[str]] = field(default_factory=dict)  # name -> reader labels (Settings or a path)
    writes: dict[str, set[str]] = field(default_factory=dict)

    def read(self, name: str, label: str) -> None:
        if name.startswith("AEOS_"):
            self.reads.setdefault(name, set()).add(label)

    def write(self, name: str, label: str) -> None:
        if name.startswith("AEOS_"):
            self.writes.setdefault(name, set()).add(label)


def _files(base: Path, suffixes: set[str] | None = None) -> list[Path]:
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if (p.is_file() and not (set(p.relative_to(base).parts) & SKIP_DIRS)
                and (suffixes is None or p.suffix in suffixes)):
            out.append(p)
    return out


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (isinstance(node, ast.Name)
                                                                           and node.id == "environ")


def _str_seq(node: ast.AST | None) -> list[str] | None:
    """The strings of a literal tuple / list / set of string constants, else None."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [str(e.value) for e in node.elts if isinstance(e, ast.Constant)]
    return None


def _python_usage(tree: ast.AST, label: str, usage: Usage) -> None:
    # module-level NAME = "AEOS_X" (one name) and NAMES = ("AEOS_X", "AEOS_Y") (a sequence of names)
    consts: dict[str, list[str]] = {}
    for n in getattr(tree, "body", []):
        if isinstance(n, ast.Assign):
            if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                values: list[str] | None = [n.value.value]
            else:
                values = _str_seq(n.value)
            if values is not None:
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = values
    # loop variables: ``for k in NAMES`` and ``[... for k in NAMES ...]`` read every name of the sequence. A loop
    # variable belongs to the function (or module) that holds the loop, so two functions may reuse ``k``.
    scope_of: dict[ast.AST, ast.AST] = {}
    loops: dict[tuple[ast.AST, str], list[str]] = {}

    def assign_scopes(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            scope_of[child] = scope
            assign_scopes(child, child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                          else scope)

    assign_scopes(tree, tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(node.target, ast.Name):
            seq = _str_seq(node.iter) or (consts.get(node.iter.id) if isinstance(node.iter, ast.Name) else None)
            if seq:
                loops.setdefault((scope_of.get(node, tree), node.target.id), []).extend(seq)

    def keys(node: ast.AST | None) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            return [*consts.get(node.id, []), *loops.get((scope_of.get(node, tree), node.id), [])]
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            for name in keys(node.slice):
                (usage.read if isinstance(node.ctx, ast.Load) else usage.write)(name, label)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            environ_call = (isinstance(func, ast.Attribute) and _is_environ(func.value)
                            and func.attr in {"get", "pop", "setdefault"})
            getenv_call = (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
                isinstance(func, ast.Name) and func.id == "getenv")
            if environ_call or getenv_call:
                for name in keys(node.args[0]):
                    usage.read(name, label)
        elif isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            if any(_is_environ(c) for c in node.comparators):
                for name in keys(node.left):
                    usage.read(name, label)
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                for name in keys(k):
                    if NAME.fullmatch(name):
                        usage.write(name, label)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "$env:" in node.value:
            for m in PS_ENV.finditer(node.value):
                usage.read(m.group(1).upper(), label)
    for node in ast.walk(tree):  # Python source embedded in a string (a plugin handed to a child process)
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and "AEOS_" in node.value
                and ("environ" in node.value or "getenv" in node.value)):
            try:
                embedded = ast.parse(node.value)
            except (SyntaxError, ValueError):
                continue
            _python_usage(embedded, label, usage)


def _literal_names(node: ast.AST | None) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Call):  # AliasChoices("a", "b")
        return [n for a in node.args for n in _literal_names(a)]
    return []


def settings_names(tree: ast.AST) -> set[str]:
    """Env names of every BaseSettings subclass: prefix + field name, or the alias (which has no prefix)."""
    names: set[str] = set()
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if not any((isinstance(b, ast.Name) and b.id == "BaseSettings")
                   or (isinstance(b, ast.Attribute) and b.attr == "BaseSettings") for b in cls.bases):
            continue
        prefix = ""
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "model_config"
                                                    for t in stmt.targets) and isinstance(stmt.value, ast.Call):
                for kw in stmt.value.keywords:
                    if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                        prefix = str(kw.value.value)
        for stmt in cls.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            field_name = stmt.target.id
            ann = ast.unparse(stmt.annotation)
            if field_name.startswith("_") or ann.startswith("ClassVar") or "ClassVar[" in ann:
                continue
            aliases: list[str] = []
            if isinstance(stmt.value, ast.Call):
                for kw in stmt.value.keywords:
                    if kw.arg in {"alias", "validation_alias"}:
                        aliases += _literal_names(kw.value)
            if aliases:
                names.update(a.upper() for a in aliases)
            else:
                names.add((prefix + field_name).upper())
    return {n for n in names if n.startswith("AEOS_")}


def _shell_usage(text: str, label: str, usage: Usage) -> None:
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in SH_VAR.finditer(line):
            usage.read(m.group(1), label)
        for m in PS_ENV.finditer(line):
            usage.read(m.group(1).upper(), label)


def collect(root: Path) -> Usage:
    usage = Usage()
    for base in ("backend", "scripts"):
        for path in _files(root / base, {".py"}):
            text = path.read_text(encoding="utf-8")
            label = path.relative_to(root).as_posix()
            if "AEOS_" not in text and label != CONFIG:
                continue
            tree = ast.parse(text, filename=label)
            _python_usage(tree, label, usage)
            if label == CONFIG:
                for name in settings_names(tree):
                    usage.read(name, SETTINGS_LABEL)
    scripts = root / "scripts"
    for path in _files(scripts):
        if path.suffix in SHELL_SUFFIXES or path.parent.name == "git-hooks":
            _shell_usage(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix(), usage)
    return usage


def _cells(row: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]


def documented(markdown: str) -> dict[str, list[str]] | None:
    """name -> table cells, from the table under ``## Environment variables``; None when the section is missing."""
    lines = markdown.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == f"## {SECTION}")
    except StopIteration:
        return None
    rows: dict[str, list[str]] = {}
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        if ln.lstrip().startswith("|"):
            cells = _cells(ln)
            first = cells[0].strip("`").strip()
            if NAME.fullmatch(first):
                rows[first] = cells if first not in rows else [*rows[first], "DUPLICATE"]
    return rows


def expected_source(readers: set[str]) -> str:
    """The "Source" cell a row must carry: how the readers take the variable."""
    kinds = []
    if SETTINGS_LABEL in readers:
        kinds.append("Settings field")
    if any(r != SETTINGS_LABEL and not r.startswith("scripts/") for r in readers):
        kinds.append("direct read")
    if any(r.startswith("scripts/") for r in readers):
        kinds.append("script")
    return " + ".join(kinds)


def section_text(markdown: str) -> str:
    lines = markdown.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == f"## {SECTION}")
    body = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        body.append(ln)
    return "\n".join(body)


def _is_refuse_call(node: ast.AST) -> TypeGuard[ast.Call]:
    return (isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == "refuse_worker_context")
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == "refuse_worker_context")))


def worker_context_scan(root: Path) -> tuple[dict[str, str], list[str]]:
    """(operation text -> file of every ``refuse_worker_context("<operation>")`` call under backend/, problems).

    A call whose operation (first positional argument, or the keyword ``action``) is not a string literal cannot be checked against the docs list, so it is a problem
    naming file:line."""
    found: dict[str, str] = {}
    bad: list[str] = []
    for path in _files(root / "backend", {".py"}):
        text = path.read_text(encoding="utf-8")
        if "refuse_worker_context" not in text:
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if not _is_refuse_call(node):
                continue
            first = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == "action"), None)  # _util.py names it `action`
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found[first.value] = rel
            else:
                bad.append(f"{rel}:{node.lineno}: refuse_worker_context called with a non-literal operation name; "
                           f"use a literal so the docs list can be checked")
    return found, bad


def worker_context_operations(root: Path) -> dict[str, str]:
    return worker_context_scan(root)[0]


def problems(root: Path, exempt: Mapping[str, str]) -> list[str]:
    usage = collect(root)
    doc_path = root / DOC
    rows = documented(doc_path.read_text(encoding="utf-8")) if doc_path.is_file() else None
    if rows is None:
        return [f"{DOC} has no '## {SECTION}' section with a table (name, source, read by, default, purpose)"]
    out: list[str] = []
    for name, readers in sorted(usage.reads.items()):
        where = ", ".join(sorted(readers))
        if name in exempt:
            if name in rows:
                out.append(f"{name} is exempt and also documented in {DOC}: delete the exemption")
            continue
        cells = rows.get(name)
        if cells is None:
            out.append(f"{name}: read by {where} but has no row in the '{SECTION}' table of {DOC} (and no exemption)")
            continue
        if "DUPLICATE" in cells or len(cells) < 5 or not all(cells[1:5]):
            out.append(f"{name}: the {DOC} row needs exactly one row with source, read by, default and purpose "
                       f"filled in")
            continue
        missing = sorted(r for r in readers if r not in cells[2].replace("`", ""))
        if missing:
            out.append(f"{name}: the 'read by' cell in {DOC} does not name {', '.join(missing)}")
        want = expected_source(readers)
        if cells[1].replace("`", "").strip() != want:
            out.append(f"{name}: the 'source' cell in {DOC} says '{cells[1]}' but its readers ({where}) make it "
                       f"'{want}'")
    for name in sorted(set(rows) - set(usage.reads)):
        out.append(f"{name} is documented in {DOC} but nothing in backend/ or scripts/ reads it: delete the row")
    section = section_text(doc_path.read_text(encoding="utf-8"))
    operations, non_literal = worker_context_scan(root)
    out.extend(non_literal)
    for operation, where_file in sorted(operations.items()):
        if operation not in section:
            out.append(f"refuse_worker_context('{operation}') in {where_file} is not named in the '{SECTION}' section "
                       f"of {DOC}: list it with the other human-only operations")
    for name, reason in sorted(exempt.items()):
        if not reason.strip():
            out.append(f"exemption {name} has no reason")
        if name not in usage.reads:
            out.append(f"stale exemption {name}: nothing reads it any more, delete it")
    return out


def test_every_env_variable_the_code_reads_is_documented_and_every_row_is_read():
    found = problems(ROOT, EXEMPT)
    assert not found, "environment contract drift:\n" + "\n".join(found)


def test_written_only_names_are_classified_from_evidence_not_guessed():
    """cli.py writes AEOS_PORT / AEOS_DEV_MODE for its own process, and Settings reads both: so they are real,
    documented variables. A name that is only written (AEOS_VERIFIER_CHILD) needs no row."""
    usage = collect(ROOT)
    for name in ("AEOS_PORT", "AEOS_DEV_MODE"):
        assert usage.reads[name] == {SETTINGS_LABEL}, name
        assert "backend/aeos/cli.py" in usage.writes[name], name
    assert "AEOS_VERIFIER_CHILD" in usage.writes and "AEOS_VERIFIER_CHILD" not in usage.reads
    assert {"AEOS_DATA_DIR", "AEOS_CONFIG", "AEOS_GATES_PYTHON", "AEOS_MMDC_BINARY"} <= set(usage.reads)
    assert usage.reads["AEOS_GATES_PYTHON"] >= {"scripts/run_tests.sh", "scripts/git-hooks/pre-push"}


# --- bite tests on a temporary tree ----------------------------------------------------------------------------------

TABLE_HEAD = ("## Environment variables\n\n| Variable | Source | Read by | Default | Purpose |\n"
              "|---|---|---|---|---|\n")


def _tree(tmp_path: Path, py: str = "", rows: str = "", shell: dict[str, str] | None = None,
          config: str = "") -> Path:
    (tmp_path / "backend" / "aeos" / "core").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "backend" / "aeos" / "x.py").write_text(py, encoding="utf-8")
    if config:
        (tmp_path / CONFIG).write_text(config, encoding="utf-8")
    for name, text in (shell or {}).items():
        (tmp_path / "scripts" / name).write_text(text, encoding="utf-8")
    (tmp_path / DOC).write_text("# Ops\n\n" + TABLE_HEAD + rows + "\n## Next\n", encoding="utf-8")
    return tmp_path


READ_NEW = 'import os\nvalue = os.environ.get("AEOS_BRAND_NEW")\n'
ROW_NEW = "| `AEOS_BRAND_NEW` | direct read | `backend/aeos/x.py` | unset | a brand new switch |\n"


def test_bite_a_new_variable_without_a_row_is_red_and_with_the_row_green(tmp_path):
    red = problems(_tree(tmp_path / "red", READ_NEW), {})
    assert len(red) == 1 and "AEOS_BRAND_NEW" in red[0] and "backend/aeos/x.py" in red[0]
    assert problems(_tree(tmp_path / "green", READ_NEW, ROW_NEW), {}) == []


def test_bite_the_row_must_name_the_reader_and_fill_every_column(tmp_path):
    wrong = problems(_tree(tmp_path / "a", READ_NEW, "| `AEOS_BRAND_NEW` | direct read | `Settings` | unset | x |\n"), {})
    assert len(wrong) == 1 and "does not name backend/aeos/x.py" in wrong[0]
    empty = problems(_tree(tmp_path / "b", READ_NEW, "| `AEOS_BRAND_NEW` | direct read | `backend/aeos/x.py` |  | x |\n"), {})
    assert len(empty) == 1 and "AEOS_BRAND_NEW" in empty[0]


def test_bite_missing_section_is_red(tmp_path):
    root = _tree(tmp_path, READ_NEW, ROW_NEW)
    (root / DOC).write_text("# Ops\n\nno table here\n", encoding="utf-8")
    assert "Environment variables" in problems(root, {})[0]


def test_bite_stale_exemption_and_stale_row_are_red(tmp_path):
    root = _tree(tmp_path / "a", "x = 1\n")
    stale = problems(root, {"AEOS_GONE": "was test-only"})
    assert len(stale) == 1 and "stale exemption AEOS_GONE" in stale[0]
    root_b = _tree(tmp_path / "b", "x = 1\n", ROW_NEW)
    row = problems(root_b, {})
    assert len(row) == 1 and "AEOS_BRAND_NEW" in row[0] and "nothing" in row[0]
    # a live exemption silences the missing row; a reason is mandatory
    live = _tree(tmp_path / "c", READ_NEW)
    assert problems(live, {"AEOS_BRAND_NEW": "test-only"}) == []
    assert "no reason" in problems(live, {"AEOS_BRAND_NEW": " "})[0]
    both = _tree(tmp_path / "d", READ_NEW, ROW_NEW)
    assert "delete the exemption" in problems(both, {"AEOS_BRAND_NEW": "x"})[0]


def test_near_miss_written_only_and_non_env_strings_need_no_row(tmp_path):
    py = ('import os\nos.environ["AEOS_ONLY_WRITTEN"] = "1"\nenv = {"AEOS_CHILD_ONLY": "1"}\n'
          'CODE = "AEOS_ERROR"\nlabel = "THIS_AEOS_INSTANCE"\nother = os.environ.get("HOME")\n')
    assert problems(_tree(tmp_path, py), {}) == []


def test_every_python_read_form_is_found(tmp_path):
    py = ('import os\nfrom os import environ, getenv\nNAME = "AEOS_VIA_CONST"\n'
          'a = os.environ["AEOS_SUBSCRIPT"]\nb = os.environ.get("AEOS_GET")\nc = os.getenv("AEOS_GETENV")\n'
          'd = environ.get("AEOS_BARE_ENVIRON")\ne = os.environ.pop("AEOS_POP", "")\nf = "AEOS_IN" in os.environ\n'
          'g = os.environ.get(NAME)\nh = getenv("AEOS_BARE_GETENV")\n'
          'ps = "Get-Acl -Path $env:AEOS_FROM_POWERSHELL_STRING"\n')
    usage = collect(_tree(tmp_path, py))
    assert set(usage.reads) == {"AEOS_SUBSCRIPT", "AEOS_GET", "AEOS_GETENV", "AEOS_BARE_ENVIRON", "AEOS_POP", "AEOS_IN",
                                "AEOS_VIA_CONST", "AEOS_BARE_GETENV", "AEOS_FROM_POWERSHELL_STRING"}, usage.reads


def test_settings_fields_prefix_and_aliases(tmp_path):
    config = (
        "from pydantic import Field, AliasChoices\n"
        "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
        "class Settings(BaseSettings):\n"
        "    model_config = SettingsConfigDict(env_prefix='AEOS_', extra='ignore')\n"
        "    port: int = 1\n"
        "    other_name: str = Field('x', validation_alias='AEOS_CUSTOM_NAME')\n"
        "    multi: str = Field('x', validation_alias=AliasChoices('AEOS_FIRST', 'AEOS_SECOND'))\n"
        "    _private: int = 0\n"
        "    @property\n"
        "    def derived(self): return 1\n")
    usage = collect(_tree(tmp_path, config=config))
    # the alias replaces the prefixed field name; a private field and a property are not settings
    assert set(usage.reads) == {"AEOS_PORT", "AEOS_CUSTOM_NAME", "AEOS_FIRST", "AEOS_SECOND"}, usage.reads
    # a Settings field with no row is red, naming Settings as the reader
    red = problems(_tree(tmp_path / "r", config=config), {})
    assert any("AEOS_PORT" in p and "Settings" in p for p in red)


def test_shell_and_powershell_reads_but_not_assignments_or_comments(tmp_path):
    shell = {
        "run.sh": '# uses $AEOS_IN_COMMENT only here\nx="${AEOS_BRACED:-}"\necho "$AEOS_PLAIN"\nexport AEOS_ASSIGNED=1\n',
        "run.ps1": 'if (-not $env:AEOS_PS_READ) { $env:AEOS_PS_READ = "x" }\n$env:AEOS_PS_ONLY_SET = "1"\n'
                   '# $env:AEOS_PS_COMMENT\n',
    }
    usage = collect(_tree(tmp_path, shell=shell))
    assert set(usage.reads) == {"AEOS_BRACED", "AEOS_PLAIN", "AEOS_PS_READ"}, usage.reads


MARKERS = ('import os\nMARKERS = ("AEOS_MARK_ONE", "AEOS_MARK_TWO")\n'
           'def guard():\n    return [k for k in MARKERS if os.environ.get(k)]\n')
ROWS_MARKERS = ("| `AEOS_MARK_ONE` | direct read | `backend/aeos/x.py` | unset | one |\n"
                "| `AEOS_MARK_TWO` | direct read | `backend/aeos/x.py` | unset | two |\n")


def test_bite_a_loop_variable_over_a_module_tuple_reads_every_name(tmp_path):
    """The reviewed hole: ``[k for k in WORKER_CONTEXT_ENV if os.environ.get(k)]`` read six names the collector missed."""
    red = problems(_tree(tmp_path / "red", MARKERS), {})
    assert len(red) == 2 and "AEOS_MARK_ONE" in red[0] and "AEOS_MARK_TWO" in red[1], red
    assert problems(_tree(tmp_path / "green", MARKERS, ROWS_MARKERS), {}) == []
    one_row = problems(_tree(tmp_path / "one", MARKERS, ROWS_MARKERS.splitlines(keepends=True)[0]), {})
    assert len(one_row) == 1 and "AEOS_MARK_TWO" in one_row[0]


def test_every_loop_form_over_a_tuple_or_literal_sequence_is_found(tmp_path):
    py = ('import os\nNAMES = ["AEOS_LIST_A", "AEOS_LIST_B"]\n'
          'for key in NAMES:\n    os.environ.get(key)\n'
          'for name in ("AEOS_LITERAL_A",):\n    os.getenv(name)\n'
          'x = {n: os.environ[n] for n in ("AEOS_DICT_A",)}\n'
          'y = [n for n in NAMES if n in os.environ]\n')
    assert set(collect(_tree(tmp_path, py)).reads) == {"AEOS_LIST_A", "AEOS_LIST_B", "AEOS_LITERAL_A", "AEOS_DICT_A"}


def test_near_miss_loops_over_non_aeos_or_unresolvable_sequences_and_writes_need_no_row(tmp_path):
    py = ('import os\nOTHER = ("HOME", "PATH")\n'
          'def a():\n    for k in OTHER:\n        os.environ.get(k)\n'
          'def b():\n    for k in dynamic():\n        os.environ.get(k)\n'
          'def c():\n    for k in ("AEOS_ONLY_WRITTEN_LOOP",):\n        os.environ[k] = "1"\n'
          'def d(k):\n    return os.environ.get(k)\n')
    assert problems(_tree(tmp_path, py), {}) == []


def test_the_worker_context_markers_are_collected_from_the_real_code():
    markers = {"AEOS_RUN_ID", "AEOS_WORKER_RUN_ID", "AEOS_TASK_ID", "AEOS_CAPABILITY_TOKEN", "AEOS_SANDBOX",
               "AEOS_CANDIDATE_BUILD"}
    reads = collect(ROOT).reads
    assert markers <= set(reads)
    assert all("backend/aeos/operations/_util.py" in reads[m] for m in markers)


# --- source column, embedded plugin source, human-only operations ----------------------------------------------------

def test_bite_the_source_column_must_match_the_readers(tmp_path):
    """Settings reads, a direct os.environ read in a backend file and a script read are three different sources."""
    config = ("from pydantic_settings import BaseSettings, SettingsConfigDict\n"
              "class Settings(BaseSettings):\n    model_config = SettingsConfigDict(env_prefix='AEOS_')\n"
              "    port: int = 1\n")
    py = 'import os\nvalue = os.environ.get("AEOS_PORT")\n'
    both = "| `AEOS_PORT` | {source} | `Settings`, `backend/aeos/x.py`, `scripts/run.sh` | 1 | the port |\n"
    shell = {"run.sh": 'echo "$AEOS_PORT"\n'}
    red = problems(_tree(tmp_path / "red", py, both.format(source="Settings field"), shell, config), {})
    assert len(red) == 1 and "AEOS_PORT" in red[0] and "'source' cell" in red[0], red
    assert "Settings field + direct read + script" in red[0]
    green = problems(_tree(tmp_path / "green", py, both.format(source="Settings field + direct read + script"), shell,
                           config), {})
    assert green == []
    # a script-only variable, and a backend-only one, are not "Settings field"
    only = problems(_tree(tmp_path / "only", READ_NEW, "| `AEOS_BRAND_NEW` | Settings field | `backend/aeos/x.py` | "
                                                        "unset | x |\n"), {})
    assert len(only) == 1 and "says 'Settings field'" in only[0] and "'direct read'" in only[0], only


EMBEDDED = ('import os\nPLUGIN = r"""\nimport os\n\n\ndef _init():\n    nonce = os.environ.pop("AEOS_EMBEDDED_NONCE", "")\n'
            '    return nonce\n"""\nENV = {"AEOS_EMBEDDED_NONCE": "x"}\n')


def test_bite_a_read_inside_an_embedded_plugin_string_is_seen_and_a_near_miss_is_not(tmp_path):
    red = problems(_tree(tmp_path / "red", EMBEDDED), {})
    assert len(red) == 1 and "AEOS_EMBEDDED_NONCE" in red[0] and "backend/aeos/x.py" in red[0], red
    assert problems(_tree(tmp_path / "ex", EMBEDDED), {"AEOS_EMBEDDED_NONCE": "internal hand-off"}) == []
    near = ('import os\nDOC = "set AEOS_NOT_A_READ with os.environ in your shell, then restart"\n'
            'BROKEN = "def (: os.environ.get(\\"AEOS_BROKEN_CODE\\")"\n'
            'PLUGIN = "import os\\nx = os.path.join(\\"AEOS_PATH_PART\\")\\n"\n')
    assert collect(_tree(tmp_path / "near", near)).reads == {}


def test_the_real_vseal_inputs_are_read_by_the_embedded_plugin_and_exempt_as_internal():
    reads = collect(ROOT).reads
    for name in ("AEOS_VSEAL_NONCE", "AEOS_VSEAL_KEYFILE"):
        assert reads[name] == {"backend/aeos/verification/seal.py"}, (name, reads.get(name))
        assert name in EXEMPT and "never by the owner" in EXEMPT[name]
        assert name not in (documented((ROOT / DOC).read_text(encoding="utf-8")) or {})


def test_bite_a_human_only_operation_missing_from_the_list_is_red_and_named(tmp_path):
    root = _tree(tmp_path / "red", 'def run():\n    refuse_worker_context("controller rollback")\n')
    red = problems(root, {})
    assert len(red) == 1 and "controller rollback" in red[0] and "backend/aeos/x.py" in red[0], red
    listed = _tree(tmp_path / "green", 'from u import refuse_worker_context\n'
                                       'def run():\n    refuse_worker_context("controller rollback")\n')
    (listed / DOC).write_text((listed / DOC).read_text(encoding="utf-8").replace(
        "\n## Next", "\n* `controller rollback`\n\n## Next"), encoding="utf-8")
    assert problems(listed, {}) == []
    mention = _tree(tmp_path / "near", 'def run():\n    print("refuse_worker_context")\n')
    assert problems(mention, {}) == []  # a mention in a string is not a call site
    keyword = _tree(tmp_path / "keyword", 'def run():\n    refuse_worker_context(action="controller rollback")\n')
    red = problems(keyword, {})  # a keyword literal is checked against the list, not reported as non-literal
    assert len(red) == 1 and "controller rollback" in red[0] and "non-literal" not in red[0], red


def test_bite_a_non_literal_operation_name_is_red_and_names_file_and_line(tmp_path):
    for label, call in {"variable": "refuse_worker_context(action)", "f-string": 'refuse_worker_context(f"x {action}")',
                        "no argument": "refuse_worker_context()",
                        "attribute": "u.refuse_worker_context(action)"}.items():
        root = _tree(tmp_path / label.replace(" ", "_"), f"def run(action):\n    {call}\n")
        red = problems(root, {})
        assert len(red) == 1, (label, red)
        assert "backend/aeos/x.py:2" in red[0] and "non-literal operation name" in red[0], (label, red)
        assert "use a literal" in red[0], red


def test_the_real_human_only_operations_are_all_listed():
    ops = worker_context_operations(ROOT)
    assert set(ops) == {"release acceptance exception", "controller promotion", "controller rollback", "live restore"}
