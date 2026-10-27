"""CI contract: what CONTRIBUTING.md says about the CI jobs, and which scripts CI really runs with ``--check``.

1. CI layout. The section ``### CI layout`` of CONTRIBUTING.md must name every job of ``.github/workflows/ci.yml``,
   and every check name it writes in backticks must exist in ci.yml. Keyed on JOB NAMES, not job ids: the ``name:``
   of a job is what GitHub reports as the check, what the ruleset on ``main`` requires and what a reader sees, so it
   is the name the prose uses. A matrix is expanded the way ``tests/unit/test_ci_gates.py`` expands it (``_check_names``);
   in the prose ``K/N`` stands for a shard such as ``2/3`` and ``<os>`` for a runner label. A backticked span is read
   as a check name when it carries a gate tag (``[G1]``, ``[G1,G2]``, ``[G3]``, ``[NO-GATE]``) plus other text; the bare
   tag and every other code span (commands, artifact names, runner labels) are not job names. Steps are not
   checked: the prose of a job may change freely.
2. ``--check`` reachability. A script under ``scripts/`` that defines a ``--check`` flag but that nothing runs with
   it is a check that never runs. It must be invoked with ``--check`` by a workflow, by ``scripts/gates.py`` or by a
   test under ``tests/``, or be listed in ``EXEMPT`` with a reason.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

from aeos.core.paths import repo_root
from tests.unit.docs_contract_support import script_flags
from tests.unit.test_ci_gates import _check_names, _workflow

ROOT = repo_root()
HEADING = "### CI layout"

# ---------------------------------------------------------------- 1. CI layout names

TAG = re.compile(r"\[(?:G\d(?:,G\d)*|NO-GATE)\]")
INLINE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
PLACEHOLDERS = {"<os>": r"[A-Za-z0-9._-]+", "K/N": r"\d+/\d+"}


def ci_layout_section(markdown: str) -> str | None:
    lines = markdown.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == HEADING)
    except StopIteration:
        return None
    body = []
    for ln in lines[start + 1:]:
        if re.match(r"#{1,3} ", ln):
            break
        body.append(ln)
    return "\n".join(body)


def _without_fences(text: str) -> str:
    out, fenced = [], False
    for ln in text.splitlines():
        if re.match(r"\s*(```|~~~)", ln):
            fenced = not fenced
        elif not fenced:
            out.append(ln)
    return "\n".join(out)


def documented_check_names(section: str) -> list[str]:
    """Backticked spans of the section that read as a check name: a gate tag plus other text."""
    names = []
    for m in INLINE.finditer(_without_fences(section)):
        span = m.group(2).strip()
        if TAG.search(span) and TAG.sub("", span).strip():
            names.append(span)
    return names


def _doc_regex(doc_name: str) -> re.Pattern[str]:
    pattern = re.escape(doc_name)
    for placeholder, regex in PLACEHOLDERS.items():
        pattern = pattern.replace(re.escape(placeholder), regex)
    return re.compile(pattern)


def workflow_check_names(workflow: Mapping) -> dict[str, list[str]]:
    """job id -> the concrete check name(s) it reports (matrix expanded; the id when a job has no name)."""
    return {job_id: [n for n in _check_names(dict(job)) if n] or [job_id]
            for job_id, job in (workflow.get("jobs") or {}).items()}


def layout_problems(workflow: Mapping, contributing: str) -> list[str]:
    section = ci_layout_section(contributing)
    if section is None:
        return [f"CONTRIBUTING.md has no '{HEADING}' section"]
    jobs = workflow_check_names(workflow)
    documented = documented_check_names(section)
    patterns = [(d, _doc_regex(d)) for d in documented]
    problems = []
    for job_id, names in jobs.items():
        for name in names:
            if not any(p.fullmatch(name) for _, p in patterns):
                problems.append(f"ci.yml job '{job_id}' ('{name}') is not named in backticks in CONTRIBUTING.md "
                                f"'{HEADING}'")
    every = [n for names in jobs.values() for n in names]
    for doc_name, _ in patterns:
        if not any(_doc_regex(doc_name).fullmatch(n) for n in every):
            problems.append(f"CONTRIBUTING.md '{HEADING}' names `{doc_name}`, which is no job of ci.yml")
    return problems


def _contributing() -> str:
    return (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


def test_ci_layout_names_every_job_and_only_real_ones():
    problems = layout_problems(_workflow(), _contributing())
    assert not problems, "CONTRIBUTING.md CI layout drift:\n" + "\n".join(problems)


def test_ci_layout_check_actually_sees_the_jobs():
    """Guards the check itself: the section is found and covers the jobs of the real workflow."""
    jobs = workflow_check_names(_workflow())
    documented = documented_check_names(ci_layout_section(_contributing()) or "")
    assert len(jobs) >= 10 and len(documented) >= 8, (len(jobs), len(documented))
    assert "backend shard K/N [G2] (<os>)" in documented
    assert sum(len(v) for v in jobs.values()) > len(jobs)  # the shard matrices are expanded


def test_bite_a_job_missing_from_the_prose_is_red_and_names_it():
    workflow = _workflow()
    workflow["jobs"]["extra-lint"] = {"name": "extra lint [G1] (ubuntu-latest)", "runs-on": "ubuntu-latest", "steps": []}
    problems = layout_problems(workflow, _contributing())
    assert len(problems) == 1
    assert "extra-lint" in problems[0] and "extra lint [G1] (ubuntu-latest)" in problems[0]


def test_bite_a_matrix_job_without_a_name_is_keyed_on_its_id():
    workflow = _workflow()
    workflow["jobs"]["unnamed-job"] = {"runs-on": "ubuntu-latest", "steps": []}
    problems = layout_problems(workflow, _contributing())
    assert len(problems) == 1 and "unnamed-job" in problems[0]


def test_bite_a_documented_name_that_is_no_job_is_red():
    text = _contributing().replace("`commit subjects [G1]`", "`commit subjectz [G1]`", 1)
    assert text != _contributing()
    problems = layout_problems(_workflow(), text)
    assert any("commit subjectz [G1]" in p and "no job of ci.yml" in p for p in problems)
    assert any("commit subjects [G1]" in p and "not named" in p for p in problems)


def test_near_miss_prose_changes_and_untagged_code_spans_stay_green():
    text = _contributing()
    assert layout_problems(_workflow(), text + "\n`ghost step` and `[G1]` and `ubuntu-latest` and `a [b] c`\n") == []
    section = ci_layout_section(text) or ""
    reworded = text.replace(section, section.replace("runs ruff", "executes ruff"))
    assert layout_problems(_workflow(), reworded) == []
    # a tagged span OUTSIDE the section is not read, and a fenced block inside it is not either
    other = text.replace(HEADING, "### Something else\n\n`ghost [G1]`\n\n" + HEADING, 1)
    assert layout_problems(_workflow(), other) == []
    fenced = text.replace("```bash\npython -m pytest -q -n auto -m", "```bash\n`ghost [G1]`\npython -m pytest -q -n auto -m", 1)
    assert fenced != text and layout_problems(_workflow(), fenced) == []


def test_bite_missing_section_is_red():
    assert layout_problems(_workflow(), "# Contributing\n") == [f"CONTRIBUTING.md has no '{HEADING}' section"]


def test_placeholders_match_real_names_only():
    p = _doc_regex("backend shard K/N [G2] (<os>)")
    assert p.fullmatch("backend shard 2/3 [G2] (macos-26-intel)")
    assert not p.fullmatch("backend shard x/y [G2] (macos-26-intel)")
    assert not p.fullmatch("backend shard 2/3 [G2] ()")


# ---------------------------------------------------------------- 2. --check reachability

# script name (scripts/<name>.py) -> reason it may define --check without any automatic caller.
EXEMPT: dict[str, str] = {}

# Test files that quote commands as data (bite fixtures) and so are not callers.
NOT_CALLERS = {"tests/unit/test_docs_commands.py", "tests/unit/test_ci_contract.py"}

CHECK = "--check"
FLAG_AFTER = re.compile(r"(?<![\w-])--check(?![\w-])")


def _text_rule(name: str, text: str) -> bool:
    """``scripts/<name>.py ... --check`` in one command: same line (a trailing backslash joins lines), up to the
    next shell operator or the next script path."""
    joined = re.sub(r"\\\r?\n", " ", text)
    for m in re.finditer(rf"scripts[/\\]{re.escape(name)}\.py", joined):
        rest = joined[m.end():].split("\n", 1)[0]
        rest = re.split(r"&&|\|\||;|\||scripts[/\\]\w+\.py", rest, maxsplit=1)[0]
        if FLAG_AFTER.search(rest):
            return True
    return False


def _strings(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _ast_rule(name: str, tree: ast.AST) -> bool:
    """A list/tuple that holds ``"--check"`` as an element and, somewhere inside it, the script's file name."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            direct = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if CHECK in direct and any(s == f"{name}.py" or s.endswith(f"/{name}.py") or s.endswith(f"\\{name}.py")
                                       for s in _strings(node)):
                return True
    return False


