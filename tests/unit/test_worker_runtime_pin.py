"""One OpenCode pin for the whole runtime (master §07 "tool selection and versions are an InstallManifest that has
been reviewed"; audit W6-166).

Before: the doctor read the reviewed manifest's approved_pin, but the container image tag, the runtime drift flag,
the contract test's pinned_version and the image build hint all used the code constant 1.19.3 - an approved
1.20.0 would still build and run 1.19.3. Now every one of them goes through ``opencode.resolve_pin``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aeos.inventory import manifest
from aeos.models.inventory import ComponentReference
from aeos.worker_runtime import container, contract, opencode, sandbox


def _reviewed_pin(monkeypatch, pin: str | None) -> None:
    """The reviewed InstallManifest as the runtime reads it (component opencode, approved_pin)."""
    data = {"components": [{"component_id": "opencode", "approved_pin": pin}]}
    monkeypatch.setattr(manifest, "load_manifest", lambda *a, **k: SimpleNamespace(data=data))


def _installed(monkeypatch, version: str) -> None:
    monkeypatch.setattr(opencode, "resolve_binary", lambda configured=None: Path("/opt/fake/opencode"))

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=f"{version}\n", stderr="")

    monkeypatch.setattr(opencode.subprocess, "run", run)


def test_an_approved_pin_is_the_image_tag_and_the_drift_baseline(monkeypatch):
    _reviewed_pin(monkeypatch, "1.20.0")
    assert opencode.resolve_pin() == {"version": "1.20.0", "source": opencode.PIN_SOURCE_MANIFEST}
    assert container.image_tag() == "dly-aeos-worker:1.20.0"
    _installed(monkeypatch, "1.20.0")
    st = opencode.opencode_status("x", refresh=True)
    assert st["status"] == "INSTALLED" and st["pinned_version"] == "1.20.0" and st["drift"] is False
    assert st["pin_source"] == opencode.PIN_SOURCE_MANIFEST
    _installed(monkeypatch, opencode.PINNED_VERSION)  # the old code default is now drift from the approved pin
    assert opencode.opencode_status("x", refresh=True)["drift"] is True


def test_without_an_approved_pin_the_labelled_default_applies(monkeypatch):
    _reviewed_pin(monkeypatch, None)
    pin = opencode.resolve_pin()
    assert pin["version"] == opencode.PINNED_VERSION and "default" in pin["source"]
    assert container.image_tag() == f"dly-aeos-worker:{opencode.PINNED_VERSION}"
    _installed(monkeypatch, "1.20.0")
    st = opencode.opencode_status("x", refresh=True)
    assert st["drift"] is True and st["pin_source"] == opencode.PIN_SOURCE_DEFAULT


def test_a_changed_pin_is_not_served_from_the_status_cache(monkeypatch):
    _installed(monkeypatch, "1.20.0")
    _reviewed_pin(monkeypatch, None)
    assert opencode.opencode_status("x", refresh=True)["drift"] is True
    _reviewed_pin(monkeypatch, "1.20.0")
    assert opencode.opencode_status("x")["drift"] is False  # same binary, new reviewed pin: re-evaluated


def test_the_seeded_component_reference_is_used_with_a_session(db, monkeypatch):
    _reviewed_pin(monkeypatch, None)
    assert opencode.resolve_pin(db)["version"] == opencode.PINNED_VERSION  # not seeded: default
    db.add(ComponentReference(component_id="opencode", kind="worker_engine", name="OpenCode", friendly_name="OpenCode",
                              function="f", why_selected="w", category="workforce", source_kind="GITHUB_REPO",
                              requirement="REQUIRED", design_label="SELECTED_DESIGN", approved_pin="v1.21.0",
                              admission_status="DISCOVERED", enablement="DISABLED", lifecycle_final="ACTIVE"))
    db.flush()
    assert opencode.resolve_pin(db) == {"version": "1.21.0", "source": opencode.PIN_SOURCE_MANIFEST}


def test_build_hint_and_contract_report_name_the_approved_pin(monkeypatch, tmp_path):
    _reviewed_pin(monkeypatch, "1.20.0")
    hint = sandbox._container_remedy("PERSONAL")
    assert "--build-arg OPENCODE_VERSION=1.20.0" in hint and "-t dly-aeos-worker:1.20.0" in hint
    rep = contract.run_contract_test(binary=str(tmp_path / "no-opencode-here"))
    assert rep["overall"] == "NOT_RUN" and rep["pinned_version"] == "1.20.0"
    assert rep["pin_source"] == opencode.PIN_SOURCE_MANIFEST


def test_the_bootstrap_status_script_reads_the_reviewed_pin(tmp_path):
    import importlib.util

    from aeos.core.paths import repo_root

    spec = importlib.util.spec_from_file_location("aeos_cs_pin", Path(repo_root()) / "scripts" / "component_status.py")
    assert spec is not None and spec.loader is not None
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    assert cs.OPENCODE_DEFAULT_PIN == opencode.PINNED_VERSION  # one default, no second constant drifting apart
    (tmp_path / "install-manifest").mkdir()
    path = tmp_path / "install-manifest" / "manifest.yaml"
    path.write_text("components:\n  - component_id: opencode\n    approved_pin: \"1.20.0\"\n"
                    "  - component_id: goose\n    approved_pin: 9.9.9\n", encoding="utf-8")
    assert cs.manifest_pin("opencode", tmp_path) == "1.20.0"
    path.write_text("components:\n  - component_id: opencode\n    approved_pin: null\n"
                    "  - component_id: goose\n    approved_pin: 9.9.9\n", encoding="utf-8")
    assert cs.manifest_pin("opencode", tmp_path) is None
    assert cs.manifest_pin("opencode") is None and cs.OPENCODE_PIN == opencode.PINNED_VERSION  # repo: not pinned


@pytest.fixture(autouse=True)
def _fresh_status_cache():
    opencode._status_cache.update(at=0.0, value=None)
    yield
    opencode._status_cache.update(at=0.0, value=None)
