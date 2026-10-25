"""Wave 7 E1 verifier steps - fast unit tests of the step logic (no candidate run):

* W6-045/W6-104 negative tests are found in the syntax tree (a comment is never a negative test);
* W6-045 contract diff: facade signatures and OpenAPI operations against the base;
* W6-105 impact category -> executed checks map;
* W6-043 the UX/UI state + a11y requirement is its own machine check (a SUPPORTED claim is not enough);
* W6-046 DB/Data Engineer evidence: migration/runtime credential split, role evidence lines;
* W6-031 per-candidate lockfile rules (hash-pinned, no pip options).
"""

from __future__ import annotations

import ast
import json
import types
from pathlib import Path

from aeos.models.portfolio import ProjectIntegration
from aeos.verification import acceptance, collector, contracts, controlled, db_evidence, envs, impact, review
from aeos.verification.steps import StepContext
from aeos.verification.verifier import CheckOutcome
from tests.unit.test_verification_support import make_world

# ------------------------------------------------------------------------------------------- negative tests

NEG_FILE = '''import pytest


def helper():
    return 1


def test_raises():
    with pytest.raises(ValueError):
        int("x")


def test_status_code(client):
    r = client.get("/x")
    assert r.status_code in (401, 403)


def test_code(client):
    assert client.post("/y").json()["error"]["code"] == "POLICY_DENIED"


def test_positive():
    # Blocked / 403 / PolicyDenied appear only in this comment
    assert helper() == 1


class TestThing:
    def test_assert_raises(self):
        self.assertRaises(KeyError, {}.__getitem__, "k")
'''


def _diff_for(path: str, text: str) -> str:
    lines = text.split("\n")
    body = "\n".join("+" + ln for ln in lines)
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n" \
           f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def test_negative_tests_come_from_the_syntax_tree_not_from_comments(tmp_path: Path):
    path = "tests/unit/test_api_neg.py"
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / path).write_text(NEG_FILE, encoding="utf-8")
    found = {t.key: t.kinds for t in controlled.find_negative_tests(tmp_path, _diff_for(path, NEG_FILE))}
    assert found == {
        "tests.unit.test_api_neg::test_raises": ["raises"],
        "tests.unit.test_api_neg::test_status_code": ["http-4xx"],
        "tests.unit.test_api_neg::test_code": ["refusal-code"],
        "tests.unit.test_api_neg.TestThing::test_assert_raises": ["raises"],
    }
    assert "tests.unit.test_api_neg::test_positive" not in found  # the old regex counted this comment


def test_only_functions_the_change_touched_count(tmp_path: Path):
    path = "tests/test_x.py"
    (tmp_path / "tests").mkdir()
    (tmp_path / path).write_text(NEG_FILE, encoding="utf-8")
    # a diff that only adds a line inside test_positive: the untouched refusal tests are not "added"
    lineno = NEG_FILE.split("\n").index("    assert helper() == 1") + 1
    diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{lineno},0 +{lineno},1 @@\n"
            "+    assert helper() == 1\n")
    assert controlled.find_negative_tests(tmp_path, diff) == []


def test_refusal_kinds_of_unittest_calls():
    fn = ast.parse("def test_a(self):\n    self.assertEqual(r.status_code, 404)\n    self.assertIn('ACCESS_DENIED', x)\n")
    assert controlled.refusal_kinds(fn.body[0]) == {"http-4xx", "refusal-code"}


def test_integration_mode_is_opt_in_only():
    assert controlled.integration_mode({}) == ""
    assert controlled.integration_mode({"integration_tests": "maybe"}) == ""
    assert controlled.integration_mode({"integration_tests": "Run"}) == "run"
    assert controlled.integration_mode({"integration_tests": "required"}) == "required"


# ------------------------------------------------------------------------------------------- contract diff

FACADE_OLD = '''
from dataclasses import dataclass

LIMIT = 3


@dataclass
class View:
    id: str
    name: str = ""


def get(session, run_id, *, actor, verbose=False):
    pass


def removed_later(x):
    pass


def _private(a):
    pass
'''


