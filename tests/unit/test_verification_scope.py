"""Scope rules (§09, §21; §50 "Worker mengubah grader/policy/pipeline di luar kontrak")."""

from __future__ import annotations

from aeos.verification import scope
from tests.unit.test_verification_support import fake_secret

PROTECTED = list(scope.GENERIC_PROTECTED_PATHS) + list(scope.PLATFORM_PROTECTED_PATHS)


def test_glob_matching():
    m = scope.path_matches
    assert m("backend/aeos/x.py", "backend/**")
    assert m("backend/aeos/x.py", "backend/")
    assert m("backend/aeos/x.py", "backend")
    assert not m("backendx/a.py", "backend")
    assert m("docs/a/b.md", "**/*.md") and m("README.md", "**/*.md")
    assert m("a/b.py", "a/*.py") and not m("a/c/b.py", "a/*.py")
    assert m("Tests/Acceptance/x.py", "tests/acceptance/**", case_insensitive=True)
    assert not m("Tests/Acceptance/x.py", "tests/acceptance/**")


def test_clean_change_passes():
    r = scope.check_scope(["backend/aeos/feature.py", "tests/unit/test_feature.py"],
                          allowed=["backend/**", "tests/unit/**"], forbidden=[], protected=PROTECTED)
    assert r.status == "PASS" and not r.violations and not r.docs_only


def test_protected_path_outside_contract_fails_case_insensitively():
    r = scope.check_scope(["Tests/Acceptance/test_x.py", "backend/aeos/policy/rules.py"],
                          allowed=["backend/aeos/feature/**"], forbidden=[], protected=PROTECTED)
    assert r.status == "FAIL"
    assert {v.rule for v in r.violations} == {"PROTECTED_PATH"}
    assert r.quarantine_reason and "candidate-only" in r.quarantine_reason


def test_protected_path_allowed_is_quarantined():
    r = scope.check_scope([".github/workflows/ci.yml"], allowed=[".github/workflows/**"], forbidden=[],
                          protected=PROTECTED)
    assert r.status == "QUARANTINED"
    assert r.protected_touched == [".github/workflows/ci.yml"]


def test_forbidden_and_outside_allowed():
    r = scope.check_scope(["backend/secret_area/x.py", "frontend/app.tsx"], allowed=["backend/**"],
                          forbidden=["backend/secret_area/**"], protected=PROTECTED)
    rules = {(v.path, v.rule) for v in r.violations}
    assert ("backend/secret_area/x.py", "FORBIDDEN_PATH") in rules
    assert ("frontend/app.tsx", "OUTSIDE_ALLOWED") in rules
    assert r.status == "FAIL"


def test_empty_allowed_paths_fails_closed():
    r = scope.check_scope(["backend/x.py"], allowed=[], forbidden=[], protected=PROTECTED)
    assert r.status == "FAIL" and r.violations[0].detail == "TaskSpec declares no allowed_paths"


def test_harness_tamper_and_unsafe_paths():
    r = scope.check_scope(["backend/sitecustomize.py", "backend/x.pth", "../evil.py", "a/CON.py", "a\\b.py"],
                          allowed=["**"], forbidden=[], protected=PROTECTED)
    rules = {v.rule for v in r.violations}
    assert {"HARNESS_TAMPER", "UNSAFE_PATH"} <= rules
    assert r.status == "FAIL"


def test_docs_only():
    r = scope.check_scope(["docs/guide.md", "README.md"], allowed=["**"], forbidden=[], protected=PROTECTED)
    assert r.docs_only
    r = scope.check_scope(["docs/guide.md", "backend/x.py"], allowed=["**"], forbidden=[], protected=PROTECTED)
    assert not r.docs_only


DIFF = """diff --git a/backend/cfg.py b/backend/cfg.py
--- a/backend/cfg.py
+++ b/backend/cfg.py
@@ -1,2 +1,4 @@
 import os
+TOKEN = "{token}"
+import _pytest
 X = 1
diff --git a/tests/unit/test_core.py b/tests/unit/test_core.py
--- a/tests/unit/test_core.py
+++ b/tests/unit/test_core.py
@@ -10,0 +11,1 @@
+SAMPLE = "{token}"
"""


def test_added_lines_secret_and_tamper_scan():
    token = fake_secret()
    lines = scope.added_lines(DIFF.format(token=token))
    assert [(a.path, a.line) for a in lines] == [("backend/cfg.py", 2), ("backend/cfg.py", 3),
                                                 ("tests/unit/test_core.py", 11)]
    secrets = scope.scan_added_secrets(lines)
    assert secrets == [{"path": "backend/cfg.py", "line": 2, "pattern": secrets[0]["pattern"]}]
    assert token not in str(secrets)
    tamper = scope.scan_harness_tamper(lines)
    assert tamper == [{"path": "backend/cfg.py", "line": 3, "rule": "HARNESS_TAMPER_CODE"}]
