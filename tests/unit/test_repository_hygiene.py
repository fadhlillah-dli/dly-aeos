"""Repository hygiene for the public, all-rights-reserved repository.

The licence, the package metadata, the citation file, the changelog and the release notes must
agree on holder, licence and version, and the governance files must exist. No tracked file may
carry an AI-assistant attribution line: commits and files carry no attribution (CONTRIBUTING.md).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
import warnings
from pathlib import Path

import pytest
import yaml

from aeos.core.attribution import AI_VENDOR_DOMAINS, ai_identity_problem, find_ai_attribution
from aeos.core.paths import repo_root
from aeos.core.pii import redact_pii

ROOT = repo_root()
REPO_URL = "https://github.com/fadhlillah-dli/dly-aeos"
OWNER_HANDLE = "@fadhlillah-dli"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]
VERSION = PROJECT["version"]
HOLDER = PROJECT["authors"][0]["name"]
LICENSE_TEXT = (ROOT / "LICENSE").read_text(encoding="utf-8")
SPDX_LINE = re.compile(r"^SPDX-License-Identifier:\s*(\S+)\s*$", re.M)

GOVERNANCE_FILES = (
    "LICENSE", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "CITATION.cff",
    ".github/CODEOWNERS", ".github/PULL_REQUEST_TEMPLATE.md", ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/licence_request.yml",
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _license_spdx() -> str:
    found = SPDX_LINE.findall(LICENSE_TEXT)
    assert len(found) == 1, "LICENSE must carry exactly one SPDX-License-Identifier line"
    return found[0]


# ------------------------------------------------------------------ licence and package metadata
def test_license_reserves_all_rights_for_the_named_holder():
    assert HOLDER == "Fadhlillah"
    assert f"Copyright (c) 2026 {HOLDER}. All rights reserved." in LICENSE_TEXT
    assert "No licence is granted" in LICENSE_TEXT
    assert _license_spdx().startswith("LicenseRef-"), "a proprietary licence is a LicenseRef, never an SPDX list id"


def test_pyproject_licence_is_the_pep639_expression_of_the_license_file():
    assert PROJECT["license"] == _license_spdx(), "pyproject license must be the SPDX expression from LICENSE"
    assert PROJECT["license-files"] == ["LICENSE"]
    assert PROJECT["authors"] == [{"name": HOLDER}], "no e-mail address in the package metadata"
    # PEP 639: a licence expression and licence classifiers are mutually exclusive
    assert not [c for c in PROJECT.get("classifiers", []) if c.startswith("License ::")]
    requires = " ".join(PYPROJECT["build-system"]["requires"])
    m = re.search(r"setuptools>=(\d+)\.(\d+)\.(\d+)", requires)
    assert m and tuple(int(g) for g in m.groups()) >= (77, 0, 3), "PEP 639 metadata needs setuptools>=77.0.3"


def test_frontend_package_is_private_and_unlicensed():
    pkg = json.loads(_read("frontend/package.json"))
    assert pkg["private"] is True, "a private package can never be published to the npm registry by mistake"
    assert pkg["license"] == "UNLICENSED"
    assert pkg["version"] == VERSION
    root = json.loads(_read("frontend/package-lock.json"))["packages"][""]
    assert root["license"] == "UNLICENSED" and root["version"] == VERSION, "lockfile root entry out of date"


# ------------------------------------------------------------------ citation, changelog, release notes
def test_citation_cff_parses_with_the_required_keys_and_the_project_version():
    cff = yaml.safe_load(_read("CITATION.cff"))
    assert {"cff-version", "message", "title", "authors"} <= set(cff), "CFF 1.2.0 required keys"
    assert cff["cff-version"] == "1.2.0" and cff["type"] == "software"
    assert cff["version"] == VERSION, "CITATION.cff version must equal pyproject version"
    assert cff["authors"] == [{"name": HOLDER}], "author is the holder, with no e-mail address"
    assert isinstance(cff["date-released"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cff["date-released"])
    assert cff["repository-code"] == REPO_URL
    assert cff["license-url"] == f"{REPO_URL}/blob/main/LICENSE"
    # CFF `license` accepts SPDX list ids only; a LicenseRef is expressed through license-url instead
    assert "license" not in cff
    assert cff["keywords"] and all(isinstance(k, str) and k for k in cff["keywords"])


def _release_check_script():
    spec = importlib.util.spec_from_file_location("aeos_script_release_check", ROOT / "scripts" / "release_check.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_every_version_source_declares_the_project_version():
    # The list of version sources lives once, in scripts/release_check.py (which also checks a release tag against
    # it). The fifth source, backend/aeos/__init__.py (__version__), is what `aeos ops release` reads.
    script = _release_check_script()
    declared = {rel: script.extract_version(rel, _read(rel)) for rel, _ in script.VERSION_SOURCES}
    assert set(declared) >= {"pyproject.toml", "frontend/package.json", "frontend/package-lock.json",
                             "CITATION.cff", "backend/aeos/__init__.py"}
    assert declared == {rel: VERSION for rel in declared}, declared


def assert_changelog_structure(log: str) -> None:
    """The CHANGELOG.md structure test. Fragments (changelog.d/) are moved into [Unreleased] by
    ``scripts/changelog.py --assemble``, so an assembled file must pass exactly this."""
    assert "keepachangelog.com/en/1.1.0" in log and "semver.org" in log
    assert re.search(r"^## \[Unreleased\]$", log, re.M)
    m = re.search(rf"^## \[{re.escape(VERSION)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", log, re.M)
    assert m, f"CHANGELOG.md has no '## [{VERSION}] - YYYY-MM-DD' entry"
    assert m.group(1) == yaml.safe_load(_read("CITATION.cff"))["date-released"], "release dates disagree"
    assert f"[{VERSION}]: {REPO_URL}/releases/tag/v{VERSION}" in log
    assert re.search(r"^\[unreleased\]: \S+/compare/v" + re.escape(VERSION) + r"\.\.\.HEAD$", log, re.M | re.I)
    assert _changelog_script().structure_problems(log) == []


def _changelog_script():
    spec = importlib.util.spec_from_file_location("aeos_script_changelog", ROOT / "scripts" / "changelog.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_changelog_has_unreleased_and_the_current_version_with_its_release_link():
    assert_changelog_structure(_read("CHANGELOG.md"))


def test_changelog_fragments_are_valid_and_count_as_part_of_unreleased():
    # A pull request adds changelog.d/<slug>.md instead of editing CHANGELOG.md; until the release assembles them
    # they ARE the pending [Unreleased] entries, so each must be well formed.
    script = _changelog_script()
    files, problems = script.check_fragments(ROOT)
    assert problems == [], problems
    assert (ROOT / "changelog.d" / "README.md").is_file()
    assert all(f.suffix == ".md" and f.name != "README.md" for f in files)
    for rel in ("CONTRIBUTING.md", ".github/PULL_REQUEST_TEMPLATE.md"):
        assert "changelog.d/" in _read(rel), f"{rel} must tell contributors to add a fragment"


def test_release_notes_exist_for_the_current_version():
    notes = _read(f"docs/releases/v{VERSION}.md")
    assert notes.startswith(f"# DLY AEOS v{VERSION}")
    for section in ("## Supported hosts", "## What needs owner setup", "## Known limitations and NOT-PROVEN",
                    "## Upgrade notes", "## Licence"):
        assert section in notes, section
    assert "All rights reserved" in notes and "aeos db upgrade" in notes


MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def test_release_notes_links_work_as_a_github_release_body():
    # The file is pasted as the GitHub Release body, where a relative link resolves against the releases
    # page and breaks; every link is therefore absolute, and links into the repository are pinned to the tag.
    notes = _read(f"docs/releases/v{VERSION}.md")
    links = MD_LINK.findall(notes)
    assert links, "the release notes link nothing"
    relative = [t for t in links if not re.match(r"^https?://", t)]
    assert not relative, f"relative or in-page links break in a release body: {relative}"
    pinned = f"{REPO_URL}/blob/v{VERSION}/"
    missing = [t for t in links if t.startswith(pinned) and not (ROOT / t[len(pinned):].split("#", 1)[0]).is_file()]
    assert not missing, f"tag-pinned links to files that do not exist: {missing}"
    unpinned = [t for t in links if t.startswith(f"{REPO_URL}/blob/") and not t.startswith(pinned)]
    assert not unpinned, f"repository links must be pinned to v{VERSION}: {unpinned}"


# ------------------------------------------------------------------ governance files and GitHub config
@pytest.mark.parametrize("rel", GOVERNANCE_FILES + ("README.md", f"docs/releases/v{VERSION}.md"))
def test_public_file_exists_and_contains_no_personal_identifier(rel):
    text = _read(rel)
    assert text.strip(), f"{rel} is empty"
    # public repository: contact paths are GitHub mechanisms only (no e-mail address, no phone number)
    assert redact_pii(text) == text, f"{rel} contains a personal identifier"


def test_security_policy_points_to_private_vulnerability_reporting():
    sec = _read("SECURITY.md")
    assert f"{REPO_URL}/security/advisories/new" in sec
    assert "0.1.x" in sec and "public issue" in sec


def test_contributing_refuses_external_pull_requests_and_names_the_licence_request():
    text = _read("CONTRIBUTING.md")
    assert "External pull requests are not accepted" in text
    assert '"Licence request"' in text and '"Licence request"' in LICENSE_TEXT


def test_code_of_conduct_is_the_contributor_covenant_with_its_attribution():
    coc = _read("CODE_OF_CONDUCT.md")
    assert coc.startswith("# Contributor Covenant Code of Conduct")
    assert "version 2.1" in coc and "https://www.contributor-covenant.org/version/2/1/code_of_conduct.html" in coc
    assert "[INSERT CONTACT METHOD]" not in coc and OWNER_HANDLE in coc


def test_codeowners_assigns_every_path_to_the_owner():
    lines = [ln.split() for ln in _read(".github/CODEOWNERS").splitlines() if ln.strip() and not ln.startswith("#")]
    assert ["*", OWNER_HANDLE] in lines


def test_issue_forms_parse_and_blank_issues_are_off():
    config = yaml.safe_load(_read(".github/ISSUE_TEMPLATE/config.yml"))
    assert config["blank_issues_enabled"] is False
    assert f"{REPO_URL}/security/advisories/new" in [link["url"] for link in config["contact_links"]]
    for link in config["contact_links"]:
        assert link["name"] and link["url"].startswith("https://") and link["about"], link
    for name in ("bug_report", "feature_request", "licence_request"):
        form = yaml.safe_load(_read(f".github/ISSUE_TEMPLATE/{name}.yml"))
        assert form["name"] and form["description"] and form["body"], name
        allowed = {"name", "description", "body", "title", "labels", "assignees", "projects", "type"}
        assert set(form) <= allowed, f"{name}: top-level keys {set(form) - allowed} are not permitted"
        _check_issue_form_body(name, form["body"])
    licence = yaml.safe_load(_read(".github/ISSUE_TEMPLATE/licence_request.yml"))
    assert licence["title"] == "Licence request", "LICENSE section 8 names this exact issue title"


# GitHub issue-form syntax and its documented validation errors ("Syntax for GitHub's form schema", "Common
# validation errors when creating issue forms"). A form that breaks these is dropped from the issue chooser.
_FORM_TYPES = {"markdown": {"value"}, "input": {"label"}, "textarea": {"label"}, "dropdown": {"label", "options"},
               "checkboxes": {"label", "options"}}
_FORM_BLOCK_KEYS = {"type", "id", "attributes", "validations"}
_FORM_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _check_issue_form_body(name: str, body: list[dict]) -> None:
    ids, labels = [], []
    for item in body:
        kind, attrs = item.get("type"), item.get("attributes") or {}
        assert kind in _FORM_TYPES, f"{name}: unknown element type {kind!r}"
        assert set(item) <= _FORM_BLOCK_KEYS, f"{name}: block keys {set(item) - _FORM_BLOCK_KEYS} are not permitted"
        assert _FORM_TYPES[kind] <= set(attrs), f"{name}: {kind} needs {_FORM_TYPES[kind]}"
        if kind == "markdown":
            continue
        assert _FORM_ID.match(item.get("id", "")), f"{name}: {kind} needs an id of letters, digits, - or _"
        ids.append(item["id"])
        label = attrs["label"]
        assert isinstance(label, str) and label.strip(), f"{name}: {item['id']} label must be a non-empty string"
        labels.append(label)
        if kind in ("input", "textarea"):
            assert "password" not in label.lower(), f"{name}: GitHub rejects 'password' in an input/textarea label"
        if kind == "dropdown":
            opts = attrs["options"]
            assert opts and all(isinstance(o, str) and o.strip() for o in opts), f"{name}: empty dropdown option"
            assert len(opts) == len(set(opts)), f"{name}: dropdown options must be unique"
            assert "none" not in {o.strip().lower() for o in opts}, f"{name}: 'None' is reserved in dropdowns"
        if kind == "checkboxes":
            boxes = attrs["options"]
            assert all(isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"].strip()
                       for o in boxes), f"{name}: every checkbox needs a label"
            labels += [o["label"] for o in boxes]  # unique among peers and among the other fields
    assert ids, f"{name}: a form needs at least one field besides markdown"
    assert len(ids) == len(set(ids)), f"{name}: field ids must be unique"
    assert len(labels) == len(set(labels)), f"{name}: field and checkbox labels must be unique"


def test_issue_form_checker_rejects_broken_forms():
    good = [{"type": "markdown", "attributes": {"value": "x"}},
            {"type": "input", "id": "v", "attributes": {"label": "Version"}}]
    _check_issue_form_body("good", good)
    broken = {
        "no field": good[:1],
        "unknown type": good + [{"type": "select", "id": "s", "attributes": {"label": "S"}}],
        "missing label": good + [{"type": "textarea", "id": "t", "attributes": {}}],
        "bad id": good + [{"type": "textarea", "id": "has space", "attributes": {"label": "T"}}],
        "duplicate id": good + [{"type": "textarea", "id": "v", "attributes": {"label": "T"}}],
        "duplicate label": good + [{"type": "textarea", "id": "t", "attributes": {"label": "Version"}}],
        "reserved option": good + [{"type": "dropdown", "id": "d", "attributes": {"label": "D", "options": ["None"]}}],
        "repeated option": good + [{"type": "dropdown", "id": "d", "attributes": {"label": "D", "options": ["a", "a"]}}],
        "unknown block key": good + [{"type": "textarea", "id": "t", "label": "T", "attributes": {"label": "T"}}],
        "password label": good + [{"type": "input", "id": "p", "attributes": {"label": "Your password"}}],
        "checkbox label clash": good + [{"type": "checkboxes", "id": "c",
                                         "attributes": {"label": "C", "options": [{"label": "Version"}]}}],
    }
    for case, body in broken.items():
        with pytest.raises(AssertionError):
            _check_issue_form_body(case, body)


def test_readme_states_the_licence_and_links_the_governance_files():
    readme = _read("README.md")
    assert "actions/workflows/ci.yml/badge.svg?branch=main" in readme
    assert "## Licence" in readme and "## Security" in readme and "## Contributing" in readme
    for target in ("(LICENSE)", "(SECURITY.md)", "(CONTRIBUTING.md)"):
        assert target in readme, target
    assert readme.rstrip().endswith(f"Copyright (c) 2026 {HOLDER}. All rights reserved.")


# ------------------------------------------------------------------ no AI attribution anywhere
# One implementation (AGENTS.md invariant 11, ADR-0121): this module holds NO attribution regex of its own. What counts
# as AI attribution is decided by aeos.core.attribution (find_ai_attribution / ai_identity_problem) alone; a second,
# private pattern set drifts from it (2026-10-26: misses and false positives). A gap in the guard is fixed in the
# guard, never patched here. Samples below are assembled from parts so that this file holds no marker verbatim.
AI_ADDRESS = "noreply" + "@" + "anthropic.com"
ATTRIBUTION_GUARD_MODULE = "backend/aeos/core/attribution.py"
GUARD_IMPORT = "aeos.core.attribution"
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".docx", ".xlsx", ".pptx", ".zip",
                   ".gz", ".sqlite3", ".sqlite", ".woff", ".woff2", ".ttf", ".otf", ".pyc"}


def parse_python(source: str | bytes) -> ast.Module:
    """Parse Python source the way the interpreter reads a file: bytes keep their BOM and coding cookie handling, and a
    str that starts with a BOM (a decoded file) is read without it. Raises SyntaxError / ValueError when unparsable."""
    return ast.parse(source.lstrip("\ufeff") if isinstance(source, str) else source)


def imports_attribution_guard(text: str | bytes) -> bool:
    """True when the Python source really imports aeos.core.attribution (``import aeos.core.attribution``,
    ``from aeos.core.attribution import x`` or ``from aeos.core import attribution``). Decided on the syntax tree, so a
    comment, a docstring or a string that merely names the module proves nothing."""
    try:
        tree = parse_python(text)
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == GUARD_IMPORT or a.name.startswith(GUARD_IMPORT + ".")
                                                for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == GUARD_IMPORT or (node.module or "").startswith(GUARD_IMPORT + "."):
                return True
            if node.module == "aeos.core" and any(a.name == "attribution" for a in node.names):
                return True
    return False


# The product's own guard (ADR-0121) and the tests that prove it refuses attribution must contain samples of what it
# refuses. Only those may: the guard module itself and test files that import it.
def _is_guard_sample_file(rel: str, text: str | bytes) -> bool:
    if rel == ATTRIBUTION_GUARD_MODULE:
        return True
    return rel.startswith("tests/") and rel.endswith(".py") and imports_attribution_guard(text)


# The guard screens OUTBOUND text, so it is deliberately conservative: product prose such as "AI-assisted software
# engineering" or an Indonesian doctor message "dibuat dengan Claude Code <version>" (a recorded tool version) reads to
# it as an AI-produced statement. These reviewed lines are the only ones accepted: (file, finding code, fragment of
# the offending line). A new line in the same file is still checked; an entry that no longer matches anything is
# stale and fails the scan, so the list can only shrink. Do not add a regex here: reword the line or extend the guard.
REVIEWED_PROSE = (
    ("CITATION.cff", "GENERATED_WITH_AI", "AI-assisted software engineering"),
    ("CITATION.cff", "GENERATED_WITH_AI", "AI-assisted development"),
    ("README.md", "GENERATED_WITH_AI", "AI-assisted software engineering"),
    ("docs/releases/v0.1.0.md", "GENERATED_WITH_AI", "control plane for AI-assisted"),
    ("docs/OPERATIONS.md", "GENERATED_WITH_AI", '"AI-generated"'),
    ("backend/aeos/doctor/checks/claude.py", "GENERATED_WITH_AI", "dibuat dengan Claude Code {version}"),
    ("backend/aeos/doctor/checks/worker.py", "GENERATED_WITH_AI", "dibuat dengan OpenCode {recorded}"),
)


def attribution_findings(rel: str, text: str, reviewed: set[int] | None = None,
                         prose: tuple[tuple[str, str, str], ...] | None = None) -> list[str]:
    """The canonical guard's findings for *text* as ``rel: ...`` lines, minus the reviewed prose. A finding is accepted
    only when the WHOLE text, with the reviewed fragments removed from that one line, still has no finding reported at
    that line: a real attribution appended to a reviewed line, or one that wraps onto the next line, stays red. Indexes
    of *prose* (default REVIEWED_PROSE) entries whose fragment sits on a finding line are added to *reviewed* (for
    the stale-entry check)."""
    entries = REVIEWED_PROSE if prose is None else prose
    lines = text.split("\n")
    found: list[str] = []
    for finding in find_ai_attribution(text):
        raw = lines[finding.line - 1].rstrip("\r") if 0 < finding.line <= len(lines) else ""
        # every reviewed fragment of this file and code that sits on the line (a line may quote several forms)
        matches = [i for i, (r, code, fragment) in enumerate(entries)
                   if r == rel and code == finding.code and fragment in raw]
        if reviewed is not None:
            reviewed.update(matches)
        if matches:
            residue = raw
            for i in matches:
                residue = residue.replace(entries[i][2], "")
            rest = [*lines[:finding.line - 1], residue, *lines[finding.line:]]
            if not any(f.line == finding.line for f in find_ai_attribution("\n".join(rest))):
                continue
        found.append(f"{rel}: {finding.describe()}")
    return found


def scan_attribution(files: dict[str, str], prose: tuple[tuple[str, str, str], ...] | None = None
                     ) -> tuple[list[str], list[tuple[str, str, str]]]:
    """(findings, stale reviewed-prose entries) for a ``{relative path: text}`` map. Guard-sample files are skipped. An
    entry is stale when its fragment sits on no finding line of its file, or when the scan of its file gives the same
    findings without it (an unneeded entry would excuse a real attribution that happens to contain its words)."""
    entries = REVIEWED_PROSE if prose is None else prose
    findings: list[str] = []
    reviewed: set[int] = set()
    per_file: dict[str, list[str]] = {}
    for rel, text in files.items():
        if _is_guard_sample_file(rel, text):
            continue
        per_file[rel] = attribution_findings(rel, text, reviewed, entries)
        findings += per_file[rel]
    for i in sorted(reviewed):
        rel = entries[i][0]
        without = tuple(e for n, e in enumerate(entries) if n != i)
        if attribution_findings(rel, files[rel], None, without) == per_file[rel]:
            reviewed.discard(i)  # nothing needs this entry
    return findings, [entry for i, entry in enumerate(entries) if i not in reviewed]


def _repository_files() -> list[str]:
    # tracked files plus new files not yet committed (a fresh change), never ignored ones
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return sorted({line for line in out.splitlines() if line})


def test_attribution_detector_flags_trailers_footers_and_the_address():
    trailer = "co-authored" + "-by: Claude <" + AI_ADDRESS + ">"
    assert attribution_findings("x.md", f"fix: y\n\n{trailer}\n")
    assert attribution_findings("x.md", "  Co-Authored" + "-By: dependabot[bot]\n")
    assert attribution_findings("x.md", "\U0001f916 Generated" + " with [Claude Code](https://claude.ai)\n")
    assert attribution_findings("x.md", "reach " + AI_ADDRESS.upper() + "\n")
    # a human co-author and product prose about the Claude Code supervisor are not attribution
    assert not attribution_findings("x.md", "Co-Authored" + "-By: Jane Doe\n")
    assert not attribution_findings("x.md", "Claude supervises through the official Claude Code client.\n")
    assert not attribution_findings("x.md", "Never add co-authored" + "-by: lines (rule, mid-sentence).\n")


def test_holder_and_author_identities_are_not_ai_identities():
    names = [HOLDER] + [a["name"] for a in yaml.safe_load(_read("CITATION.cff"))["authors"]]
    for name in names:
        assert ai_identity_problem(name, None) is None, f"{name!r} reads as an AI or bot identity"
    # bite: the same guard refuses an AI or bot identity and an AI vendor address, and accepts a human one
    assert ai_identity_problem("dependabot[bot]", None)
    assert ai_identity_problem("Claude", None)
    assert ai_identity_problem("Jane Doe", AI_ADDRESS)
    assert ai_identity_problem("Jane Doe", "you@example.com") is None


def test_only_the_guard_and_its_tests_may_hold_attribution_samples():
    sample = "S = '" + "Generated" + " with Claude'\n"
    importing = "from aeos.core.attribution import find_ai_attribution\n"
    assert _is_guard_sample_file(ATTRIBUTION_GUARD_MODULE, "")
    assert _is_guard_sample_file("tests/unit/test_x.py", importing + sample)
    assert _is_guard_sample_file("tests/unit/test_x.py", "import aeos.core.attribution as a\n" + sample)
    assert _is_guard_sample_file("tests/unit/test_x.py", "from aeos.core import attribution\n" + sample)
    assert not _is_guard_sample_file("tests/unit/test_x.py", sample)  # a test that does not exercise the guard
    assert not _is_guard_sample_file("docs/x.md", importing + sample)  # docs are never exempt
    assert not _is_guard_sample_file("backend/aeos/other.py", importing + sample)
    # near misses: the marker only in a comment, a docstring, a string or a lookalike module is NOT an import
    assert not _is_guard_sample_file("tests/unit/test_x.py", "# from aeos.core.attribution import find_ai_attribution\n" + sample)
    assert not _is_guard_sample_file("tests/unit/test_x.py", '"""aeos.core.attribution"""\n' + sample)
    assert not _is_guard_sample_file("tests/unit/test_x.py", "MARK = 'aeos.core.attribution'\n" + sample)
    assert not _is_guard_sample_file("tests/unit/test_x.py", "from aeos.core.attribution_other import x\n" + sample)
    assert not _is_guard_sample_file("tests/unit/test_x.py", "from other.aeos.core.attribution import x\n" + sample)
    assert not _is_guard_sample_file("tests/unit/test_x.py", "def broken(:\n" + sample)  # unparsable: not exempt
    # a byte order mark (a Windows editor) does not hide a real import, bytes or text; it grants nothing without one
    assert _is_guard_sample_file("tests/unit/test_x.py", "\ufeff" + importing + sample)
    assert _is_guard_sample_file("tests/unit/test_x.py", b"\xef\xbb\xbf" + (importing + sample).encode("utf-8"))
    assert not _is_guard_sample_file("tests/unit/test_x.py", "\ufeff# " + importing + sample)


def test_no_file_carries_an_ai_attribution_line():
    files: dict[str, str] = {}
    for rel in _repository_files():
        if Path(rel).suffix.lower() in BINARY_SUFFIXES or "node_modules/" in rel:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue  # deleted in the working tree
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            files[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    findings, stale = scan_attribution(files)
    assert not stale, f"REVIEWED_PROSE entries that no longer match anything (remove them): {stale}"
    assert len(files) > 100, f"only {len(files)} files scanned - is this a git checkout?"
    assert not findings, "AI attribution found:\n" + "\n".join(findings)


def test_reviewed_prose_is_one_line_not_the_whole_file():
    prose = "DLY AEOS is a control plane for AI-assisted software engineering.\n"
    assert not attribution_findings("README.md", prose)  # the reviewed line
    assert attribution_findings("docs/other.md", prose)  # the same words in another file are not reviewed
    assert attribution_findings("README.md", prose + "Generated" + " with Claude Code\n")  # a new line is checked
    assert attribution_findings("README.md", "Co-Authored" + "-By: Claude\n")


def test_reviewed_prose_does_not_excuse_a_real_attribution_on_the_same_line():
    prose = "DLY AEOS is a control plane for AI-assisted software engineering."
    attribution = "Generated" + " with Claude Code."
    assert not attribution_findings("README.md", prose + "\n")  # green: the reviewed line as it is
    assert attribution_findings("README.md", f"{prose} {attribution}\n")  # red: attribution after the fragment
    assert attribution_findings("README.md", f"{attribution} {prose}\n")  # red: attribution before the fragment
    assert attribution_findings("README.md", f"intro\r\n{prose} {attribution}\r\n")  # red: CRLF text
    assert not attribution_findings("README.md", f"{prose} Nothing else here.\n")  # green: harmless words added


def test_stale_reviewed_prose_entries_are_reported():
    entries = (("a.md", "GENERATED_WITH_AI", "AI-assisted software engineering"),
               ("b.md", "GENERATED_WITH_AI", "written with the help of"))
    live = "control plane for AI-assisted software engineering\n"
    findings, stale = scan_attribution({"a.md": live, "b.md": "an ordinary line\n"}, entries)
    assert findings == [] and stale == [entries[1]]  # red: b.md no longer holds its reviewed line
    # every entry matched -> nothing stale (green); no file at all -> every entry stale (red)
    both = {"a.md": live, "b.md": "documented, written with the help of an AI assistant\n"}
    assert scan_attribution(both, entries)[1] == []
    assert scan_attribution({}, entries)[1] == list(entries)
    # the same fragment in the wrong file does not keep an entry alive
    assert scan_attribution({"c.md": live, "b.md": both["b.md"]}, entries)[1] == [entries[0]]


def test_an_unneeded_reviewed_prose_entry_is_stale_and_cannot_excuse_a_real_attribution():
    """docs/OPERATIONS.md quotes the phrase 'written with the help of' in a list of forbidden forms: removing the other
    entry's fragment is what clears the line, so an entry for the phrase itself was never needed."""
    line = 'forms: "written with the help of", "AI-generated", the robot emoji\n'
    needed = ("a.md", "GENERATED_WITH_AI", '"AI-generated"')
    unneeded = ("a.md", "GENERATED_WITH_AI", "written with the help of")
    assert scan_attribution({"a.md": line}, (needed,)) == ([], [])  # green: the one entry that clears the line
    assert scan_attribution({"a.md": line}, (needed, unneeded)) == ([], [unneeded])  # red: the other is stale
    # and with the unneeded entry present a real attribution on the same file would have been excused; without it it is not
    real = "This runbook was written with the help of " + "Claude.\n"
    assert attribution_findings("a.md", real, None, (unneeded,)) == []  # the hole the entry opened
    assert attribution_findings("a.md", real, None, (needed,))  # red once the entry is gone


