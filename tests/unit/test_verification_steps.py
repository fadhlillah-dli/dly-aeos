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


def test_the_owner_can_name_both_credential_references_of_a_deployment_target(personal_client, db, tmp_path: Path):
    """W6-046: the references the db_credentials check compares are settable through the integration API
    (validated like credential_ref: secret-store syntax, domain namespace, never a value) - the generic config
    rule refuses every key containing 'credential', which made the split unprovable for a real target."""
    from aeos.models.portfolio import Project
    from tests.unit.test_portfolio_support import create_project

    p = create_project(personal_client, name="Split App")
    url = f"/api/projects/{p['id']}/integrations"
    base = {"provider": "deployment-target", "purpose": "staging", "root_id": "https://staging.example.invalid",
            "credential_ref": "personal/split-runtime"}
    r = personal_client.put(url, json={**base, "config": {"migration_credential_ref": "personal/split-migrate"}})
    assert r.status_code == 200, r.text
    assert r.json()["integration"]["config"]["migration_credential_ref"] == "personal/split-migrate"
    # a value instead of a reference, another domain's namespace, or another provider: refused
    leaked = personal_client.put(url, json={**base, "config": {"migration_credential_ref": "ghp_" + "a" * 36}})
    assert leaked.status_code == 422, leaked.text
    office_ns = personal_client.put(url, json={**base, "config": {"runtime_credential_ref": "office/db"}})
    assert office_ns.status_code == 422 and office_ns.json()["error"]["code"] == "CREDENTIAL_NAMESPACE_MISMATCH"
    other = personal_client.put(url, json={"provider": "github-issues", "purpose": "issues", "root_id": "octo/split",
                                           "config": {"migration_credential_ref": "personal/split-migrate"}})
    assert other.status_code == 422 and other.json()["error"]["code"] == "SECRET_IN_FIELD"
    db.expire_all()
    project = db.get(Project, p["id"])
    snap = tmp_path / "s"
    snap.mkdir()
    res = db_evidence.run(StepContext(runner=None, snapshot=snap, workdir=tmp_path, categories={"migration": ["m.py"]},
                                      session=db, project=project))
    assert res is not None and res.status == "PASS", res
    assert res.details["targets"][0]["migration_ref"] == "personal/split-migrate"
    assert res.details["targets"][0]["runtime_ref"] == "personal/split-runtime"


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


# ------------------------------------------------------------------------------------------- adversarial pack


class _JUnitRunner:
    """Writes a scripted junit report for the probe run (no candidate code runs)."""

    def __init__(self, cases: list[tuple[str, str]], returncode: int = 0):
        self.cases, self.returncode = cases, returncode

    def python(self, args, *, pythonpath, autoload, timeout):
        from aeos.verification.procs import ProcResult

        junit = Path(args[args.index("--junitxml") + 1])
        rows = "".join(
            f'<testcase classname="tests.aeos_verifier_pack.test_probe_api_boundary" name="{n}">'
            + ('<failure message="x"/>' if o == "failed" else "") + "</testcase>" for n, o in self.cases)
        junit.write_text(f'<testsuites><testsuite name="pytest" tests="{len(self.cases)}">{rows}</testsuite>'
                         "</testsuites>", encoding="utf-8")
        return ProcResult(args=args, returncode=self.returncode, stdout="", stderr="", timed_out=False, duration_s=0.1)


def _probe_ctx(tmp_path: Path, rn) -> StepContext:
    snap, tr = tmp_path / "s", tmp_path / "tr"
    (snap / "backend" / "aeos" / "api").mkdir(parents=True)
    (snap / "backend" / "aeos" / "api" / "app.py").write_text("", encoding="utf-8")
    (tr / "tests").mkdir(parents=True)
    (tr / "tests" / "conftest.py").write_text("", encoding="utf-8")
    ini = tmp_path / "pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    return StepContext(runner=rn, snapshot=snap, workdir=tmp_path, categories={"auth": ["backend/aeos/identity/x.py"]},
                       suite_kind="CONTROLLER", is_platform=True, ini=ini, trusted_copy=tr)


