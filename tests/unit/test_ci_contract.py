"""CI contract: what CONTRIBUTING.md says about the CI jobs, and which scripts CI really runs with ``--check``.

1. CI layout. The section ``### CI layout`` of CONTRIBUTING.md must name every job of every workflow under
   ``.github/workflows`` that runs on ``pull_request`` or ``push`` (ci.yml and, for example, pr-title.yml), and every
   check name it writes in backticks must be a job of one of them. Keyed on JOB NAMES, not job ids: the ``name:``
   of a job is what GitHub reports as the check, what the ruleset on ``main`` requires and what a reader sees, so it
   is the name the prose uses. A matrix is expanded the way ``tests/unit/test_ci_gates.py`` expands it (``_check_names``);
   in the prose ``K/N`` stands for a shard such as ``2/3`` and ``<os>`` for a runner label. A backticked span is read
   as a check name when it carries a gate tag (``[G1]``, ``[G1,G2]``, ``[G3]``, ``[NO-GATE]``) plus other text; the bare
   tag and every other code span (commands, artifact names, runner labels) are not job names. Steps are not
   checked: the prose of a job may change freely.
2. ``--check`` reachability. A script under ``scripts/`` that defines a ``--check`` flag (``add_argument`` in its AST)
   but that nothing runs with it is a check that never runs. Each such script is either run with ``--check`` by CI
   itself, read from code and never from prose (in a workflow only the ``run:`` values of the parsed YAML count, so a
   ``#`` comment does not; in ``scripts/gates.py`` only string constants that are not docstrings), or DECLARED in
   ``TEST_CALLERS``: script -> ``tests/unit/<file>.py::<test function>`` plus a one-line reason. The registry is
   reviewed by humans. The test proves only that the set of scripts with a ``--check`` flag equals the registry plus
   the CI-run scripts (a new one fails with its name; a stale entry fails), that every registered test node exists
   (module or class level), holds the string ``--check`` and sits in a file that names ``<script>.py`` or imports it.
   It does NOT prove that the node really runs the script with the flag, that it passes, or that CI runs it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from aeos.core.paths import repo_root
from tests.unit.docs_contract_support import script_flags
from tests.unit.test_ci_gates import _check_names

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


def _runs_on_pr_or_push(workflow: Mapping) -> bool:
    on = workflow.get("on", workflow.get(True))  # PyYAML reads the bare key `on` as True
    triggers = [on] if isinstance(on, str) else list(on or [])
    return any(t in ("pull_request", "push") for t in triggers)


def checked_workflows(directory: Path | None = None) -> dict:
    """The jobs of every workflow that runs on pull_request or push, as one ``{"jobs": ...}`` mapping. Job ids of a file
    other than ci.yml are prefixed with its stem (``pr-title/pr-title``) so two files cannot collide."""
    jobs: dict[str, object] = {}
    for path in sorted((directory or ROOT / ".github" / "workflows").glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not _runs_on_pr_or_push(workflow):
            continue
        for job_id, job in (workflow.get("jobs") or {}).items():
            jobs[job_id if path.name == "ci.yml" else f"{path.stem}/{job_id}"] = job
    return {"jobs": jobs}


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
                problems.append(f"workflow job '{job_id}' ('{name}') is not named in backticks in CONTRIBUTING.md "
                                f"'{HEADING}'")
    every = [n for names in jobs.values() for n in names]
    for doc_name, _ in patterns:
        if not any(_doc_regex(doc_name).fullmatch(n) for n in every):
            problems.append(f"CONTRIBUTING.md '{HEADING}' names `{doc_name}`, which is no job of any workflow")
    return problems


def _contributing() -> str:
    return (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


def test_ci_layout_names_every_job_and_only_real_ones():
    problems = layout_problems(checked_workflows(), _contributing())
    assert not problems, "CONTRIBUTING.md CI layout drift:\n" + "\n".join(problems)


def test_ci_layout_check_actually_sees_the_jobs():
    """Guards the check itself: the section is found and covers the jobs of the real workflow."""
    jobs = workflow_check_names(checked_workflows())
    documented = documented_check_names(ci_layout_section(_contributing()) or "")
    assert len(jobs) >= 10 and len(documented) >= 8, (len(jobs), len(documented))
    assert "backend shard K/N [G2] (<os>)" in documented
    assert sum(len(v) for v in jobs.values()) > len(jobs)  # the shard matrices are expanded


def test_bite_a_job_missing_from_the_prose_is_red_and_names_it():
    workflow = checked_workflows()
    workflow["jobs"]["extra-lint"] = {"name": "extra lint [G1] (ubuntu-latest)", "runs-on": "ubuntu-latest", "steps": []}
    problems = layout_problems(workflow, _contributing())
    assert len(problems) == 1
    assert "extra-lint" in problems[0] and "extra lint [G1] (ubuntu-latest)" in problems[0]


def test_bite_a_matrix_job_without_a_name_is_keyed_on_its_id():
    workflow = checked_workflows()
    workflow["jobs"]["unnamed-job"] = {"runs-on": "ubuntu-latest", "steps": []}
    problems = layout_problems(workflow, _contributing())
    assert len(problems) == 1 and "unnamed-job" in problems[0]


def test_bite_a_documented_name_that_is_no_job_is_red():
    text = _contributing().replace("`commit subjects [G1]`", "`commit subjectz [G1]`")
    assert text != _contributing()
    problems = layout_problems(checked_workflows(), text)
    assert any("commit subjectz [G1]" in p and "no job of any workflow" in p for p in problems)
    assert any("commit subjects [G1]" in p and "not named" in p for p in problems)


def test_near_miss_prose_changes_and_untagged_code_spans_stay_green():
    text = _contributing()
    assert layout_problems(checked_workflows(), text + "\n`ghost step` and `[G1]` and `ubuntu-latest` and `a [b] c`\n") == []
    section = ci_layout_section(text) or ""
    reworded = text.replace(section, section.replace("runs ruff", "executes ruff"))
    assert layout_problems(checked_workflows(), reworded) == []
    # a tagged span OUTSIDE the section is not read, and a fenced block inside it is not either
    other = text.replace(HEADING, "### Something else\n\n`ghost [G1]`\n\n" + HEADING, 1)
    assert layout_problems(checked_workflows(), other) == []
    fenced = text.replace("```bash\npython -m pytest -q -n auto -m", "```bash\n`ghost [G1]`\npython -m pytest -q -n auto -m", 1)
    assert fenced != text and layout_problems(checked_workflows(), fenced) == []


def test_bite_a_job_of_another_pull_request_workflow_counts_and_others_do_not(tmp_path):
    """A second workflow that runs on pull_request is part of the layout; a dispatch-only workflow is not."""
    (tmp_path / "ci.yml").write_text("on: [push, pull_request]\njobs:\n  a:\n    name: a [G1]\n", encoding="utf-8")
    (tmp_path / "title.yml").write_text("on:\n  pull_request:\n    types: [edited]\njobs:\n  t:\n    name: t [G1]\n",
                                        encoding="utf-8")
    (tmp_path / "manual.yml").write_text("on: workflow_dispatch\njobs:\n  m:\n    name: m [G1]\n", encoding="utf-8")
    jobs = checked_workflows(tmp_path)["jobs"]
    assert set(jobs) == {"a", "title/t"}, jobs
    doc = f"{HEADING}\n\n`a [G1]`\n"
    red = layout_problems({"jobs": jobs}, doc)
    assert red == [f"workflow job 'title/t' ('t [G1]') is not named in backticks in CONTRIBUTING.md '{HEADING}'"], red
    assert layout_problems({"jobs": jobs}, doc + "`t [G1]`\n") == []


def test_bite_missing_section_is_red():
    assert layout_problems(checked_workflows(), "# Contributing\n") == [f"CONTRIBUTING.md has no '{HEADING}' section"]


def test_placeholders_match_real_names_only():
    p = _doc_regex("backend shard K/N [G2] (<os>)")
    assert p.fullmatch("backend shard 2/3 [G2] (macos-26-intel)")
    assert not p.fullmatch("backend shard x/y [G2] (macos-26-intel)")
    assert not p.fullmatch("backend shard 2/3 [G2] ()")


# ---------------------------------------------------------------- 2. --check reachability

# script name (scripts/<name>.py) -> ("tests/unit/<file>.py::<test function>", one-line reason). Scripts that no
# workflow ``run:`` value and no ``scripts/gates.py`` constant runs with --check are declared here, with the one
# test that calls them with it. Reviewed by humans: a new entry is a review decision.
TEST_CALLERS: dict[str, tuple[str, str]] = {
    "owner_matrix": ("tests/unit/test_docs_links.py::test_owner_target_capability_matrix_is_up_to_date",
                     "runs the script as a subprocess with --check and asserts exit 0 on the real repo"),
    "ci_durations": ("tests/unit/test_ci_shards.py::test_the_weights_script_refuses_reports_it_cannot_use",
                     "loads the script as a module and asserts main([... '--check']) returns 0 and writes nothing"),
    "github_settings": ("tests/unit/test_github_settings.py::test_identical_live_state_is_ok_and_check_never_writes",
                        "loads the script as a module and runs main(['--check']) against a fake API; it never "
                        "runs in CI (it needs the owner's gh login to read the live settings)"),
    "paste_safe": ("tests/unit/test_owner_commands_paste_safe.py::test_check_cli_exit_codes_and_verdict_line",
                   "loads the script as a module and asserts main(['--check', ...]) exit codes 0, 1 and 2 and the "
                   "verdict line; the same test file runs its rules over every Markdown file of the repository"),
}

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


def _code_strings(tree: ast.AST) -> list[str]:
    """String constants of a module except bare string statements (docstrings): they are prose, not commands."""
    bare = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in bare]


def _workflow_runs(text: str) -> list[str]:
    """The ``run:`` strings of a workflow, from the parsed YAML (a comment is not in the parse tree)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    runs: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "run" and isinstance(value, str):
                    runs.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return runs