def test_reviewed_prose_does_not_excuse_an_attribution_that_wraps_onto_the_next_line():
    prose = "DLY AEOS is a control plane for AI-assisted software engineering."
    wrapped = prose + " Generated" + " with\n" + "Claude Code.\n"
    assert attribution_findings("README.md", wrapped)  # red: reported at the first line, whose fragment is reviewed
    assert attribution_findings("README.md", wrapped.replace("\n", "\r\n"))  # red with CRLF
    assert not attribution_findings("README.md", prose + " It runs on your\nown machine.\n")  # green near miss


# ------------------------------------------------------------------ one attribution implementation (ratchet)
# No other module may carry its own attribution pattern: a string-constant pattern given to re.compile / re.search /
# ... that matches an attribution marker (co-authored-by, generated with/by, a noreply address at an AI vendor) is a
# second implementation that drifts from aeos.core.attribution. Python only; the pattern is read from the syntax tree
# (string constants, concatenations, f-string constant parts and module-level constants), so splitting the marker
# into parts does not hide it.
_RE_FUNCTIONS = frozenset({"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"})
_RATCHET_ROOTS = ("backend/", "scripts/", "tests/")
_VENDOR_LABELS = tuple(sorted({d.split(".")[0] for d in AI_VENDOR_DOMAINS if len(d.split(".")[0]) >= 5}))


