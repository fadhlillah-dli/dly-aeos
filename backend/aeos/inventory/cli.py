"""`aeos inventory` - reconcile, list and show components (ASCII output, no LLM)."""

from __future__ import annotations

import json

import typer

from aeos.inventory.asciitext import ascii_safe, table

app = typer.Typer(help="Tools & Repos inventory: reconcile (read-only probes), list, show.", no_args_is_help=True)

DOMAIN_OPTION = typer.Option("PERSONAL", help="Domain view (PERSONAL or OFFICE); platform components appear in both")


def _domain(value: str) -> str:
    v = value.upper()
    if v not in ("PERSONAL", "OFFICE"):
        raise typer.BadParameter("domain must be PERSONAL or OFFICE")
    return v


@app.command("reconcile")
def reconcile_cmd(
    component: str = typer.Option(None, "--component", "-c", help="Refresh one component id (default: core set)"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
) -> None:
    """Run read-only probes and compare desired vs observed. Never installs or changes anything."""
    from aeos.cli import ensure_database
    from aeos.core.config import get_settings
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.inventory.reconcile import reconcile, run_view

    ensure_database()
    try:
        with session_scope() as s:
            run = reconcile(s, get_settings(), component_id=component, actor="local-operator", trigger="cli")
            view = run_view(run) or {}
    except AeosError as exc:
        typer.echo(ascii_safe(f"{exc.code}: {exc.message}"), err=True)
        raise typer.Exit(1) from exc
    if as_json:
        typer.echo(json.dumps(view, indent=2, ensure_ascii=True, default=str))
        return
    rows = [[cid, st.get("status"), st.get("observations", ""), st.get("reason", "")]
            for cid, st in sorted((view.get("component_status") or {}).items())]
    typer.echo(table(["COMPONENT", "PROBE", "OBS", "NOTE"], rows))
    drift = (view.get("summary") or {}).get("drift") or {}
    typer.echo(ascii_safe(f"Run {view.get('id')} status={view.get('status')} observations={view.get('observations_recorded')}"))
    typer.echo(ascii_safe(f"Drift: open={drift.get('open_detected')} new={drift.get('new')} resolved={drift.get('resolved')}"))
    for cls, n in sorted((drift.get("by_class") or {}).items()):
        if n:
            typer.echo(ascii_safe(f"  {cls}: {n}"))
    for cid, inv in sorted(((view.get("summary") or {}).get("invalidated") or {}).items()):
        typer.echo(ascii_safe(f"Changed: {cid} ({', '.join(inv.get('invalidators') or [])}) -> "
                              f"{inv.get('checks_marked_stale', 0)} readiness check(s) STALE; run: aeos doctor"))
    for src in view.get("sources") or []:
        if src.get("status") != "READ":
            typer.echo(ascii_safe(f"Coverage gap: {src.get('source')} {src.get('status')}"))


@app.command("list")
def list_cmd(
    kind: str = typer.Option(None, help="Filter by kind"),
    requirement: str = typer.Option(None, help="REQUIRED | OPTIONAL | CANDIDATE | REFERENCE_ONLY"),
    category: str = typer.Option(None, help="Fold domain, e.g. workforce"),
    q: str = typer.Option(None, help="Search text"),
    domain: str = DOMAIN_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
) -> None:
    """List components with separate status dimensions (design labels are not runtime state)."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.inventory.views import list_components

    ensure_database()
    with session_scope() as s:
        data = list_components(s, domain=_domain(domain), kind=kind, requirement=requirement, category=category,
                               q=q, page_size=200)
    if as_json:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=True, default=str))
        return
    rows = []
    for it in data["items"]:
        d = it["dimensions"]
        rows.append([it["component_id"], it["requirement"], it["design_label"], d["installation"]["effective"],
                     d["connectivity"]["effective"], d["functional"]["effective"], d["enablement"]["effective"],
                     d["usage"]["effective"], it["version"]["observed_version"] or "-"])
    typer.echo(table(["COMPONENT", "REQ", "DESIGN", "INSTALL", "CONNECT", "FUNCTION", "ENABLE", "USAGE", "VERSION"],
                     rows, max_width=32))
    typer.echo(ascii_safe(f"{data['total']} components (mode {data['mode']}); "
                          f"last reconciled: {data['checked_at'] or 'never'}"))
    for g in (data.get("coverage") or {}).get("gaps", []):
        if g["severity"] in ("HIGH", "MEDIUM"):
            typer.echo(ascii_safe(f"Gap [{g['severity']}]: {g['message']}"))


@app.command("show")
def show_cmd(
    component_id: str = typer.Argument(..., help="Component id, e.g. claude-code"),
    domain: str = DOMAIN_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
) -> None:
    """Show every field group of one component (identity, source, versions, lifecycle, evidence...)."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.inventory.views import component_detail

    ensure_database()
    try:
        with session_scope() as s:
            data = component_detail(s, component_id, domain=_domain(domain))
    except AeosError as exc:
        typer.echo(ascii_safe(f"{exc.code}: {exc.message}"), err=True)
        raise typer.Exit(1) from exc
    if as_json:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=True, default=str))
        return
    ident, src, ver, use = data["identity"], data["source"], data["versions"], data["usage_role"]
    typer.echo(ascii_safe(f"{ident['friendly_name']} [{ident['component_id']}] kind={ident['kind']}"))
    typer.echo(ascii_safe(f"  function: {ident['function']}"))
    typer.echo(ascii_safe(f"  why: {use['why_selected']}"))
    typer.echo(ascii_safe(f"  requirement={use['requirement']} design={use['design_label']} role={use['usage_role']}"))
    typer.echo(ascii_safe(f"  source: {src['source_url'] or 'UNKNOWN (' + str(src['source_url_note']) + ')'}"
                          f" identity={src['identity_verified']} refs={','.join(src['source_refs']) or '-'}"))
    typer.echo(ascii_safe(f"  license: {src['license']['value'] or 'UNKNOWN'}"))
    typer.echo(ascii_safe(f"  version: pin={ver['approved_pin'] or 'UNKNOWN'} observed={ver['observed_version'] or '-'}"
                          f" status={ver['status']}"))
    rows = [[k, v["effective"], v["at"] or "-", v["source"], v.get("reason") or ""] for k, v in data["lifecycle"].items()]
    typer.echo(table(["DIMENSION", "VALUE", "AT", "SOURCE", "REASON"], rows, max_width=48))
    for d in data["evidence"]["drift"]:
        typer.echo(ascii_safe(f"  drift: {d['drift_class']} [{d['severity']}] {d['summary']}"))
    nxt = data.get("next_action") or {}
    if nxt:
        typer.echo(ascii_safe(f"  next action: {nxt.get('label')}" + (f" ({nxt['command']})" if nxt.get("command")
                                                                      else "")))


