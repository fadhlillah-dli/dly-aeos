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


@app.command("visuals-status")
def visuals_status() -> None:
    """Report whether the optional mermaid-cli (mmdc) renderer is available."""
    from aeos.docs_engine.visuals import detect_mmdc

    info = detect_mmdc()
    _echo(f"mmdc: {info.status}" + (f" {info.version}" if info.version else ""))
    if info.reason:
        _echo(info.reason)
