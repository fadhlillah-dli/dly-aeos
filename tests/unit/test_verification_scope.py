"""Scope rules (§09, §21; §50 "Worker mengubah grader/policy/pipeline di luar kontrak")."""

from __future__ import annotations

import pytest

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


def test_added_line_that_looks_like_a_file_header_cannot_dodge_the_scan():
    """Hunk bodies are consumed by their declared counts: an added line starting with "++ b/<allow-listed>"
    is still an added line of the real file, so a following credential is scanned (and attributed)."""
    token = fake_secret()
    diff = (
        "diff --git a/backend/cfg.py b/backend/cfg.py\n"
        "--- a/backend/cfg.py\n"
        "+++ b/backend/cfg.py\n"
        "@@ -1,1 +1,3 @@\n"
        " X = 1\n"
        "+++ b/tests/unit/test_core.py\n"
        f'+TOKEN = "{token}"\n'
    )
    lines = scope.added_lines(diff)
    assert [(a.path, a.line) for a in lines] == [("backend/cfg.py", 2), ("backend/cfg.py", 3)]
    assert [f["path"] for f in scope.scan_added_secrets(lines)] == ["backend/cfg.py"]


def test_hunk_without_parsable_header_is_still_scanned():
    token = fake_secret()
    lines = scope.added_lines(f'@@ -0,0 +1 @@\n+TOKEN = "{token}"\n')
    assert [a.path for a in lines] == ["<unparsed-path>"]
    assert scope.scan_added_secrets(lines)


def test_crlf_diff_lines_are_parsed():
    lines = scope.added_lines("+++ b/a.py\r\n@@ -0,0 +1,2 @@\r\n+one\r\n+two\r\n")
    assert [(a.path, a.line, a.text) for a in lines] == [("a.py", 1, "one"), ("a.py", 2, "two")]


def test_binary_secret_scan_never_returns_the_value():
    token = fake_secret()
    findings = scope.scan_binary_secrets("assets/x.bin", b"\x00\xff" + token.encode() + b"\x00")
    assert len(findings) == 1 and findings[0]["binary"] is True and findings[0]["offset"] == 2
    assert token not in str(findings)
    assert scope.scan_binary_secrets("assets/y.bin", b"\x00\x01\x02 plain bytes") == []
    assert scope.scan_binary_secrets("tests/unit/test_core.py", token.encode()) == []  # same allow-list


@pytest.mark.parametrize("path", [
    "docs/architecture/DLY_AEOS_Master_Architecture_Final.md",  # U-23: the master is not a docs-only change
    "AGENTS.md", "CLAUDE.md",
    "backend/aeos/audit/service.py", "backend/aeos/events/service.py",  # §50 T2 #2.14
    "backend/aeos/control_center/views.py", "backend/aeos/workforce/scheduler.py",
    "backend/aeos/doctor/checks/delivery.py", "backend/aeos/worker_runtime/sandbox.py",  # §09 capability code
    "backend/aeos/routing/service.py", "backend/aeos/cost/service.py",
])
def test_capability_audit_dashboard_and_master_paths_are_protected(path):
    """A candidate that rewrites the master/agent instructions, the audit trail, the dashboard projections or
    the code deciding its own capabilities is never accepted silently: FAIL outside its contract, QUARANTINED
    (special review + human promotion) inside it — never an ordinary (docs-only) PASS."""
    from aeos.worker_runtime import scope as runtime_scope

    outside = scope.check_scope([path], allowed=["backend/aeos/feature/**"], forbidden=[], protected=PROTECTED)
    assert outside.status == "FAIL", path
    inside = scope.check_scope([path], allowed=["**"], forbidden=[], protected=PROTECTED)
    assert inside.status == "QUARANTINED" and inside.quarantine_reason, path
    # the runtime's first line of defence uses the same list
    assert set(scope.PLATFORM_PROTECTED_PATHS) == set(runtime_scope.PLATFORM_PROTECTED_PATHS)


@pytest.mark.parametrize("path,category", [
    ("services/api/migrations/0003_add_col.py", "migration"), ("db/alembic/versions/x.py", "migration"),
    ("sql/seed.sql", "migration"), ("api/openapi.yaml", "contract"), ("proto/user.proto", "contract"),
    ("infra/main.tf", "infrastructure"), ("deploy/k8s/app.yaml", "infrastructure"),
    ("deploy/helm/values.yaml", "infrastructure"), ("Dockerfile", "infrastructure"),
    (".gitlab-ci.yml", "ci"), ("bitbucket-pipelines.yml", "ci"), ("config/feature_flags.json", "config_flags"),
    ("requirements.lock", "dependency"), ("web/package-lock.json", "dependency"), ("svc/poetry.lock", "dependency"),
    ("svc/uv.lock", "dependency"), ("app/.env.example", "config_flags"), ("svc/auth/login.py", "auth"),
])
def test_generic_product_layouts_are_high_impact_even_for_small_diffs(path, category):
    """§31: routing/IAM/secrets/CI/migration/schema/flags/IaC/dependencies are high-impact in any repository,
    not only in the AEOS layout."""
    r = scope.check_scope([path], allowed=["**"], forbidden=[], protected=[])
    assert r.to_dict()["impact"] == "HIGH", path
    assert path in r.impact_categories.get(category, []), (path, r.impact_categories)


@pytest.mark.parametrize("path", ["web/src/components/Button.tsx", "app/views/Login.vue", "ui/Card.svelte",
                                  "static/css/site.css", "client/src/App.jsx"])
def test_ui_output_in_any_layout_is_the_frontend_category(path):
    """§17: UI output activates the a11y/UX specialist in product repositories too, not only under frontend/."""
    assert path in scope.impact_categories([path]).get("frontend", [])


def test_ordinary_code_is_not_high_impact():
    r = scope.check_scope(["src/feature/calc.py", "tests/unit/test_calc.py"], allowed=["**"], forbidden=[],
                          protected=[])
    assert r.to_dict()["impact"] == "NORMAL" and r.impact_categories == {}
