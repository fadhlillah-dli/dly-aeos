"""`aeos docs ...` - Product 1 structure and local document previews (ASCII-only output)."""

from __future__ import annotations

from pathlib import Path

import typer

from aeos.docs_engine import product1

app = typer.Typer(help="Product 1 documentation: structure, local render preview, visuals.", no_args_is_help=True)


_ASCII_MAP = {0x2014: "-", 0x2013: "-", 0x2192: "->", 0x2019: "'", 0x201C: '"', 0x201D: '"'}


def _ascii(text: str) -> str:
    return text.translate(_ASCII_MAP).encode("ascii", "replace").decode("ascii")


def _echo(text: str = "") -> None:
    typer.echo(_ascii(text))


def _session():
    from aeos.core.db import init_engine, session_factory
    from aeos.core.migrate import upgrade_database

    init_engine()
    upgrade_database()
    return session_factory()()


@app.command("structure")
def structure() -> None:
    """Print the PRODUCT 1 tree (folders, documents, sections and content types)."""
    _echo(product1.PRODUCT_TITLE_TEMPLATE.format(name="<Nama Produk>"))
    _echo("[folder] <nama proyek>")

    def walk(nodes: tuple[product1.Node, ...], depth: int) -> None:
        for n in nodes:
            pad = "  " * depth
            if n.kind == product1.NodeKind.FOLDER:
                extra = f" (one {n.collection} per {n.collection_per})" if n.collection else ""
                _echo(f"{pad}[folder] {n.title}{extra}")
                if n.collection in product1.DOC_TYPES:
                    _echo(f"{pad}  [doc per {n.collection_per}] {product1.DOC_TYPES[n.collection].title}")
                    for s in product1.DOC_TYPES[n.collection].sections:
                        _echo(f"{pad}      - {s.title} [{s.content_type.value}]")
            else:
                _echo(f"{pad}[doc] {n.title}")
                for s in product1.DOC_TYPES[n.doc_type or ""].sections:
                    _echo(f"{pad}    - {s.title} [{s.content_type.value}]")
            walk(n.children, depth + 1)

    walk(product1.TREE, 1)
    _echo("  Templates: " + ", ".join(t.title for t in product1.TEMPLATES))


@app.command("render")
def render(
    plan_version_id: str = typer.Argument(..., help="PlanVersion id to render"),
    out: Path = typer.Option(None, "--out", help="Output directory (default: data dir publications/preview)"),
    docx: bool = typer.Option(False, "--docx", help="Also write DOCX snapshots"),
    no_visuals: bool = typer.Option(False, "--no-visuals", help="Skip mermaid-cli rendering"),
) -> None:
    """Render a LOCAL preview (Markdown, optional DOCX). Never publishes anything externally.

    DocumentRecords are updated only when the version is the APPROVED one.
    """
    from aeos.core.config import get_settings
    from aeos.core.enums import Domain
    from aeos.core.errors import AeosError
    from aeos.docs_engine import service
    from aeos.models.planning import PlanVersion
    from aeos.models.portfolio import Project

    settings = get_settings()
    session = _session()
    try:
        version = session.get(PlanVersion, plan_version_id)
        if version is None:
            _echo(f"plan version {plan_version_id} not found")
            raise typer.Exit(2)
        project = session.get(Project, version.project_id)
        assert project is not None
        try:
            result, delta, state = service.render_version(session, settings, version, persist=True,
                                                          render_visuals=not no_visuals)
            if delta is not None:  # DocumentRecords changed (approved version only) -> audited like the API
                from aeos.audit import service as audit
                from aeos.events import service as events

                audit.record(session, actor_type="cli", actor_id="local-operator", action="documents.render",
                             target_type="project", target_id=project.id, project_id=project.id,
                             domain=project.domain, details={"plan_version_id": version.id, **delta.to_dict()})
                events.emit(session, source="docs_engine", kind="documents.rendered", entity_type="project",
                            entity_id=project.id, entity_version=version.id, project_id=project.id,
                            domain=project.domain, payload={k: len(v) for k, v in delta.to_dict().items()})
            session.commit()
        except AeosError as exc:
            session.rollback()
            _echo(f"render failed: {exc.code}: {exc.message}")
            raise typer.Exit(1) from exc
        target = out or (settings.layout.publications(Domain(project.domain), project.project_key) / "preview" /
                         f"v{version.version}-{version.manifest_hash.removeprefix('sha256:')[:12]}")
        info = service.write_preview(session, result, target, docx=docx)
        _echo(f"Product: {result.product_title}")
        _echo("Approved version: " + ("yes" if state.approved else f"NO ({state.reason}) - local preview only"))
        if delta is not None:
            _echo(f"DocumentRecords: created={len(delta.created)} changed={len(delta.changed)} "
                  f"unchanged={len(delta.unchanged)} superseded={len(delta.superseded)}")
        pending = sum(1 for d in result.documents if d.pending_sections)
        _echo(f"Documents: {sum(1 for d in result.documents if not d.is_folder)}  folders: "
              f"{sum(1 for d in result.documents if d.is_folder)}  with documentation pending: {pending}")
        for v in result.visuals:
            _echo(f"Visual {v.key}: {v.status}" + (f" ({v.reason})" if v.reason else ""))
        _echo(f"Preview written to: {info['out_dir']}")
    finally:
        session.close()


