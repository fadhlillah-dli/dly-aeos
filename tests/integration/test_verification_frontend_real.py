"""W6-043/W6-044 with the real tools: the verifier's UI driver in headless Chromium and the frontend step (npm ci
--ignore-scripts, vitest with the verifier's config, tsc, vite build, UI states) on the platform's own frontend.

Marked ``integration`` (Chromium/node on the host); the full frontend step is also ``network`` (npm ci fetches the
lockfile's packages). Both skip cleanly when the tool is absent.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from aeos.verification import frontend, runner, seal
from aeos.verification.steps import StepContext

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]

GOOD = ("<!doctype html><html lang='id'><head><title>Contoh</title></head><body><main><h1>Masuk</h1>"
        "<label for='u'>Username</label><input id='u'><button>Masuk</button>"
        "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw=' alt='logo'></main></body></html>")
BAD = ("<!doctype html><html><head></head><body><img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='>"
       "<button></button><input type='text'><div style='width:2000px'>wide</div></body></html>")


def _chromium() -> None:
    if importlib.util.find_spec("playwright") is None:
        pytest.skip("playwright is not installed")
    if not Path(frontend.browsers_path()).is_dir():
        pytest.skip("Playwright browsers are not installed (python -m playwright install chromium)")


def test_ui_driver_renders_states_and_finds_a11y_violations(tmp_path: Path):
    _chromium()
    results = {}
    for name, html in (("good", GOOD), ("bad", BAD)):
        dist = tmp_path / name / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text(html, encoding="utf-8")
        workdir = runner.new_workdir()
        try:
            rn = runner.ProcessRunner(workdir=workdir)
            ui = frontend.run_ui(rn, dist, workdir / "r" / "ui")
            res = frontend.ui_result(ui, workdir / "r" / "ui")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if ui.get("status") == "NOT_RUN" and "chromium" in str(ui.get("reason")):
            pytest.skip(str(ui.get("reason")))
        results[name] = res
    good, bad = results["good"], results["bad"]
    assert good.status == "PASS", good.details
    assert len([e for e in good.evidence if e.kind == "SCREENSHOT"]) == 8  # 4 states x 2 viewports
    assert bad.status == "FAIL" and bad.reasons
    report = bad.evidence[0].content
    assert isinstance(report, dict)
    found = {v["rule"] for v in report["violations"]}
    assert {"html-has-lang", "document-title", "image-alt", "control-name", "form-label", "reflow"} <= found


@pytest.mark.network
def test_frontend_step_on_the_platform_frontend(tmp_path: Path):
    _chromium()
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    snapshot = tmp_path / "s"
    src = REPO / "frontend"
    shutil.copytree(src, snapshot / "frontend", ignore=shutil.ignore_patterns("node_modules", "dist"))
    # the DLY AEOS layout: the empty state is answered by the candidate's own app on an empty database
    shutil.copytree(REPO / "backend", snapshot / "backend", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    workdir = runner.new_workdir()
    checked: dict = {}

    def check(step_seal, res, out_dir=None):  # what the verifier wires (_Verifier._driver_seal)
        checked[step_seal.name] = seal.check_driver(step_seal, res.channel or res.stdout, out_dir=out_dir)
        return checked[step_seal.name].payload

    try:
        seal.install_plugin(workdir)
        rn = runner.ProcessRunner(workdir=workdir)
        ctx = StepContext(runner=rn, snapshot=snapshot, workdir=workdir,
                          changed=["frontend/src/App.tsx"], categories={"frontend": ["frontend/src/App.tsx"]},
                          seal_step=lambda name: seal.StepSeal.new(workdir, name), check_driver_seal=check)
        results = {r.check: r for r in frontend.run(ctx)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    fe = results["frontend_tests"]
    if fe.status == "NOT_PROVEN" and "npm ci" in fe.summary:
        pytest.skip(f"npm ci could not reach the registry here: {fe.summary[:200]}")
    assert fe.status == "PASS", (fe.summary, fe.details)
    assert results["ui_a11y"].status == "PASS", results["ui_a11y"].details
    report = results["ui_a11y"].evidence[0].content
    assert isinstance(report, dict) and report["live_app"] and not report["unproven_states"]
    assert {s["answered_by"] for s in report["states"] if s["state"] == "empty"} == {"candidate-app"}
    # the UI driver was a sealed step: its report and every screenshot digest passed the host-side check
    assert report["sealed_report"] is True and report["screenshots_without_sealed_digest"] == []
    assert checked[frontend.UI_STEP].status == "PASS"
    assert len([e for e in results["ui_a11y"].evidence if e.kind == "SCREENSHOT"]) == 8
    # honest npm ci / vitest / tsc / vite / UI runs leave nothing behind in their kill groups (never QUARANTINED)
    named = {s["step"]: s for s in rn.steps}
    assert {*frontend.STEP_NAMES.values(), frontend.UI_STEP} <= set(named), sorted(named)
    assert rn.survivors() == [], rn.survivors()