def _ast_rule(name: str, tree: ast.AST) -> bool:
    """A list/tuple that holds ``"--check"`` as an element and, somewhere inside it, the script's file name."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            direct = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if CHECK in direct and any(s == f"{name}.py" or s.endswith(f"/{name}.py") or s.endswith(f"\\{name}.py")
                                       for s in _strings(node)):
                return True
    return False


def automatic_callers(root: Path, name: str) -> list[str]:
    """Where scripts/<name>.py is run with --check by CI itself: a workflow ``run:`` value or scripts/gates.py."""
    found: list[str] = []
    for wf in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        if any(_text_rule(name, run) for run in _workflow_runs(wf.read_text(encoding="utf-8"))):
            found.append(wf.relative_to(root).as_posix())
    gates = root / "scripts" / "gates.py"
    if gates.is_file():
        tree = ast.parse(gates.read_text(encoding="utf-8"))
        if any(_text_rule(name, s) for s in _code_strings(tree)) or _ast_rule(name, tree):
            found.append("scripts/gates.py")
    return found


def check_flag_scripts(root: Path) -> list[str]:
    scripts = root / "scripts"
    return sorted(p.stem for p in scripts.glob("*.py") if CHECK in script_flags(p))


def _test_function(tree: ast.Module, func: str) -> ast.AST | None:
    """A function defined at module level or directly in a class of the module."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return node
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == func:
                    return sub
    return None


