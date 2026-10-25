"""UI state + accessibility smoke of a built frontend bundle (§16 UX/UI Designer and Frontend Engineer
"browser test, visual evidence, a11y", §31 G3 "Browser/UI/a11y"; W6-043/W6-044).

Run by the verifier through its runner as ``python -c <this file> <dist dir> <out dir> <browsers path or ->
[<options json or ->]``. It serves the candidate's built bundle from a loopback-only static server, opens it in
headless Chromium (Playwright from the controller environment; outbound traffic goes to a dead proxy, loopback
only) and renders each UI state by answering the bundle's ``/api`` requests itself:

* ``loading``         - API requests are held (never answered before the screenshot);
* ``error``           - every API request answers 500 with the standard error envelope;
* ``unauthenticated`` - every API request answers 401 (session missing);
* ``empty``           - every API request answers 200 with an empty list envelope (``mode: MOCK``), or - with the
  ``live_app`` option (DLY AEOS layout) - is answered by the candidate's own app on an empty, throwaway database
  with a signed-in throwaway admin, so every endpoint has its real shape and no data.

Options (JSON object): ``fixtures`` ``{"<GET path>": <body>}`` answers those exact paths with 200 in every state
except ``unauthenticated`` (e.g. the session endpoint, so loading/error render for a signed-in user instead of the
login screen); ``live_app`` ``"aeos"`` enables the live empty state (the driver then runs candidate code: it is
executed through the verifier runner, inside the tamper window, with the candidate backend on ``PYTHONPATH``).

A render that raises an uncaught script error is recorded with its state (``page_errors``) and who answered the
API (``answered_by``); the verifier never reads such a render as evidence of that state.

Per state and viewport (desktop 1280x800, mobile 390x844) it writes a full-page PNG and runs the verifier's
accessibility probe: a fixed subset of WCAG 2.x checks (document language, title, image alternatives,
accessible names of controls, form labels, duplicate referenced ids, positive tabindex, reflow at 390 px). It is
NOT axe-core and NOT a full WCAG audit: colour contrast, focus order and screen-reader behaviour stay NOT-PROVEN.
Prints one JSON line; never raises to the verifier.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import threading
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

STATES = ("loading", "error", "unauthenticated", "empty")
VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "mobile": {"width": 390, "height": 844}}
DEAD_PROXY = "http://127.0.0.1:9"
SETTLE_MS = 1500
RULES = ("html-has-lang 3.1.1", "document-title 2.4.2", "image-alt 1.1.1", "control-name 4.1.2",
         "form-label 1.3.1/4.1.2", "duplicate-id-referenced 4.1.2", "tabindex-positive 2.4.3", "reflow 1.4.10")
LIVE_USER = "verifier-ui"

A11Y_PROBE = r"""
() => {
  const out = [];
  const describe = (el) => {
    if (!el || !el.tagName) return "document";
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    const cls = (el.getAttribute("class") || "").trim().split(/\s+/).filter(Boolean).slice(0, 2);
    if (cls.length) s += "." + cls.join(".");
    return s.slice(0, 120);
  };
  const add = (rule, wcag, el, detail) => {
    if (out.length < 200) out.push({rule, wcag, target: describe(el), detail: (detail || "").slice(0, 120)});
  };
  const hidden = (el) => {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      if (e.getAttribute("aria-hidden") === "true" || e.hidden) return true;
      const cs = getComputedStyle(e);
      if (cs.display === "none" || cs.visibility === "hidden") return true;
    }
    return false;
  };
  const text = (el) => (el.textContent || "").replace(/\s+/g, " ").trim();
  const byIds = (ids) => ids.split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean)
    .map(text).join(" ").trim();
  const name = (el) => {
    const lab = el.getAttribute("aria-labelledby");
    if (lab && byIds(lab)) return byIds(lab);
    const al = (el.getAttribute("aria-label") || "").trim();
    if (al) return al;
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map(text).join(" ").trim();
      if (t) return t;
    }
    if (el.tagName !== "INPUT" && el.tagName !== "SELECT" && el.tagName !== "TEXTAREA") {
      const t = text(el);
      if (t) return t;
      const img = el.querySelector("img[alt]");
      if (img && img.getAttribute("alt").trim()) return img.getAttribute("alt").trim();
      const svgTitle = el.querySelector("svg title");
      if (svgTitle && text(svgTitle)) return text(svgTitle);
    }
    const title = (el.getAttribute("title") || "").trim();
    if (title) return title;
    const ph = (el.getAttribute("placeholder") || "").trim();
    if (ph) return ph;
    if (el.tagName === "INPUT" && ["submit", "reset", "button"].includes((el.type || "").toLowerCase())
        && (el.value || "").trim()) return el.value.trim();
    return "";
  };
  if (!(document.documentElement.getAttribute("lang") || "").trim())
    add("html-has-lang", "3.1.1", document.documentElement, "");
  if (!(document.title || "").trim()) add("document-title", "2.4.2", document.documentElement, "");
  document.querySelectorAll("img").forEach((el) => {
    if (hidden(el)) return;
    const role = el.getAttribute("role");
    if (!el.hasAttribute("alt") && role !== "presentation" && role !== "none") add("image-alt", "1.1.1", el, "");
  });
  document.querySelectorAll("button, a[href], [role=button], [role=link], [role=tab], [role=menuitem]")
    .forEach((el) => { if (!hidden(el) && !name(el)) add("control-name", "4.1.2", el, ""); });
  document.querySelectorAll("input, select, textarea").forEach((el) => {
    if (hidden(el)) return;
    const t = (el.getAttribute("type") || "").toLowerCase();
    if (["hidden", "submit", "reset", "button", "image"].includes(t)) return;
    if (!name(el)) add("form-label", "1.3.1/4.1.2", el, "");
  });
  const seen = new Map();
  document.querySelectorAll("[id]").forEach((el) => seen.set(el.id, (seen.get(el.id) || 0) + 1));
  const referenced = new Set();
  document.querySelectorAll("[aria-labelledby],[aria-describedby],[aria-controls],label[for]").forEach((el) => {
    ["aria-labelledby", "aria-describedby", "aria-controls"].forEach((a) =>
      (el.getAttribute(a) || "").split(/\s+/).filter(Boolean).forEach((i) => referenced.add(i)));
    if (el.htmlFor) referenced.add(el.htmlFor);
  });
  referenced.forEach((i) => { if ((seen.get(i) || 0) > 1) add("duplicate-id-referenced", "4.1.2",
    document.getElementById(i), i); });
  document.querySelectorAll("[tabindex]").forEach((el) => {
    if (parseInt(el.getAttribute("tabindex"), 10) > 0) add("tabindex-positive", "2.4.3", el, "");
  });
  return out;
}
"""
OVERFLOW_PROBE = "() => document.documentElement.scrollWidth - window.innerWidth"


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Static files of the bundle only (``directory`` confines translate_path); unknown paths -> index.html."""

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if not Path(self.translate_path(path)).is_file():
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, format: str, *args: Any) -> None:
        return


