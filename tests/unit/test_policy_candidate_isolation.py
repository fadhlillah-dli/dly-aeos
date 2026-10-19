"""§09 "Hot reload tidak pernah memuat policy dari source kandidat ke controller tepercaya".

A candidate workspace may contain an edited ``policy/office-defaults.yaml`` that opens OFFICE. The controller
reads policy only from its own checkout (``repo_root()/policy``) and the DB snapshot, whatever the current
directory is, and ``aeos serve --reload`` watches only the controller's backend code.
"""

from __future__ import annotations

from pathlib import Path

from aeos.core.paths import repo_root
from aeos.policy import office

OPEN_OFFICE = "office:\n  external_worker_allowed: true\n  approved_worker_routes: ['openrouter/x']\n" \
              "  claude_max_allowed_for_office_data: true\n"


def _candidate(tmp_path: Path) -> Path:
    cand = tmp_path / "task-data" / "candidate"
    (cand / "policy").mkdir(parents=True)
    (cand / "policy" / "office-defaults.yaml").write_text(OPEN_OFFICE, encoding="utf-8")
    (cand / "backend" / "aeos" / "policy").mkdir(parents=True)
    return cand


def test_candidate_policy_file_is_never_read_by_the_controller(db, tmp_path, monkeypatch):
    cand = _candidate(tmp_path)
    monkeypatch.chdir(cand)  # even when the controller process runs from inside a candidate tree
    defaults, source = office.load_defaults()
    assert office.policy_dir() == repo_root() / "policy" and office.policy_dir() != cand / "policy"
    assert defaults["external_worker_allowed"] is False and defaults["approved_worker_routes"] == []
    assert source == "policy/office-defaults.yaml"
    pol = office.office_policy(db)
    assert pol["external_worker_allowed"] is False and pol["claude_max_allowed_for_office_data"] is False


def test_serve_reload_watches_only_the_controller_backend(monkeypatch, settings):
    import uvicorn
    from typer.testing import CliRunner

    from aeos import cli

    seen: dict = {}
    # a development server runs only on a separate dev profile (never the real keyring service, §09)
    monkeypatch.setenv("AEOS_KEYRING_SERVICE", "dly-aeos-dev")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(cli, "ensure_database", lambda: None)
    res = CliRunner().invoke(cli.app, ["serve", "--reload", "--port", "18999"])
    assert res.exit_code == 0, res.output
    assert seen["reload"] is True and seen["reload_dirs"] == [str(repo_root() / "backend")]
    assert seen["host"] == "127.0.0.1"
    seen.clear()
    assert CliRunner().invoke(cli.app, ["serve", "--port", "18999"]).exit_code == 0
    assert "reload_dirs" not in seen and seen["reload"] is False
