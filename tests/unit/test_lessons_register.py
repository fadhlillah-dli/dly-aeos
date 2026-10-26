"""The lessons register (docs/agent/LESSONS.md) and the delegation brief stay consistent.

A mistake becomes a rule with its incident, gets counted, and at a threshold becomes a tool. This test
keeps the register honest: every column filled, ids unique, a tool once the threshold is reached, no
rule that can never retire, and every lesson marked for the brief present in the brief. The checker
works on text and a root directory, so the bite tests run it on temporary copies, never on the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
LESSONS = "docs/agent/LESSONS.md"
BRIEF = "docs/agent/DELEGATION-BRIEF.md"
MAX_LINES = 150
LEVELS = {"written-rule", "memory", "process", "test", "ci-gate", "git-hook", "harness-hook", "tool-refusal"}
STRICT_CLASSES = {"data-loss", "secret", "public-exposure"}
CLASSES = STRICT_CLASSES | {"process", "environment", "correctness", "quality"}
RULE_COLUMNS = ["id", "date", "incident", "class", "strikes", "level", "tool", "brief", "stops applying when"]
METHOD_COLUMNS = ["method", "re-runnable proof", "failure it prevents", "retire when"]
_PENDING = re.compile(r"^PENDING:[A-Za-z0-9._/-]+(\s.*)?$")
_OWNER = re.compile(r"^OWNER-SETTING:\S.*$")
_NEVER = re.compile(r"\bnever\b", re.I)
# A leading dot is allowed (.github/workflows/ci.yml, .githooks/..., .claude/...); '..' is caught by the segment
# check and '.' by is_file.
_REPO_PATH = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9._/-]*$")
_LESSON_ID = re.compile(r"^L(\d+)$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_tables(text: str) -> dict[str, list[list[str]]]:
    """Return {first header cell: data rows} for every markdown table in the text."""
    tables: dict[str, list[list[str]]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = _cells(lines[i])
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            tables["|".join(header)] = rows
        else:
            i += 1
    return tables


def _is_repo_file(token: str, root: Path) -> bool:
    """One relative token that names an existing file inside root (no '..', no absolute path)."""
    if not _REPO_PATH.match(token) or ".." in token.split("/"):
        return False
    base = root.resolve()
    target = (base / token).resolve()
    return target.is_relative_to(base) and target.is_file()


def _tool_ok(tool: str, root: Path) -> bool:
    parts = [p.strip() for p in tool.split(" + ")]
    for part in parts:
        if _PENDING.match(part) or _OWNER.match(part) or _is_repo_file(part, root):
            continue
        return False
    return bool(parts)


def lessons_problems(text: str, brief_text: str, root: Path) -> list[str]:
    problems: list[str] = []
    if len(text.splitlines()) > MAX_LINES:
        problems.append(f"file has more than {MAX_LINES} lines")
    tables = parse_tables(text)
    rules = tables.get("|".join(RULE_COLUMNS))
    methods = tables.get("|".join(METHOD_COLUMNS))
    if rules is None:
        return problems + ["table 'Rules with incidents' not found"]
    if methods is None:
        problems.append("table 'Proven methods' not found")
    seen: set[str] = set()
    for row in rules:
        if len(row) != len(RULE_COLUMNS):
            problems.append(f"row {row[:1]}: {len(row)} columns, expected {len(RULE_COLUMNS)}")
            continue
        rec = dict(zip(RULE_COLUMNS, row, strict=True))
        rid = rec["id"]
        for col, value in rec.items():
            if not value:
                problems.append(f"{rid}: column '{col}' is empty")
        if rid in seen:
            problems.append(f"{rid}: duplicate id")
        seen.add(rid)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec["date"]):
            problems.append(f"{rid}: date '{rec['date']}' is not YYYY-MM-DD")
        if rec["class"] not in CLASSES:
            problems.append(f"{rid}: unknown class '{rec['class']}' (use one of {', '.join(sorted(CLASSES))})")
            continue
        if rec["level"] not in LEVELS:
            problems.append(f"{rid}: unknown level '{rec['level']}' (use one of {', '.join(sorted(LEVELS))})")
        if rec["brief"] not in {"yes", "no"}:
            problems.append(f"{rid}: brief must be yes or no")
        if not rec["strikes"].isdigit() or int(rec["strikes"]) < 1:
            problems.append(f"{rid}: strikes must be a number >= 1")
            continue
        due = 1 if rec["class"] in STRICT_CLASSES else 3
        if int(rec["strikes"]) >= due and not _tool_ok(rec["tool"], root):
            problems.append(f"{rid}: strikes {rec['strikes']} reached the threshold {due} but tool "
                            f"'{rec['tool']}' is not an existing repo path, OWNER-SETTING: or PENDING:<branch>")
        if _NEVER.search(rec["stops applying when"]):
            problems.append(f"{rid}: 'stops applying when' must not say never")
        if rec["brief"] == "yes" and not re.search(rf"\b{re.escape(rid)}\b", brief_text):
            problems.append(f"{rid}: brief=yes but the id is missing from DELEGATION-BRIEF.md")
    for row in methods or []:
        if len(row) != len(METHOD_COLUMNS) or not all(row):
            problems.append(f"method row {row[:1]}: every column must be filled")
        elif _NEVER.search(row[3]):
            problems.append(f"method '{row[0]}': 'retire when' must not say never")
    return problems


SEED_IDS = range(1, 17)


def pruned_ids(text: str) -> set[int]:
    """Ids named by entries of the '## Pruned' section: 'Lnn - date - reason', bare or as a markdown list item."""
    section = re.split(r"^## Pruned\s*$", text, maxsplit=1, flags=re.M)
    if len(section) < 2:
        return set()
    body = re.split(r"^## ", section[1], maxsplit=1, flags=re.M)[0]
    return {int(n) for n in re.findall(r"^\s*(?:[*+-]\s+)?L(\d+) - ", body, re.M)}


def removed_seeds(text: str) -> list[int]:
    """Seed ids that are neither a row of table 1 nor named in the Pruned section."""
    rows = parse_tables(text).get("|".join(RULE_COLUMNS), [])
    present = {int(m.group(1)) for m in (_LESSON_ID.match(r[0]) for r in rows if r) if m}
    return sorted(set(SEED_IDS) - present - pruned_ids(text))


def _repo_texts() -> tuple[str, str]:
    return (ROOT / LESSONS).read_text(encoding="utf-8"), (ROOT / BRIEF).read_text(encoding="utf-8")


def test_the_repository_register_is_consistent():
    text, brief = _repo_texts()
    assert lessons_problems(text, brief, ROOT) == []


def test_the_seed_rows_exist_and_ids_are_well_formed():
    text, _ = _repo_texts()
    rows = parse_tables(text)["|".join(RULE_COLUMNS)]
    ids = [r[0] for r in rows]
    numbers = [int(m.group(1)) for m in (_LESSON_ID.match(i) for i in ids) if m]
    assert len(numbers) == len(ids), f"ids must match L<number>: {ids}"
    assert numbers == sorted(set(numbers)), "ids must be ascending and unique"
    assert not removed_seeds(text), f"seed rows removed without a Pruned entry: {removed_seeds(text)}"
    assert len(parse_tables(text)["|".join(METHOD_COLUMNS)]) >= 4


# ---------------------------------------------------------------------------------- bite tests
def _register_without(seed: int, pruned_section: str) -> str:
    live = (ROOT / LESSONS).read_text(encoding="utf-8")
    kept = [ln for ln in live.splitlines() if not re.match(rf"^\| L{seed} \|", ln)]
    assert len(kept) == len(live.splitlines()) - 1
    text = "\n".join(kept) + "\n"
    head, _, _tail = text.partition("## Pruned")
    return head + "## Pruned\n\n" + pruned_section + "\n"


def test_a_seed_row_removed_without_a_pruned_entry_is_red():
    assert removed_seeds(_register_without(16, "None yet.")) == [16]


@pytest.mark.parametrize("entry", ["L16 - 2026-11-01 - obstructs - owner", "* L16 - 2026-11-01 - obstructs - owner",
                                   "- L16 - 2026-11-01 - obstructs - owner", "  - L16 - 2026-11-01 - x - owner"])
def test_a_seed_row_removed_with_a_pruned_entry_is_green(entry):
    assert removed_seeds(_register_without(16, entry)) == []


def test_a_pruned_looking_line_outside_the_pruned_section_does_not_excuse_a_removal():
    text = _register_without(16, "None yet.") + "\n## Later\n\nL16 - 2026-11-01 - elsewhere - owner\n"
    assert removed_seeds(text) == [16]


def test_the_live_register_has_every_seed():
    assert removed_seeds((ROOT / LESSONS).read_text(encoding="utf-8")) == []


# The fixtures are a small synthetic register built in tmp_path, never the live file, so the bite tests
# stay valid when the real register grows, a PENDING tool merges or a row is pruned.
RULE_HEADER = "| " + " | ".join(RULE_COLUMNS) + " |\n|" + "---|" * len(RULE_COLUMNS) + "\n"
METHOD_HEADER = "| " + " | ".join(METHOD_COLUMNS) + " |\n|" + "---|" * len(METHOD_COLUMNS) + "\n"
BASE_ROWS = {
    "L1": ["L1", "2026-10-26", "A key was pasted", "secret", "1", "written-rule", "PENDING:fix/x", "yes", "Keys leave chat"],
    "L2": ["L2", "2026-10-26", "A changelog conflict", "process", "3", "ci-gate", "scripts/real_tool.py", "yes", "Fragments exist"],
    "L3": ["L3", "2026-10-26", "A claim was too strong", "process", "2", "written-rule", "-", "no", "Evidence records exist"],
    "L4": ["L4", "2026-10-26", "A link removed a venv", "data-loss", "1", "written-rule", "OWNER-SETTING:deny links", "yes", "Tool refuses"],
}
BASE_METHODS = [["Blind verifier", "Re-run on the claim", "A claim confirmed by reasoning", "A tool derives it"]]
BASE_BRIEF = "Rules [L1] [L2] [L4] are in the block.\n"


def build(rows: dict[str, list[str]] | None = None, methods: list[list[str]] | None = None, *, with_methods: bool = True) -> str:
    out = "# LESSONS\n\n" + RULE_HEADER
    for row in (rows or BASE_ROWS).values():
        out += "| " + " | ".join(row) + " |\n"
    if with_methods:
        out += "\n" + METHOD_HEADER
        for m in BASE_METHODS if methods is None else methods:
            out += "| " + " | ".join(m) + " |\n"
    return out


def with_cell(rid: str, column: str, value: str) -> str:
    """The base register with one cell changed; asserts the change is real, so no bite is a no-op."""
    rows = {k: list(v) for k, v in BASE_ROWS.items()}
    idx = RULE_COLUMNS.index(column)
    assert rows[rid][idx] != value, "the replacement must differ from the base value"
    rows[rid][idx] = value
    return build(rows)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "real_tool.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    return tmp_path


def problems(text: str, root: Path, brief: str = BASE_BRIEF) -> list[str]:
    return lessons_problems(text, brief, root)


def test_valid_synthetic_register_is_green(root):
    assert problems(build(), root) == []


def test_strike_three_with_no_tool_is_red(root):
    assert any("L2" in p and "threshold 3" in p for p in problems(with_cell("L2", "tool", "-"), root))


def test_strict_class_at_strike_one_with_no_tool_is_red(root):
    assert any("L4" in p and "threshold 1" in p for p in problems(with_cell("L4", "tool", "-"), root))
    assert any("L1" in p and "threshold 1" in p for p in problems(with_cell("L1", "tool", "-"), root))


def test_non_strict_class_at_strike_two_without_tool_is_green(root):
    assert BASE_ROWS["L3"][6] == "-" and BASE_ROWS["L3"][3] == "process" and BASE_ROWS["L3"][4] == "2"
    assert problems(build(), root) == []


@pytest.mark.parametrize("tool", ["scripts/missing_tool.py", "docs", ".", "..", "../outside.py", "/etc/hosts",
                                  "scripts/../scripts/real_tool.py", "docs nothing built", "scripts/real_tool.py extra",
                                  "C:/Windows/win.ini", "scripts\\real_tool.py"])
def test_tool_that_is_not_an_existing_repo_file_is_red(root, tool):
    assert any("L2" in p and "threshold" in p for p in problems(with_cell("L2", "tool", tool), root))


def test_a_dot_prefixed_repo_file_is_a_tool_but_dot_and_dotdot_are_not(root):
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (root / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    for tool in (".github/workflows/ci.yml", ".gitattributes"):
        assert problems(with_cell("L2", "tool", tool), root) == [], tool
    for bad in (".", "./", "..", ".github", ".github/workflows/missing.yml"):  # a folder or a missing file
        assert any("L2" in p and "threshold" in p for p in problems(with_cell("L2", "tool", bad), root)), bad


def test_a_symlink_that_leaves_the_root_is_not_a_repo_file(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "target.py"
    outside.write_text("x\n", encoding="utf-8")
    link = root / "scripts" / "link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks need a privilege on this host")
    assert any("L2" in p and "threshold" in p for p in problems(with_cell("L2", "tool", "scripts/link.py"), root))
    inside = root / "scripts" / "inside_link.py"  # near miss: a link that stays inside the root is a repo file
    inside.symlink_to(root / "scripts" / "real_tool.py")
    assert problems(with_cell("L2", "tool", "scripts/inside_link.py"), root) == []


def test_unknown_class_and_level_messages_list_the_allowed_values(root):
    msg = next(p for p in problems(with_cell("L4", "class", "security"), root) if "unknown class" in p)
    assert all(c in msg for c in CLASSES)
    msg = next(p for p in problems(with_cell("L3", "level", "hook"), root) if "unknown level" in p)
    assert all(lv in msg for lv in LEVELS)


def test_existing_repo_file_is_green_and_so_is_a_combined_tool(root):
    assert problems(with_cell("L2", "tool", "scripts/real_tool.py + OWNER-SETTING:a hook"), root) == []
    assert problems(with_cell("L4", "tool", "PENDING:feat/x scripts/real_tool.py"), root) == []
    assert problems(with_cell("L4", "tool", "scripts/real_tool.py"), root) == []


def test_a_file_outside_the_root_is_not_a_repo_file(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "tool.py"
    outside.write_text("x\n", encoding="utf-8")
    assert not _is_repo_file(str(outside), root)
    assert not _is_repo_file("../" + outside.parent.name + "/tool.py", root)


@pytest.mark.parametrize("cls", ["public", "Data-Loss", "data_loss", "secrets", "other"])
def test_unknown_class_is_red_even_at_strike_one_without_a_tool(root, cls):
    assert any("L4" in p and "unknown class" in p for p in problems(with_cell("L4", "class", cls), root))
    rows = {k: list(v) for k, v in BASE_ROWS.items()}
    rows["L4"][3], rows["L4"][6] = cls, "-"
    assert any("L4" in p and "unknown class" in p for p in problems(build(rows), root))


def test_every_known_class_is_green(root):
    for cls in sorted(CLASSES):
        rows = {k: list(v) for k, v in BASE_ROWS.items()}
        rows["L3"][3], rows["L3"][6] = cls, "OWNER-SETTING:any"  # strict classes need a tool at strike 1
        assert problems(build(rows), root) == [], cls


def test_line_151_is_red_and_line_150_is_green(root):
    lines = build().splitlines()
    assert len(lines) <= MAX_LINES
    exactly = "\n".join(lines + [""] * (MAX_LINES - len(lines))) + "\n"
    assert len(exactly.splitlines()) == MAX_LINES
    assert problems(exactly, root) == []
    too_long = exactly + "x\n"
    assert len(too_long.splitlines()) == MAX_LINES + 1
    assert any("150" in p for p in problems(too_long, root))


def test_brief_id_missing_from_the_brief_is_red(root):
    assert any("L4" in p and "DELEGATION-BRIEF" in p for p in problems(build(), root, "Rules [L1] [L2] only.\n"))
    # near miss: L1 must not be satisfied by L10 (word boundary), and brief=no needs no mention
    assert any(p.startswith("L1:") for p in problems(build(), root, "Rules [L10] [L2] [L4].\n"))
    assert problems(build(), root, BASE_BRIEF) == []


def test_never_in_a_retire_column_is_red(root):
    assert any("L3" in p and "never" in p for p in problems(with_cell("L3", "stops applying when", "Never"), root))
    bad_method = [["Blind verifier", "Re-run", "A claim", "never"]]
    assert any("Blind verifier" in p for p in problems(build(methods=bad_method), root))
    # near miss: the word inside another word is fine
    assert problems(with_cell("L3", "stops applying when", "Whenever claims are generated"), root) == []
    assert problems(build(methods=[["Blind verifier", "Re-run", "A claim", "Whenever a tool exists"]]), root) == []


def test_empty_column_and_duplicate_id_are_red(root):
    assert any("L3" in p and "empty" in p for p in problems(with_cell("L3", "brief", ""), root))
    rows = {k: list(v) for k, v in BASE_ROWS.items()}
    rows["L3"][0] = "L2"
    assert any("duplicate" in p for p in problems(build(rows), root))


@pytest.mark.parametrize("date", ["26-10-2026", "2026-1-5", "2026/10/26", "yesterday", "2026-10-26T10:00"])
def test_malformed_date_is_red_and_iso_date_is_green(root, date):
    assert any("L3" in p and "YYYY-MM-DD" in p for p in problems(with_cell("L3", "date", date), root))
    assert problems(with_cell("L3", "date", "2027-01-31"), root) == []


@pytest.mark.parametrize("level", ["rule", "Written-Rule", "hook", "ci"])
def test_unknown_level_is_red_and_every_known_level_is_green(root, level):
    assert any("L3" in p and "unknown level" in p for p in problems(with_cell("L3", "level", level), root))
    for known in sorted(LEVELS - {"written-rule"}):
        assert problems(with_cell("L3", "level", known), root) == [], known


@pytest.mark.parametrize("brief", ["maybe", "Yes", "true", "y"])
def test_brief_other_than_yes_or_no_is_red_and_yes_no_are_green(root, brief):
    assert any("L3" in p and "yes or no" in p for p in problems(with_cell("L3", "brief", brief), root))
    assert BASE_ROWS["L3"][7] == "no" and problems(build(), root) == []  # near miss: the base value 'no'
    assert problems(with_cell("L3", "brief", "yes"), root, BASE_BRIEF + "[L3]\n") == []


def test_missing_methods_table_is_red_and_present_table_is_green(root):
    assert any("Proven methods" in p for p in problems(build(with_methods=False), root))
    assert problems(build(), root) == []
    assert any("every column must be filled" in p for p in problems(build(methods=[["A", "", "B", "C"]]), root))


def test_missing_rules_table_is_red(root):
    assert any("Rules with incidents" in p for p in problems("# LESSONS\n\nno table\n", root))


def test_strikes_must_be_a_positive_number(root):
    for bad in ("0", "x", "-1", "1.5"):
        assert any("strikes must be" in p for p in problems(with_cell("L3", "strikes", bad), root)), bad
    assert problems(with_cell("L3", "strikes", "1"), root) == []