@app.command("release-candidate")
def release_candidate(
    plan_version_id: str = typer.Argument(..., help="APPROVED PlanVersion id"),
    release: str = typer.Option(..., "--release", help="Release plan version/key, e.g. 1.0.0"),
    commit: str = typer.Option(..., "--commit", help="Full git SHA of the release candidate"),
) -> None:
    """Record the release-candidate documentation snapshot (commit + approved version + document hashes).

    Records documentation state only: no readiness, deployment or sign-off claim, nothing is published.
    """
    from aeos.core.config import get_settings
    from aeos.core.errors import AeosError
    from aeos.docs_engine import service
    from aeos.models.planning import PlanVersion

    session = _session()
    try:
        version = session.get(PlanVersion, plan_version_id)
        if version is None:
            _echo(f"plan version {plan_version_id} not found")
            raise typer.Exit(2)
        try:
            out = service.record_release_candidate(session, get_settings(), version, release_key=release,
                                                   commit=commit, actor_user_id="local-operator", actor_type="cli")
            session.commit()
        except AeosError as exc:
            session.rollback()
            _echo(f"release candidate refused: {exc.code}: {exc.message}")
            for item in (exc.details or {}).get("pending", []):
                _echo(f"  pending: {item['logical_doc_id']}: {', '.join(item['sections'])}")
            raise typer.Exit(1) from exc
        _echo(f"Release plan: {out['release_plan']}")
        _echo(f"Snapshot: {out['snapshot_hash']} ({out['documents']} documents) at commit {out['commit']}"
              + ("" if out["created"] else " (already recorded)"))
    finally:
        session.close()


@app.command("screenshot")
def screenshot(
    project_id: str = typer.Argument(..., help="Project UUID (its domain is the project's own, never inferred)"),
    environment: str = typer.Option(..., "--environment", help="staging | production (registered deployment target)"),
    commit: str = typer.Option(..., "--commit", help="Git SHA the target must report on its health endpoint"),
    route: list[str] = typer.Option(..., "--route", help="Same-origin path to capture, e.g. / (repeatable)"),
    viewport: str = typer.Option("desktop", "--viewport", help="desktop (1440x900) | mobile (390x844)"),
) -> None:
    """Capture pages of the running app as trusted SCREENSHOT evidence (section 29).

    Observe-only: one health GET proves the commit first, then one headless browser GET per route. A page
    showing a credential pattern or personal data is refused and nothing is stored.
    """
    from aeos.core.errors import AeosError
    from aeos.docs_engine.screenshots import capture_screenshots
    from aeos.models.portfolio import Project

    session = _session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            _echo(f"project {project_id} not found")
            raise typer.Exit(2)
        try:
            out = capture_screenshots(session, project, environment=environment, commit=commit, routes=route,
                                      actor="local-operator", actor_type="cli", viewport=viewport)
            session.commit()
        except AeosError as exc:
            session.rollback()
            from aeos.audit import service as audit

            # a refused capture (gate not READY, commit not proven, sensitive page ...) is audited after rollback
            audit.record(session, actor_type="cli", actor_id="local-operator", action="docs.screenshot.capture",
                         result="DENIED" if exc.status_code in (403, 409) else "FAILED", target_type="project",
                         target_id=project.id, project_id=project.id, domain=project.domain,
                         reason=f"{exc.code}: {exc.message}"[:500], details={"environment": environment})
            session.commit()
            _echo(f"screenshot refused: {exc.code}: {exc.message}")
            remedy = (exc.details or {}).get("remedy")
            if remedy:
                _echo(f"  remedy: {remedy}")
            raise typer.Exit(1) from exc
        for item in out:
            _echo(f"{item['route']}: evidence {item['evidence_id']} ({item['environment']}, commit "
                  f"{item['commit'][:12]}, {item['viewport']})")
    finally:
        session.close()


