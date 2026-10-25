"""§09 Zone 1 "read-only for workers" / "a worker cannot load its changes into the active controller": the
integrity snapshot covers the running controller checkout AND the controller interpreter's site-packages and
scripts (the editable-install finder/.pth that maps ``aeos`` and every installed dependency are loaded on the
next start, outside the checkout). Startup hooks are content-hashed; other files are fingerprinted by
size+mtime. Re-audit 2026-10-19 (K2)."""

from __future__ import annotations

from pathlib import Path

from aeos.worker_runtime import integrity


def _site(tmp_path: Path) -> Path:
    sp = tmp_path / "site-packages"
    (sp / "dep").mkdir(parents=True)
    (sp / "dep" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (sp / "a1_coverage.pth").write_text("import coverage\n", encoding="utf-8")
    (sp / "__editable__.dly_aeos-0.1.0.pth").write_text("import __editable___dly_aeos\n", encoding="utf-8")
    (sp / "sitecustomize.py").write_text("# startup\n", encoding="utf-8")
    (sp / "dly_aeos-0.1.0.dist-info").mkdir()
    (sp / "dly_aeos-0.1.0.dist-info" / "RECORD").write_text("aeos/__init__.py,,\n", encoding="utf-8")
    (sp / "requests-2.0.dist-info").mkdir()
    (sp / "requests-2.0.dist-info" / "RECORD").write_text("requests/__init__.py,,\n", encoding="utf-8")
    return sp


def test_startup_hooks_are_content_hashed_and_other_files_fingerprinted(tmp_path):
    files: dict[str, str] = {}
    assert integrity._interpreter_files("site-packages", _site(tmp_path), files)
    # startup-critical: pth, editable finder, sitecustomize and the PLATFORM dist-info are sha256 content hashes
    for rel in ("a1_coverage.pth", "__editable__.dly_aeos-0.1.0.pth", "sitecustomize.py",
                "dly_aeos-0.1.0.dist-info/RECORD"):
        key = f"interpreter:site-packages/{rel}"
        assert key in files and len(files[key]) == 64 and all(c in "0123456789abcdef" for c in files[key]), rel
    # an ordinary dependency file (and a non-platform dist-info) is size+mtime, not a content hash
    assert files["interpreter:site-packages/dep/mod.py"].startswith("stat:")
    assert files["interpreter:site-packages/requests-2.0.dist-info/RECORD"].startswith("stat:")


def test_startup_critical_classification():
    assert integrity._startup_critical("foo.pth")
    assert integrity._startup_critical("__editable__.dly_aeos-0.1.0.pth")
    assert integrity._startup_critical("sitecustomize.py")
    assert integrity._startup_critical("dly_aeos-0.1.0.dist-info/RECORD")
    assert integrity._startup_critical("aeos.egg-link")
    assert not integrity._startup_critical("dep/mod.py")
    assert not integrity._startup_critical("requests-2.0.dist-info/RECORD")
    assert not integrity._startup_critical("pth_tools/helper.py")  # .pth only at the top level


def test_editable_pth_write_is_detected(tmp_path, monkeypatch):
    """A worker writing the controller's editable-install finder (how it would load its own ``aeos`` on the
    next start) is detected by compare()."""
    sp = _site(tmp_path)
    monkeypatch.setattr(integrity, "interpreter_roots", lambda: [("site-packages", sp)])
    checkout = tmp_path / "checkout"
    (checkout / "backend").mkdir(parents=True)
    (checkout / "backend" / "x.py").write_text("X = 1\n", encoding="utf-8")
    before = integrity.snapshot(root=checkout, interpreter=True)
    assert before["interpreter"] is True
    assert any(k.startswith("interpreter:site-packages/") for k in before["files"])
    assert integrity.compare(before) == []
    # the worker rewrites the editable finder to map aeos to its own candidate tree
    (sp / "__editable__.dly_aeos-0.1.0.pth").write_text("import evil\n", encoding="utf-8")
    changed = integrity.compare(before)
    assert [c for c in changed if c["path"].endswith("__editable__.dly_aeos-0.1.0.pth")
            and c["change"] == "modified"], changed


def test_real_controller_snapshot_includes_the_interpreter(settings):
    """The default snapshot of the running controller includes the interpreter site-packages (the editable
    finder that maps ``aeos``), not only the checkout files (re-audit probe: 0 files under the venv)."""
    snap = integrity.snapshot()
    assert snap["interpreter"] is True and snap["interpreter_roots"]
    interp = [k for k in snap["files"] if k.startswith("interpreter:")]
    assert interp, "interpreter files are fingerprinted"
    editable = [k for k in interp if "__editable__" in k or k.endswith(".pth")]
    assert editable, "the editable-install finder / .pth startup hooks are covered"
