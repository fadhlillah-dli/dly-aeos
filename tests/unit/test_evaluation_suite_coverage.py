"""Suite protocol coverage (master §43 "uji normal, negative, edge, adversarial, incident/recovery",
§44 task classes) and secret hygiene of planned runs (AGENTS.md invariant 5)."""

from __future__ import annotations

from aeos.evaluation import definitions as D
from aeos.evaluation import service

CFG = {"model_id": "glm-5.3-flash", "model_version": "UNKNOWN", "provider": "opencode-zen",
       "provider_route": "opencode-zen/glm-flash-free", "harness": "opencode", "harness_version": "1.4.0",
       "prompt_revision": "impl-v1", "tools": ["edit"], "policy_version": "pol-1", "context_params": {}}


def test_every_suite_covers_the_section_43_case_kinds():
    for s in D.SUITES:
        kinds = {c["kind"] for c in s["cases"]}
        missing = set(D.REQUIRED_CASE_KINDS) - kinds
        assert not missing, (s["suite_key"], missing)
        assert kinds & set(D.INCIDENT_OR_RECOVERY), s["suite_key"]
        assert len({c["case_id"] for c in s["cases"]}) == len(s["cases"]), s["suite_key"]
        for c in s["cases"]:
            assert c["acceptance"] and c["expected"] and set(c["gate_checks"]) == set(D.GATES)


def test_section_44_task_classes_are_all_present():
    assert {s["task_class"] for s in D.SUITES} >= {
        "small-bugfix", "feature-fe-api", "shared-schema-auth-change", "independent-readonly-reviews",
        "docs-only-change", "provider-quota-interruption", "self-build-controller-change", "crash-recovery"}
    assert len({s["suite_key"] for s in D.SUITES}) == len(D.SUITES)


def test_every_executor_role_and_tech_area_has_a_protected_suite():
    """§46/§48 (re-audit): only 6 of 26 executor roles (and ~5 of the 10 named areas) could be assessed. Every
    executor role now has a protected suite definition, so product discovery, UX, architecture, data and delivery
    outcomes can be scored once the owner-authored fixtures exist (results stay BELUM DIUJI until then)."""
    from aeos.workforce import catalog

    covered = {s["role_key"] for s in D.SUITES}
    assert covered >= catalog.EXECUTOR_ROLE_KEYS, sorted(catalog.EXECUTOR_ROLE_KEYS - covered)
    areas = {  # the §46 areas -> a role that is assessed in that area
        "product discovery": "product-analyst", "UX": "ux-ui-designer", "architecture": "solution-architect",
        "coding": "feature-implementer", "data": "db-data-engineer", "security": "security-engineer",
        "delivery": "release-coordinator", "reliability": "sre-operations-assistant",
        "documentation": "technical-writer", "operations": "incident-analyst"}
    assert all(role in covered for role in areas.values())
    assert covered <= catalog.EXECUTOR_ROLE_KEYS  # no suite for a title that grants nothing


def test_planned_run_notes_and_limits_are_redacted(db):
    service.seed_suites(db)
    db.commit()
    secret = "sk-or-v1-" + "b" * 48
    run = service.plan_run(db, suite_key="eval.small-bugfix", configuration=CFG, role_key=None, topology="A",
                           repetitions=1, limits={"max_attempts": 1, "max_minutes": 5, "max_cost_usd": 0,
                                                  "note": f"key={secret}"},
                           actor_user_id="owner", notes=f"use token {secret}")
    db.commit()
    assert secret not in run.notes and secret not in str(run.limits)
    assert run.status == "PLANNED" and run.sample_size == 0


def test_reliability_reports_provider_availability_without_dropping_samples():
    """§43 Reliability: completion, tool/schema failures, repair count, provider availability."""
    samples = [{"status": "ACCEPTED"}, {"status": "QUOTA_INTERRUPTED"}, {"status": "FAILED", "provider_unavailable": True},
               {"status": "REJECTED"}]
    m = service.compute_metrics(samples)
    assert m["sample_size"] == 4
    assert m["reliability"]["provider_unavailable"] == 2
    assert m["reliability"]["provider_availability_rate"] == 0.5
    assert "provider_availability_rate" in next(d for d in D.DIMENSIONS if d["key"] == "reliability")["metrics"]
    assert service.compute_metrics([])["reliability"]["provider_availability_rate"] is None


def test_cli_leaderboard_never_ranks_other_domain_runs(db):
    """The local CLI leaderboard ranks platform runs only unless a domain is chosen explicitly."""
    from typer.testing import CliRunner

    from aeos.cli import app
    from aeos.evaluation import facade

    service.seed_suites(db)
    run = service.plan_run(db, suite_key="eval.small-bugfix", configuration=CFG, role_key=None, topology="A",
                           repetitions=1, limits={"max_attempts": 1, "max_minutes": 5, "max_cost_usd": 0},
                           actor_user_id="owner", notes="office pilot", domain="OFFICE")
    db.commit()
    facade.record_result(db, run.id, samples=[{"status": "ACCEPTED"}],
                         gate_results={"security": "PASS", "authority": "PASS", "evidence": "PASS"},
                         result_source="verifier")
    db.commit()
    db.close()
    runner = CliRunner()
    plain = runner.invoke(app, ["eval", "leaderboard", "--role", "feature-implementer"])
    assert plain.exit_code == 0, plain.output
    assert "glm-5.3-flash" not in plain.output and "BELUM DIUJI" in plain.output
    personal = runner.invoke(app, ["eval", "leaderboard", "--role", "feature-implementer", "--domain", "PERSONAL"])
    assert personal.exit_code == 0 and "glm-5.3-flash" not in personal.output
    office = runner.invoke(app, ["eval", "leaderboard", "--role", "feature-implementer", "--domain", "OFFICE"])
    assert office.exit_code == 0 and "glm-5.3-flash" in office.output and office.output.isascii()