@app.command("register-pt-report")
def register_pt_report_cmd(
    project_id: str = typer.Argument(..., help="Project UUID (the report's domain is the project's own)"),
    attachment: str = typer.Option(..., "--attachment", help="Attachment id of the uploaded report "
                                   "(upload first: aeos intake upload)"),
    tester: str = typer.Option(..., "--tester", help="Who performed the test (person or vendor)"),
    performed_on: str = typer.Option(..., "--performed-on", help="Test date, YYYY-MM-DD"),
    scope: str = typer.Option(..., "--scope", help="What was tested"),
    method: str = typer.Option("", "--method", help="Method / tools named by the report"),
    remediation: str = typer.Option("", "--remediation", help="Remediation named by the report"),
    environment: str = typer.Option("", "--environment", help="Tested environment, e.g. staging"),
    commit: str = typer.Option(None, "--commit", help="Tested commit SHA, if the report names one"),
) -> None:
    """Register an external penetration-test report as PT_REPORT evidence (section 27 04).

    The report itself is the real result; AEOS records its hash, the redacted intake text and the metadata, and
    states that it did not perform or verify the test.
    """
    from aeos.core.errors import AeosError
    from aeos.docs_engine.pt_reports import register_pt_report
    from aeos.models.portfolio import Project

    session = _session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            _echo(f"project {project_id} not found")
            raise typer.Exit(2)
        try:
            out = register_pt_report(session, project, attachment_id=attachment, tester=tester,
                                     performed_on=performed_on, scope=scope, method=method, remediation=remediation,
                                     environment=environment, commit=commit, actor="local-operator")
            session.commit()
        except AeosError as exc:
            session.rollback()
            _echo(f"register-pt-report refused: {exc.code}: {exc.message}")
            raise typer.Exit(1) from exc
        _echo(f"PT_REPORT {out['evidence_id']} (attachment {out['attachment_id']}, {out['sha256']}, "
              f"{out['provenance']})")
    finally:
        session.close()


@app.command("register-va-report")
def register_va_report_cmd(
    project_id: str = typer.Argument(..., help="Project UUID (the report's domain is the project's own)"),
    attachment: str = typer.Option(..., "--attachment", help="Attachment id of the uploaded report "
                                   "(upload first: aeos intake upload)"),
    assessor: str = typer.Option(..., "--assessor", help="Who ran the assessment (person, vendor or team)"),
    performed_on: str = typer.Option(..., "--performed-on", help="Assessment date, YYYY-MM-DD"),
    scope: str = typer.Option(..., "--scope", help="What was assessed (components, dependencies, hosts)"),
    tools: str = typer.Option(..., "--tools", help="Scanner(s) / method named by the report, e.g. 'npm audit'"),
    findings: str = typer.Option("", "--findings", help="Findings summary named by the report"),
    remediation: str = typer.Option("", "--remediation", help="Remediation named by the report"),
    environment: str = typer.Option("", "--environment", help="Assessed environment, e.g. staging"),
    commit: str = typer.Option(None, "--commit", help="Assessed commit SHA, if the report names one"),
) -> None:
    """Register an external vulnerability-assessment report as VA_REPORT evidence (section 27 04).

    For any project, including a product the platform scanner does not cover. The report is the real result;
    AEOS records its hash, the redacted intake text and the metadata, states that it did not perform or verify
    the assessment, and refuses a report without readable content.
    """
    from aeos.core.errors import AeosError
    from aeos.docs_engine.pt_reports import register_va_report
    from aeos.models.portfolio import Project

    session = _session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            _echo(f"project {project_id} not found")
            raise typer.Exit(2)
        try:
            out = register_va_report(session, project, attachment_id=attachment, assessor=assessor,
                                     performed_on=performed_on, scope=scope, tools=tools, findings=findings,
                                     remediation=remediation, environment=environment, commit=commit,
                                     actor="local-operator")
            session.commit()
        except AeosError as exc:
            session.rollback()
            _echo(f"register-va-report refused: {exc.code}: {exc.message}")
            raise typer.Exit(1) from exc
        _echo(f"VA_REPORT {out['evidence_id']} (attachment {out['attachment_id']}, {out['sha256']}, "
              f"{out['provenance']})")
    finally:
        session.close()