def _squeeze(pattern: str) -> str:
    """Lower-case letters and digits of a regex source, without escapes, character classes and separators:
    ``Co-?authored-?by:`` and ``generated\\s+with`` read ``coauthoredby`` and ``generatedwith``."""
    flat = re.sub(r"\\[A-Za-z]", "", pattern)
    flat = re.sub(r"\[([^\]]*)\]", r"\1", flat)
    return "".join(ch for ch in flat.lower() if ch.isalnum())


# Behavioural check: a pattern is a private attribution matcher when it really finds a probe statement but none of its
# decoys. There is one decoy per marker word, each changing that word ONLY ("reported with x", "generated near x",
# "Re-Viewed-By: x", "Co-Authored-On: x", "noreply@example.com"), so a generic pattern built on one word alone (a
# preposition such as ``(\w+) by (\w+)``, a ``[\w-]+-by:`` trailer reader) accepts a decoy as well and is not flagged,
# and neither is a validator (an e-mail, host:path or name check, a catch-all). A probe counts only when the match
# covers the marker itself (verb and preposition, key and ``by``, ``@`` and the vendor label), so ``generated`` or
# ``noreply@`` alone is not attribution.
_PROBE_SUBJECTS = ("x", "Claude", "Claude Code", "[Claude Code](https://claude.ai)", "ChatGPT", "Copilot")
_PROBE_VERBS = (("generated", "reported"), ("created", "printed"), ("written", "carried"))
_PROBE_PREPOSITIONS = (("with", "near"), ("by", "for"), ("using", "over"))
_NEUTRAL_CONTROLS = ("plain text 123, release v1.2.3 for user@example.org", "user@example.org", "v1.2.3", "abc")


