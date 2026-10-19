"""`aeos intake upload` (master §01 U-11 "every intake surface supports file upload, including .md")."""

from __future__ import annotations

from sqlalchemy import select
from typer.testing import CliRunner

from aeos.cli import app
from aeos.models.portfolio import Attachment, Project
from tests.conftest import create_admin

runner = CliRunner()


def test_cli_uploads_a_markdown_file_through_the_intake_pipeline(settings, db, tmp_path):
    create_admin(db)
    p = Project(project_key="cli-intake", name="CLI Intake", domain="PERSONAL")
    db.add(p)
    db.commit()
    md = tmp_path / "ide.md"
    md.write_text("# Ide\nEkspor CSV untuk akuntan.\n", encoding="utf-8")
    r = runner.invoke(app, ["intake", "upload", "cli-intake", str(md), "--domain", "PERSONAL", "--actor", "owner"])
    assert r.exit_code == 0, r.output
    db.expire_all()
    att = db.scalar(select(Attachment).where(Attachment.project_id == p.id))
    assert att is not None and att.filename == "ide.md" and att.id in r.output
    assert "aeos plan create" in r.output
    # domain is explicit and must match; executables are refused by the same pipeline as the dashboard
    r = runner.invoke(app, ["intake", "upload", "cli-intake", str(md), "--domain", "OFFICE", "--actor", "owner"])
    assert r.exit_code != 0
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ\x90\x00")
    r = runner.invoke(app, ["intake", "upload", "cli-intake", str(exe), "--domain", "PERSONAL", "--actor", "owner"])
    assert r.exit_code != 0 and "ERROR" in r.output
    r = runner.invoke(app, ["intake", "upload", "cli-intake", str(md), "--domain", "PERSONAL", "--actor", "nobody"])
    assert r.exit_code != 0
