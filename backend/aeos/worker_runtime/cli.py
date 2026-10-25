"""`aeos worker ...` - sandbox levels, OpenCode status and containment probes (ASCII output)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape

app = typer.Typer(help="Worker runtime: sandbox, OpenCode engine, containment tests.", no_args_is_help=True)
console = Console()


def _out(text: str) -> None:
    """ASCII-only, markup-safe output (AGENTS.md: legacy Windows consoles). Reasons can come from
    other modules and contain non-ASCII characters or square brackets."""
    clean = str(text).replace("\u00a7", "s").replace("\u2014", "-").replace("\u2192", "->")
    console.print(escape(clean.encode("ascii", "replace").decode("ascii")), highlight=False)


@app.command("status")
def status(domain: str = typer.Option("PERSONAL", help="PERSONAL or OFFICE")) -> None:
    """Show sandbox levels, the policy requirement and the OpenCode engine status."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.worker_runtime.integrity import hold_view
    from aeos.worker_runtime.opencode import opencode_status
    from aeos.worker_runtime.sandbox import sandbox_readiness

    ensure_database()
    with session_scope() as s:
        sb = sandbox_readiness(s, domain=domain.upper())
        hold = hold_view(s, domain=domain.upper())
    if hold["status"] == "ACTIVE":
        h = hold["hold"]
        subject = h.get("subject_id") or "(a run in the other domain)"
        _out(f"integrity hold: ACTIVE id={h.get('id')} detected={h.get('detected_at')} source={h.get('source')} "
             f"subject={subject} - no worker is admitted in any domain until the owner reconciles")
        for f in (h.get("findings") or [])[:20]:
            _out(f"  finding: {f.get('kind', '')} {f.get('path', '')} {f.get('change', '')} {f.get('detail', '')}")
        if h.get("other_domain_findings"):
            _out(f"  {h['other_domain_findings']} finding(s) of the other domain: review them with "
                 "`aeos worker status --domain <that domain>`")
        _out(f"  reconcile after review: {hold.get('remedy')}")
    else:
        _out("integrity hold: CLEAR")
    for kind, lvl in sb["capabilities"].items():
        strength = lvl["strength"]
        if kind == "container" and sb.get("kind") == "container":
            # the host probe cannot know the proof; this domain's fresh PASS containment report does
            strength = f"PROVEN for {domain.upper()} (containment PASS {(sb.get('containment') or {}).get('checked_at')})"
        _out(f"sandbox {kind:9s} available={lvl['available']}  strength={strength}  {lvl['evidence']}")
    _out(f"requirement for {domain.upper()}: {sb['status']} (kind={sb.get('kind')})")
    for r in sb.get("reasons", []):
        _out(f"  - {r}")
    for r in sb.get("residual_risks", []):
        _out(f"  residual risk: {r}")
    oc = opencode_status(refresh=True)
    _out(f"opencode: {oc['status']} version={oc.get('version')} pinned={oc.get('pinned_version')} "
                  f"functional={oc.get('functional')}")


@app.command("containment")
def containment(kind: str = typer.Option("process", help="process or container"),
                domain: str = typer.Option("PERSONAL", help="container profile: PERSONAL or OFFICE")) -> None:
    """Run the negative containment tests in a real child process / the worker image and record the report."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.worker_runtime.containment import containment_report, record_containment_report

    if kind not in ("process", "container") or domain.upper() not in ("PERSONAL", "OFFICE"):
        _out("kind must be process|container and domain PERSONAL|OFFICE")
        raise typer.Exit(2)
    ensure_database()
    report = containment_report(kind, domain=domain.upper())
    with session_scope() as s:
        record_containment_report(s, report, actor="cli:local-operator")
    _out(f"containment ({report['kind']}{' ' + report['domain'] if report.get('domain') else ''}): "
         f"overall={report['overall']}  {report.get('summary')}")
    for r in report["results"]:
        _out(f"  [{r['status']:12s}] {r['check']}: {r['evidence']}")
    if report.get("remedy"):
        _out(f"remedy: {report['remedy']}")


@app.command("contract-test")
def contract_test(binary: str = typer.Option(None, help="OpenCode binary (default: configured / PATH)")) -> None:
    """Run the worker engine contract test (real OpenCode, scripted loopback model) and record the evidence."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.worker_runtime.contract import record_contract_test, run_contract_test

    ensure_database()
    report = run_contract_test(binary=binary)
    with session_scope() as s:
        ev = record_contract_test(s, report, actor="cli:local-operator")
    _out(f"worker contract test: overall={report['overall']} engine={report.get('engine_status')} "
         f"version={report.get('engine_version')} evidence={ev or 'not recorded (NOT_RUN)'}")
    for r in report["results"]:
        _out(f"  [{r['status']:8s}] {r['check']}: {r['evidence']}")
    _out(f"model behaviour: {report['scope']['model_behaviour']}")
    if report["overall"] not in ("PASS",):
        raise typer.Exit(1)


@app.command("reap")
def reap() -> None:
    """Remove worker containers whose run is no longer active (after a controller crash)."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.worker_runtime.container import reap_orphans_for_session

    ensure_database()
    with session_scope() as s:
        removed = reap_orphans_for_session(s)
    _out(f"removed {len(removed)} orphan worker container(s): {', '.join(removed) or '-'}")