def _behavioural_probes() -> list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
    """(marker name, probe text, decoy texts, words the match on the probe must cover)."""
    probes: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
    for subject in _PROBE_SUBJECTS:
        for tail in (subject, f"{subject} <noreply@example.invalid>"):  # a bare trailer and one that carries an address
            probes.append(("co-authored-by", f"Co-Authored-By: {tail}",
                           (f"Re-Viewed-By: {tail}", f"Co-Authored-On: {tail}"), ("authored", "by")))
        for verb, plain_verb in _PROBE_VERBS:
            for prep, plain_prep in _PROBE_PREPOSITIONS:
                probes.append(("generated with/by", f"{verb} {prep} {subject}",
                               (f"{plain_verb} {prep} {subject}", f"{verb} {plain_prep} {subject}"), (verb, prep)))
    for domain in AI_VENDOR_DOMAINS:
        label, _, tld = domain.partition(".")
        probes.append(("noreply address at an AI vendor", f"noreply@{domain}", (f"noreply@example.{tld}",),
                       ("@" + label,)))
    return probes


def _behavioural_marker(pattern: str) -> str | None:
    """The marker a compilable regex source really matches (case-insensitively), or None. A source that does not
    compile, or that matches a neutral control, is left to the textual check."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rx = re.compile(pattern, re.I)
    except (re.error, RecursionError, OverflowError):
        return None
    if any(rx.search(control) for control in _NEUTRAL_CONTROLS):
        return None
    for marker, probe, decoys, words in _behavioural_probes():
        m = rx.search(probe)
        if m and all(word in m.group(0).lower() for word in words) and not any(rx.search(d) for d in decoys):
            return marker
    return None


def attribution_marker_in_pattern(pattern: str) -> str | None:
    """The attribution marker a regex source contains or matches, or None."""
    if not pattern:
        return None
    behavioural = _behavioural_marker(pattern)
    if behavioural:
        return behavioural
    squeezed = _squeeze(pattern)
    if "coauthoredby" in squeezed:
        return "co-authored-by"
    if "generatedwith" in squeezed or "generatedby" in squeezed:
        return "generated with/by"
    if "noreply" in squeezed and any(label in squeezed for label in _VENDOR_LABELS):
        return "noreply address at an AI vendor"
    if "@" in pattern and any(label in squeezed for label in _VENDOR_LABELS):
        return "address at an AI vendor"
    return None


def _constant_text(node: ast.AST, names: dict[str, str]) -> str:
    """The constant text of a string expression ("" for the parts that are not constants)."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _constant_text(node.left, names) + _constant_text(node.right, names)
    if isinstance(node, ast.JoinedStr):
        return "".join(_constant_text(v.value, names) if isinstance(v, ast.FormattedValue) else _constant_text(v, names)
                       for v in node.values)
    if isinstance(node, ast.Name):
        return names.get(node.id, "")
    return ""


