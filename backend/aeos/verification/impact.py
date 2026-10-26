"""Impact category -> checks the verifier executes (§31 "Test scope follows impact, not file count"; W6-105).

``aeos.verification.scope.IMPACT_CATEGORIES`` groups the changed paths. This module is the single map from each
category to the verifier checks that category adds, and turns the checks of one verification into the
``impact_checks`` section of the VERIFICATION_REPORT: per category, which checks it selected, their status, and
which of them actually *executed* (PASS/FAIL). A category whose selected checks all stayed NOT_RUN/NOT_PROVEN is
stated as such on the report - impact is never silently satisfied by the always-run trusted suite.

Check names (``CheckOutcome.name`` in the verifier):

* ``migration_rollback``  - Alembic upgrade/downgrade/upgrade + seeded backup/restore + rollback with data;
* ``db_credentials``      - migration credential named separately from the runtime credential (static);
* ``contract_diff``       - facade signature / OpenAPI operation diff against the base commit;
* ``contract_tests``      - trusted tests selected by the changed contract paths (executed outcomes);
* ``negative_tests``      - added refusal tests (AST-detected) executed against the head;
* ``adversarial_pack``    - verifier-owned probes + trusted suite modules selected by category;
* ``integration_tests``   - integration-marked tests (real tools), when the TaskSpec opts them in;
* ``dependency_diff``     - dependency manifest diff (static);
* ``dependency_env``      - trusted suite in a per-candidate environment built from the candidate lockfile;
* ``frontend_tests``      - npm ci --ignore-scripts + vitest (trusted config) + typecheck + build;
* ``ui_a11y``             - per-state screenshots + the verifier's a11y probe of the built bundle.
"""

from __future__ import annotations

from typing import Any

EXECUTED = frozenset({"PASS", "FAIL"})

IMPACT_CHECKS: dict[str, tuple[str, ...]] = {
    "migration": ("migration_rollback", "db_credentials", "adversarial_pack"),
    "contract": ("contract_diff", "contract_tests"),
    "api": ("negative_tests", "adversarial_pack", "contract_diff", "integration_tests"),
    "auth": ("negative_tests", "adversarial_pack"),
    "dependency": ("dependency_diff", "dependency_env"),
    "frontend": ("frontend_tests", "ui_a11y"),
    "config_flags": ("adversarial_pack", "integration_tests"),
    "infrastructure": ("adversarial_pack",),
    "ci": ("adversarial_pack",),
    "instructions": ("adversarial_pack",),
}


def checks_for(category: str) -> tuple[str, ...]:
    return IMPACT_CHECKS.get(category, ())


def selected_checks(categories: dict[str, list[str]]) -> set[str]:
    """Every check name the impact of a change selects."""
    return {name for cat in categories for name in checks_for(cat)}


def summarize(categories: dict[str, list[str]], checks: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``impact_checks`` report section: per category its paths, selected checks with status, executed ones."""
    out: dict[str, dict[str, Any]] = {}
    for cat in sorted(categories):
        statuses = {name: str(getattr(checks.get(name), "status", "NOT_RUN") or "NOT_RUN")
                    for name in checks_for(cat)}
        executed = sorted(n for n, s in statuses.items() if s in EXECUTED)
        out[cat] = {
            "paths": list(categories[cat])[:10],
            "checks": statuses,
            "executed": executed,
            "status": "EXECUTED" if executed else "NOT_PROVEN",
        }
    return out


def unexecuted_statements(summary: dict[str, dict[str, Any]]) -> list[str]:
    """One NOT-PROVEN line per impact category for which the verifier executed nothing."""
    lines: list[str] = []
    for cat, entry in summary.items():
        if entry["executed"]:
            continue
        detail = ", ".join(f"{n}={s}" for n, s in entry["checks"].items()) or "no verifier check exists"
        lines.append(f"impact '{cat}' ({', '.join(entry['paths'][:3])}): no impact-selected check executed "
                     f"by the verifier ({detail})")
    return lines