def test_facade_signature_diff_separates_breaking_from_additive():
    new = (FACADE_OLD.replace("def get(session, run_id, *, actor, verbose=False)",
                              "def get(session, run_id, *, actor, verbose=False, page=1)")
           .replace("def removed_later(x):\n    pass\n", "")
           .replace("def _private(a)", "def _private(a, b)")
           + "\n\ndef added(y=1):\n    pass\n")
    diff = contracts.compare_surfaces(contracts.public_surface(FACADE_OLD), contracts.public_surface(new))
    assert diff["breaking"] == ["removed_later: removed"]
    assert "added" in diff["added"]
    worse = FACADE_OLD.replace("def get(session, run_id, *, actor, verbose=False)",
                               "def get(session, *, actor, scope, verbose)")
    worse = worse.replace("    name: str = \"\"\n", "    name: str = \"\"\n    owner: str\n")
    breaking = contracts.compare_surfaces(contracts.public_surface(FACADE_OLD), contracts.public_surface(worse))
    text = " | ".join(breaking["breaking"])
    assert "positional parameter 'run_id' removed" in text
    assert "keyword parameter 'verbose' lost its default" in text
    assert "new required keyword parameter 'scope'" in text
    assert "View.owner: new field without default" in text


def test_openapi_diff_reports_removed_operations_and_new_requirements():
    old = {"paths": {"/a/{id}": {"get": {"parameters": [{"in": "path", "name": "id", "required": True}]},
                                 "delete": {}},
                     "/b": {"post": {"requestBody": {"required": False}}}}}
    new = {"paths": {"/a/{run_id}": {"get": {"parameters": [{"in": "path", "name": "run_id", "required": True},
                                                            {"in": "query", "name": "q", "required": True}]}},
                     "/b": {"post": {"requestBody": {"required": True}}}, "/c": {"get": {}}}}
    diff = contracts.compare_openapi(old, new)
    assert diff["breaking"] == ["delete /a/{}: operation removed", "get /a/{}: newly required parameter(s) query:q",
                                "post /b: request body newly required"]
    assert diff["added"] == ["get /c"]


def _ctx(tmp_path: Path, **kw) -> StepContext:
    return StepContext(runner=None, snapshot=tmp_path / "s", workdir=tmp_path, **kw)


def test_contract_diff_blocks_breaking_change_unless_the_taskspec_approves_it(tmp_path: Path):
    base, head = tmp_path / "bs", tmp_path / "s"
    for root, text in ((base, FACADE_OLD), (head, FACADE_OLD.replace("def removed_later(x):\n    pass\n", ""))):
        (root / "svc").mkdir(parents=True)
        (root / "svc" / "facade.py").write_text(text, encoding="utf-8")
    cats = {"contract": ["svc/facade.py"]}
    res = contracts.run_contract_diff(_ctx(tmp_path, categories=cats, base_tree=lambda: base))
    assert res is not None and res.status == "NOT_PROVEN" and res.blocking and "removed_later" in res.summary
    ok = contracts.run_contract_diff(_ctx(tmp_path, categories=cats, base_tree=lambda: base,
                                          policy={"contract_breaking": "approved"}))
    assert ok is not None and ok.status == "NOT_PROVEN" and not ok.blocking and ok.not_proven


def test_contract_tests_select_trusted_modules_that_use_the_changed_facade(tmp_path: Path):
    tr = tmp_path / "tr"
    (tr / "tests" / "acceptance").mkdir(parents=True)
    (tr / "tests" / "acceptance" / "test_uses.py").write_text("from aeos.workforce import facade\n", encoding="utf-8")
    (tr / "tests" / "acceptance" / "test_other.py").write_text("import os\n", encoding="utf-8")
    cases = {"tests.acceptance.test_uses::test_a": "passed", "tests.acceptance.test_other::test_b": "passed"}
    res = contracts.run_contract_tests(_ctx(tmp_path, trusted_copy=tr, trusted_cases=cases,
                                            categories={"contract": ["backend/aeos/workforce/facade.py"]}))
    assert res is not None and res.status == "PASS"
    assert res.details["contracts"][0]["trusted_modules"] == ["tests.acceptance.test_uses"]
    none = contracts.run_contract_tests(_ctx(tmp_path, trusted_copy=tr, trusted_cases=cases,
                                             categories={"contract": ["backend/aeos/cost/facade.py"]}))
    assert none is not None and none.status == "NOT_PROVEN" and "no trusted test exercises" in none.summary


# ------------------------------------------------------------------------------------------- impact map