def attribution_regexes(rel: str, source: str | bytes) -> list[str]:
    """``rel:line: re.<function>() pattern ...`` for every re call whose constant pattern holds or matches an attribution
    marker. A file that cannot be parsed is itself a finding (the ratchet must not fail open on it)."""
    try:
        tree = parse_python(source)
    except (SyntaxError, ValueError) as err:
        return [f"{rel}: cannot be parsed as Python ({type(err).__name__}), so the ratchet cannot check it"]
    modules: set[str] = set()  # names bound to the re module
    functions: dict[str, str] = {}  # local name -> re function (from re import compile as c)
    names: dict[str, str] = {}  # module-level NAME = <string expression>
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "re")
        elif isinstance(node, ast.ImportFrom) and node.module == "re" and node.level == 0:
            functions.update({a.asname or a.name: a.name for a in node.names if a.name in _RE_FUNCTIONS})
    for node in tree.body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if value is not None:
            for target in targets:
                if isinstance(target, ast.Name):
                    names[target.id] = _constant_text(value, names)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in modules \
                and func.attr in _RE_FUNCTIONS:
            name = func.attr
        elif isinstance(func, ast.Name) and func.id in functions:
            name = functions[func.id]
        else:
            continue
        pattern = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "pattern"), None)
        marker = attribution_marker_in_pattern(_constant_text(pattern, names)) if pattern is not None else None
        if marker:
            found.append(f"{rel}:{node.lineno}: re.{name}() pattern matches {marker}")
    return found