def test_every_verifier_probe_must_be_in_the_report(tmp_path: Path):
    """W6-063: a probe the candidate made disappear (deselected, lost) or an abnormal pytest exit never reads as
    the pack passing; the probe set comes from the trusted pack source."""
    from aeos.verification import adversarial

    names = adversarial.probe_test_names("probe_api_boundary")
    assert "test_every_operation_refuses_an_unauthenticated_request" in names and len(names) >= 7
    full = [(n, "passed") for n in names]
    ok = adversarial.run(_probe_ctx(tmp_path / "a", _JUnitRunner(full)))
    assert ok is not None and ok.status == "PASS", ok
    partial = adversarial.run(_probe_ctx(tmp_path / "b", _JUnitRunner(full[1:])))
    assert partial is not None and partial.status == "NOT_PROVEN" and partial.blocking, partial
    assert names[0] in partial.summary
    crashed = adversarial.run(_probe_ctx(tmp_path / "c", _JUnitRunner(full, returncode=3)))
    assert crashed is not None and crashed.status == "NOT_PROVEN" and "exit 3" in crashed.summary


def test_frontend_materialisation_keeps_source_folders_named_like_build_output(tmp_path: Path):
    """W6-044: build output is left out at the package root only; npm config and installed packages everywhere."""
    from aeos.verification import frontend

    src = tmp_path / "pkg"
    for rel in ("src/build/steps.ts", "src/dist/x.ts", "dist/index.html", "coverage/x.json", "node_modules/a/i.js",
                "src/node_modules/b/i.js", ".npmrc", "src/.npmrc", "package.json"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text("x", encoding="utf-8")
    dest = tmp_path / "fe"
    frontend._materialise(src, dest)
    kept = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert kept == ["package.json", "src/build/steps.ts", "src/dist/x.ts"], kept


def test_a_ui_render_with_an_uncaught_script_error_is_never_evidence_of_its_state(tmp_path: Path):
    """W6-043: a blank page after a crash used to count as a clean render (0 violations -> ui_a11y PASS). A crash
    under the standard error/401 envelopes or the candidate's own empty app is the UI's defect (FAIL); under the
    verifier's generic answers the state is NOT-PROVEN, never PASS."""
    from aeos.verification import frontend

    def ui(states):
        for s in states:
            (tmp_path / s["screenshot"]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return {"status": "RAN", "states": states, "rules": [], "browser": "fake"}

    def state(name, errors=(), answered_by="verifier"):
        return {"state": name, "viewport": "desktop", "screenshot": f"{name}.png", "title": "t", "violations": [],
                "page_errors": list(errors), "console_errors": 0, "answered_by": answered_by}

    clean = frontend.ui_result(ui([state("loading"), state("error"), state("empty", answered_by="candidate-app")]),
                               tmp_path)
    assert clean.status == "PASS", clean
    generic_empty = frontend.ui_result(ui([state("loading"), state("empty", ["TypeError: x is undefined"])]),
                                       tmp_path)
    assert generic_empty.status == "NOT_PROVEN" and "empty/desktop" in generic_empty.summary
    shot = [e for e in generic_empty.evidence if e.kind == "SCREENSHOT" and e.env_extra["ui_state"] == "empty"][0]
    assert shot.env_extra["render_error"] is True and "uncaught script error" in shot.claim
    for crash in (state("error", ["TypeError: boom"]), state("empty", ["TypeError: boom"], "candidate-app")):
        res = frontend.ui_result(ui([state("loading"), crash]), tmp_path)
        assert res.status == "FAIL" and "uncaught-script-error" in res.summary and res.reasons, res


# ------------------------------------------------------------------------------------- sealed verifier drivers
# ADR-0120 integration note: the OpenAPI dump and the UI driver are sealed like the migration pack driver - only the
# result sealed at the driver's very end counts, and the files it wrote must still have the digests it sealed.

_APP = '''class _App:
    def openapi(self):
        return {"openapi": "3.1.0", "paths": {"/api/items": {"get": {"parameters": []}}}}


def create_app(testing=False, init_db=True):
    __EXTRA__
    return _App()
'''
_ATEXIT_REWRITE = ('import atexit, sys\n'
                   '    atexit.register(lambda: open(sys.argv[1], "w").write(\'{"openapi": "3.1.0", "paths": {}}\'))')
_FORGED_EXIT = ('import os, sys\n'
                '    open(sys.argv[1], "w").write(\'{"openapi": "3.1.0", "paths": {}}\')\n'
                '    print(\'{"ok": true, "paths": 0, "files": {}}\', flush=True)\n'
                '    os._exit(0)')


def _app_tree(root: Path, extra: str = "pass") -> Path:
    api = root / "backend" / "aeos" / "api"
    api.mkdir(parents=True)
    (root / "backend" / "aeos" / "__init__.py").write_text("", encoding="utf-8")
    (api / "__init__.py").write_text("", encoding="utf-8")
    (api / "app.py").write_text(_APP.replace("__EXTRA__", extra), encoding="utf-8")
    return root


def _sealed_ctx(tmp_path: Path) -> tuple[StepContext, dict]:
    """A step context with the verifier's real result seal on a real process runner (what the verifier wires)."""
    from aeos.verification import runner, seal

    workdir = tmp_path / "w"
    workdir.mkdir()
    seal.install_plugin(workdir)
    checked: dict = {}

    def check(step_seal, res, out_dir=None):
        checked[step_seal.name] = seal.check_driver(step_seal, res.channel or res.stdout, out_dir=out_dir)
        return checked[step_seal.name].payload

    ctx = StepContext(runner=runner.ProcessRunner(workdir=workdir), snapshot=tmp_path, workdir=workdir, timeout=120,
                      seal_step=lambda name: seal.StepSeal.new(workdir, name), check_driver_seal=check)
    return ctx, checked


def test_the_openapi_dump_is_sealed_with_the_digest_of_its_document(tmp_path: Path):
    ctx, checked = _sealed_ctx(tmp_path)
    doc, err = contracts._generated_openapi(ctx, _app_tree(tmp_path / "honest"), "head")
    assert err == "" and doc is not None and "/api/items" in doc["paths"]
    assert checked["contract_openapi_head"].status == "PASS"
    assert "openapi-head.json" in checked["contract_openapi_head"].payload["files"]
    assert {"contract_openapi_head"} <= {s["step"] for s in ctx.runner.steps}  # its own kill group, recorded


def test_a_document_rewritten_after_the_sealed_end_is_tampering_not_a_contract(tmp_path: Path):
    """Candidate app code used to be able to replace the dumped document from an atexit hook (the diff then read a
    document of its choice); the sealed digest no longer matches: FAIL (QUARANTINED by the verifier)."""
    ctx, checked = _sealed_ctx(tmp_path)
    doc, err = contracts._generated_openapi(ctx, _app_tree(tmp_path / "rewrite", _ATEXIT_REWRITE), "head")
    assert doc is None and "no usable sealed result" in err
    assert checked["contract_openapi_head"].status == "FAIL"
    assert any("changed after the driver sealed it" in p for p in checked["contract_openapi_head"].forged)


def test_a_printed_status_without_the_sealed_end_is_never_used(tmp_path: Path):
    ctx, checked = _sealed_ctx(tmp_path)
    doc, err = contracts._generated_openapi(ctx, _app_tree(tmp_path / "forged", _FORGED_EXIT), "head")
    assert doc is None and "no usable sealed result" in err
    assert checked["contract_openapi_head"].status == "MISSING" and not checked["contract_openapi_head"].forged
    # the same tree without the verifier's seal (a scripted context) would have taken the printed line
    from aeos.verification import runner

    unsealed = StepContext(runner=runner.ProcessRunner(workdir=ctx.workdir), snapshot=tmp_path, workdir=ctx.workdir,
                           timeout=120)
    doc2, _ = contracts._generated_openapi(unsealed, tmp_path / "forged", "head")
    assert doc2 == {"openapi": "3.1.0", "paths": {}}


def test_sealed_output_digests_are_checked_on_the_host(tmp_path: Path):
    import hashlib

    from aeos.verification import seal

    (tmp_path / "a.png").write_bytes(b"one")
    good = {"files": {"a.png": hashlib.sha256(b"one").hexdigest()}}
    assert seal.output_problems(good, tmp_path) == [] and seal.sealed_output(good, "a.png")
    assert seal.output_problems({}, tmp_path) == [] and not seal.sealed_output({}, "a.png")
    (tmp_path / "a.png").write_bytes(b"two")
    assert "changed after the driver sealed it" in seal.output_problems(good, tmp_path)[0]
    (tmp_path / "a.png").unlink()
    assert "disappeared" in seal.output_problems(good, tmp_path)[0]
    for bad in ("../a.png", "sub/a.png", "C:a.png", ".hidden"):
        assert "not a plain file name" in seal.output_problems({"files": {bad: "0" * 64}}, tmp_path)[0]
    assert seal.output_problems({"files": ["a.png"]}, tmp_path) == ["the sealed result's output digests are not "
                                                                    "readable"]


def test_only_screenshots_the_sealed_ui_report_digested_are_evidence(tmp_path: Path):
    import hashlib

    from aeos.verification import frontend

    states = []
    for name in ("loading", "error"):
        (tmp_path / f"{name}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        states.append({"state": name, "viewport": "desktop", "screenshot": f"{name}.png", "title": "t",
                       "violations": [], "page_errors": [], "console_errors": 0, "answered_by": "verifier"})
    digest = hashlib.sha256((tmp_path / "loading.png").read_bytes()).hexdigest()
    ui = {"status": "RAN", "states": states, "rules": [], "browser": "fake", "sealed": True,
          "files": {"loading.png": digest}}
    res = frontend.ui_result(ui, tmp_path)
    shots = [e.env_extra["ui_state"] for e in res.evidence if e.kind == "SCREENSHOT"]
    assert shots == ["loading"], shots
    report = res.evidence[0].content
    assert isinstance(report, dict) and report["sealed_report"] is True
    assert report["screenshots_without_sealed_digest"] == ["error.png"]
    assert any("without a digest" in n for n in res.not_proven)
    # a direct (unsealed) call keeps using what the driver printed
    unsealed = frontend.ui_result({**ui, "sealed": False}, tmp_path)
    assert sorted(e.env_extra["ui_state"] for e in unsealed.evidence if e.kind == "SCREENSHOT") == ["error", "loading"]


def test_the_ui_driver_is_a_sealed_step(tmp_path: Path):
    """No Chromium needed: a bundle without index.html makes the driver report NOT_RUN - through the seal."""
    from aeos.verification import frontend

    ctx, checked = _sealed_ctx(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    ui = frontend.run_ui(ctx.runner, dist, ctx.workdir / "r" / "ui", ctx=ctx)
    assert ui["sealed"] is True and ui["status"] == "NOT_RUN" and "index.html" in ui["reason"], ui
    assert checked[frontend.UI_STEP].status == "PASS"
    assert frontend.UI_STEP in {s["step"] for s in ctx.runner.steps}


def test_installed_dependency_listing_and_startup_hooks_are_read_by_the_verifier(tmp_path: Path):
    site = tmp_path / "site-packages"
    for name, version in (("Alpha", "1.0"), ("beta_pkg", "2.1")):
        info = site / f"{name}-{version}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    (site / "broken-0.1.dist-info").mkdir()  # no METADATA: the listing is incomplete, never silently short
    (site / "a1_hook.pth").write_text("import os; os.environ.setdefault('X', '1')\n", encoding="utf-8")
    (site / "paths.pth").write_text("./extra\n", encoding="utf-8")
    pins, problems = envs.installed_pins([site, tmp_path / "missing"])
    assert pins == ["alpha==1.0", "beta_pkg==2.1"]
    assert len(problems) == 1 and problems[0].startswith("broken-0.1.dist-info")
    assert envs.startup_hooks([site]) == [{"file": "a1_hook.pth", "executes_code": True},
                                          {"file": "paths.pth", "executes_code": False}]
    assert all(d.name == "site-packages" for d in envs.site_dirs(tmp_path / "ve"))
