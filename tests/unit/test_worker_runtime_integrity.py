"""§09 Zone 1 "read-only for workers" / "a worker cannot load its changes into the active controller": the
integrity snapshot covers the running controller checkout AND the controller interpreter's site-packages and
scripts (the editable-install finder/.pth that maps ``aeos`` and every installed dependency are loaded on the
next start, outside the checkout). Startup hooks are content-hashed; other files are fingerprinted by
size+mtime. Re-audit 2026-10-19 (K2)."""

from __future__ import annotations

import os
import subprocess
import sys
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


# ----------------------------------------------------------------------------- wave 7: bytecode + owner config
TAG = sys.implementation.cache_tag


def _planted_pyc(source: Path, planted_code: str, target: Path) -> None:
    """A cache file whose header matches *source* (mtime + size) but whose code is *planted_code*: exactly what
    the importer loads INSTEAD of the source (re-audit probe printed 'planted')."""
    from importlib._bootstrap_external import _code_to_timestamp_pyc  # type: ignore[attr-defined]

    st = source.stat()
    code = compile(planted_code, str(source), "exec", dont_inherit=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(_code_to_timestamp_pyc(code, int(st.st_mtime), st.st_size)))


def _import_value(src_dir: Path, module: str, *, prefix: Path | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH", "WINDIR")}
    argv = [sys.executable, "-S"]
    if prefix is not None:
        argv += ["-X", f"pycache_prefix={prefix}"]
    out = subprocess.run([*argv, "-c", f"import sys; sys.path.insert(0, {str(src_dir)!r}); import {module}; "
                          f"print({module}.VALUE)"], capture_output=True, text=True, env=env, check=True, timeout=60)
    return out.stdout.strip()


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    src = checkout / "backend" / "aeosmod.py"
    src.parent.mkdir(parents=True)
    src.write_text("VALUE = 'source'\n", encoding="utf-8")
    return checkout, src


def test_bytecode_names_the_importer_reads():
    assert integrity.bytecode_name_ok(f"x.{TAG}.pyc") and integrity.bytecode_name_ok(f"x.{TAG}.opt-1.pyc")
    assert not integrity.bytecode_name_ok("x.cpython-27.pyc")  # another interpreter's tag is never read
    assert not integrity.bytecode_name_ok(f"x.{TAG}-pytest-9.1.1.pyc")  # pytest's rewrite cache
    assert not integrity.bytecode_name_ok(f"x.{TAG}.pyc.12345")  # importer temp file
    assert not integrity.bytecode_name_ok("x.py")


def test_planted_header_matching_pyc_in_the_checkout_is_detected_and_removed(tmp_path):
    """W6-033 (1): a header-matching .pyc in a controller __pycache__ is imported instead of its source; after the
    run it is a finding (its code differs from a fresh compile of the source) and the cache file is deleted."""
    checkout, src = _checkout(tmp_path)
    before = integrity.snapshot(root=checkout, interpreter=False)
    pyc = src.parent / "__pycache__" / f"aeosmod.{TAG}.pyc"
    _planted_pyc(src, "VALUE = 'planted'\n", pyc)
    assert _import_value(src.parent, "aeosmod") == "planted"  # the attack works without the check
    changed = integrity.compare(before)
    key = f"backend/__pycache__/aeosmod.{TAG}.pyc"
    assert [c["path"] for c in changed] == [key], changed
    assert "MISMATCH" in changed[0]["detail"] and "removed" in changed[0]["detail"]
    assert not pyc.exists()
    assert _import_value(src.parent, "aeosmod") == "source"  # neutralised: recompiled from the source


def test_lazily_compiled_bytecode_of_the_unchanged_source_is_accepted(tmp_path):
    """The controller compiles lazily during a run: a cache file equal to a fresh compile is not a finding."""
    import py_compile

    checkout, src = _checkout(tmp_path)
    before = integrity.snapshot(root=checkout, interpreter=False)
    py_compile.compile(str(src), cfile=str(src.parent / "__pycache__" / f"aeosmod.{TAG}.pyc"), doraise=True)
    assert _import_value(src.parent, "aeosmod") == "source"  # and a real import's own cache file
    assert integrity.compare(before) == []
    assert (src.parent / "__pycache__" / f"aeosmod.{TAG}.pyc").exists()  # an accepted cache file is kept


def test_rewritten_existing_pyc_is_detected(tmp_path):
    import py_compile

    checkout, src = _checkout(tmp_path)
    pyc = src.parent / "__pycache__" / f"aeosmod.{TAG}.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    before = integrity.snapshot(root=checkout, interpreter=False)
    assert f"backend/__pycache__/aeosmod.{TAG}.pyc" in before["files"]  # bytecode is part of the snapshot
    _planted_pyc(src, "VALUE = 'planted'\n", pyc)
    changed = integrity.compare(before)
    assert [(c["path"], c["change"]) for c in changed] == [(f"backend/__pycache__/aeosmod.{TAG}.pyc", "modified")]


def test_planted_pyc_in_the_site_packages_cache_is_detected(tmp_path, monkeypatch):
    sp = _site(tmp_path)
    monkeypatch.setattr(integrity, "interpreter_roots", lambda: [("site-packages", sp)])
    checkout, _src = _checkout(tmp_path)
    before = integrity.snapshot(root=checkout, interpreter=True)
    _planted_pyc(sp / "dep" / "mod.py", "X = 'planted'\n", sp / "dep" / "__pycache__" / f"mod.{TAG}.pyc")
    changed = integrity.compare(before)
    assert [c["path"] for c in changed] == [f"interpreter:site-packages/dep/__pycache__/mod.{TAG}.pyc"], changed
    assert not (sp / "dep" / "__pycache__" / f"mod.{TAG}.pyc").exists()


def test_planted_pyc_in_the_release_pycache_prefix_tree_is_detected(tmp_path, monkeypatch):
    """W6-033 (2): release mode reads ALL bytecode from PYTHONPYCACHEPREFIX under the data dir."""
    import importlib.util

    checkout, src = _checkout(tmp_path)
    prefix = tmp_path / "data" / "cache" / "release-pycache" / "r1"
    prefix.mkdir(parents=True)
    monkeypatch.setattr(integrity, "pycache_prefix_root", lambda: prefix)
    before = integrity.snapshot(root=checkout, interpreter=False, pycache_prefix=prefix)
    assert before["pycache_prefix"] == str(prefix)
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    target = Path(importlib.util.cache_from_source(str(src)))
    monkeypatch.setattr(sys, "pycache_prefix", None)
    assert target.is_relative_to(prefix)
    _planted_pyc(src, "VALUE = 'planted'\n", target)
    assert _import_value(src.parent, "aeosmod", prefix=prefix) == "planted"
    changed = integrity.compare(before)
    assert len(changed) == 1 and changed[0]["path"].startswith("pycache-prefix:"), changed
    assert "MISMATCH" in changed[0]["detail"] and not target.exists()
    assert _import_value(src.parent, "aeosmod", prefix=prefix) == "source"


def test_owner_config_change_is_detected_and_restored(tmp_path):
    """W6-033 (3): aeos.yaml (opencode_binary/claude_binary ...) is read on the next start."""
    checkout, _src = _checkout(tmp_path)
    cfg = tmp_path / "config" / "aeos.yaml"
    cfg.parent.mkdir()
    cfg.write_text("port: 8787\n", encoding="utf-8")
    before = integrity.snapshot(root=checkout, interpreter=False, config_path=cfg)
    cfg.write_text("port: 8787\nopencode_binary: C:/evil/opencode.exe\n", encoding="utf-8")
    changed = integrity.compare(before)
    assert [(c["path"], c["change"]) for c in changed] == [("config:aeos.yaml", "modified")]
    assert "restored to the pre-run content" in changed[0]["detail"]
    assert cfg.read_text(encoding="utf-8") == "port: 8787\n"


def test_owner_config_created_during_a_run_is_removed(tmp_path):
    checkout, _src = _checkout(tmp_path)
    cfg = tmp_path / "config" / "aeos.yaml"
    before = integrity.snapshot(root=checkout, interpreter=False, config_path=cfg)
    assert before["files"]["config:aeos.yaml"] == "absent"
    cfg.parent.mkdir()
    cfg.write_text("claude_binary: C:/evil/claude.exe\n", encoding="utf-8")
    changed = integrity.compare(before)
    assert [(c["path"], c["change"]) for c in changed] == [("config:aeos.yaml", "added")]
    assert not cfg.exists()


def test_real_controller_snapshot_covers_bytecode_and_the_config_file(settings):
    snap = integrity.snapshot()
    assert snap["config_path"] and any(k.startswith("config:") for k in snap["files"])
    assert any("/__pycache__/" in k for k in snap["files"]), "controller bytecode caches are fingerprinted"


def test_unreadable_planted_bytecode_never_verifies(tmp_path):
    """The verifier subprocess fails closed on a malformed file (marshal is not safe on crafted input: it runs
    out of the controller process)."""
    import importlib.util

    checkout, src = _checkout(tmp_path)
    pyc = src.parent / "__pycache__" / f"aeosmod.{TAG}.pyc"
    pyc.parent.mkdir()
    pyc.write_bytes(importlib.util.MAGIC_NUMBER + b"\x00" * 12 + b"\xff\xfe garbage")
    res = integrity.verify_bytecode([("k", pyc, src)])
    assert res["k"] not in integrity.ACCEPTED_VERDICTS
    assert integrity.verify_bytecode([("k", pyc, src)], timeout=0.001)["k"] == "VERIFIER_FAILED"


def test_a_capped_snapshot_is_never_an_untouched_controller(tmp_path, monkeypatch):
    """Review wave 7 (fail closed): when the file cap is reached only part of the controller was fingerprinted, so
    a change beyond the cap would go unseen - the comparison reports that instead of an empty (= untouched) list."""
    checkout, src = _checkout(tmp_path)
    for i in range(5):
        (src.parent / f"extra{i}.py").write_text(f"X = {i}\n", encoding="utf-8")
    monkeypatch.setattr(integrity, "MAX_FILES", 3)
    before = integrity.snapshot(root=checkout, patterns=("backend/**",), interpreter=False)
    assert before["truncated"] is True
    changed = integrity.compare(before)
    assert [(c["path"], c["change"]) for c in changed] == [("*", "incomplete")], changed
    monkeypatch.setattr(integrity, "MAX_FILES", 1000)
    assert integrity.compare(integrity.snapshot(root=checkout, patterns=("backend/**",), interpreter=False)) == []
