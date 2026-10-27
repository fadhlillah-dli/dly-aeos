"""W4 - the Control Center end to end in a REAL browser (Playwright + Chromium).

Starts the real server (uvicorn ``aeos.api.app:create_app`` factory) on a free loopback port with a
fresh ``AEOS_DATA_DIR``, runs ``aeos db upgrade`` + ``aeos seed`` + ``aeos inventory reconcile`` +
``aeos work demo-fixtures``, creates a SUPER_ADMIN and a VIEWER through the identity service and
then drives the UI like a human: login form -> domain gate -> project switch -> every route of
``frontend/src/layout/nav.ts`` plus detail routes reachable from the seeded data.

Per page it asserts: no uncaught page error, no console error, no ``/api`` response >= 500 (and in
admin flows no 4xx either: that is a broken API path or a page/backend shape mismatch), the
PageHeader ``h1`` and its provenance ("Sumber data", "Diperiksa pada", "Batas bukti") render, no
untranslated control/heading copy, no horizontal overflow at 390px and readable contrast in dark mode.
MOCK/DEMO/DESIGN rows must carry their label. OFFICE shows empty/blocked states without PERSONAL data; a
VIEWER sees no admin-only controls and a direct write is rejected with 403 by the backend.

External tools are never called: claude/opencode/bao/mmdc point at absent paths, OpenBao at a closed
loopback port and the OS keyring is the null backend (``PYTHON_KEYRING_BACKEND``, the one ``tests/conftest.py``
gives sandboxed children) under a service name that is unique to the run. Nothing leaves the machine.

Screenshots (full page, 1440px and 390px, plus one dark-mode run) are written to a temp dir
(override with ``AEOS_UI_SHOTS_DIR``). ``AEOS_UI_EVIDENCE=1`` refreshes the curated set in
``docs/evidence/ui/`` - seeded DESIGN catalog + DEMO fixtures, never live usage.
"""

from __future__ import annotations

import datetime as dt
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import NULL_KEYRING

pytestmark = pytest.mark.integration

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed (pip install -e .[dev])")

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
NAV_TS = FRONTEND / "src" / "layout" / "nav.ts"
EVIDENCE_DIR = REPO / "docs" / "evidence" / "ui"

ADMIN = ("owner", "correct-horse-battery-staple")
VIEWER = ("viewer", "read-only-observer-pass-77")
PLATFORM_PROJECT = "DLY AEOS"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

# Curated evidence set (file stem -> route), see docs/evidence/ui/README.md.
CURATED = {
    "ringkasan": "/",
    "kesiapan": "/readiness",
    "peta-sistem": "/system-map",
    "tools-repos": "/tools",
    "model-rute": "/models",
    "pekerjaan-live": "/work",
    "organisasi": "/organization",
    "rencana": "/plans",
    "usulan-upgrade": "/proposals",
    "riwayat": "/history",
}


def nav_routes() -> list[str]:
    routes = re.findall(r'\bto:\s*"(/[^"]*)"', NAV_TS.read_text(encoding="utf-8"))
    assert routes, "no routes parsed from nav.ts"
    return list(dict.fromkeys(routes))


def _slug(path: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", path.strip("/")).strip("-")
    return s or "root"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------------------------------------- setup


def _chromium_or_skip() -> None:
    try:
        with sync_api.sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
    except Exception as exc:  # driver missing or broken install
        pytest.skip(f"playwright is not usable: {exc}")
    if not exe.is_file():
        pytest.skip("chromium is not installed (python -m playwright install chromium)")


def _newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for root in paths:
        if root.is_file():
            newest = max(newest, root.stat().st_mtime)
        elif root.is_dir():
            for p in root.rglob("*"):
                if p.is_file():
                    newest = max(newest, p.stat().st_mtime)
    return newest


def _ensure_frontend_built() -> None:
    index = FRONTEND / "dist" / "index.html"
    sources = [FRONTEND / "src", FRONTEND / "index.html", FRONTEND / "package.json", FRONTEND / "vite.config.ts"]
    if index.is_file() and index.stat().st_mtime >= _newest_mtime(sources):
        return
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        pytest.skip("frontend/dist is missing or stale and npm is not available to build it")
    if not (FRONTEND / "node_modules").is_dir():
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=FRONTEND, check=True, timeout=1200)
    subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True, timeout=1200)
    assert index.is_file(), "npm run build did not produce frontend/dist/index.html"


CREATE_USERS = """
import os
from aeos.core.db import init_engine, session_scope
from aeos.core.enums import Role
from aeos.identity import service as identity
init_engine()
with session_scope() as s:
    identity.bootstrap_admin(s, username=os.environ["UI_SMOKE_ADMIN_USER"], password=os.environ["UI_SMOKE_ADMIN_PW"])
    identity.create_user(s, username=os.environ["UI_SMOKE_VIEWER_USER"], password=os.environ["UI_SMOKE_VIEWER_PW"],
                         role=Role.VIEWER)
print("users created")
"""


@dataclass
class Server:
    base_url: str
    env: dict[str, str]
    data_dir: Path
    log_path: Path
    proc: subprocess.Popen[bytes]

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def log_tail(self, n: int = 60) -> str:
        return "\n".join(self.log_text().splitlines()[-n:])


