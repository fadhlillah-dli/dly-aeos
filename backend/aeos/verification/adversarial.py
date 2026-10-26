"""Trusted adversarial pack selected by impact (§22 "Verifier melakukan negative/edge/adversarial tests sesuai
dampak, bukan hanya tes yang dibuat implementer"; §31 G2; W6-063/W6-104).

Two parts, both trusted controller code and never the candidate's:

* **Verifier-owned probes** (``aeos/verification/packs/probe_*.py``): executed for this change only, against the
  candidate, through the verifier runner and inside the tamper window. ``probe_api_boundary`` enumerates every
  operation of the candidate app's OpenAPI document (unauthenticated, cross-origin, non-loopback Host, VIEWER
  write, malformed JSON, path-traversal ids, hostile login payloads). Selected by ``api`` and ``auth`` impact.
* **Trusted suite modules by category**: the trusted acceptance modules that hold the §50 adversarial rows for a
  category (auth boundary, domain scope, untrusted input, runtime binding, immutability, containment, delivery,
  self-build). They ran with the full trusted suite; the pack records their executed outcome per category.

Both exist for the platform's own layout and trusted suite (``CONTROLLER``). A managed project's owner-registered
suite carries no category map yet: the report then states the categories as NOT-PROVEN (non-blocking) instead of
implying adversarial coverage. A probe that fails is a FAIL; a probe that could not run blocks PASS.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Any

from aeos.verification.steps import (
    StepContext,
    StepEvidence,
    StepResult,
    outcomes_for,
    pack_text,
    run_pytest,
)

SUITE_PACK: dict[str, tuple[str, ...]] = {
    "api": ("test_auth_boundary", "test_domain_scope", "test_untrusted_input"),
    "auth": ("test_auth_boundary", "test_domain_scope", "test_runtime_binding"),
    "config_flags": ("test_runtime_binding", "test_routing_cost"),
    "migration": ("test_immutability", "test_operations"),
    "infrastructure": ("test_containment",),
    "ci": ("test_delivery", "test_self_build"),
    "instructions": ("test_self_build", "test_governance"),
}
PROBE_PACK: dict[str, tuple[str, ...]] = {
    "api": ("probe_api_boundary",),
    "auth": ("probe_api_boundary",),
}
PACK_DIR = "aeos_verifier_pack"
SUITE_MODULE_PREFIX = "tests.acceptance."
MARK_EXPR = "not integration and not network"


def covered_categories(categories: dict[str, list[str]]) -> list[str]:
    return sorted(c for c in categories if c in SUITE_PACK or c in PROBE_PACK)


def platform_layout(snapshot: Path) -> bool:
    return (snapshot / "backend" / "aeos" / "api" / "app.py").is_file()


def probe_test_names(probe: str) -> list[str]:
    """The test functions a verifier-owned probe module defines (from its trusted source, syntax tree only)."""
    tree = ast.parse(pack_text(f"{probe}.py"))
    return sorted(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name.startswith("test_"))


def install_probes(trusted_copy: Path, probes: list[str]) -> list[Path]:
    """Copy the selected probe modules next to the trusted ``tests/conftest.py`` of the run's suite copy."""
    pack = trusted_copy / "tests" / PACK_DIR
    if pack.exists():
        shutil.rmtree(pack)
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text("", encoding="utf-8")
    out = []
    for name in probes:
        target = pack / f"test_{name}.py"
        target.write_text(pack_text(f"{name}.py"), encoding="utf-8")
        out.append(target)
    return out