class _LiveApp:
    """The candidate's own DLY AEOS app on an empty throwaway database, signed in as a throwaway admin."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["AEOS_DATA_DIR"] = str(data_dir)
        from fastapi.testclient import TestClient

        from aeos.api.app import create_app
        from aeos.core import config as config_mod
        from aeos.core.db import create_all, init_engine, session_factory
        from aeos.identity import service as identity

        config_mod.reset_settings_cache()
        settings = config_mod.get_settings()
        self.engine = init_engine(settings)
        create_all(self.engine)
        password = secrets.token_urlsafe(24)  # throwaway: lives only in this process and the throwaway DB
        with session_factory()() as s:
            identity.bootstrap_admin(s, username=LIVE_USER, password=password)
            s.commit()
        self.client = TestClient(create_app(settings, testing=True, init_db=False))
        self.client.__enter__()
        r = self.client.post("/api/auth/login", json={"username": LIVE_USER, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"live app login answered {r.status_code}")
        self.csrf = str(r.json().get("csrf_token") or "")
        self.client.post("/api/session/context", json={"domain": "PERSONAL"}, headers={"X-CSRF-Token": self.csrf})

    def fulfill(self, route: Any) -> None:
        req = route.request
        if "text/event-stream" in (req.headers.get("accept") or ""):
            route.fulfill(status=204, body="")  # the event stream never completes through a test client
            return
        parts = urlsplit(req.url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {"X-CSRF-Token": self.csrf}
        if req.headers.get("content-type"):
            headers["Content-Type"] = req.headers["content-type"]
        resp = self.client.request(req.method, target, content=req.post_data_buffer, headers=headers)
        route.fulfill(status=resp.status_code, content_type=resp.headers.get("content-type", "application/json"),
                      body=resp.content)

    def close(self) -> None:
        try:
            self.client.__exit__(None, None, None)
        finally:
            self.engine.dispose()


def _respond(state: str, held: list[Any], fixtures: dict[str, Any], live: _LiveApp | None, route: Any) -> None:
    if state == "empty" and live is not None:
        live.fulfill(route)
        return
    path = urlsplit(route.request.url).path
    if state != "unauthenticated" and route.request.method == "GET" and path in fixtures:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(fixtures[path]))
        return
    if state == "loading":
        held.append(route)
        return
    if state == "error":
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"error": {"code": "INTERNAL", "message": "verifier UI state: server error",
                                                 "details": {}}}))
    elif state == "unauthenticated":
        route.fulfill(status=401, content_type="application/json",
                      body=json.dumps({"error": {"code": "NOT_AUTHENTICATED", "message": "no session",
                                                 "details": {}}}))
    else:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"items": [], "source": "verifier-ui-state", "checked_at": None,
                                       "mode": "MOCK"}))


def _options(argv: list[str]) -> dict[str, Any]:
    if len(argv) <= 4 or argv[4] in ("", "-"):
        return {}
    try:
        data = json.loads(argv[4])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str], out: dict[str, Any]) -> None:
    dist, out_dir, browsers = Path(argv[1]), Path(argv[2]), argv[3] if len(argv) > 3 else "-"
    options = _options(argv)
    raw_fixtures = options.get("fixtures")
    fixtures: dict[str, Any] = raw_fixtures if isinstance(raw_fixtures, dict) else {}
    if browsers and browsers != "-":
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    if not (dist / "index.html").is_file():
        out.update(status="NOT_RUN", reason="built bundle has no index.html")
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        out.update(status="NOT_RUN", reason="playwright is not installed in the verifier environment")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    live: _LiveApp | None = None
    if options.get("live_app") == "aeos":
        try:
            live = _LiveApp(out_dir / "live-data")
            out["live_app"] = "candidate app, empty database, throwaway admin session"
        except Exception as exc:  # the empty state then uses the generic answers (stated)
            out["live_app_error"] = type(exc).__name__ + ": " + str(exc)[:300]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(dist)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    results: list[dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, proxy={"server": DEAD_PROXY,
                                                                  "bypass": "127.0.0.1,localhost"})
            except Exception as exc:
                out.update(status="NOT_RUN", reason="chromium could not be launched: " + str(exc)[:200])
                return
            out["browser"] = "chromium " + str(browser.version)
            try:
                for state in STATES:
                    answered_by = ("candidate-app" if state == "empty" and live is not None else
                                   "verifier-fixtures" if fixtures and state != "unauthenticated" else "verifier")
                    for vp_name, vp in VIEWPORTS.items():
                        held: list[Any] = []
                        page: Any = browser.new_page(viewport=vp)  # type: ignore[arg-type]
                        page_errors: list[str] = []
                        console_errors: list[str] = []
                        page.on("pageerror", lambda e, sink=page_errors: sink.append(str(e)[:300]))
                        page.on("console", lambda m, sink=console_errors: sink.append(m.text[:300])
                                if m.type == "error" else None)
                        page.route("**/api/**", partial(_respond, state, held, fixtures, live))
                        page.goto(base + "/", wait_until="load", timeout=30_000)
                        page.wait_for_timeout(SETTLE_MS)
                        shot = f"{state}-{vp_name}.png"
                        page.screenshot(path=str(out_dir / shot), full_page=True)
                        violations = list(page.evaluate(A11Y_PROBE) or [])
                        if vp_name == "mobile":
                            overflow = int(page.evaluate(OVERFLOW_PROBE) or 0)
                            if overflow > 1:
                                violations.append({"rule": "reflow", "wcag": "1.4.10", "target": "document",
                                                   "detail": f"horizontal overflow of {overflow}px at 390px"})
                        results.append({"state": state, "viewport": vp_name, "screenshot": shot,
                                        "title": page.title()[:200], "violations": violations[:100],
                                        "page_errors": page_errors[:20], "console_errors": len(console_errors),
                                        "answered_by": answered_by})
                        for route in held:
                            try:
                                route.abort()
                            except Exception:  # noqa: S110 - page already closed the request
                                pass
                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        if live is not None:
            live.close()
    out.update(status="RAN", base_path="/", states=results, rules=list(RULES), fixtures=sorted(fixtures),
               violations=sum(len(r["violations"]) for r in results))


if __name__ == "__main__":
    result: dict[str, Any] = {"status": "NOT_RUN"}
    try:
        main(sys.argv, result)
    except Exception as exc:  # reported, never raised to the verifier
        result.update(status="ERROR", reason=type(exc).__name__ + ": " + str(exc)[:300])
    print(json.dumps(result, default=str))