def _isolated_env(root: Path, port: int) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AEOS_")}
    absent = root / "absent-tools"
    exe = ".exe" if os.name == "nt" else ""
    env.update(
        AEOS_DATA_DIR=str(root / "data"),
        AEOS_CONFIG=str(root / "no-config.yaml"),
        AEOS_PORT=str(port),
        AEOS_ENVIRONMENT="local",
        # Never touch the owner's real tools, vault or keyring from a UI test.
        AEOS_CLAUDE_BINARY=str(absent / f"claude{exe}"),
        AEOS_OPENCODE_BINARY=str(absent / f"opencode{exe}"),
        AEOS_OPENBAO_BINARY=str(absent / f"bao{exe}"),
        AEOS_MMDC_BINARY=str(absent / f"mmdc{exe}"),
        AEOS_OPENBAO_ADDR=f"http://127.0.0.1:{_free_port()}",
        AEOS_KEYRING_SERVICE=f"dly-aeos-ui-smoke-{uuid.uuid4().hex[:8]}",
        # The server child runs outside the SecretGuard: it selects the null keyring (the backend the guard gives
        # sandboxed children), so no lookup ever reaches the macOS Keychain or Windows Credential Manager (L20).
        PYTHON_KEYRING_BACKEND=NULL_KEYRING,
        PYTHONPATH=str(REPO / "backend"),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        UI_SMOKE_ADMIN_USER=ADMIN[0],
        UI_SMOKE_ADMIN_PW=ADMIN[1],
        UI_SMOKE_VIEWER_USER=VIEWER[0],
        UI_SMOKE_VIEWER_PW=VIEWER[1],
    )
    return env


def _run(env: dict[str, str], *args: str, timeout: float = 600) -> str:
    res = subprocess.run([sys.executable, *args], cwd=REPO, env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout, check=False)
    assert res.returncode == 0, f"{' '.join(args)} failed ({res.returncode}):\n{res.stdout}\n{res.stderr}"
    return res.stdout


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    _chromium_or_skip()
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        try:
            yield b
        finally:
            b.close()


