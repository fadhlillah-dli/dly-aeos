"""TaskSpec scope rules (§19 write sets, §21 allowed/forbidden, §09 protected paths, §50 'Worker mengubah
grader/policy/pipeline di luar kontrak', 'File path traversal/symlink keluar sandbox')."""

from __future__ import annotations

import pytest

from aeos.worker_runtime.scope import (
    PLATFORM_PROTECTED_PATHS,
    ChangedPath,
    ScopePathError,
    check_scope,
    matches,
    normalize,
    patterns_overlap,
    write_sets_overlap,
)


@pytest.mark.parametrize("path,pattern,ok", [
    ("backend/aeos/workforce/x.py", "backend/aeos/workforce/**", True),
    ("backend/aeos/workforce", "backend/aeos/workforce/", False),
    ("src/a.ts", "src/*.ts", True),
    ("src/sub/a.ts", "src/*.ts", False),
    ("src/sub/a.ts", "src/**/*.ts", True),
    ("docs/x.md", "docs", True),
    ("docsx/y.md", "docs", False),
    ("README.md", "README.md", True),
])
def test_matches(path, pattern, ok):
    assert matches(path, pattern) is ok


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "C:\\Windows\\x", "a/../../b", "~/.ssh/id_rsa", ""])
def test_normalize_rejects_escapes(bad):
    with pytest.raises(ScopePathError):
        normalize(bad)


def test_overlap_is_conservative():
    assert patterns_overlap("backend/aeos/**", "backend/aeos/workforce/x.py")
    assert patterns_overlap("Backend/AEOS/x.py", "backend/aeos/x.py")
    assert not patterns_overlap("frontend/src/a.tsx", "frontend/src/b.tsx")
    assert patterns_overlap("**", "anything")
    assert write_sets_overlap([], ["a/**"])  # empty = whole repo
    assert write_sets_overlap(["a/**"], ["b/**"]) == []


def test_check_scope_violations_and_quarantine():
    changes = [ChangedPath("src/ok.py", "M"), ChangedPath("other/x.py", "A"), ChangedPath("src/secret.env", "A"),
               ChangedPath("Policy/mci.yaml", "M"), ChangedPath("src/link", "A", mode="120000"),
               ChangedPath(".git/hooks/pre-push", "A"), ChangedPath("src/new.py", "R", old_path="../outside.py")]
    res = check_scope(changes, allowed=["src/**", "Policy/**"], forbidden=["src/*.env"],
                      protected=PLATFORM_PROTECTED_PATHS)
    rules = {(v["path"], v["rule"]) for v in res.violations}
    assert ("other/x.py", "outside-allowed-paths") in rules
    assert ("src/secret.env", "forbidden-path") in rules
    assert (".git/hooks/pre-push", "git-internals") in rules
    assert any(v["rule"] == "unsafe-path" for v in res.violations)
    prot = {(h["path"], h["rule"]) for h in res.protected_hits}
    assert ("Policy/mci.yaml", "protected-path") in prot  # case-insensitive protected match
    assert ("src/link", "symlink") in prot
    assert not res.ok and res.quarantine


def test_clean_change_passes():
    res = check_scope([ChangedPath("src/ok.py", "M")], allowed=["src/**"], forbidden=[], protected=PLATFORM_PROTECTED_PATHS)
    assert res.ok and not res.quarantine