def test_single_attribution_implementation():
    findings: list[str] = []
    scanned = 0
    for rel in _repository_files():
        if rel == ATTRIBUTION_GUARD_MODULE or not rel.endswith(".py") or not rel.startswith(_RATCHET_ROOTS):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue  # deleted in the working tree
        scanned += 1
        findings += attribution_regexes(rel, path.read_bytes())
    assert scanned > 100, f"only {scanned} Python files scanned - is this a git checkout?"
    assert not findings, ("AI attribution patterns outside backend/aeos/core/attribution.py - use "
                          "find_ai_attribution / ai_identity_problem instead:\n" + "\n".join(findings))


def test_attribution_ratchet_flags_private_patterns_and_passes_legitimate_ones():
    flagged = {
        "split constants": "import re\nP = re.compile('co-' + 'authored-by:')\n",
        "search": "import re\nre.search(r'(?i)generated\\s+(?:with|by)\\s+x', 'y')\n",
        "from-import alias": "from re import compile as rc\nrc('Co-?authored-?by')\n",
        "module alias": "import re as regex\nregex.finditer('GENERATED' + ' WITH', 't')\n",
        "named constant": "import re\nMARK = 'co-authored' + '-by'\nre.sub(MARK, '', 'x')\n",
        "f-string": "import re\nHEAD = 'generated'\nre.match(f'{HEAD} with', 'x')\n",
        "keyword pattern": "import re\nre.compile(pattern='generated by')\n",
        "vendor address": "import re\nre.compile('noreply' + '@' + 'anthropic.com')\n",
        "vendor domain": "import re\nre.compile(r'[\\w.]+@openai\\.com')\n",
        "character classes": "import re\nre.compile(r'^[Cc]o-[Aa]uthored-[Bb]y:')\n",
        "alternation": "import re\nre.compile(r'(?i)(?:co-authored|signed-off)-by:')\n",
        "verb alternation": "import re\nre.compile(r'(?i)(?:generated|written)\\s+(?:with|by)\\s+claude')\n",
        "canonical verbs": "import re\nre.compile(r'(?:generated|created|written)(?:\\s+\\w+ly)?\\s+(?:with|by|using)')\n",
        "short vendor label": "import re\nre.compile(r'noreply@groq\\.com')\n",
        "short vendor domain": "import re\nre.compile(r'noreply@x\\.ai')\n",
        "short vendor tld": "import re\nre.compile(r'noreply@z\\.ai')\n",
        "trailer parser with an address": "import re\nre.compile(r'(?im)^[Cc]o-[Aa]uthored-[Bb]y:\\s*(?P<name>[^<]+)<(?P<mail>[^>]+)>')\n",
        "markdown footer": "import re\nre.compile(r'[Gg]enerated [Ww]ith \\[Claude Code\\]')\n",
        "byte order mark": "\ufeffimport re\nPAT = re.compile('co-' + 'authored-by:')\n",
        "unparsable": "re.compile('co-authored-by'\n",
    }
    for case, source in flagged.items():
        assert attribution_regexes("scripts/x.py", source), case
    legitimate = {
        "ordinary pattern": "import re\nre.compile(r'^v(\\d+)\\.(\\d+)$')\n",
        "marker only in a comment": "import re\n# co-authored-by: the rule\nre.compile('abc')\n",
        "marker only in a string": "import re\nTEXT = 'Co-Authored-By'\nre.compile('abc')\nprint(TEXT)\n",
        "marker in a docstring": 'import re\n"""generated with"""\nre.compile("abc")\n',
        "non-constant pattern": "import re\nre.compile(some_function())\n",
        "not the re module": "import regex\nregex.compile('co-authored-by')\n",
        "other call": "class P:\n    def compile(self, s): ...\nP().compile('generated with')\n",
        "generated alone": "import re\nre.compile(r'\\bgenerated\\b')\n",
        "with alone": "import re\nre.compile(r'\\bwith\\b')\n",
        "generic noreply": "import re\nre.compile(r'noreply@\\w+\\.org')\n",
        "catch-all": "import re\nre.compile(r'.*')\nre.compile(r'\\S+@\\S+')\n",
        "email validator": "import re\nre.compile(r'^[^@\\s<>]+@[^@\\s<>]+\\.[^@\\s<>]+$')\n",
        "host path validator": "import re\nre.match(r'^(?:[\\w.-]+@)?([\\w.-]+):(?!//)(.+)$', 'h:p')\n",
        "name validator": "import re\nre.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$')\n",
        "env secret names": "import re\nre.compile(r'(?i)(TOKEN|ANTHROPIC|OPENAI|GITHUB)')\n",
        "generic by pattern": "import re\nre.compile(r'(\\w+)\\s+by\\s+(\\w+)')\n",
        "generic with pattern": "import re\nre.compile(r'(\\w+) with (\\w+)')\n",
        "generic using pattern": "import re\nre.compile(r'(\\S+) using (\\S+)')\n",
        "dco style trailer reader": "import re\nre.compile(r'(?im)^[\\w-]+-by:\\s*(.+)$')\n",
        "uncompilable constant": "import re\nre.compile('(unclosed')\n",
    }
    for case, source in legitimate.items():
        assert not attribution_regexes("scripts/x.py", source), case


