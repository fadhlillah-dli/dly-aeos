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


def test_an_unreadable_manifest_labels_the_pin_unknown_not_unapproved(monkeypatch):
    from aeos.core.errors import ValidationFailed

    def broken(*_a, **_k):
        raise ValidationFailed("install manifest failed validation (1 errors)", code="MANIFEST_INVALID")

    monkeypatch.setattr(manifest, "load_manifest", broken)
    pin = opencode.resolve_pin()
    assert pin["version"] == opencode.PINNED_VERSION and pin["source"] != opencode.PIN_SOURCE_DEFAULT
    assert "MANIFEST_INVALID" in pin["source"] and "UNKNOWN" in pin["source"]


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


def test_the_doctor_reads_the_seeded_pin_normalised_like_the_runtime(settings, db, monkeypatch):
    """The Doctor and the runtime compare the same pin: a seeded ``v1.21.0`` is 1.21.0 for both (the Doctor used to
    compare the raw string and reported drift for an engine that the runtime considered pinned)."""
    from aeos.worker_runtime import facade as worker
    from tests.unit.test_doctor_checks import _ctx, check
    from tests.unit.test_inventory_fakes import base_runner as runner

    db.add(ComponentReference(component_id="opencode", kind="worker_engine", name="OpenCode", friendly_name="OpenCode",
                              function="f", why_selected="w", category="workforce", source_kind="GITHUB_REPO",
                              requirement="REQUIRED", design_label="SELECTED_DESIGN", approved_pin="v1.21.0",
                              admission_status="DISCOVERED", enablement="DISABLED", lifecycle_final="ACTIVE"))
    db.commit()
    monkeypatch.setattr(worker, "opencode_status", lambda configured=None, *, refresh=False: {
        "status": "INSTALLED", "binary": None, "version": "1.21.0", "pinned_version": "1.21.0",
        "pin_source": opencode.PIN_SOURCE_MANIFEST, "drift": False, "functional": "NOT_PROVEN"})
    res = check("worker.opencode", _ctx(settings, db, runner()))
    assert res.status == "VERIFIED", (res.status, res.error_code, res.summary)
    assert res.observed_evidence["pinned_version"] == "1.21.0"
    assert res.observed_evidence["pin_source"] == "install-manifest approved_pin"


def _inspect(monkeypatch, *, rc: int = 0, labels: dict | None = None, image_id: str = "sha256:img") -> None:
    import json

    config = {"User": "10001:10001", "Labels": labels}

    def run_docker(args, *, timeout=60):
        assert args[:2] == ["image", "inspect"]
        out = f"{image_id}|{json.dumps(config)}" if rc == 0 else ""
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="" if rc == 0 else "No such image")

    monkeypatch.setattr(container, "run_docker", run_docker)


def test_the_image_hints_build_the_approved_pin_not_the_dockerfile_default(monkeypatch):
    """`docker build -t dly-aeos-worker:1.20.0 sandbox/` without the build arg installs the Dockerfile default (1.19.3)
    under the 1.20.0 tag: every hint carries the pin as the build arg too."""
    _reviewed_pin(monkeypatch, "1.20.0")
    want = "docker build --build-arg OPENCODE_VERSION=1.20.0 -t dly-aeos-worker:1.20.0 sandbox/"
    assert container.image_build_command() == want
    _inspect(monkeypatch, rc=1)
    st = container.image_status()
    assert st["present"] is False and want in st["evidence"]


def test_an_image_built_with_another_engine_is_never_run_as_the_pin(monkeypatch):
    _reviewed_pin(monkeypatch, "1.20.0")
    _inspect(monkeypatch, labels={"aeos.opencode_version": "1.19.3"})
    st = container.image_status()
    assert st["present"] is False and st["opencode_version"] == "1.19.3" and "not the pin 1.20.0" in st["evidence"]
    _inspect(monkeypatch, labels={"aeos.opencode_version": "1.20.0"})
    assert container.image_status()["present"] is True
    _inspect(monkeypatch, labels=None)  # built before the label existed: accepted, and said so
    st = container.image_status()
    assert st["present"] is True and st["opencode_version"] is None and "no OpenCode version label" in st["evidence"]


def test_the_dockerfile_labels_the_engine_version_it_installs():
    from aeos.core.paths import repo_root

    text = (Path(repo_root()) / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    assert f'LABEL {container.OPENCODE_VERSION_LABEL}="${{OPENCODE_VERSION}}"' in text
    assert f"ARG OPENCODE_VERSION={opencode.PINNED_VERSION}" in text  # the default is the runtime default


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