def _names_script(name: str, tree: ast.Module) -> bool:
    """The module holds a code string constant naming ``<name>.py`` or imports ``<name>``."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(name)}\.py(?!\w)")
    if any(pattern.search(s) for s in _code_strings(tree)):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[-1] == name for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and ((node.module or "").split(".")[-1] == name
                                                 or any(a.name == name for a in node.names)):
            return True
    return False


def registry_problems(root: Path, registry: Mapping[str, tuple[str, str]]) -> list[str]:
    """Every script with a --check flag is run by CI/gates.py or declared in ``registry``; every entry is real.

    A declared test node must exist (module or class level), its body must hold the string constant ``--check``,
    and its file must name the script (a ``<name>.py`` string constant outside docstrings, or an import of it)."""
    problems = []
    defining = check_flag_scripts(root)
    for name in defining:
        if name not in registry and not automatic_callers(root, name):
            problems.append(f"scripts/{name}.py defines {CHECK} but no workflow run: value or scripts/gates.py runs it "
                            f"with {CHECK} and it is not in TEST_CALLERS: wire it in, or register the test that "
                            f"calls it with {CHECK}")
    for name, (node_id, reason) in sorted(registry.items()):
        if name not in defining:
            problems.append(f"stale registry entry scripts/{name}.py: it does not define {CHECK}")
            continue
        if not reason.strip():
            problems.append(f"registry entry scripts/{name}.py has no reason")
        rel, sep, func = node_id.partition("::")
        path = root / rel
        if not sep or not func or not path.is_file():
            problems.append(f"registry entry scripts/{name}.py: test file of '{node_id}' is not a file "
                            f"(expected tests/unit/<file>.py::<test function>)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        node = _test_function(tree, func)
        if node is None:
            problems.append(f"registry entry scripts/{name}.py: test node '{node_id}' does not exist")
        elif CHECK not in _strings(node):
            problems.append(f"registry entry scripts/{name}.py: test node '{node_id}' never uses the string "
                            f"'{CHECK}'")
        elif not _names_script(name, tree):
            problems.append(f"registry entry scripts/{name}.py: {rel} never names {name}.py or imports it")
    return problems


def test_every_check_flag_is_run_by_ci_gates_or_a_declared_test():
    """The registry is reviewed by humans. Proven here: each registered test node exists, uses the string
    '--check' and its file names the script. NOT proven: that the test really runs the script with it, that it
    passes, or that it runs in CI (the rest of the suite does that, not this file)."""
    problems = registry_problems(ROOT, TEST_CALLERS)
    assert not problems, "\n".join(problems)


def test_the_real_check_scripts_are_found_with_their_callers():
    """changelog.py runs in CI; owner_matrix.py, ci_durations.py, github_settings.py and paste_safe.py are declared
    test callers."""
    assert {"changelog", "owner_matrix", "ci_durations", "github_settings", "paste_safe"} <= set(check_flag_scripts(ROOT))
    assert ".github/workflows/ci.yml" in automatic_callers(ROOT, "changelog")
    assert set(TEST_CALLERS) == {"owner_matrix", "ci_durations", "github_settings", "paste_safe"}


# --- bite tests on a temporary tree ---

CHECK_SCRIPT = 'import argparse\nap = argparse.ArgumentParser()\nap.add_argument("--check", action="store_true")\n'
GOOD_TEST = "def test_x():\n    run(['python', 'scripts/orphan.py', '--check'])\n"
NODE = "tests/test_x.py::test_x"


def _repo(tmp_path: Path, workflow: str = "", gates: str = "", test: str = "") -> Path:
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "orphan.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    (tmp_path / "scripts" / "plain.py").write_text("print('no flags')\n", encoding="utf-8")
    (tmp_path / "scripts" / "gates.py").write_text(gates or "x = 1\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(workflow or "name: ci\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(test or GOOD_TEST, encoding="utf-8")
    return tmp_path


def _problems(tmp_path: Path, label: str, registry: Mapping[str, tuple[str, str]] | None = None,
              **kwargs) -> list[str]:
    return registry_problems(_repo(tmp_path / label.replace(" ", "_"), **kwargs), registry or {})


def test_bite_an_unregistered_check_script_nobody_runs_is_red_and_named(tmp_path):
    problems = _problems(tmp_path, "red")
    assert len(problems) == 1 and "scripts/orphan.py" in problems[0] and "TEST_CALLERS" in problems[0], problems
    # near miss: the same tree with the script registered (its test uses --check and names the script) is green
    assert _problems(tmp_path, "green", {"orphan": (NODE, "subprocess")}) == []


def test_bite_a_test_that_merely_exists_does_not_make_an_unregistered_script_a_caller(tmp_path):
    """The old inference counted any test that mentioned the script and '--check' somewhere. Now only a declared one."""
    test = ("import importlib.util\nfrom pathlib import Path\n"
            "def load(p):\n    return importlib.util.module_from_spec(importlib.util.spec_from_file_location('m', p))\n"
            "def test_x(cli):\n    a = load(Path('scripts') / 'orphan.py')\n    cli.main(['--check'])\n")
    problems = _problems(tmp_path, "unrelated", test=test)
    assert len(problems) == 1 and "scripts/orphan.py" in problems[0], problems


def test_bite_a_stale_registry_entry_is_red_and_named(tmp_path):
    entry = (NODE, "subprocess")
    problems = _problems(tmp_path, "plain", {"orphan": entry, "plain": entry})
    assert len(problems) == 1 and "stale registry entry scripts/plain.py" in problems[0], problems
    problems = _problems(tmp_path, "gone", {"orphan": entry, "gone": entry})
    assert len(problems) == 1 and "stale registry entry scripts/gone.py" in problems[0], problems


def test_bite_a_registered_node_that_is_missing_or_does_not_call_with_check_is_red(tmp_path):
    cases = {
        "no function": ({"orphan": ("tests/test_x.py::test_nope", "r")}, "does not exist"),
        "no file": ({"orphan": ("tests/test_nope.py::test_x", "r")}, "is not a file"),
        "no separator": ({"orphan": ("tests/test_x.py", "r")}, "is not a file"),
        "no check string": ({"orphan": (NODE, "r")}, "never uses the string '--check'"),
        "script not named": ({"orphan": (NODE, "r")}, "never names orphan.py"),
        "no reason": ({"orphan": (NODE, " ")}, "has no reason"),
    }
    tests = {"no check string": "def test_x():\n    run(['python', 'scripts/orphan.py'])\n",
             "script not named": "def test_x():\n    run(['python', 'scripts/other.py', '--check'])\n"}
    for label, (registry, message) in cases.items():
        problems = _problems(tmp_path, label, registry, test=tests.get(label, ""))
        assert len(problems) == 1 and "scripts/orphan.py" in problems[0] and message in problems[0], (label, problems)


def test_near_miss_a_registered_node_in_a_class_or_naming_the_script_by_import_is_green(tmp_path):
    cases = {
        "class method": ("class TestX:\n    def test_x(self):\n        run(['scripts/orphan.py', '--check'])\n",
                         "tests/test_x.py::test_x"),
        "name in a helper": ("def _script():\n    return load('scripts/orphan.py')\n"
                             "def test_x():\n    assert _script().main(['--check']) == 0\n", NODE),
        "import": ("import orphan\ndef test_x():\n    assert orphan.main(['--check']) == 0\n", NODE),
    }
    for label, (test, node) in cases.items():
        assert _problems(tmp_path, label, {"orphan": (node, "r")}, test=test) == [], label


def test_bite_a_nested_function_is_not_a_declared_node(tmp_path):
    test = "def helper():\n    def test_x():\n        run(['scripts/orphan.py', '--check'])\n"
    problems = _problems(tmp_path, "nested", {"orphan": (NODE, "r")}, test=test)
    assert len(problems) == 1 and "does not exist" in problems[0], problems


# --- workflow run: values and scripts/gates.py are still read from code, not from comments ---

def test_bite_a_neighbouring_other_tool_check_does_not_count(tmp_path):
    wf = ("jobs:\n  a:\n    steps:\n      - run: python scripts/orphan.py\n      - run: ruff format --check .\n"
          "      - run: python scripts/orphan_two.py --check\n")
    problems = _problems(tmp_path, "neighbour", workflow=wf)
    assert len(problems) == 1 and "scripts/orphan.py" in problems[0]


def test_bite_a_script_name_in_a_comment_is_not_a_caller(tmp_path):
    """Reviewed hole: ``# python scripts/orphan.py --check`` counted as a caller."""
    workflow = ("jobs:\n  a:\n    steps:\n      # python scripts/orphan.py --check\n      - run: echo hello\n"
                "      - name: later  # python scripts/orphan.py --check\n        run: ruff format --check .\n")
    problems = _problems(tmp_path, "workflow comment", workflow=workflow)
    assert len(problems) == 1 and "scripts/orphan.py" in problems[0]
    gates = ('"""Gates. Run python scripts/orphan.py --check by hand."""\n# python scripts/orphan.py --check\n'
             'x = 1  # python scripts/orphan.py --check\n')
    problems = _problems(tmp_path, "gates comment", gates=gates)
    assert len(problems) == 1 and "scripts/orphan.py" in problems[0]


def test_near_miss_a_real_run_value_or_gates_string_stays_a_caller(tmp_path):
    cases = {
        "workflow run": {"workflow": "jobs:\n  a:\n    steps:\n      # note\n"
                                     "      - run: python scripts/orphan.py --check\n"},
        "workflow continuation": {"workflow": "jobs:\n  a:\n    steps:\n      - run: |\n          set -e\n"
                                              "          python scripts/orphan.py \\\n            --check\n"},
        "gates tuple": {"gates": 'GATE = ("python", "scripts/orphan.py", "--check")\n'},
        "gates string": {"gates": 'CMD = "python scripts/orphan.py --check"\n'},
    }
    for label, kwargs in cases.items():
        assert registry_problems(_repo(tmp_path / label.replace(" ", "_"), **kwargs), {}) == [], label