@app.command("sbom")
def sbom_cmd(out: str = typer.Option(None, "--out", help="Write the CycloneDX JSON here (default: print)")) -> None:
    """CycloneDX 1.5 SBOM of requirements.lock + frontend/package-lock.json (declarations, not running processes)."""
    from pathlib import Path

    from aeos import __version__
    from aeos.core.paths import repo_root
    from aeos.inventory.dependencies import cyclonedx

    body = cyclonedx(repo_root(), version=__version__)
    text = json.dumps(body, indent=2, ensure_ascii=True, sort_keys=False)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        typer.echo(ascii_safe(f"SBOM written: {path.name} ({len(body['components'])} components)"))
        return
    typer.echo(text)


@app.command("va-scan")
def va_scan_cmd() -> None:
    """Vulnerability assessment of the platform's requirements.lock with pip-audit (needs network) -> VA_REPORT.

    Stores the real scanner result as trusted evidence for the DLY AEOS project; refuses when pip-audit is
    missing or produced no result (nothing is recorded then).
    """
    from sqlalchemy import select

    from aeos.cli import ensure_database
    from aeos.core.config import get_settings
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.inventory.va_scan import scan_platform_dependencies
    from aeos.models.portfolio import Project
    from aeos.operations import facade as operations

    ensure_database()
    ctrl = operations.controller_report(get_settings())
    with session_scope() as s:
        project = s.scalar(select(Project).where(Project.is_platform.is_(True)))
        if project is None:
            typer.echo("platform project not registered (run: aeos seed)")
            raise typer.Exit(2)
        try:
            out = scan_platform_dependencies(s, project, actor="local-operator", commit=ctrl.get("commit"),
                                             dirty=ctrl.get("dirty"))
        except AeosError as exc:
            typer.echo(ascii_safe(f"va-scan refused: {exc.code}: {exc.message}"))
            if (exc.details or {}).get("remedy"):
                typer.echo(ascii_safe(f"  remedy: {exc.details['remedy']}"))
            raise typer.Exit(1) from exc
    typer.echo(ascii_safe(f"VA_REPORT {out['evidence_id']}: {out['findings']} finding(s) in "
                          f"{out['packages_scanned']} packages"))


@app.command("poll")
def poll_cmd(
    enable: bool = typer.Option(None, "--enable/--disable", help="Turn the optional TTL-driven polling on or off"),
    reason: str = typer.Option("", "--reason", help="Why (audited)"),
) -> None:
    """Show or set optional inventory polling (off by default; read-only refresh of expired components only)."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.inventory.loop import MAX_PER_TICK, TICK_SECONDS, due_components, poll_enabled, set_poll_enabled

    ensure_database()
    with session_scope() as s:
        if enable is None:
            typer.echo(ascii_safe(f"inventory polling: {'ENABLED' if poll_enabled(s) else 'DISABLED'} (every "
                                  f"{int(TICK_SECONDS)}s, max {MAX_PER_TICK} components per tick); due now: "
                                  f"{', '.join(due_components(s)) or '-'}"))
            return
        if not reason.strip():
            raise typer.BadParameter("--reason is required when changing polling")
        set_poll_enabled(s, enable, actor="local-operator", reason=reason)
    typer.echo("inventory polling " + ("enabled" if enable else "disabled") + " (audited); takes effect on the next tick")