def test_attribution_ratchet_finds_a_violating_file_on_disk(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import re\nPAT = re.compile('co-' + 'authored-by:')\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text("import re\n# co-authored-by is handled by aeos.core.attribution\nPAT = re.compile('x')\n",
                    encoding="utf-8")
    assert attribution_regexes("scripts/bad.py", bad.read_text(encoding="utf-8")) == [
        "scripts/bad.py:2: re.compile() pattern matches co-authored-by"]
    assert attribution_regexes("scripts/good.py", good.read_text(encoding="utf-8")) == []
    # a file with a UTF-8 byte order mark runs under Python, so the ratchet must read it (bytes and decoded text)
    bom = tmp_path / "bom.py"
    bom.write_bytes(b"\xef\xbb\xbf" + b"import re\nPAT = re.compile('co-' + 'authored-by:')\n")
    expected = ["scripts/bom.py:2: re.compile() pattern matches co-authored-by"]
    assert attribution_regexes("scripts/bom.py", bom.read_bytes()) == expected
    assert attribution_regexes("scripts/bom.py", bom.read_bytes().decode("utf-8")) == expected
    # a file that cannot be parsed is reported, never skipped
    broken = tmp_path / "broken.py"
    broken.write_text("import re\nPAT = re.compile('co-' + 'authored-by:'\n", encoding="utf-8")
    assert len(attribution_regexes("scripts/broken.py", broken.read_bytes())) == 1


def test_comment_only_marker_does_not_exempt_a_file_from_the_attribution_scan(tmp_path):
    """Bite for the guard-sample exemption, on files written to disk: a marker in a comment is not an import."""
    sample = "S = '" + "Generated" + " with Claude'\n"
    faked = tmp_path / "test_faked.py"
    faked.write_text("# uses aeos.core.attribution and find_ai_attribution\n" + sample, encoding="utf-8")
    real = tmp_path / "test_real.py"
    real.write_text("from aeos.core.attribution import find_ai_attribution\n" + sample, encoding="utf-8")
    rel = "tests/unit/test_x.py"
    faked_text, real_text = faked.read_text(encoding="utf-8"), real.read_text(encoding="utf-8")
    assert not _is_guard_sample_file(rel, faked_text) and attribution_findings(rel, faked_text)  # red
    assert _is_guard_sample_file(rel, real_text)  # green: a real import is exempt
