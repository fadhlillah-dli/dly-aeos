"""Repository hygiene for the public, all-rights-reserved repository.

The licence, the package metadata, the citation file, the changelog and the release notes must
agree on holder, licence and version, and the governance files must exist. No tracked file may
carry an AI-assistant attribution line: commits and files carry no attribution (CONTRIBUTING.md).
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

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


def test_changelog_has_unreleased_and_the_current_version_with_its_release_link():
    log = _read("CHANGELOG.md")
    assert "keepachangelog.com/en/1.1.0" in log and "semver.org" in log
    assert re.search(r"^## \[Unreleased\]$", log, re.M)
    m = re.search(rf"^## \[{re.escape(VERSION)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", log, re.M)
    assert m, f"CHANGELOG.md has no '## [{VERSION}] - YYYY-MM-DD' entry"
    assert m.group(1) == yaml.safe_load(_read("CITATION.cff"))["date-released"], "release dates disagree"
    assert f"[{VERSION}]: {REPO_URL}/releases/tag/v{VERSION}" in log
    assert re.search(r"^\[unreleased\]: \S+/compare/v" + re.escape(VERSION) + r"\.\.\.HEAD$", log, re.M | re.I)


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
# Assembled from parts so that this file does not itself contain the markers it looks for.
_AI_NAMES = r"claude|anthropic|openai|chatgpt|\bgpt\b|copilot|gemini|codex|cursor|devin|aider|codeium|tabnine"
CO_AUTHOR = re.compile(r"(?im)^[ \t>#*-]*" + "co-authored" + r"-by:(?P<who>[^\n]*)$")
BOT = re.compile(rf"(?i){_AI_NAMES}|\[bot\]|\bbot\b")
GENERATED = re.compile(r"(?i)generated\s+(?:with|by)\s+\[?\s*(?:" + _AI_NAMES + r"|an?\s+ai\b)")
AI_ADDRESS = "noreply" + "@" + "anthropic.com"
ATTRIBUTION_ALLOW = {"tests/unit/test_repository_hygiene.py"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".docx", ".xlsx", ".pptx", ".zip",
                   ".gz", ".sqlite3", ".sqlite", ".woff", ".woff2", ".ttf", ".otf", ".pyc"}


def attribution_findings(rel: str, text: str) -> list[str]:
    found = [f"{rel}: co-author trailer naming an AI or bot: {m.group(0).strip()}"
             for m in CO_AUTHOR.finditer(text) if BOT.search(m.group("who"))]
    found += [f"{rel}: generated-by footer: {m.group(0)}" for m in GENERATED.finditer(text)]
    if AI_ADDRESS in text.lower():
        found.append(f"{rel}: AI attribution address")
    return found


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
    assert not attribution_findings("x.md", "Co-Authored" + "-By: Fadhlillah\n")
    assert not attribution_findings("x.md", "Claude supervises through the official Claude Code client.\n")
    assert not attribution_findings("x.md", "Never add co-authored" + "-by: lines (rule, mid-sentence).\n")


def test_no_file_carries_an_ai_attribution_line():
    findings: list[str] = []
    scanned = 0
    for rel in _repository_files():
        if rel in ATTRIBUTION_ALLOW or Path(rel).suffix.lower() in BINARY_SUFFIXES or "node_modules/" in rel:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue  # deleted in the working tree
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        findings += attribution_findings(rel, text)
    assert scanned > 100, f"only {scanned} files scanned - is this a git checkout?"
    assert not findings, "AI attribution found:\n" + "\n".join(findings)
