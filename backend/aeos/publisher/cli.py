"""`aeos publish ...` - connect publication accounts and inspect/run publications (ASCII-only output).

Approval of a publication is a human action in the dashboard ("Approve & Publish" binds the exact plan
version, approved hash and preview hash). The CLI never approves; `run` only processes publications
that are already RUNNING (approved).
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="Publication: Google Drive/Docs + GitHub Issues (PERSONAL), outbox status.",
                  no_args_is_help=True)


_ASCII_MAP = {0x2014: "-", 0x2013: "-", 0x2192: "->", 0x2019: "'", 0x201C: '"', 0x201D: '"'}


def _echo(text: str = "") -> None:
    typer.echo(text.translate(_ASCII_MAP).encode("ascii", "replace").decode("ascii"))


def _session():
    from aeos.core.db import init_engine, session_factory
    from aeos.core.migrate import upgrade_database

    init_engine()
    upgrade_database()
    return session_factory()()


@app.command("google-connect")
def google_connect(
    client_secret: Path = typer.Option(..., "--client-secret", exists=True, dir_okay=False, readable=True,
                                       help="OAuth client JSON of type 'Desktop app' from Google Cloud Console"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the consent URL instead of opening a browser"),
) -> None:
    """Run Google consent on 127.0.0.1 (scope drive.file only) and store client + token in the secret store."""
    from aeos.audit import service as audit
    from aeos.core.errors import AeosError
    from aeos.publisher.providers import google

    _echo("Requesting ONLY scope: " + google.DRIVE_FILE_SCOPE)
    _echo("A browser window opens for consent; the redirect goes to 127.0.0.1 on a free local port.")
    try:
        summary = google.connect(client_secret, actor="cli", open_browser=not no_browser)
    except AeosError as exc:
        _echo(f"google-connect failed: {exc.code}: {exc.message}")
        if exc.code == "SECRET_STORE_UNAVAILABLE":
            _echo("Remedy: configure the secret store first (see `aeos doctor`, OpenBao / OS keyring).")
        raise typer.Exit(1) from exc
    session = _session()
    try:
        audit.record(session, actor_type="cli", actor_id="local-operator", action="publisher.google.connect",
                     target_type="secret", target_id=google.TOKEN_REF,
                     details={"granted_scopes": summary["granted_scopes"]})
        session.commit()
    finally:
        session.close()
    _echo(f"Stored client config at {summary['client_ref']} and token at {summary['token_ref']}.")
    _echo("Granted scopes: " + ", ".join(summary["granted_scopes"]))
    if not summary["least_privilege"]:
        _echo("WARNING: granted scopes differ from drive.file only; review the OAuth client configuration.")
    _echo("Status: CONFIGURED (not yet VERIFIED - the first publication call is the proof).")


@app.command("google-status")
def google_status() -> None:
    """Show whether the Google client/token exist and which scopes the token carries (no secret values)."""
    from aeos.publisher.providers import google

    st = google.google_status()
    _echo(f"status: {st.get('status')}")
    for k in ("client", "token"):
        v = st.get(k)
        _echo(f"{k}: " + ("missing" if not v or not v.get("exists") else f"stored ({v.get('backend')}, "
                                                                          f"updated {v.get('updated_at')})"))
    if st.get("granted_scopes") is not None:
        _echo("granted scopes: " + (", ".join(st["granted_scopes"]) or "(none recorded)"))
    if st.get("reason"):
        _echo("reason: " + st["reason"])
    if st.get("status") != "CONFIGURED":
        _echo("remedy: " + google.CONNECT_REMEDY)


@app.command("github-connect")
def github_connect() -> None:
    """Explain how to store the GitHub token used for issues (the token never goes into config files)."""
    from aeos.core.errors import Blocked
    from aeos.publisher.providers import github_issues
    from aeos.secrets import facade as secrets

    _echo("GitHub Issues uses the secret reference personal/github.")
    _echo("Least privilege (recommended): a fine-grained PAT for ONLY the project repository with Issues RW,")
    _echo("Pull requests RW, Contents R, Metadata R, stored at a hidden prompt:")
    _echo("  aeos secrets set personal/github")
    _echo("Alternative (classic OAuth token, usually repo/workflow over ALL repositories - reported as a")
    _echo("residual risk by `aeos doctor --functional`):")
    _echo("  " + github_issues.CONNECT_REMEDY)
    try:
        st = secrets.secret_status(github_issues.TOKEN_REF)
        _echo("current status: " + ("CONFIGURED (stored, not yet verified)" if st.exists else "MISSING"))
    except Blocked as exc:
        _echo(f"current status: BLOCKED ({exc.message})")
    _echo("GitHub Projects (roadmap view) is optional and NOT implemented by this adapter.")


@app.command("idempotency-test")
def idempotency_test() -> None:
    """Prove duplicate-publication prevention (timeout / partial / replay) at this commit with loopback fakes.

    Runs the publisher's own reconciliation tests (no real tenant, no credentials) and records a trusted
    PUBLICATION_IDEMPOTENCY_TEST evidence only when all pass (read by Doctor recovery.publication_idempotency).
    """
    from aeos.core.errors import AeosError
    from aeos.publisher import selftest

    session = _session()
    try:
        try:
            out = selftest.run_idempotency_selftest(session, actor="local-operator")
            session.commit()
        except AeosError as exc:
            session.rollback()
            _echo(f"idempotency test unavailable: {exc.code}: {exc.message}")
            raise typer.Exit(2) from exc
        _echo(f"status: {out['status']} (commit {out['commit'] or 'UNKNOWN'})")
        for line in (out.get("summary") or "").splitlines()[-5:]:
            _echo("  " + line)
        if out["evidence_id"]:
            _echo(f"evidence: {out['evidence_id']} (kind {selftest.EVIDENCE_KIND}); run `aeos doctor` to refresh")
        else:
            _echo("no evidence recorded: " + out.get("reason", "the tests did not pass"))
            raise typer.Exit(1)
    finally:
        session.close()


@app.command("list")
def list_cmd(project_id: str = typer.Option(None, "--project", help="Filter by project id")) -> None:
    """List publications with status and operation counts."""
    from sqlalchemy import select

    from aeos.models.publishing import Publication
    from aeos.publisher import jobs

    session = _session()
    try:
        q = select(Publication).order_by(Publication.created_at.desc()).limit(50)
        if project_id:
            q = q.where(Publication.project_id == project_id)
        for pub in session.scalars(q):
            counts = jobs._counts(jobs._ops_for(session, pub.id))
            _echo(f"{pub.id}  {pub.status:<9} {pub.domain:<8} plan v{pub.plan_version_number}  "
                  + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    finally:
        session.close()


@app.command("show")
def show(publication_id: str) -> None:
    """Show the operations of one publication (status, attempts, external ids, errors)."""
    from aeos.models.publishing import Publication
    from aeos.publisher import jobs

    session = _session()
    try:
        pub = session.get(Publication, publication_id)
        if pub is None:
            _echo("publication not found")
            raise typer.Exit(2)
        _echo(f"publication {pub.id} status={pub.status} plan_version={pub.plan_version_id} "
              f"approved_hash={pub.approved_hash}")
        if pub.status_reason:
            _echo("reason: " + pub.status_reason)
        for op in jobs._ops_for(session, pub.id):
            _echo(f"  #{op.order_index:<3} {op.status:<9} {op.provider}/{op.operation} {op.logical_id}"
                  + (f" -> {op.external_id}" if op.external_id else "") + (f"  [{op.error[:120]}]" if op.error else ""))
    finally:
        session.close()


@app.command("run")
def run(publication_id: str) -> None:
    """Process due operations of an already approved (RUNNING) publication now."""
    from aeos.models.publishing import Publication
    from aeos.publisher import jobs

    session = _session()
    try:
        pub = session.get(Publication, publication_id)
        if pub is None:
            _echo("publication not found")
            raise typer.Exit(2)
        if pub.status != jobs.RUNNING:
            _echo(f"publication is {pub.status}; approve it in the dashboard (Approve & Publish) or use retry there.")
            raise typer.Exit(1)
        pub = jobs.process_publication(session, publication_id)
        _echo(f"status: {pub.status} " + " ".join(f"{k}={v}" for k, v in sorted(
            jobs._counts(jobs._ops_for(session, pub.id)).items())))
    finally:
        session.close()


@app.command("reconcile")
def reconcile(publication_id: str) -> None:
    """Read-only reconciliation of UNKNOWN/FAILED operations (find earlier side effects by marker)."""
    from aeos.core.errors import AeosError
    from aeos.publisher import jobs

    session = _session()
    try:
        try:
            result = jobs.reconcile_publication(session, publication_id, actor_user_id="cli")
            session.commit()
        except AeosError as exc:
            session.rollback()
            _echo(f"reconcile failed: {exc.code}: {exc.message}")
            raise typer.Exit(1) from exc
        _echo(" ".join(f"{k}={v}" for k, v in result.items()))
    finally:
        session.close()