def test_impact_summary_lists_selected_and_executed_checks():
    checks = {"migration_rollback": CheckOutcome("migration_rollback", "PASS"),
              "frontend_tests": CheckOutcome("frontend_tests", "NOT_PROVEN")}
    summary = impact.summarize({"migration": ["m/1.py"], "frontend": ["f/App.tsx"], "ci": [".github/x.yml"]}, checks)
    assert summary["migration"]["executed"] == ["migration_rollback"] and summary["migration"]["status"] == "EXECUTED"
    assert summary["frontend"]["checks"] == {"frontend_tests": "NOT_PROVEN", "ui_a11y": "NOT_RUN"}
    assert summary["frontend"]["status"] == "NOT_PROVEN" and summary["ci"]["executed"] == []
    lines = impact.unexecuted_statements(summary)
    assert len(lines) == 2 and any("'ci'" in ln for ln in lines)
    assert impact.selected_checks({"api": []}) >= {"negative_tests", "adversarial_pack", "contract_diff"}
    # every scope impact category is mapped (none silently adds nothing)
    from aeos.verification.scope import IMPACT_CATEGORIES

    assert set(IMPACT_CATEGORIES) == set(impact.IMPACT_CHECKS)


# ------------------------------------------------------------------------------------------- acceptance


def _report(db, world, checks: dict, categories: list[str]):
    return collector.collect_evidence(
        db, kind="VERIFICATION_REPORT", origin="verifier", collector_identity="aeos-verifier/test", claim="report",
        content={"status": "PASS", "checks": checks, "impact_categories": categories}, evidence_level="E2",
        project_id=world.project.id, run_id=world.run.id, task_id=world.task.id)


def test_ux_state_and_a11y_requirement_needs_the_verifier_ui_check(db, settings):
    """W6-043: the UX/UI Designer line is no longer MET by any SUPPORTED claim; it needs ui_a11y PASS."""
    from aeos.planning.dag import ROLE_EVIDENCE

    line = ROLE_EVIDENCE["ux-ui-designer"][0]
    old_line = "loading/error/empty state coverage and an accessibility check mapped to the acceptance criteria"
    world = make_world(db, evidence_requirements=[line, old_line])
    review_rec = types.SimpleNamespace(id="r1", verify={"claims": [{"claim": "x", "status": "SUPPORTED"}]})
    frontend_unproven = _report(db, world, {"frontend_tests": {"status": "NOT_PROVEN", "summary": "no node"}},
                                ["frontend"])
    rows = acceptance.evidence_requirements_status(db, world.task, frontend_unproven, review_rec)
    assert [r["status"] for r in rows] == ["UNMET", "UNMET"], rows
    passed = _report(db, world, {"ui_a11y": {"status": "PASS", "summary": "8 renders", "evidence_id": "ev-ui"}},
                     ["frontend"])
    rows = acceptance.evidence_requirements_status(db, world.task, passed, review_rec)
    assert [r["status"] for r in rows] == ["MET", "MET"] and rows[0]["evidence"] == "ev-ui"
    no_ui = _report(db, world, {}, ["docs"])
    rows = acceptance.evidence_requirements_status(db, world.task, no_ui, review_rec)
    assert [r["status"] for r in rows] == ["REVIEWER_ATTESTED", "REVIEWER_ATTESTED"]


def test_db_data_engineer_role_evidence_is_machine_checked(db, settings):
    """W6-046: the DB/Data Engineer lines name verifier checks; NOT_PROVEN credential split is UNMET."""
    from aeos.planning.dag import ROLE_EVIDENCE

    lines = ROLE_EVIDENCE["db-data-engineer"]
    assert [acceptance.verifier_check_requirement(x) for x in lines] == ["migration_rollback", "db_credentials"]
    world = make_world(db, evidence_requirements=lines)
    ok = _report(db, world, {"migration_rollback": {"status": "PASS", "summary": "seeded"},
                             "db_credentials": {"status": "NOT_APPLICABLE", "summary": "no deployment target"}},
                 ["migration"])
    assert [r["status"] for r in acceptance.evidence_requirements_status(db, world.task, ok, None)] == [
        "MET", "NOT_APPLICABLE"]
    gap = _report(db, world, {"migration_rollback": {"status": "PASS"},
                              "db_credentials": {"status": "NOT_PROVEN", "summary": "names only one ref"}},
                  ["migration"])
    assert [r["status"] for r in acceptance.evidence_requirements_status(db, world.task, gap, None)] == [
        "MET", "UNMET"]
    no_migration = _report(db, world, {"db_credentials": {"status": "PASS"}}, ["api"])
    assert [r["status"] for r in acceptance.evidence_requirements_status(db, world.task, no_migration, None)] == [
        "NOT_APPLICABLE", "MET"]
    assert ROLE_EVIDENCE["backend-api-engineer"] and acceptance.verifier_check_requirement(
        ROLE_EVIDENCE["backend-api-engineer"][0]) == "negative_tests"


