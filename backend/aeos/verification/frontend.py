"""Frontend verification step (§16 Frontend Engineer "Browser test, visual evidence, a11y", UX/UI Designer "state/
loading/error, aksesibilitas"; §31 G3 "Browser/UI/a11y"; §09 Zone 2; W6-044, W6-043, W6-031).

For a change with ``frontend`` impact the verifier itself, through its runner and inside its tamper window:

1. materialises the candidate's frontend package (the ``package.json`` + ``package-lock.json`` directory owning the
   changed UI files) into ``<run dir>/fe`` - without ``node_modules``, build output or project npm/yarn config
   files (registry/auth redirection);
2. installs it from the candidate lockfile: ``npm ci --ignore-scripts`` (no install scripts run; a separate
   install step with package-registry egress, not enforced on the process runner; stated);
3. runs vitest with a **verifier-owned config** (test selection, reporter and result file are the verifier's; the
   candidate's vite plugins, environment and setup files are reused, lower trust) and records each test result;
4. type-checks (``tsc -b --noEmit``) and builds (``vite build``) when the lockfile provides those tools;
5. renders the built bundle per UI state (loading / error / unauthenticated / empty) at desktop and mobile width in
   headless Chromium, stores a screenshot per state and runs the verifier's accessibility probe
   (``packs/ui_driver.py``; a WCAG subset, not axe-core).

``frontend_tests`` is PASS only when vitest ran tests and every executed frontend step passed; FAIL when a test,
the type check, the build or the lockfile itself failed; otherwise NOT_PROVEN with the reason (no node/npm on this
host, container runner without a node runtime, install did not complete, no tests) - visible and non-blocking (G3
provider CI remains the product-test gate), never a pass. ``ui_a11y`` is PASS/FAIL from the probe, NOT_RUN when no
browser or bundle exists. Evidence is attached to the report, so the A11Y review package can cite it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from aeos.verification import runner as runner_mod
from aeos.verification.procs import ProcResult, run_process
from aeos.verification.steps import StepContext, StepEvidence, StepResult, last_json_line, pack_text

INSTALL_TIMEOUT = 900.0
STEP_TIMEOUT = 900.0
UI_TIMEOUT = 600.0
EXCLUDED_NAMES = frozenset({"node_modules", "dist", "build", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs",
                            ".git", "coverage"})
TRUSTED_CONFIG_NAME = "aeos-verifier.vitest.config.mts"
CANDIDATE_CONFIGS = ("vitest.config.ts", "vitest.config.mts", "vitest.config.js", "vitest.config.mjs",
                     "vite.config.ts", "vite.config.mts", "vite.config.js", "vite.config.mjs")
TRUSTED_VITEST_CONFIG = """// Verifier-owned vitest configuration (DLY AEOS independent verifier, section 31 G3).
// Test selection, reporters and the result file are the verifier's; the candidate's vite plugins, test environment,
// globals and setup files are reused (lower trust). The candidate's own test/report settings are ignored.
import { defineConfig } from "vitest/config";
__IMPORT__
let base: any = __BASE__;
if (typeof base === "function") base = await base({ command: "serve", mode: "test" });
const t = (base && base.test) || {};
export default defineConfig({
  plugins: (base && base.plugins) || [],
  resolve: (base && base.resolve) || {},
  test: {
    include: ["**/*.{test,spec}.?(c|m)[jt]s?(x)"],
    exclude: ["**/node_modules/**", "**/dist/**", "**/build/**"],
    environment: t.environment ?? "node",
    globals: t.globals ?? false,
    setupFiles: t.setupFiles ?? [],
    css: false,
    passWithNoTests: false,
    allowOnly: false,
    watch: false,
  },
});
"""


def frontend_roots(snapshot: Path, changed: list[str], frontend_paths: list[str]) -> list[str]:
    """Package directories (with package.json + package-lock.json) owning the changed UI files."""
    roots: list[str] = []
    for path in frontend_paths:
        parts = PurePosixPath(path).parts[:-1]
        for i in range(len(parts), -1, -1):
            rel = "/".join(parts[:i])
            d = snapshot.joinpath(*parts[:i])
            if (d / "package.json").is_file():
                if (d / "package-lock.json").is_file() and rel not in roots:
                    roots.append(rel)
                break
    return roots[:3]


def node_tools() -> tuple[list[str] | None, Path | None, str]:
    """(node command, npm-cli.js, reason). npm is run as ``node npm-cli.js`` (never a ``.cmd`` shim)."""
    node = shutil.which("node")
    if not node:
        return None, None, "node is not installed on this host"
    base = Path(node).resolve().parent
    for cand in (base / "node_modules" / "npm" / "bin" / "npm-cli.js",
                 base.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"):
        if cand.is_file():
            return [node], cand, ""
    return [node], None, "npm (npm-cli.js) was not found next to node"


def browsers_path() -> str:
    """Where Playwright's browsers live for the controller user (the run's redirected home has none)."""
    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if explicit:
        return explicit
    if sys.platform == "win32":
        return str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright")
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Caches" / "ms-playwright")
    return str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright")


def trusted_config(fe: Path) -> str:
    """The verifier's vitest config; the candidate's own config (if any) is imported for plugins/environment only."""
    name = next((n for n in CANDIDATE_CONFIGS if (fe / n).is_file()), None)
    if name is None:
        return TRUSTED_VITEST_CONFIG.replace("__IMPORT__", "").replace("__BASE__", "{}")
    return (TRUSTED_VITEST_CONFIG.replace("__IMPORT__", f'import candidateConfig from "./{name}";')
            .replace("__BASE__", "candidateConfig"))


def _materialise(src: Path, dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in EXCLUDED_NAMES}

    shutil.copytree(src, dest, ignore=ignore)
    return sum(1 for p in dest.rglob("*") if p.is_file())


def _env(rn: Any, fe: Path, *, egress: bool) -> dict[str, str]:
    env: dict[str, str] = dict(rn.env(fe, autoload=False))
    home = Path(env.get("HOME") or rn.workdir / "h")
    user_cfg, global_cfg = home / ".npmrc-user", home / ".npmrc-global"  # npm refuses one file for both
    for cfg in (user_cfg, global_cfg):
        cfg.parent.mkdir(parents=True, exist_ok=True)
        if not cfg.is_file():
            cfg.write_text("", encoding="utf-8")
    env.update({"npm_config_userconfig": str(user_cfg), "npm_config_globalconfig": str(global_cfg),
                "npm_config_cache": str(home / ".npm-cache"), "npm_config_update_notifier": "false",
                "npm_config_fund": "false", "npm_config_audit": "false", "CI": "1", "NO_COLOR": "1",
                "FORCE_COLOR": "0"})
    if egress:  # the install step only: the package registry must be reachable
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(k, None)
    return env


def _classify_install(res: ProcResult) -> str:
    text = (res.stdout or "") + (res.stderr or "")
    if "in sync" in text or "EJSONPARSE" in text or "EUSAGE" in text or "ERESOLVE" in text:
        return "FAIL"
    return "NOT_RUN"


def _vitest(node: list[str], fe: Path, out: Path, env: dict[str, str], timeout: float) -> tuple[str, str, dict[str, Any]]:
    cli = fe / "node_modules" / "vitest" / "vitest.mjs"
    if not cli.is_file():
        return "NOT_RUN", "the candidate lockfile provides no vitest", {}
    (fe / TRUSTED_CONFIG_NAME).write_text(trusted_config(fe), encoding="utf-8")
    res = run_process([*node, str(cli), "run", "--config", str(fe / TRUSTED_CONFIG_NAME), "--root", str(fe),
                       "--reporter=json", f"--outputFile={out}"], cwd=fe, env=env, timeout=timeout)
    data: dict[str, Any] = {}
    if out.is_file():
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    rows = [{"test": str(a.get("fullName") or a.get("title") or "")[:200], "status": a.get("status"),
             "file": Path(str(r.get("name") or "")).name}
            for r in data.get("testResults") or [] for a in r.get("assertionResults") or []]
    info = {"exit_code": res.returncode, "timed_out": res.timed_out, "duration_s": res.duration_s,
            "total": data.get("numTotalTests"), "passed": data.get("numPassedTests"),
            "failed": data.get("numFailedTests"), "failed_suites": data.get("numFailedTestSuites"),
            "pending": data.get("numPendingTests"), "tests": rows[:500], "output_tail": res.tail(3000)}
    if res.timed_out:
        return "FAIL", f"vitest timed out after {timeout:.0f}s", info
    if not data:
        return "NOT_RUN", f"vitest produced no JSON report (exit {res.returncode})", info
    failed = int(data.get("numFailedTests") or 0) + int(data.get("numFailedTestSuites") or 0)
    passed = int(data.get("numPassedTests") or 0)
    if failed or (res.returncode not in (0, None) and passed):
        return "FAIL", f"vitest: {data.get('numFailedTests') or 0} failed test(s), {data.get('numFailedTestSuites') or 0} " \
                       "failed suite(s)", info
    if not passed:
        return "NOT_RUN", "vitest selected no passing frontend tests", info
    return "PASS", f"vitest: {passed} frontend test(s) passed (verifier config)", info


def _tool_step(node: list[str], script: Path, args: list[str], fe: Path, env: dict[str, str], timeout: float,
               label: str) -> tuple[str, str, dict[str, Any]]:
    if not script.is_file():
        return "NOT_RUN", f"{label}: not provided by the candidate lockfile", {}
    res = run_process([*node, str(script), *args], cwd=fe, env=env, timeout=timeout)
    info = {"exit_code": res.returncode, "timed_out": res.timed_out, "duration_s": res.duration_s,
            "output_tail": res.tail(3000)}
    if res.timed_out:
        return "FAIL", f"{label} timed out after {timeout:.0f}s", info
    return ("PASS", f"{label} passed", info) if res.returncode == 0 else (
        "FAIL", f"{label} failed (exit {res.returncode})", info)


def run_package_tests(rn: Any, package_dir: Path, workdir: Path) -> dict[str, Any]:
    """``npm ci --ignore-scripts`` + vitest (verifier config) for one package directory (project test baseline)."""
    if not isinstance(rn, runner_mod.ProcessRunner):
        return {"status": "NOT_RUN", "summary": "the container runner's verifier image has no node runtime"}
    node, npm_cli, why = node_tools()
    if node is None or npm_cli is None:
        return {"status": "NOT_RUN", "summary": why}
    fe = workdir / "fe"
    files = _materialise(package_dir, fe)
    install = run_process([*node, str(npm_cli), "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                           "--loglevel=error"], cwd=fe, env=_env(rn, fe, egress=True), timeout=INSTALL_TIMEOUT)
    inst = {"exit_code": install.returncode, "timed_out": install.timed_out, "duration_s": install.duration_s,
            "output_tail": install.tail(2000), "command": "npm ci --ignore-scripts"}
    if install.returncode != 0 or install.timed_out:
        st = _classify_install(install)
        return {"status": st, "summary": f"npm ci --ignore-scripts did not complete (exit {install.returncode})",
                "install": inst, "files": files}
    reports = workdir / "r"
    reports.mkdir(parents=True, exist_ok=True)
    st, summary, info = _vitest(node, fe, reports / "vitest-baseline.json", _env(rn, fe, egress=False), STEP_TIMEOUT)
    return {"status": st, "summary": summary, "install": inst, "files": files, **info}


def run_ui(rn: Any, dist: Path, out_dir: Path, timeout: float = UI_TIMEOUT) -> dict[str, Any]:
    """The UI state/a11y driver against a built bundle, through the runner (process runner only)."""
    res = rn.python(["-c", pack_text("ui_driver.py"), str(dist), str(out_dir), browsers_path()],
                    pythonpath=out_dir.parent, autoload=False, timeout=timeout)
    data = last_json_line(res.stdout)
    if not data:
        data = {"status": "ERROR", "reason": f"ui driver produced no report (exit {res.returncode}): {res.tail(400)}"}
    data["exit_code"] = res.returncode
    data["duration_s"] = res.duration_s
    return data


def ui_result(ui: dict[str, Any], out_dir: Path) -> StepResult:
    states = list(ui.get("states") or [])
    evidence: list[StepEvidence] = []
    if ui.get("status") != "RAN" or not states:
        msg = f"UI states/a11y not rendered: {ui.get('reason') or ui.get('status')}"
        return StepResult("ui_a11y", "NOT_RUN", msg, {"ui": ui}, not_proven=[msg])
    for s in states:
        shot = out_dir / str(s.get("screenshot") or "")
        if shot.is_file() and shot.suffix == ".png":
            evidence.append(StepEvidence("SCREENSHOT", f"UI state '{s.get('state')}' ({s.get('viewport')}) of the "
                                                       "candidate bundle (API answered by the verifier)",
                                         shot.read_bytes(), "E2", None,
                                         {"ui_state": s.get("state"), "viewport": s.get("viewport")}))
    violations = [{"state": s["state"], "viewport": s["viewport"], **v} for s in states for v in s.get("violations")
                  or []]
    status = "FAIL" if violations else "PASS"
    content = {"suite": "ui-a11y", "status": status, "probe": "verifier a11y probe (WCAG subset; not axe-core)",
               "rules": ui.get("rules"), "browser": ui.get("browser"), "states": states,
               "violations": violations[:200],
               "not_covered": ["colour contrast", "focus order / keyboard traps", "screen-reader announcements",
                               "routes other than the bundle entry '/'"]}
    evidence.insert(0, StepEvidence("TEST_REPORT", f"UI states + accessibility probe of the candidate bundle: {status} "
                                                   f"({len(states)} state renders, {len(violations)} violation(s))",
                                    content, "E2", "ui_a11y"))
    details = {"states": [f"{s['state']}/{s['viewport']}" for s in states], "violations": violations[:20],
               "screenshots": len(evidence) - 1}
    if violations:
        rules = sorted({str(v.get("rule")) for v in violations})
        msg = f"accessibility probe: {len(violations)} violation(s) ({', '.join(rules[:5])})"
        return StepResult("ui_a11y", "FAIL", msg, details, evidence, reasons=[msg])
    return StepResult("ui_a11y", "PASS", f"{len(states)} UI state render(s) captured; accessibility probe found no "
                                         "violation (WCAG subset)", details, evidence,
                      not_proven=["UI accessibility beyond the verifier probe (contrast, focus order, screen readers) "
                                  "NOT-PROVEN"])


def run(ctx: StepContext) -> list[StepResult]:
    paths = list(ctx.categories.get("frontend", []))
    if not paths:
        return []
    roots = frontend_roots(ctx.snapshot, ctx.changed, paths)

    def not_run(reason: str) -> list[StepResult]:
        msg = (f"frontend changed ({', '.join(paths[:3])}): {reason}; unit/browser/visual/a11y tests NOT-PROVEN "
               "here (G3 needs the provider CI frontend job)")
        return [StepResult("frontend_tests", "NOT_PROVEN", msg, {"paths": paths[:20]}, not_proven=[msg])]

    if not roots:
        return not_run("no package.json + package-lock.json owns the changed files")
    if not isinstance(ctx.runner, runner_mod.ProcessRunner):
        return not_run("the container runner's verifier image has no node runtime (frontend steps run on the "
                       "process runner only)")
    node, npm_cli, why = node_tools()
    if node is None or npm_cli is None:
        return not_run(why)
    root = roots[0]
    fe = ctx.workdir / "fe"
    files = _materialise(ctx.snapshot.joinpath(*root.split("/")) if root else ctx.snapshot, fe)
    reports = ctx.workdir / "r"
    reports.mkdir(parents=True, exist_ok=True)
    steps: dict[str, dict[str, Any]] = {}
    install = run_process([*node, str(npm_cli), "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                           "--loglevel=error"], cwd=fe, env=_env(ctx.runner, fe, egress=True), timeout=INSTALL_TIMEOUT)
    inst_status = "PASS" if install.returncode == 0 and not install.timed_out else _classify_install(install)
    steps["install"] = {"status": inst_status, "command": "npm ci --ignore-scripts", "exit_code": install.returncode,
                        "timed_out": install.timed_out, "duration_s": install.duration_s,
                        "output_tail": install.tail(2000),
                        "egress": "package registry reachable for this step only; not enforced on the process runner"}
    results: list[StepResult] = []
    env = _env(ctx.runner, fe, egress=False)
    if inst_status == "PASS":
        st, summary, info = _vitest(node, fe, reports / "vitest.json", env, STEP_TIMEOUT)
        steps["unit"] = {"status": st, "summary": summary, **info}
        if (fe / "tsconfig.json").is_file():
            st, summary, info = _tool_step(node, fe / "node_modules" / "typescript" / "bin" / "tsc", ["-b", "--noEmit"],
                                           fe, env, STEP_TIMEOUT, "type check (tsc -b --noEmit)")
            steps["typecheck"] = {"status": st, "summary": summary, **info}
        st, summary, info = _tool_step(node, fe / "node_modules" / "vite" / "bin" / "vite.js", ["build"], fe, env,
                                       STEP_TIMEOUT, "build (vite build)")
        steps["build"] = {"status": st, "summary": summary, **info}
        dist = fe / "dist"
        if st == "PASS" and (dist / "index.html").is_file():
            ui = run_ui(ctx.runner, dist, reports / "ui")
            results.append(ui_result(ui, reports / "ui"))
        else:
            msg = "UI states/a11y not rendered: no built bundle (dist/index.html)"
            results.append(StepResult("ui_a11y", "NOT_RUN", msg, {}, not_proven=[msg]))
    failed = [k for k, v in steps.items() if v.get("status") == "FAIL"]
    unit = steps.get("unit", {}).get("status")
    if failed:
        status = "FAIL"
        summary = "; ".join(str(steps[k].get("summary") or f"{k} failed") for k in failed)[:400]
        if "install" in failed:
            summary = f"npm ci --ignore-scripts refused the candidate lockfile (exit {install.returncode})"
    elif unit == "PASS":
        status, summary = "PASS", str(steps["unit"]["summary"]) + "; " + ", ".join(
            f"{k} {v['status']}" for k, v in steps.items() if k != "unit")
    else:
        status = "NOT_RUN"
        last = [ln.strip() for ln in install.tail(600).splitlines() if ln.strip()][-1:] or [""]
        summary = (steps.get("unit", {}).get("summary") or
                   f"npm ci --ignore-scripts did not complete (exit {install.returncode}, timed out "
                   f"{install.timed_out}): {last[0][:160]}")
    content = {"suite": "frontend", "package_root": root or ".", "files_materialised": files, "status": status,
               "steps": steps, "excluded": sorted(EXCLUDED_NAMES),
               "trust": "LOW for the candidate's own frontend tests; selection/reporting by the verifier config"}
    ev = StepEvidence("TEST_REPORT", f"Frontend step (npm ci --ignore-scripts, vitest, type check, build) on "
                                     f"{root or '.'}: {status}", content, "E2", "frontend_tests")
    details = {"package_root": root or ".", "steps": {k: v.get("status") for k, v in steps.items()}}
    if status == "FAIL":
        res = StepResult("frontend_tests", "FAIL", summary, details, [ev], reasons=[f"frontend: {summary}"])
    elif status == "PASS":
        res = StepResult("frontend_tests", "PASS", summary, details, [ev])
    else:
        msg = (f"frontend changed ({', '.join(paths[:3])}): {summary}; frontend tests NOT-PROVEN here (G3 needs the "
               "provider CI frontend job)")
        res = StepResult("frontend_tests", "NOT_PROVEN", msg, details, [ev], not_proven=[msg])
    return [res, *results]
