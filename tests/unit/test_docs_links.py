"""Documentation integrity (master §02 decision/coverage register, §49 milestone status, Header "Panduan
membaca", §01 U-01 VS Code tasks, §49 M0 owner/target/capability matrix)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
MASTER = ROOT / "docs" / "architecture" / "DLY_AEOS_Master_Architecture_Final.md"
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    files = [ROOT / p for p in out.splitlines() if p]
    # files created in this change are not tracked yet in a fresh worktree: include the key ones explicitly
    for extra in ("docs/IMPLEMENTATION_COVERAGE.md", "docs/OWNER_TARGET_CAPABILITY_MATRIX.md"):
        if (ROOT / extra).is_file() and ROOT / extra not in files:
            files.append(ROOT / extra)
    return files


def test_repo_relative_markdown_links_resolve():
    broken = []
    for md in _markdown_files():
        if "node_modules" in md.parts or md.parts[-2:-1] == ("evidence",) and md.name != "README.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for target in LINK.findall(text):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            resolved = (md.parent / path).resolve()
            if not resolved.exists():
                broken.append(f"{md.relative_to(ROOT).as_posix()} -> {target}")
    assert not broken, "broken repo-relative links:\n" + "\n".join(broken)


def _slug(heading: str) -> str:
    """GitHub-style anchor slug."""
    s = unicodedata.normalize("NFKC", heading).strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def test_master_reading_guide_anchors_resolve():
    text = MASTER.read_text(encoding="utf-8")
    slugs = {_slug(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, flags=re.M)}
    anchors = [a for a in re.findall(r"\]\(#([^)]+)\)", text)]
    assert anchors, "the reading guide has no anchors"
    missing = sorted({a for a in anchors if a.lower() not in slugs})
    assert not missing, missing


def test_coverage_matrix_has_every_master_section_and_real_tests():
    cov = (ROOT / "docs" / "IMPLEMENTATION_COVERAGE.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## (\d\d)\. ", MASTER.read_text(encoding="utf-8"), flags=re.M)
    assert len(sections) >= 52
    for num in sections:
        assert re.search(rf"^\| §{num} \|", cov, flags=re.M), f"§{num} has no row"
    cited = set(re.findall(r"`(tests/[A-Za-z0-9_/]+\.py)`", cov))
    assert cited, "no tests cited"
    missing = sorted(t for t in cited if not (ROOT / t).is_file())
    assert not missing, missing
    for milestone in ("M0", "M1", "M2", "M3", "M4", "M5", "M6"):
        assert f"| {milestone} " in cov


def test_owner_target_capability_matrix_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "owner_matrix.py"), "--check"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def _registered_commands() -> set[str]:
    from typer.main import get_command

    from aeos.cli import app

    root = get_command(app)
    names: set[str] = set()

    def walk(cmd, prefix: str) -> None:
        for name, sub in getattr(cmd, "commands", {}).items():
            full = f"{prefix} {name}".strip()
            names.add(full)
            walk(sub, full)

    walk(root, "")
    return names


def test_vscode_tasks_call_registered_aeos_commands():
    tasks = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    commands = _registered_commands()
    seen = 0
    for task in tasks["tasks"]:
        for os_key in ("windows", "osx", "linux"):
            cmd = (task.get(os_key) or {}).get("command") or ""
            m = re.match(r"^\.?[\\/]?(?:\.\\)?aeos(?:\.ps1)?\s+(.+)$", cmd)
            if not m:
                continue
            words = [w for w in m.group(1).split() if not w.startswith("-")]
            assert words and (words[0] in commands or " ".join(words[:2]) in commands), (task["label"], cmd)
            seen += 1
    assert seen >= 4


@pytest.mark.parametrize("doc", ["docs/SETUP_WINDOWS.md", "docs/SETUP_MACOS.md"])
def test_setup_guides_document_model_discovery(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "/model" in text and "never" in text.lower()


def test_domain_migration_is_a_documented_owner_procedure_not_an_edit(personal_client):
    """§04: moving a project between domains is a migration with an owner decision; the domain is never edited."""
    text = (ROOT / "docs" / "runbooks" / "domain-migration.md").read_text(encoding="utf-8")
    for section in ("## Trigger", "## Authority", "## Steps", "## Verification", "## Evidence"):
        assert section in text, section
    assert "never UPDATEd" in text and "company authorisation" in text
    ids = {r["id"] for r in personal_client.get("/api/ops/runbooks").json()["items"]}
    assert "domain-migration" in ids