def _module_rule(name: str, tree: ast.AST) -> bool:
    """A test that loads the script as a module and calls ``main([... "--check"])``.

    Both halves are read from the code (a comment or docstring does not count): (1) the file loads the script, that
    is a string literal ``<name>.py`` (a path such as ``ROOT / "scripts" / "<name>.py"``) or an ``import <name>``;
    (2) a call of a function named ``main`` takes a list or tuple holding a literal ``"--check"`` element. Any other
    call or list with ``--check`` (``ruff format --check``) is not the script's."""
    def loads_script(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return node.value == f"{name}.py"
        if isinstance(node, ast.Import):
            return any(a.name.split(".")[-1] == name for a in node.names)
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").split(".")[-1] == name
        return False

    loads = any(loads_script(node) for node in ast.walk(tree))
    if not loads:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not ((isinstance(func, ast.Name) and func.id == "main") or (isinstance(func, ast.Attribute)
                                                                       and func.attr == "main")):
            continue
        args = [*node.args, *(k.value for k in node.keywords)]
        for arg in args:
            if isinstance(arg, (ast.List, ast.Tuple)) and any(isinstance(e, ast.Constant) and e.value == CHECK
                                                              for e in arg.elts):
                return True
    return False


def _callers(root: Path, name: str) -> list[str]:
    """Where scripts/<name>.py is run with --check: workflows, scripts/gates.py and tests (relative paths)."""
    found: list[str] = []
    for wf in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        if _text_rule(name, wf.read_text(encoding="utf-8")):
            found.append(wf.relative_to(root).as_posix())
    gates = root / "scripts" / "gates.py"
    if gates.is_file():
        text = gates.read_text(encoding="utf-8")
        if _text_rule(name, text) or _ast_rule(name, ast.parse(text)):
            found.append("scripts/gates.py")
    tests = root / "tests"
    for path in sorted(tests.rglob("*.py")) if tests.is_dir() else []:
        rel = path.relative_to(root).as_posix()
        if rel in NOT_CALLERS:
            continue
        text = path.read_text(encoding="utf-8")
        if name not in text:
            continue
        tree = ast.parse(text, filename=rel)
        if _text_rule(name, text) or _ast_rule(name, tree) or _module_rule(name, tree):
            found.append(rel)
    return found


def check_flag_scripts(root: Path) -> list[str]:
    scripts = root / "scripts"
    return sorted(p.stem for p in scripts.glob("*.py") if CHECK in script_flags(p))


def reachability_problems(root: Path, exempt: Mapping[str, str]) -> list[str]:
    problems = []
    defining = check_flag_scripts(root)
    for name in defining:
        callers = _callers(root, name)
        if name in exempt:
            if not exempt[name].strip():
                problems.append(f"exemption scripts/{name}.py has no reason")
            if callers:
                problems.append(f"stale exemption scripts/{name}.py: {callers[0]} already runs it with {CHECK}")
        elif not callers:
            problems.append(f"scripts/{name}.py defines {CHECK} but no workflow, scripts/gates.py or test runs it with "
                            f"{CHECK}: wire it in, or exempt it with a reason")
    for name in sorted(set(exempt) - set(defining)):
        problems.append(f"stale exemption scripts/{name}.py: it does not define {CHECK}")
    return problems


def test_every_check_flag_is_run_by_ci_gates_or_a_test():
    problems = reachability_problems(ROOT, EXEMPT)
    assert not problems, "\n".join(problems)


def test_the_real_check_scripts_are_found_with_their_callers():
    """changelog.py runs in CI; owner_matrix.py in test_docs_links.py (subprocess); ci_durations.py is called by
    test_ci_shards.py as a module (main([... '--check'])), so it is covered and needs no exemption."""
    assert {"changelog", "owner_matrix", "ci_durations"} <= set(check_flag_scripts(ROOT))
    assert ".github/workflows/ci.yml" in _callers(ROOT, "changelog")
    assert "tests/unit/test_docs_links.py" in _callers(ROOT, "owner_matrix")
    assert "tests/unit/test_ci_shards.py" in _callers(ROOT, "ci_durations")


# --- bite tests on a temporary tree ---

CHECK_SCRIPT = ('import argparse\nap = argparse.ArgumentParser()\nap.add_argument("--check", action="store_true")\n')


def _repo(tmp_path: Path, workflow: str = "", gates: str = "", test: str = "") -> Path:
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "orphan.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    (tmp_path / "scripts" / "plain.py").write_text("print('no flags')\n", encoding="utf-8")
    (tmp_path / "scripts" / "gates.py").write_text(gates or "x = 1\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(workflow or "name: ci\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(test or "def test_x():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_bite_a_check_script_nobody_runs_is_red_and_named(tmp_path):
    problems = reachability_problems(_repo(tmp_path), {})
    assert len(problems) == 1 and "scripts/orphan.py" in problems[0] and "--check" in problems[0]


def test_bite_a_neighbouring_other_tool_check_does_not_count(tmp_path):
    wf = ("jobs:\n  a:\n    steps:\n      - run: python scripts/orphan.py\n      - run: ruff format --check .\n"
          "      - run: python scripts/orphan_two.py --check\n")
    assert len(reachability_problems(_repo(tmp_path, workflow=wf), {})) == 1


def test_near_miss_each_kind_of_caller_makes_it_green(tmp_path):
    cases = {
        "workflow": {"workflow": "jobs:\n  a:\n    steps:\n      - run: python scripts/orphan.py --check\n"},
        "workflow continuation": {"workflow": "      - run: |\n          python scripts/orphan.py \\\n            --check\n"},
        "gates": {"gates": 'GATE = ("python", "scripts/orphan.py", "--check")\n'},
        "test subprocess": {"test": "import sys\nfrom pathlib import Path\nR = Path('.')\n"
                                    "args = [sys.executable, str(R / 'scripts' / 'orphan.py'), '--check']\n"},
        "test module": {"test": "import importlib.util\nfrom pathlib import Path\n"
                                "spec = importlib.util.spec_from_file_location('o', Path('.') / 'scripts' / 'orphan.py')\n"
                                "def test_x(orphan):\n    assert orphan.main(['--check']) == 0\n"},
        "test imported module": {"test": "import orphan\ndef test_x():\n    assert orphan.main(['x', '--check']) == 0\n"},
    }
    for label, kwargs in cases.items():
        root = _repo(tmp_path / label.replace(" ", "_"), **kwargs)
        assert reachability_problems(root, {}) == [], label


def test_bite_a_comment_and_an_unrelated_check_literal_do_not_count_as_a_caller(tmp_path):
    """The reviewed hole: the script name only in a comment, plus ``ruff format --check`` in some other call."""
    cases = {
        "comment plus ruff": "# orphan helper is documented elsewhere\nimport subprocess\n"
                             "subprocess.run(['ruff', 'format', '--check', '.'])\n",
        "name in a docstring": '"""orphan.py is described here."""\ndef test_x(ci):\n    ci.main(["--check"])\n',
        "loaded but check passed to another call": "from pathlib import Path\nP = Path('scripts') / 'orphan.py'\n"
                                                   "import subprocess\nsubprocess.run('ruff', '--check')\n",
        "loaded but main has no check": "from pathlib import Path\nP = Path('scripts') / 'orphan.py'\n"
                                        "def test_x(m):\n    m.main(['--dry-run'])\n    other = ['--check']\n",
    }
    for label, test in cases.items():
        problems = reachability_problems(_repo(tmp_path / label.replace(" ", "_"), test=test), {})
        assert len(problems) == 1 and "scripts/orphan.py" in problems[0], (label, problems)


def test_exemptions_need_a_reason_and_must_match_something(tmp_path):
    root = _repo(tmp_path / "a")
    assert reachability_problems(root, {"orphan": "manual maintenance tool, run by the owner"}) == []
    assert "no reason" in reachability_problems(root, {"orphan": " "})[0]
    called = _repo(tmp_path / "b", gates='GATE = ("python", "scripts/orphan.py", "--check")\n')
    assert "stale exemption scripts/orphan.py" in reachability_problems(called, {"orphan": "manual"})[0]
    assert any("does not define" in p for p in reachability_problems(root, {"plain": "manual"}))
    assert any("scripts/gone.py" in p and "does not define" in p
               for p in reachability_problems(root, {"gone": "manual", "orphan": "manual"}))