def test_credential_split_check(db, settings, tmp_path: Path):
    world = make_world(db)
    snap = tmp_path / "s"
    (snap / "config").mkdir(parents=True)
    ctx = StepContext(runner=None, snapshot=snap, workdir=tmp_path, categories={"migration": ["m/1.py"]},
                      session=db, project=world.project)
    res = db_evidence.run(ctx)
    assert res is not None and res.status == "NOT_APPLICABLE" and "no deployment target" in res.summary
    integ = ProjectIntegration(project_id=world.project.id, domain="PERSONAL", provider="deployment-target",
                               purpose="staging", root_id="https://staging.example.invalid",
                               credential_ref="personal/app-runtime", config={})
    db.add(integ)
    db.commit()
    res = db_evidence.run(ctx)
    assert res is not None and res.status == "NOT_PROVEN" and "migration_credential_ref" in res.summary
    integ.config = {"migration_credential_ref": "personal/app-runtime"}
    db.commit()
    res = db_evidence.run(ctx)
    assert res is not None and res.status == "FAIL" and "same credential reference" in res.summary
    integ.config = {"migration_credential_ref": "personal/app-migrate"}
    db.commit()
    res = db_evidence.run(ctx)
    assert res is not None and res.status == "PASS"
    (snap / "config" / "runtime.yaml").write_text("db_auth: personal/app-migrate\n", encoding="utf-8")
    res = db_evidence.run(ctx)
    assert res is not None and res.status == "FAIL" and "config/runtime.yaml" in res.summary
    # not a DB change and not the DB role: the check does not apply
    assert db_evidence.run(StepContext(runner=None, snapshot=snap, workdir=tmp_path, session=db,
                                       project=world.project)) is None


# ------------------------------------------------------------------------------------------- lockfiles


def test_lockfile_rules_for_the_per_candidate_environment(tmp_path: Path):
    good = tmp_path / "good.txt"
    good.write_text("# comment\nleftpad==1.0.0 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    assert envs.lockfile_problems(good) == []
    bad = tmp_path / "bad.txt"
    bad.write_text("--index-url https://evil.example/simple\nfastapi==0.115.0\n-e .\n", encoding="utf-8")
    problems = envs.lockfile_problems(bad)
    assert any("--index-url" in p for p in problems) and any("without --hash" in p for p in problems)
    assert any("-e" in p for p in problems)
    snap = tmp_path / "snap"
    (snap / "sub").mkdir(parents=True)
    (snap / "requirements-dev.txt").write_text("x==1\n", encoding="utf-8")
    pip, other = envs.requirement_files(snap, ["requirements-dev.txt", "sub/poetry.lock"])
    assert pip == ["requirements-dev.txt"] and other == ["sub/poetry.lock"]


# ------------------------------------------------------------------------------------------- A11Y package


def test_a11y_review_package_carries_the_ui_evidence(db, settings):
    world = make_world(db)
    ui = collector.collect_evidence(
        db, kind="TEST_REPORT", origin="verifier", collector_identity="v", claim="ui",
        content={"suite": "ui-a11y", "states": [{"state": "error", "viewport": "mobile", "title": "t",
                                                  "violations": [], "page_errors": []}],
                 "probe": "verifier a11y probe", "not_covered": ["colour contrast"]},
        evidence_level="E2", project_id=world.project.id)
    shot = collector.collect_evidence(db, kind="SCREENSHOT", origin="verifier", collector_identity="v",
                                      claim="shot", content=b"\x89PNG\r\n\x1a\nfake", evidence_level="E2",
                                      project_id=world.project.id,
                                      environment={"ui_state": "error", "viewport": "mobile"})
    section = review.ui_evidence_section([ui, shot], {"checks": {"ui_a11y": {"status": "PASS", "evidence_id": ui.id},
                                                                 "frontend_tests": {"status": "PASS"}}})
    assert section is not None and section["ui_report_evidence_id"] == ui.id
    assert section["screenshots"] == [{"evidence_id": shot.id, "state": "error", "viewport": "mobile"}]
    assert section["states"][0]["state"] == "error" and section["not_covered"] == ["colour contrast"]
    assert review.ui_evidence_section([], {"checks": {"ruff": {}}}) is None
    assert "ui_evidence" in review.FOCUS["A11Y"]
    assert json.dumps(section)  # serialisable into the package