def run(ctx: StepContext) -> StepResult | None:
    cats = covered_categories(ctx.categories)
    if not cats:
        return None
    if ctx.suite_kind != "CONTROLLER" or not ctx.is_platform:
        msg = (f"verifier-authored adversarial cases for impact {', '.join(cats)}: no trusted adversarial pack exists "
               "for this project's registered suite/layout (probes target the DLY AEOS boundary); NOT-PROVEN")
        return StepResult("adversarial_pack", "NOT_PROVEN", msg, {"categories": cats}, not_proven=[msg])
    wanted = sorted({m for c in cats for m in SUITE_PACK.get(c, ())})
    # pack modules this trusted suite actually contains (collected with the trusted code); others are absent
    modules = [m for m in wanted if any(k.startswith((f"{SUITE_MODULE_PREFIX}{m}::", f"{SUITE_MODULE_PREFIX}{m}."))
                                        for k in ctx.expected)]
    absent = [m for m in wanted if m not in modules]
    probes = sorted({p for c in cats for p in PROBE_PACK.get(c, ())}) if platform_layout(ctx.snapshot) else []
    if probes and not (ctx.trusted_copy is not None and (ctx.trusted_copy / "tests" / "conftest.py").is_file()):
        probes = []  # the probes reuse the trusted suite's shared fixtures (settings/db/make_app/create_admin)
    if not modules and not probes:
        msg = (f"verifier-authored adversarial cases for impact {', '.join(cats)}: the trusted suite has none of the "
               f"pack modules ({', '.join(wanted) or '-'}) and the candidate is not the DLY AEOS layout the probes "
               "target; NOT-PROVEN")
        return StepResult("adversarial_pack", "NOT_PROVEN", msg, {"categories": cats, "absent_modules": absent},
                          not_proven=[msg])
    suite_rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    blocking: list[str] = []
    stated: list[str] = []
    if absent:
        stated.append(f"adversarial pack: trusted module(s) {', '.join(absent)} are not part of this trusted suite")
    for mod in modules:
        counts = outcomes_for(ctx.trusted_cases, [f"{SUITE_MODULE_PREFIX}{mod}::", f"{SUITE_MODULE_PREFIX}{mod}."])
        state = ("FAIL" if counts["failed"] else "NOT_PROVEN" if (counts["skipped"] or not counts["passed"])
                 else "PASS")
        suite_rows.append({"module": mod, **counts, "status": state})
        if state == "FAIL":
            reasons.append(f"adversarial pack: trusted module {mod} failed ({counts['failed']} test(s))")
        elif state == "NOT_PROVEN":
            blocking.append(f"adversarial pack: trusted module {mod} did not pass completely "
                            f"({counts['passed']} passed, {counts['skipped']} skipped)")
    probe_rows: list[dict[str, Any]] = []
    evidence: list[StepEvidence] = []
    if probes:
        if ctx.trusted_copy is None or ctx.ini is None:
            blocking.append("adversarial probes could not run: the trusted suite copy is missing")
        else:
            files = install_probes(ctx.trusted_copy, probes)
            junit = ctx.workdir / "r" / "adversarial-junit.xml"
            junit.parent.mkdir(parents=True, exist_ok=True)
            args = ["-m", "pytest", *[str(f) for f in files], "-c", str(ctx.ini), "--rootdir", str(ctx.trusted_copy),
                    "--confcutdir", str(ctx.trusted_copy), "-p", "no:cacheprovider", "-q", "--junitxml", str(junit),
                    "-m", MARK_EXPR, *ctx.basetemp("b7")]
            res, report, parse_error, seal_problems = run_pytest(
                ctx, "adversarial_pack", args, pythonpath=ctx.snapshot / "backend", autoload=False, junit=junit,
                label="adversarial probes")
            if res.timed_out:
                reasons.append(f"adversarial probes timed out after {ctx.timeout:.0f}s")
            elif seal_problems:  # the junit/exit code cannot be trusted (verifier result seal): never a pass
                reasons.append("adversarial probes: " + "; ".join(seal_problems)[:500])
            elif report is None:
                blocking.append(f"adversarial probes produced no report ({parse_error}; exit {res.returncode})")
            else:
                for c in report.cases:
                    probe_rows.append({"test": c.key, "outcome": c.outcome, "message": c.message[:400]})
                bad = [c.key for c in report.cases if c.outcome in ("failed", "error")]
                if bad:
                    reasons.append(f"adversarial probe(s) failed against the candidate: {', '.join(bad[:5])}")
                if report.skipped or report.passed == 0:
                    blocking.append(f"adversarial probes did not all run ({report.passed} passed, "
                                    f"{report.skipped} skipped)")
                elif res.returncode not in (0, 1):  # interrupted / internal / usage error: report may be partial
                    blocking.append(f"adversarial probes ended with pytest exit {res.returncode}; the report may "
                                    "be incomplete")
                ran = {c.key.rsplit("::", 1)[-1].split("[", 1)[0] for c in report.cases}
                absent_probes = sorted(n for p in probes for n in probe_test_names(p) if n not in ran)
                if absent_probes:  # every verifier-authored case must be in the report (none deselected/lost)
                    blocking.append(f"adversarial probe(s) missing from the report: {', '.join(absent_probes[:5])}")
            evidence.append(StepEvidence(
                "TEST_REPORT", f"Verifier-owned adversarial probes ({', '.join(probes)}) against the candidate: "
                               f"{'FAIL' if reasons else 'PASS' if not blocking else 'NOT_PROVEN'}",
                {"suite": "verifier-adversarial-pack", "trust": "HIGH - verifier-authored cases from the trusted "
                 "controller, not from the candidate", "categories": cats, "probes": probes, "cases": probe_rows,
                 "exit_code": res.returncode, "timed_out": res.timed_out, "duration_s": res.duration_s,
                 "parse_error": parse_error, "output_tail": res.tail(4000)}, "E2", "adversarial_pack"))
    status = "FAIL" if reasons else ("NOT_PROVEN" if blocking else "PASS")
    passed_probes = sum(1 for r in probe_rows if r["outcome"] == "passed")
    summary = (reasons[0] if reasons else blocking[0] if blocking else
               f"{passed_probes} verifier probe(s) + {len(modules)} trusted adversarial module(s) passed for "
               f"impact {', '.join(cats)}")
    details = {"categories": cats, "probes": probes, "probe_cases": probe_rows[:60], "suite_modules": suite_rows,
               "absent_modules": absent}
    return StepResult("adversarial_pack", status, summary, details, evidence, reasons=reasons,
                      not_proven=[*blocking, *stated], blocking=list(blocking))
