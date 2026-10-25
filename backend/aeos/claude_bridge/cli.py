"""`aeos bridge ...` - inspect and test the ClaudeNativeBridge (ASCII output only)."""

from __future__ import annotations

import json

import typer

app = typer.Typer(help="Claude native bridge (official Claude Code, native Max login, no API).", no_args_is_help=True)


def _line(label: str, value: object) -> None:
    typer.echo(f"{label:<22} {value}")


@app.command("status")
def status_cmd(as_json: bool = typer.Option(False, "--json", help="Print JSON")) -> None:
    """Show binary, version, auth mode, env hygiene and the last capability test."""
    from aeos.claude_bridge import bridge
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope

    ensure_database()
    with session_scope() as s:
        out = bridge.status(s, refresh=True)
    if as_json:
        typer.echo(json.dumps(out, default=str, indent=2, ensure_ascii=True))
        return
    b, a, h = out["binary"], out["auth"], out["env_hygiene"]
    _line("binary", f"{b['status']} {b['path_logical']} version={b['version'] or 'UNKNOWN'}")
    for r in b.get("reasons") or []:
        _line("  reason", r)
    _line("auth", f"{a['status']} method={a['authMethod']} provider={a['apiProvider']} plan={a['subscriptionType']}")
    for r in a.get("reasons") or []:
        _line("  reason", r)
    au = b.get("authenticity") or {}
    _line("authenticity", f"{au.get('gate', 'UNKNOWN')} signature={au.get('signature_status', 'NOT_CHECKED')} "
                          f"pinned={au.get('approved_signer') or 'NONE'} ({au.get('os', '?')})")
    _line("  reason", str(au.get("reason") or ""))
    _line("bridge", out["bridge_status"])
    _line("env hygiene", f"{h['status']} {', '.join(h['forbidden_present'])}".strip())
    _line("quota", out["quota"]["state"])
    t = out.get("last_capability_test")
    if t:
        _line("last capability test", f"{t['status']} at {t['checked_at']} model={t['model_resolved'] or 'UNKNOWN'}")
        if t.get("stale_reason"):
            _line("  stale", t["stale_reason"])
    else:
        _line("last capability test", "never - run: aeos bridge test")
    for m in out["models"]:
        _line(f"  {m['route_key']}", f"id={m['model_id']} resolved={m['resolved_version']} status={m['status']}")


@app.command("test")
def test_cmd(model: str = typer.Option("opus", help="Supervisor alias to probe: opus or fable")) -> None:
    """Run the capability test: binary, auth, env hygiene and ONE tiny structured call (uses Max quota)."""
    from aeos.claude_bridge import bridge
    from aeos.cli import ensure_database

    if model not in {"opus", "fable"}:
        raise typer.BadParameter("model must be opus or fable")
    ensure_database()
    out = bridge.capability_test(model, actor="cli")
    for step in out["steps"]:
        _line(step["id"], f"{step['status']} {step.get('detail', '')}")
    _line("overall", out["status"])
    _line("model resolved", out.get("model_resolved") or "UNKNOWN")
    _line("evidence", out.get("evidence_id") or "NOT PERSISTED (verification module unavailable)")
    if out["status"] not in {"VERIFIED", "DEGRADED"}:
        raise typer.Exit(1)