@app.command("checkpoint")
def checkpoint_cmd(
    plan_version_id: str = typer.Argument(..., help="APPROVED PlanVersion id of the project"),
    kind: str = typer.Option(..., "--kind", help="ARCHITECTURE_SNAPSHOT | MILESTONE"),
    ref: str = typer.Option(..., "--ref", help="Approved ArchitectureSnapshot id, or the accepted milestone key"),
) -> None:
    """Record an ACCEPTED documentation checkpoint (sections 28 and 41).

    ARCHITECTURE_SNAPSHOT needs a human APPROVAL bound to the snapshot hash (platform project); MILESTONE needs
    every TaskSpec of the milestone ACCEPTED and finalises its documentation. Records the immutable change
    package and a Tech Writer / Diagram Author TaskSpec DRAFT; nothing runs and nothing is published.
    """
    from aeos.core.config import get_settings
    from aeos.core.errors import AeosError
    from aeos.docs_engine import checkpoints
    from aeos.models.planning import PlanVersion

    session = _session()
    try:
        version = session.get(PlanVersion, plan_version_id)
        if version is None:
            _echo(f"plan version {plan_version_id} not found")
            raise typer.Exit(2)
        try:
            out = checkpoints.record_checkpoint(session, get_settings(), version, kind=kind, ref=ref,
                                                actor_user_id="local-operator", actor_type="cli")
            session.commit()
        except AeosError as exc:
            session.rollback()
            _echo(f"checkpoint refused: {exc.code}: {exc.message}")
            for key in ("tasks", "milestones", "reason"):
                value = (exc.details or {}).get(key)
                if value:
                    _echo(f"  {key}: {value if isinstance(value, str) else ', '.join(map(str, value))}")
            raise typer.Exit(1) from exc
        _echo(f"Checkpoint {out['id']} ({out['kind']} {out['ref']})" + ("" if out["created"] else " (already recorded)"))
        _echo(f"Package: {out['package_hash']}  material: {'yes' if out['material'] else 'no'}  finalised: "
              f"{'yes' if out['finalised'] else 'no'}")
        _echo(f"Changed documents: {len(out['changed_documents'])}  diagrams changed: {len(out['diagram_delta'])}")
        for c in out["changed_documents"][:20]:
            _echo(f"  {c['logical_doc_id']} ({c['change']}): {', '.join(c['sections'][:6]) or '-'}")
        task = out.get("writer_task") or {}
        _echo(f"Writer task draft: {task.get('key')} [{task.get('status')}] - runs only after the owner approves it "
              "into a plan version")
        if out["material"]:
            _echo("Advisory Claude review (material package): Control Center > Dokumen & Publikasi > Checkpoint")
    finally:
        session.close()


@app.command("checkpoints")
def checkpoints_cmd(
    project_id: str = typer.Argument(..., help="Project UUID"),
) -> None:
    """List documentation checkpoints of a project (read-only, sections 28 and 41).

    Shows every milestone of the approved plan version with its ACCEPTED task count, approved architecture
    snapshots (platform project), the OFFERS (fully accepted milestone / approved snapshot without a checkpoint;
    record one with 'aeos docs checkpoint') and the recorded immutable packages. Nothing is recorded here.
    """
    from aeos.docs_engine import checkpoints
    from aeos.models.portfolio import Project

    session = _session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            _echo(f"project {project_id} not found")
            raise typer.Exit(2)
        cand = checkpoints.checkpoint_candidates(session, project)
        version = cand["plan_version"]
        _echo(f"Approved plan version: {version['id']} v{version['version']}" if version
              else "Approved plan version: none (a checkpoint needs the APPROVED plan version)")
        for m in cand["milestones"]:
            state = f"checkpoint {m['checkpoint_id']}" if m["checkpoint_id"] else "no checkpoint"
            _echo(f"  milestone {m['key']}: {m['tasks_accepted']}/{m['tasks_total']} tasks ACCEPTED, {state}")
        for s in cand["snapshots"]:
            state = f"checkpoint {s['checkpoint_id']}" if s["checkpoint_id"] else "no checkpoint"
            _echo(f"  snapshot {s['id']} ({s.get('label') or '-'}): "
                  f"{'APPROVED' if s['approved'] else 'not approved'}, {state}")
        if cand["offers"]:
            _echo("Offers (record with: aeos docs checkpoint <plan_version_id> --kind <KIND> --ref <REF>):")
            for o in cand["offers"]:
                _echo(f"  {o['kind']} {o['ref']} on plan version {o['plan_version_id']}: {o['reason']}")
        else:
            _echo("Offers: none")
        rows = checkpoints.list_checkpoints(session, project.id, with_planning=True)
        _echo(f"Recorded checkpoints: {len(rows)}")
        for c in rows[:20]:
            planned = c.get("writer_task_planned")
            writer = (f"writer task {planned['task_key']} in plan version v{planned['version']}" if planned
                      else "writer task not yet in a plan version")
            _echo(f"  {c['id']} {c['kind']} {c['ref']} material: {'yes' if c['material'] else 'no'} "
                  f"finalised: {'yes' if c['finalised'] else 'no'} - {writer}")
    finally:
        session.close()


@app.command("visuals-status")
def visuals_status() -> None:
    """Report whether the optional mermaid-cli (mmdc) renderer is available."""
    from aeos.docs_engine.visuals import detect_mmdc

    info = detect_mmdc()
    _echo(f"mmdc: {info.status}" + (f" {info.version}" if info.version else ""))
    if info.reason:
        _echo(info.reason)
