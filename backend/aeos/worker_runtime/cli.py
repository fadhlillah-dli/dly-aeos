"""`aeos worker ...` - sandbox levels, OpenCode status and containment probes (ASCII output)."""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(help="Worker runtime: sandbox, OpenCode engine, containment tests.", no_args_is_help=True)
console = Console()


@app.command("status")
def status(domain: str = typer.Option("PERSONAL", help="PERSONAL or OFFICE")) -> None:
    """Show sandbox levels, the policy requirement and the OpenCode engine status."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.worker_runtime.opencode import opencode_status
    from aeos.worker_runtime.sandbox import sandbox_readiness

    ensure_database()
    with session_scope() as s:
        sb = sandbox_readiness(s, domain=domain.upper())
    for kind, lvl in sb["capabilities"].items():
        console.print(f"sandbox {kind:9s} available={lvl['available']}  strength={lvl['strength']}  {lvl['evidence']}")
    console.print(f"requirement for {domain.upper()}: {sb['status']} (kind={sb.get('kind')})")
    for r in sb.get("reasons", []):
        console.print(f"  - {r}")
    for r in sb.get("residual_risks", []):
        console.print(f"  residual risk: {r}")
    oc = opencode_status(refresh=True)
    console.print(f"opencode: {oc['status']} version={oc.get('version')} pinned={oc.get('pinned_version')} "
                  f"functional={oc.get('functional')}")


@app.command("containment")
def containment(kind: str = typer.Option("process", help="process or container")) -> None:
    """Run the negative containment tests in a real child process and record the report."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.worker_runtime.containment import containment_report, record_containment_report

    ensure_database()
    report = containment_report(kind)
    with session_scope() as s:
        record_containment_report(s, report, actor="cli:local-operator")
    console.print(f"containment ({report['kind']}): overall={report['overall']}  {report.get('summary')}")
    for r in report["results"]:
        console.print(f"  [{r['status']:12s}] {r['check']}: {r['evidence']}")