@pytest.fixture(scope="module")
def server(browser: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    _ensure_frontend_built()
    root = tmp_path_factory.mktemp("ui-smoke")
    port = _free_port()
    env = _isolated_env(root, port)
    _run(env, "-m", "aeos", "db", "upgrade")
    _run(env, "-m", "aeos", "seed")
    _run(env, "-m", "aeos", "inventory", "reconcile")
    _run(env, "-m", "aeos", "work", "demo-fixtures")
    _run(env, "-c", CREATE_USERS)
    log_path = root / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aeos.api.app:create_app", "--factory", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "info"],
            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    srv = Server(f"http://127.0.0.1:{port}", env, root / "data", log_path, proc)
    try:
        deadline = time.monotonic() + 90
        while True:
            if proc.poll() is not None:
                pytest.fail(f"server exited early ({proc.returncode}):\n{srv.log_tail()}")
            try:
                if httpx.get(f"{srv.base_url}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"server did not become healthy:\n{srv.log_tail()}")
            time.sleep(0.3)
        yield srv
    finally:
        _stop_tree(proc)


def _stop_tree(proc: subprocess.Popen[bytes]) -> None:
    """Stop the server and any child (a Windows venv launcher runs the real interpreter as a child)."""
    import psutil

    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        children = []
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass


@pytest.fixture(scope="module")
def shots(tmp_path_factory: pytest.TempPathFactory) -> Path:
    override = os.environ.get("AEOS_UI_SHOTS_DIR")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return tmp_path_factory.mktemp("ui-shots")


# ---------------------------------------------------------------------------------------- page watch


@dataclass
class Watch:
    """Collects browser-side problems per route; ``report()`` renders them for the assertion."""

    problems: list[str] = field(default_factory=list)
    inflight: dict[int, float] = field(default_factory=dict)  # id(request) -> start (monotonic)
    current: str = "(login)"
    # Admin flows on seeded PERSONAL data must not hit 4xx: a 404/405/422 there is a broken API path
    # or a request-shape mismatch between a page and the backend. OFFICE/VIEWER flows expect 403/404.
    strict_4xx: bool = False
    streams: int = 0  # /api/events/stream connections opened since the last visit() (one per tab expected)

    def attach(self, page: Any) -> None:
        page.on("pageerror", lambda exc: self.problems.append(f"{self.current}: uncaught page error: {exc}"))
        page.on("console", self._console)
        page.on("request", self._request)
        page.on("requestfinished", self._done)
        page.on("requestfailed", self._failed)
        page.on("response", self._response)

    @staticmethod
    def _tracked(url: str) -> bool:
        return "/api/" in url and "/api/events/stream" not in url

    def _console(self, msg: Any) -> None:
        if msg.type != "error":
            return
        text = msg.text
        # Network failures are asserted from the responses themselves (status + URL).
        if text.startswith("Failed to load resource"):
            return
        self.problems.append(f"{self.current}: console error: {text}")

    def _request(self, req: Any) -> None:
        if "/api/events/stream" in req.url:
            self.streams += 1
        if self._tracked(req.url):
            self.inflight[id(req)] = time.monotonic()

    def _done(self, req: Any) -> None:
        self.inflight.pop(id(req), None)

    def _failed(self, req: Any) -> None:
        self.inflight.pop(id(req), None)
        failure = req.failure or ""
        if self._tracked(req.url) and "ERR_ABORTED" not in failure and "NS_BINDING_ABORTED" not in failure:
            self.problems.append(f"{self.current}: request failed: {req.method} {req.url} ({failure})")

    def _response(self, resp: Any) -> None:
        if "/api/" not in resp.url:
            return
        if resp.status >= 500:
            self.problems.append(f"{self.current}: server error {resp.status} on {resp.request.method} {resp.url}")
        elif resp.status >= 400 and self.strict_4xx and "/api/auth/session" not in resp.url:
            self.problems.append(f"{self.current}: unexpected {resp.status} on {resp.request.method} {resp.url}")

    def reset_inflight(self) -> None:
        """A full navigation abandons the old page's requests without always emitting requestfailed."""
        self.inflight.clear()

    def settle(self, page: Any, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            pending = [t for t in self.inflight.values() if now - t < 10.0]
            if not pending:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= 0.6:
                    break
            else:
                quiet_since = None
            page.wait_for_timeout(100)
        page.wait_for_timeout(150)

    def report(self) -> str:
        return "\n".join(self.problems)


def new_page(browser: Any, *, viewport: dict[str, int], dark: bool = False, strict_4xx: bool = False) -> tuple[Any, Any, Watch]:
    ctx = browser.new_context(viewport=viewport, locale="id-ID", timezone_id="Asia/Jakarta",
                              color_scheme="dark" if dark else "light", device_scale_factor=1,
                              is_mobile=viewport["width"] < 768, has_touch=viewport["width"] < 768)
    ctx.set_default_timeout(15_000)
    page = ctx.new_page()
    watch = Watch(strict_4xx=strict_4xx)
    watch.attach(page)
    page.on("dialog", lambda d: d.accept())  # domain switch confirmation
    return ctx, page, watch


def login(page: Any, srv: Server, watch: Watch, user: tuple[str, str]) -> None:
    watch.current = "/ (login)"
    page.goto(srv.base_url + "/")
    page.get_by_label("Username").fill(user[0])
    page.get_by_label("Password").fill(user[1])
    page.get_by_role("button", name="Masuk").click()
    page.get_by_role("heading", name="Pilih domain kerja").wait_for()


def choose_domain_on_gate(page: Any, domain: str) -> None:
    page.get_by_role("button", name=re.compile(rf"^{domain}")).click()
    page.get_by_role("banner").wait_for()
    page.locator("main h1").first.wait_for()


def select_project(page: Any, watch: Watch, name: str) -> None:
    sel = page.get_by_label("Proyek", exact=True)
    sel.wait_for()
    page.wait_for_function(
        "name => [...document.querySelectorAll('select[aria-label=\"Proyek\"] option')].some(o => o.textContent === name)",
        arg=name,
    )
    sel.select_option(label=name)
    watch.settle(page)


def api_json(page: Any, path: str) -> Any:
    """GET as the logged-in browser user (the context shares the session cookie)."""
    origin = re.match(r"^https?://[^/]+", page.url)
    assert origin, page.url
    r = page.request.get(origin.group(0) + path)
    assert r.ok, f"GET {path} -> {r.status}: {r.text()[:300]}"
    return r.json()


def api_write(page: Any, method: str, path: str, body: dict[str, Any]) -> Any:
    """A direct write as the logged-in browser user (session cookie + CSRF header), like the SPA does."""
    return page.evaluate(
        """async ([method, path, body]) => {
            const s = await (await fetch('/api/auth/session', {credentials: 'same-origin'})).json();
            const r = await fetch(path, {method, credentials: 'same-origin',
              headers: {'Content-Type': 'application/json', 'X-CSRF-Token': s.csrf_token}, body: JSON.stringify(body)});
            let data = null; try { data = await r.json(); } catch (e) { data = null; }
            return {status: r.status, data};
        }""",
        [method, path, body],
    )


OVERFLOW_JS = """() => {
  const doc = document.documentElement;
  const out = [];
  if (doc.scrollWidth > window.innerWidth + 1) out.push(`document ${doc.scrollWidth}px > viewport ${window.innerWidth}px`);
  const main = document.querySelector('main');
  if (main && main.scrollWidth > main.clientWidth + 1) out.push(`main ${main.scrollWidth}px > ${main.clientWidth}px`);
  // Name the widest offenders (outside horizontally scrollable containers) to make fixes obvious.
  if (out.length) {
    const vw = window.innerWidth;
    const scrollable = el => { for (let p = el.parentElement; p; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX; if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return true; } return false; };
    const bad = [...document.querySelectorAll('main *, header *')].filter(el => {
      const r = el.getBoundingClientRect(); return r.right > vw + 1 && r.width > 0 && !scrollable(el); })
      .slice(0, 4).map(el => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)} right=${Math.round(el.getBoundingClientRect().right)}`);
    out.push(...bad);
  }
  return out;
}"""


# UI copy is Indonesian (AGENTS.md); technical ids stay English. These words on controls/headings mean
# an untranslated string, not an id.
ENGLISH_UI = re.compile(r"^(Loading|Submit|Cancel|Save|Delete|Search|Close|Refresh|Error|No data|Back|Next|Previous|"
                        r"Details|Settings|Open|Retry|Not found|Unknown)\b")

ENGLISH_JS = """() => [...document.querySelectorAll('main button, main h1, main h2, main h3, main th, main label, header button')]
  .filter(el => el.offsetParent !== null).map(el => (el.getAttribute('aria-label') || el.textContent || '').trim())
  .filter(Boolean)"""


def visit(page: Any, srv: Server, watch: Watch, path: str, *, shots: Path | None, tag: str,
          check_overflow: bool = False) -> None:
    watch.current = f"{path} [{tag}]"
    watch.reset_inflight()
    watch.streams = 0
    page.goto(srv.base_url + path, wait_until="domcontentloaded")
    h1 = page.locator("main h1").first
    try:
        h1.wait_for(state="visible", timeout=20_000)
    except sync_api.TimeoutError:
        watch.problems.append(f"{watch.current}: PageHeader h1 did not render")
        return
    watch.settle(page)
    title = h1.inner_text().strip()
    if not title:
        watch.problems.append(f"{watch.current}: PageHeader h1 is empty")
    # §41 (W6-113): the PageHeader provenance block names its source, when it was checked and the evidence limit
    # side by side ("Diperiksa pada: belum pernah" when nothing was ever checked).
    provenance = page.locator("main dl").filter(has=page.get_by_text("Sumber data:", exact=True)).first
    if provenance.count() == 0:
        watch.problems.append(f"{watch.current}: PageHeader provenance ('Sumber data') missing")
    else:
        for label in ("Diperiksa pada:", "Batas bukti:"):
            if provenance.get_by_text(label, exact=True).count() == 0:
                watch.problems.append(f"{watch.current}: PageHeader provenance ('{label[:-1]}') missing next to "
                                      "'Sumber data'")
    if page.locator("main").get_by_text("Memuat…").count() > 0:
        watch.settle(page, timeout=5)
    if watch.streams > 1:
        # One shared SSE connection per tab: several would exhaust the browser's per-origin limit.
        watch.problems.append(f"{watch.current}: {watch.streams} event-stream connections opened (expected one per tab)")
    for text in page.evaluate(ENGLISH_JS):
        if ENGLISH_UI.match(text):
            watch.problems.append(f"{watch.current}: untranslated UI copy: {text[:60]!r}")
    if check_overflow:
        for issue in page.evaluate(OVERFLOW_JS):
            watch.problems.append(f"{watch.current}: horizontal overflow: {issue}")
    if shots is not None:
        page.screenshot(path=str(shots / f"{tag}-{_slug(path)}.png"), full_page=True)
    _save_evidence(page, tag, path)


@dataclass
class Seeded:
    project_id: str
    component_id: str
    proposal_id: str
    run_ids: list[str]
    plan_ids: list[str]

    def detail_routes(self) -> list[str]:
        routes = [f"/projects/{self.project_id}", f"/tools/{self.component_id}", f"/proposals/{self.proposal_id}"]
        routes += [f"/runs/{r}" for r in self.run_ids[:1]]
        routes += [f"/plans/{p}" for p in self.plan_ids[:1]]
        return routes


_SEEDED: dict[str, Seeded] = {}


def seeded_routes(page: Any) -> Seeded:
    """Detail ids from the seeded data (PERSONAL); the proposal is created once through the API."""
    if "s" in _SEEDED:
        return _SEEDED["s"]
    projects = api_json(page, "/api/projects")["items"]
    platform_project = next(p for p in projects if p["name"] == PLATFORM_PROJECT)
    comps = api_json(page, "/api/inventory/components?page_size=200")["items"]
    ids = [c["component_id"] for c in comps]
    component_id = "claude-code" if "claude-code" in ids else ids[0]
    created = api_write(page, "POST", "/api/proposals", {
        "kind": "COMPARE", "title": "UI smoke: bandingkan worker engine (DESIGN)",
        "sources": ["https://github.com/anomalyco/opencode"], "problem": "Smoke test UI: review saja, tanpa instalasi.",
        "expected_benefit": "UNKNOWN", "scope_type": "PLATFORM", "existing_component_ids": [component_id],
    })
    assert created["status"] == 200, created
    runs = api_json(page, "/api/runs")["items"]
    plans = api_json(page, "/api/plans")["items"]
    _SEEDED["s"] = Seeded(platform_project["id"], component_id, created["data"]["id"],
                          [r.get("id") or r.get("run_id") for r in runs], [p["id"] for p in plans])
    return _SEEDED["s"]


def _evidence_enabled() -> bool:
    return os.environ.get("AEOS_UI_EVIDENCE", "").lower() in {"1", "true", "yes"}


EVIDENCE_SUFFIX = {"desktop": "1440", "mobile": "390", "dark": "dark"}
EVIDENCE_MAX_HEIGHT = 6000  # px; full-page PNGs of all 30 curated shots would be ~14 MB in the repo
EVIDENCE_QUALITY = 60


def _save_evidence(page: Any, tag: str, path: str) -> None:
    """Curated copies for docs/evidence/ui: JPEG, full page up to EVIDENCE_MAX_HEIGHT (desktop/mobile),
    first viewport for the dark run. The lossless full-page PNGs stay in the temp shots dir."""
    if not _evidence_enabled() or tag not in EVIDENCE_SUFFIX:
        return
    stem = next((k for k, v in CURATED.items() if v == path), None)
    if stem is None:
        return
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = str(EVIDENCE_DIR / f"{stem}-{EVIDENCE_SUFFIX[tag]}.jpg")
    if tag == "dark":
        page.screenshot(path=target, type="jpeg", quality=EVIDENCE_QUALITY)
        return
    width, height = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
    clip = {"x": 0, "y": 0, "width": width, "height": min(height, EVIDENCE_MAX_HEIGHT)}
    page.screenshot(path=target, full_page=True, clip=clip, type="jpeg", quality=EVIDENCE_QUALITY)


# ------------------------------------------------------------------------------------------- tests


@contextmanager
def step(watch: Watch, name: str) -> Iterator[None]:
    """Soft assertion block: a failing UX check is recorded and the run continues to the next one."""
    watch.current = name
    try:
        yield
    except (AssertionError, sync_api.Error) as exc:
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        watch.problems.append(f"{name}: {type(exc).__name__}: {first[:400]}")


def test_admin_personal_desktop(browser: Any, server: Server, shots: Path) -> None:
    ctx, page, watch = new_page(browser, viewport=DESKTOP, strict_4xx=True)
    try:
        login(page, server, watch, ADMIN)
        choose_domain_on_gate(page, "PERSONAL")
        select_project(page, watch, PLATFORM_PROJECT)
        seeded = seeded_routes(page)

        for path in [*nav_routes(), *seeded.detail_routes()]:
            visit(page, server, watch, path, shots=shots, tag="desktop")

        with step(watch, "header context"):
            # Header: domain, project, environment, freshness, LIVE/MOCK (§35 "Bahasa dan navigasi").
            page.goto(server.base_url + "/")
            watch.settle(page)
            header = page.get_by_role("banner")
            assert header.get_by_role("button", name=re.compile("PERSONAL")).get_attribute("aria-pressed") == "true"
            assert page.get_by_label("Proyek", exact=True).evaluate("s => s.options[s.selectedIndex].text") == PLATFORM_PROJECT
            assert header.get_by_test_id("environment").inner_text(timeout=3000).strip().endswith("local")
            mode = header.get_by_test_id("data-mode").inner_text(timeout=3000)
            # DEMO fixtures exist in PERSONAL: the header must not claim plain LIVE (evidence honesty).
            assert "DEMO" in mode or "MOCK" in mode, mode
            assert header.get_by_test_id("freshness").count() == 1, "data freshness missing from header"

        with step(watch, "live event stream (SSE) in the browser"):
            # §35: a persisted event reaches an open page through the stream, without a reload.
            page.goto(server.base_url + "/")
            watch.settle(page)
            page.get_by_role("banner").get_by_test_id("freshness").filter(has_text="Stream live").wait_for(timeout=10_000)
            created = api_write(page, "POST", "/api/proposals", {
                "kind": "COMPARE", "title": "UI smoke: event stream check (DESIGN)",
                "sources": ["https://github.com/NousResearch/hermes-agent"], "problem": "Smoke test: event only.",
                "expected_benefit": "UNKNOWN", "scope_type": "PLATFORM", "existing_component_ids": [],
            })
            assert created["status"] == 200, created
            feed = page.locator('main ol[aria-label="Aktivitas"]')
            feed.get_by_text(created["data"]["id"][:12]).first.wait_for(timeout=10_000)
            watch.settle(page)

        with step(watch, "DEMO run detail is labelled"):
            assert seeded.run_ids, "no DEMO runs were seeded"
            page.goto(server.base_url + f"/runs/{seeded.run_ids[0]}")
            watch.settle(page)
            page.locator("main").get_by_text(re.compile(r"fixture (DEMO|MOCK); bukan pekerjaan nyata")).wait_for(timeout=5000)

        with step(watch, "/work DEMO labels"):
            page.goto(server.base_url + "/work")
            watch.settle(page)
            demo_rows = page.locator("main tr", has_text="demo-")
            assert demo_rows.count() > 0, "DEMO fixture runs are not listed on /work"
            for i in range(demo_rows.count()):
                text = demo_rows.nth(i).inner_text()
                assert "DEMO" in text, f"DEMO row without DEMO label: {text[:120]}"

        with step(watch, "/tools design labels"):
            page.goto(server.base_url + "/tools")
            watch.settle(page)
            rows = page.locator("main tbody tr")
            assert rows.count() > 0
            for i in range(min(rows.count(), 50)):
                # Seeded catalog vocabulary (§37): never INSTALLED/ACTIVE/IN_USE as a design label.
                labelled = rows.nth(i).get_by_title(
                    re.compile("^(SELECTED_DESIGN|PROFILE_DESIGN|DESIGN|PLANNED|CANDIDATE|OPTIONAL_CANDIDATE|"
                               "REFERENCE_ONLY|WATCHLIST|UNVERIFIED)$"))
                assert labelled.count() > 0, f"tools row {i} has no DESIGN/CANDIDATE label"

        with step(watch, "/tools 'Usulkan perubahan'"):
            link = page.locator(f'main a[href="/proposals?component={seeded.component_id}"]')
            assert link.count() >= 1, "Tools & Repos row has no 'Usulkan perubahan' link"

        with step(watch, "component detail 'Usulkan perubahan'"):
            page.goto(server.base_url + f"/tools/{seeded.component_id}")
            watch.settle(page)
            detail_link = page.locator("main").get_by_role("link", name=re.compile("^Usulkan perubahan")).first
            assert detail_link.get_attribute("href", timeout=3000) == f"/proposals?component={seeded.component_id}"
            detail_link.click()
            page.get_by_text(f"Usulkan perubahan untuk komponen {seeded.component_id}").wait_for()
            watch.settle(page)

        with step(watch, "system map node card 'Usulkan perubahan'"):
            page.goto(server.base_url + "/system-map")
            watch.settle(page)
            # Domains start folded (§36 zoom-out); unfold, then open one component node's card.
            page.get_by_role("button", name="Buka semua").click()
            node = page.locator(".react-flow__node").filter(has_not_text="klik untuk buka").first
            node.wait_for()
            node.click()
            card_link = page.locator("main").get_by_role("link", name=re.compile("^Usulkan perubahan")).first
            card_link.wait_for()
            assert (card_link.get_attribute("href") or "").startswith("/proposals?component=")
            watch.settle(page)

        with step(watch, "header search"):
            search = page.get_by_role("banner").get_by_role("searchbox", name="Cari metadata")
            search.fill("DLY")
            search.press("Enter")
            hit = page.get_by_role("banner").get_by_role("link", name=re.compile(PLATFORM_PROJECT)).first
            hit.wait_for()
            hit.click()
            page.wait_for_url(re.compile(r"/projects/"))
            watch.settle(page)

        with step(watch, "nav decision count"):
            queue = api_json(page, "/api/decisions/queue")
            badge = page.get_by_test_id("nav-decisions-count")
            if queue["items"]:
                badge.wait_for()
                shown = re.match(r"\d+", badge.inner_text().strip())
                assert shown and int(shown.group(0)) == len(queue["items"]), badge.inner_text()
            else:
                assert badge.count() == 0
    finally:
        ctx.close()
    assert not watch.problems, "browser problems:\n" + watch.report()


def test_admin_personal_mobile(browser: Any, server: Server, shots: Path) -> None:
    ctx, page, watch = new_page(browser, viewport=MOBILE, strict_4xx=True)
    try:
        login(page, server, watch, ADMIN)
        choose_domain_on_gate(page, "PERSONAL")
        select_project(page, watch, PLATFORM_PROJECT)
        seeded = seeded_routes(page)

        for path in [*nav_routes(), *seeded.detail_routes()]:
            visit(page, server, watch, path, shots=shots, tag="mobile", check_overflow=True)

        with step(watch, "mobile drawer"):
            # The sidebar is hidden below md: a menu button opens a drawer with the full navigation.
            page.goto(server.base_url + "/")
            watch.settle(page)
            assert not page.get_by_role("navigation", name="Navigasi utama").is_visible()
            page.get_by_role("button", name="Buka menu navigasi").click()
            drawer = page.get_by_role("dialog", name="Navigasi")
            drawer.wait_for()
            drawer.get_by_role("link", name="Tools & Repos").click()
            page.wait_for_url(re.compile(r"/tools$"))
            drawer.wait_for(state="hidden")
            watch.settle(page)
    finally:
        ctx.close()
    assert not watch.problems, "browser problems:\n" + watch.report()


CONTRAST_JS = """() => {
  const parse = c => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    return m[1].split(',').map(x => parseFloat(x)); };
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor);
    if (c && (c.length < 4 || c[3] > 0.5)) return c; } return parse(getComputedStyle(document.body).backgroundColor); };
  const ratio = el => { const fg = parse(getComputedStyle(el).color), bg = bgOf(el);
    if (!fg || !bg) return 21; const a = lum(fg), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
  const bodyBg = parse(getComputedStyle(document.body).backgroundColor);
  const low = [...document.querySelectorAll('main h1, main h2, main p, main td, main dd, main a, main button, header span')]
    .filter(el => el.offsetParent !== null && el.textContent.trim() && !el.closest('[disabled]'))
    .map(el => [el, ratio(el)]).filter(([, r]) => r < 3).slice(0, 5)
    .map(([el, r]) => `${el.tagName.toLowerCase()} '${el.textContent.trim().slice(0, 40)}' contrast ${r.toFixed(2)}`);
  return {bodyLum: lum(bodyBg), low};
}"""


def test_admin_dark_mode(browser: Any, server: Server, shots: Path) -> None:
    ctx, page, watch = new_page(browser, viewport=DESKTOP, strict_4xx=True)
    try:
        login(page, server, watch, ADMIN)
        choose_domain_on_gate(page, "PERSONAL")
        select_project(page, watch, PLATFORM_PROJECT)
        seeded = seeded_routes(page)

        with step(watch, "theme toggle"):
            page.get_by_role("banner").get_by_role("button", name=re.compile("^Tema")).click()
            page.wait_for_function("() => document.documentElement.dataset.theme === 'dark'")
            page.reload()
            page.locator("main h1").first.wait_for()
            assert page.evaluate("() => document.documentElement.dataset.theme") == "dark", "dark theme not persisted"
            assert page.evaluate("() => localStorage.getItem('aeos.theme')") == "dark"

        for path in [*nav_routes(), *seeded.detail_routes()]:
            visit(page, server, watch, path, shots=shots, tag="dark")
            res = page.evaluate(CONTRAST_JS)
            if res["bodyLum"] > 0.2:
                watch.problems.append(f"{path} [dark]: body background is not dark (luminance {res['bodyLum']:.2f})")
            for issue in res["low"]:
                watch.problems.append(f"{path} [dark]: low contrast: {issue}")
    finally:
        ctx.close()
    assert not watch.problems, "browser problems:\n" + watch.report()


def test_office_empty_and_blocked(browser: Any, server: Server, shots: Path) -> None:
    ctx, page, watch = new_page(browser, viewport=DESKTOP)
    try:
        login(page, server, watch, ADMIN)
        choose_domain_on_gate(page, "PERSONAL")
        seeded = seeded_routes(page)

        with step(watch, "switch to OFFICE"):
            # Through the header, as a human would (the confirmation dialog is accepted).
            page.get_by_role("banner").get_by_role("button", name=re.compile("OFFICE")).click()
            page.wait_for_function(
                "() => document.querySelector('header [aria-pressed=\"true\"]')?.textContent.includes('OFFICE')")
            watch.settle(page)
            options = page.get_by_label("Proyek", exact=True).evaluate("s => [...s.options].map(o => o.text)")
            assert PLATFORM_PROJECT not in options, "PERSONAL project leaks into the OFFICE project switch"
            # The PERSONAL DEMO rows are not counted in the OFFICE header mode either.
            page.wait_for_function("() => /LIVE|MOCK|DEMO/.test(document.querySelector('[data-testid=\"data-mode\"]')?.textContent || '')")
            mode = page.get_by_role("banner").get_by_test_id("data-mode").inner_text()
            assert "DEMO" not in mode, f"OFFICE header mode counts PERSONAL DEMO rows: {mode}"

        for path in nav_routes():
            visit(page, server, watch, path, shots=shots, tag="office")

        with step(watch, "/projects [office] empty state"):
            page.goto(server.base_url + "/projects")
            watch.settle(page)
            assert page.locator("main").get_by_text(PLATFORM_PROJECT, exact=True).count() == 0
            assert page.locator("main").get_by_text(re.compile("Belum ada proyek", re.I)).count() > 0, (
                "OFFICE projects empty state missing")

        with step(watch, "/work [office] no PERSONAL DEMO rows"):
            page.goto(server.base_url + "/work")
            watch.settle(page)
            assert page.locator("main tr", has_text="demo-").count() == 0, "PERSONAL DEMO runs leak into OFFICE"

        # PERSONAL detail records are not reachable from OFFICE (backend scope, not UI hiding).
        for path in (f"/projects/{seeded.project_id}", f"/proposals/{seeded.proposal_id}", f"/runs/{seeded.run_ids[0]}"):
            with step(watch, f"{path} [office] blocked"):
                page.goto(server.base_url + path)
                main = page.locator("main")
                blocked = main.get_by_role("alert").or_(main.get_by_text(re.compile("tidak ditemukan|NOT_FOUND", re.I)))
                blocked.first.wait_for(timeout=10_000)
                watch.settle(page)
                assert main.get_by_text(PLATFORM_PROJECT, exact=True).count() == 0

        with step(watch, "/models [office] default deny"):
            page.goto(server.base_url + "/models")
            watch.settle(page)
            assert page.locator("main").get_by_text(re.compile("OFFICE")).count() > 0
    finally:
        ctx.close()
    assert not watch.problems, "browser problems:\n" + watch.report()


ADMIN_ONLY_CONTROLS: dict[str, tuple[str | None, list[tuple[str, str]]]] = {
    # route: (tab to open first, [(role, label)] of admin-only controls). "Usulkan perubahan" as a
    # button on the component detail is the read-only explanation toggle (§35), so only links count.
    "/tools": (None, [("button", "Segarkan (rekonsiliasi)"), ("link", "Usulkan perubahan"), ("link", "Usulkan komponen baru")]),
    "/tools/claude-code": (None, [("button", "Segarkan komponen ini"), ("link", "Usulkan perubahan")]),
    "/system-map": (None, [("link", "Usulkan perubahan")]),
    "/proposals": (None, [("button", "Usulkan perubahan")]),
    "/readiness": (None, [("button", "Jalankan pemeriksaan")]),
    "/work": (None, [("button", "Jeda penjadwalan"), ("button", "Lanjutkan penjadwalan"), ("button", "Batalkan")]),
    "/history": ("Backup & pemulihan", [("button", "Buat backup")]),
}


def test_viewer_is_read_only(browser: Any, server: Server, shots: Path) -> None:
    ctx, page, watch = new_page(browser, viewport=DESKTOP)
    try:
        login(page, server, watch, VIEWER)
        choose_domain_on_gate(page, "PERSONAL")
        with step(watch, "viewer header"):
            assert page.get_by_role("banner").get_by_text("VIEWER (baca saja)").count() == 1

        for path, (tab, labels) in ADMIN_ONLY_CONTROLS.items():
            visit(page, server, watch, path, shots=shots, tag="viewer")
            main = page.locator("main")
            if tab:
                with step(watch, f"{path} [viewer] tab {tab}"):
                    main.get_by_role("tab", name=tab).click()
                    watch.settle(page)
            for role, label in labels:
                for el in main.get_by_role(role, name=re.compile("^" + re.escape(label))).all():
                    if el.is_visible() and (role == "link" or el.is_enabled()):
                        watch.problems.append(f"{path} [viewer]: admin-only {role} '{label}' is usable")

        with step(watch, "/system-map [viewer] node card"):
            page.goto(server.base_url + "/system-map")
            watch.settle(page)
            page.get_by_role("button", name="Buka semua").click()
            node = page.locator(".react-flow__node").filter(has_not_text="klik untuk buka").first
            node.wait_for()
            node.click()
            page.locator("main").get_by_role("link", name="Buka detail komponen").first.wait_for()
            watch.settle(page)
            assert page.locator("main").get_by_role("link", name=re.compile("^Usulkan perubahan")).count() == 0, (
                "VIEWER sees 'Usulkan perubahan' on the System Map node card")

        # Hiding a button is not enforcement: the backend rejects a VIEWER write (§50).
        for method, path, body in (
            ("POST", "/api/proposals", {"kind": "COMPARE", "title": "viewer write", "sources": [], "scope_type": "PLATFORM"}),
            ("POST", "/api/inventory/refresh", {}),
            ("POST", "/api/workforce/pause", {}),
        ):
            with step(watch, f"viewer direct write {path}"):
                res = api_write(page, method, path, body)
                assert res["status"] == 403, f"VIEWER {method} {path} -> {res}"
                # The role check rejected it - not a CSRF failure of the helper (that would prove nothing).
                code = ((res.get("data") or {}).get("error") or {}).get("code")
                assert code in {"READ_ONLY_ROLE", "ADMIN_REQUIRED"}, f"VIEWER {method} {path} -> {res}"
    finally:
        ctx.close()
    assert not watch.problems, "browser problems:\n" + watch.report()


def test_server_log_has_no_tracebacks(server: Server) -> None:
    text = server.log_text()
    errors = [ln for ln in text.splitlines() if re.search(r'" 5\d\d\b', ln)]
    assert "Traceback" not in text, "server logged a traceback:\n" + server.log_tail(120)
    assert not errors, "server answered 5xx:\n" + "\n".join(errors[:20])
    if _evidence_enabled():
        _write_evidence_readme(browser_version=_BROWSER_VERSION.get("v", "chromium"))


_BROWSER_VERSION: dict[str, str] = {}


@pytest.fixture(scope="module", autouse=True)
def _record_browser_version(browser: Any) -> None:
    _BROWSER_VERSION["v"] = f"Chromium {browser.version} (Playwright, headless)"


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"


def _write_evidence_readme(*, browser_version: str) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    head = _git("rev-parse", "HEAD") or "UNKNOWN"
    dirty = bool(_git("status", "--porcelain"))
    rows = "\n".join(
        f"| {stem} | `{route}` | [1440]({stem}-1440.jpg) | [390]({stem}-390.jpg) | [dark]({stem}-dark.jpg) |"
        for stem, route in CURATED.items()
    )
    text = f"""# Control Center UI evidence (real browser)

Captured by `tests/e2e/test_ui_smoke.py` (`AEOS_UI_EVIDENCE=1`) against the real server
(`uvicorn aeos.api.app:create_app --factory`) on 127.0.0.1 with a fresh data directory.

| Field | Value |
|---|---|
| Commit | `{head}`{" + uncommitted working-tree changes (the commit that adds these files)" if dirty else ""} |
| Date | {dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")} |
| OS | {platform.system()} {platform.release()} ({platform.version()}) |
| Browser | {browser_version} |
| Viewports | 1440x900 (desktop) and 390x844 (mobile): full page, cut at {EVIDENCE_MAX_HEIGHT} px; dark (`data-theme="dark"`, 1440x900): first screen. JPEG q{EVIDENCE_QUALITY}; lossless full-page PNGs of every route stay in the test's temp dir |
| Data | seeded **DESIGN** catalog (`aeos seed`) + read-only `aeos inventory reconcile` probes with claude/opencode/bao deliberately absent + labelled **DEMO** runs (`aeos work demo-fixtures`). **Not live usage**: no worker ran, no model was called, no external account was connected. |

Evidence level: E1 visual smoke (pages render without page/console errors or 5xx; labels visible).
It does not prove functional readiness of any component; DESIGN/DEMO rows remain DESIGN/DEMO.

| Page | Route | Desktop | Mobile | Dark |
|---|---|---|---|---|
{rows}

## Reproduce

```
pip install -e .[dev]                  # includes playwright
python -m playwright install chromium
python -m pytest tests/e2e/test_ui_smoke.py -m integration -q            # screenshots to a temp dir
AEOS_UI_EVIDENCE=1 python -m pytest tests/e2e/test_ui_smoke.py -m integration -q   # refresh this folder
```

The test builds `frontend/dist` when it is missing or older than `frontend/src`, starts its own
server on a free 127.0.0.1 port with a fresh data directory, and never calls Claude, OpenCode,
OpenBao, GitHub, Google or Atlassian: claude/opencode/bao/mmdc point at absent paths, OpenBao at a
closed loopback port, and no GitHub/Google/Atlassian account or credential is configured.
"""
    (EVIDENCE_DIR / "README.md").write_text(text, encoding="utf-8", newline="\n")
