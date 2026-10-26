"""Trusted screenshot collector (master §29 "screenshot dari aplikasi berjalan pada commit/environment tertentu,
tanpa secret/PII yang tidak perlu", §10 Documentation & Visuals, §16 Diagram/Visual Author).

Captures pages of a project's *running* app on its registered deployment target (Integration Registry provider
``deployment-target``, purpose ``staging`` or ``production``) and stores each PNG as a trusted ``SCREENSHOT``
EvidenceArtifact (collector origin; commit, environment, route and viewport recorded). Documents and the
screenshot manifest then *reference* that evidence by id (docs_engine.visuals.screenshot_manifest).

Fail closed, observe-only:

* Before anything is captured the target's health endpoint must report the expected commit and healthy (the
  rule of the deployment observer, delivery facade ``classify_deployment_health``, which also accepts the DLY AEOS
  ``/api/health`` shape while ``controller`` runs the ACTIVE clean release); otherwise NOTHING is captured
  (``Blocked`` SCREENSHOT_COMMIT_NOT_PROVEN): a screenshot of an unknown build proves nothing. The target and its
  ``health_path`` are the observer's checked target (an off-host path is refused, HEALTH_PATH_INVALID).
* A staging capture is a staging validation operation: it runs only while ``CAN_VALIDATE_STAGING`` is READY for
  the project (``Blocked`` CAPABILITY_NOT_READY otherwise, §08).
* Each page is loaded with one headless browser GET (no login, no form input, no clicks). A final URL on
  another origin (redirect) is refused, and so is an error page (HTTP 4xx/5xx or no main response). Whether an
  authenticated, read-only capture is allowed is the owner's POLICY decision (``authenticated_capture_status``);
  this build has no authenticated capture path, so the collector never logs in whatever the decision says.
* The page text - with the values of visible form fields, placeholders and image alt text - is checked before the
  image is stored: a credential pattern or personal data refuses the capture (``PolicyDenied``). An image cannot
  be redacted, so it is never stored in that case. Text drawn inside images is not examined (no OCR).
* Playwright/Chromium are optional dependencies: when absent the capture is ``Blocked`` with the install
  remedy, never a placeholder image.

AEOS never deploys or changes the target. Services take a Session and never commit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.core.pii import contains_pii
from aeos.core.redaction import contains_secret, redact_text
from aeos.core.timeutil import utcnow
from aeos.models.portfolio import Project

COLLECTOR = "screenshot-collector"
ENVIRONMENTS = ("staging", "production")
VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
MAX_ROUTES = 20
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_ROUTE = re.compile(r"^/(?!/)[A-Za-z0-9._~/%-]{0,200}$")
INSTALL_REMEDY = "python -m pip install playwright==1.64.0 && python -m playwright install chromium"
# The collector never logs in: pages behind a login (e.g. DLY AEOS' own Control Center routes) would only show the
# login page. Whether an authenticated, read-only capture is allowed is the owner's POLICY decision (preset
# screenshot_authenticated_capture, read through the policy facade). This build has no authenticated capture path, so
# every status - ALLOWED included - captures unauthenticated (``capture_mode``); UNDECIDED/DENIED stay so by rule.
CAPTURE_MODE = "UNAUTHENTICATED"
_RECORD_HINT = ("Catat di Keputusan -> Catat keputusan manusia -> POLICY -> preset 'Capture screenshot terautentikasi "
                "read-only', atau: aeos policy decide --kind POLICY --authenticated-capture ALLOWED|DENIED "
                "--capture-scope \"<halaman>\" --domain PERSONAL --as <admin> --rationale \"<alasan>\".")
_CAPTURE_NOTES = {
    "UNDECIDED": "Collector tidak pernah login: halaman di balik login (mis. rute Control Center DLY AEOS) hanya akan "
                 "menampilkan halaman login. Capture terautentikasi read-only memerlukan KEPUTUSAN pemilik yang belum "
                 "tercatat; sampai itu hanya halaman publik target yang dapat menjadi bukti SCREENSHOT.",
    "DENIED": "Pemilik memutuskan DENIED: collector tidak login; hanya halaman publik target yang dapat menjadi bukti "
              "SCREENSHOT.",
    "ALLOWED": "Pemilik mengizinkan capture terautentikasi read-only untuk scope yang tercatat, tetapi build ini belum "
               "punya jalur capture terautentikasi: collector tetap tidak login (NOT_BUILT), jadi halaman di balik "
               "login belum dapat menjadi bukti SCREENSHOT.",
}


def authenticated_capture_status(session: Session | None, domain: str | None) -> dict[str, Any]:
    """The owner's decision on authenticated, read-only capture (ALLOWED / DENIED / UNDECIDED) and what the collector
    actually does: always ``capture_mode`` UNAUTHENTICATED in this build (fail closed)."""
    from aeos.policy import facade as policy

    decision = policy.authenticated_screenshot_capture(session, domain)
    status = str(decision["status"])
    return {**decision, "capture_mode": CAPTURE_MODE, "authenticated_capture_built": False,
            "note": _CAPTURE_NOTES.get(status, _CAPTURE_NOTES["UNDECIDED"]), "record_hint": _RECORD_HINT}


@dataclass(frozen=True)
class PageCapture:
    final_url: str
    text: str
    png: bytes
    http_status: int | None


# (url, viewport) -> PageCapture. The default drives headless Chromium through Playwright.
CaptureFn = Callable[[str, tuple[int, int]], PageCapture]

# Text the screenshot can show that ``innerText`` does not contain: the values of visible form fields (a prefilled
# token or e-mail address), placeholders and image alt text. It is checked together with the page text. Text drawn
# inside an image (pixels) is not examined - there is no OCR - so that case stays NOT-PROVEN.
VISIBLE_FIELD_TEXT_JS = (
    "() => Array.from(document.querySelectorAll("
    "'input:not([type=hidden]), textarea, select, [placeholder], img[alt]'))"
    ".map(e => [e.value, e.getAttribute('placeholder'), e.getAttribute('alt')]"
    ".filter(v => typeof v === 'string' && v.trim()).join(' '))"
    ".filter(Boolean).join('\\n')"
)
TEXT_CHECKED = ("no credential pattern / personal data in the page text, visible form-field values, placeholders "
                "or image alt text (text drawn inside images is not examined)")


def playwright_capture(url: str, viewport: tuple[int, int]) -> PageCapture:  # pragma: no cover - real browser
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise Blocked("Playwright is not installed; screenshots cannot be captured",
                      code="SCREENSHOT_BROWSER_MISSING", details={"remedy": INSTALL_REMEDY}) from exc
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]},
                                              accept_downloads=False, service_workers="block")
                page = context.new_page()
                resp = page.goto(url, wait_until="networkidle", timeout=30_000)
                text = page.inner_text("body") if page.query_selector("body") else ""
                # the picture also shows prefilled field values and placeholders, which innerText leaves out
                text = "\n".join(t for t in (text, str(page.evaluate(VISIBLE_FIELD_TEXT_JS) or "")) if t)
                png = page.screenshot(type="png", full_page=False)
                return PageCapture(final_url=page.url, text=text, png=png,
                                   http_status=resp.status if resp is not None else None)
            finally:
                browser.close()
    except PlaywrightError as exc:
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            raise Blocked("Chromium for Playwright is not installed", code="SCREENSHOT_BROWSER_MISSING",
                          details={"remedy": INSTALL_REMEDY}) from exc
        raise Blocked(f"page could not be captured: {redact_text(msg)[:300]}", code="SCREENSHOT_CAPTURE_FAILED") \
            from exc


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _verify_commit(target: dict[str, Any], sha: str, client: httpx.Client | None) -> dict[str, Any]:
    from aeos.delivery import facade as delivery

    url = target["base_url"] + target["health_path"]
    owns = client is None
    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    status_code: int | None = None
    body: Any = None
    try:
        resp = http.get(url, headers={"Accept": "application/json", "User-Agent": "dly-aeos-screenshot-collector"})
        status_code = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
    except httpx.HTTPError as exc:
        raise Blocked(f"deployment target unreachable ({type(exc).__name__}); nothing was captured",
                      code="SCREENSHOT_COMMIT_NOT_PROVEN") from exc
    finally:
        if owns:
            http.close()
    verdict, reason, reported = delivery.classify_deployment_health(sha, status_code, body)
    if verdict != "PASS":
        raise Blocked(f"the target does not prove it runs commit {sha[:12]} healthy ({reason}); nothing was "
                      "captured", code="SCREENSHOT_COMMIT_NOT_PROVEN", details={"health": verdict})
    return {"health_url": url, "reported_commit": reported, "health_http_status": status_code}


def capture_screenshots(session: Session, project: Project, *, environment: str, commit: str, routes: list[str],
                        actor: str, actor_type: str = "user", viewport: str = "desktop",
                        capture: CaptureFn | None = None, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Capture *routes* of the project's running app on its *environment* target at *commit*; one trusted
    SCREENSHOT EvidenceArtifact per route. Refuses before storing anything when a check fails."""
    from aeos.delivery import facade as delivery
    from aeos.verification import facade as verification

    if environment not in ENVIRONMENTS:
        raise ValidationFailed(f"environment must be one of {list(ENVIRONMENTS)}", code="ENVIRONMENT_INVALID")
    if not _SHA.match(commit or ""):
        raise ValidationFailed("commit sha required (7-64 hex characters)", code="SHA_INVALID")
    if viewport not in VIEWPORTS:
        raise ValidationFailed(f"viewport must be one of {sorted(VIEWPORTS)}", code="VIEWPORT_INVALID")
    clean = list(dict.fromkeys(r.strip() for r in routes if r and r.strip()))
    if not clean or len(clean) > MAX_ROUTES:
        raise ValidationFailed(f"give 1-{MAX_ROUTES} route paths", code="ROUTES_INVALID")
    bad = [r for r in clean if not _ROUTE.match(r)]
    if bad:
        raise ValidationFailed("routes must be same-origin absolute paths such as /dashboard", code="ROUTES_INVALID",
                               details={"invalid": bad[:5]})
    # one checked target for the observer and the collector: a stored health_path that is not an absolute path
    # (e.g. '@other.host/health') is refused here (HEALTH_PATH_INVALID) before any request is made
    target = delivery.deployment_target(session, project, environment)
    if target is None:
        raise Blocked(f"no {environment} deployment target is registered for this project: register it in the "
                      f"Control Center (Portofolio -> project -> Integrasi -> Deployment target {environment}; "
                      f"API: PUT /api/projects/{project.id}/integrations provider=deployment-target "
                      f"purpose={environment})", code="NO_DEPLOYMENT_TARGET")
    if environment == "staging":
        # §08: capturing the staging app is a staging validation operation - only while CAN_VALIDATE_STAGING is READY
        delivery.require_staging_gate(session, project)
    health = _verify_commit(target, commit, client)
    shoot = capture or playwright_capture
    size = VIEWPORTS[viewport]
    captured: list[tuple[str, str, PageCapture]] = []
    for route in clean:
        url = target["base_url"] + route
        page = shoot(url, size)
        if _origin(page.final_url) != _origin(target["base_url"]):
            raise PolicyDenied(f"{route} left the target origin; nothing was stored", code="SCREENSHOT_OFF_ORIGIN")
        if page.http_status is None or not 200 <= page.http_status < 400:
            # an error page (404/500, or no main response at all) is not a screenshot of the running app
            raise Blocked(f"{route} answered HTTP {page.http_status if page.http_status is not None else 'none'}; "
                          "nothing was stored", code="SCREENSHOT_PAGE_ERROR",
                          details={"route": route, "http_status": page.http_status})
        if contains_secret(page.text) or contains_pii(page.text):
            # the image cannot be redacted: refuse instead of storing a picture of a secret / personal data
            raise PolicyDenied(f"{route} shows a credential pattern or personal data; nothing was stored (§29)",
                               code="SCREENSHOT_SENSITIVE_CONTENT")
        if not page.png:
            raise Blocked(f"{route} produced no image", code="SCREENSHOT_CAPTURE_FAILED")
        captured.append((route, url, page))
    out = []
    for route, url, page in captured:
        ev = verification.collect_evidence(
            session, kind="SCREENSHOT", origin="collector", collector_identity=f"{COLLECTOR}:{environment}",
            claim=redact_text(f"screenshot {environment} {route} at {commit[:12]} ({size[0]}x{size[1]})")[:400],
            content=page.png, evidence_level="E3", subject_type="commit", subject_id=commit.lower(),
            project_id=project.id, domain=project.domain,
            environment={"commit": commit.lower(), "name": environment, "route": route, "url": url,
                         "viewport": f"{size[0]}x{size[1]}", "page_http_status": page.http_status,
                         "captured_at": utcnow().isoformat(), "integration_id": target["integration_id"],
                         "text_checked": TEXT_CHECKED, **health},
        )
        out.append({"evidence_id": ev.id, "route": route, "url": url, "commit": commit.lower(),
                    "environment": environment, "viewport": f"{size[0]}x{size[1]}"})
    audit.record(session, actor_type=actor_type, actor_id=actor, action="docs.screenshot.capture", result="SUCCESS",
                 target_type="integration", target_id=target["integration_id"], project_id=project.id,
                 domain=project.domain, details={"environment": environment, "commit": commit.lower(),
                                                 "routes": clean, "evidence_ids": [o["evidence_id"] for o in out],
                                                 # the owner's decision as it stood, and what the collector did
                                                 "authenticated_capture": authenticated_capture_status(
                                                     session, project.domain)["status"],
                                                 "capture_mode": CAPTURE_MODE})
    return out
