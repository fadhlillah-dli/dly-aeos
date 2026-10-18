"""Research & Technology Registry service (§45, §47, §50, ADR-0083).

* Registry rows are candidates/references — never installation/connection/usage evidence.
* "Check Upstream" is manual: for GitHub-hosted entries it reads public repo + latest release
  metadata (GitHub REST API, no credentials, only the repo path leaves the machine). A rename,
  transfer or redirect flags the identity as CHANGED and trust is NOT inherited (lifecycle drops
  back to DISCOVERED). "Update available" is never "compatible"; nothing is installed, updated or
  activated, and no policy/route/model changes.
* Add-by-URL creates a DISCOVERED entry only (no network call, no install, no data sent).
* Entries added from an OFFICE session never reach public research endpoints (§39, §47).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aeos import __version__
from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import Blocked, NotFound, PolicyDenied, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.inventory import ComponentReference
from aeos.models.research import ResearchEntry
from aeos.research import catalog
from aeos.research.identity import IdentityError, canonical_identity, github_repo, safe_public_url

GITHUB_API = "https://api.github.com"
STALE_AFTER = timedelta(days=30)
LIFECYCLES = ("DISCOVERED", "SOURCE_VERIFIED", "CONTROLLED_EVAL", "CANARY", "RECOMMENDED_FOR_PROMOTION",
              "REJECTED", "DEFERRED", "ENABLED", "REFERENCE_ONLY")
TRUSTED_LIFECYCLES = ("SOURCE_VERIFIED", "CONTROLLED_EVAL", "CANARY", "RECOMMENDED_FOR_PROMOTION")
REFERENCE_CATEGORY = "source-reference"
PLATFORM_ENTRY_KEY = "tool:dly-aeos"
HISTORY_CHECK_EVENTS_KEPT = 50  # routine UPSTREAM_CHECK rows are capped; trust events are never dropped
_DESCRIPTIVE = ("name", "category", "placement", "relevance", "claims", "limitations", "source_refs", "section_refs",
                "evidence_level", "research_date", "release_date", "source_kind")


# --------------------------------------------------------------------------- seed


def _identity_for(urls: tuple[str, ...] | list[str], name: str) -> str:
    for u in urls:
        try:
            return canonical_identity(u)
        except IdentityError:
            continue
    return canonical_identity(name)


def _seed_rows(platform_repo_slug: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for e in catalog.ENTRIES:
        urls = list(e.urls)
        identity_status = e.identity_status
        if e.key == PLATFORM_ENTRY_KEY:
            slug = (platform_repo_slug or "").strip().strip("/")
            if slug and github_repo(f"github:{slug.lower()}") is not None:
                urls = [f"https://github.com/{slug}"]  # from owner configuration, not a guess
            else:
                urls, identity_status = [], catalog.UNV  # §45: repository not given -> UNVERIFIED
        if identity_status == catalog.UNV:
            urls = []  # unresolved identities never get a URL (§45, §50)
        rows.append({
            "entry_key": e.key, "name": e.name, "category": e.category, "placement": e.placement,
            "lifecycle": e.lifecycle, "identity_status": identity_status, "source_kind": e.source_kind,
            "urls": urls, "source_refs": list(e.source_refs), "evidence_level": e.evidence,
            "relevance": e.relevance, "claims": list(e.claims), "limitations": e.limitations,
            "section_refs": list(e.sections), "research_date": catalog.MASTER_RESEARCH_DATE, "release_date": None,
            "identity": _identity_for(urls, e.name), "hints": list(e.component_hints), "group": e.group,
        })
    for r in catalog.REFERENCES:
        rows.append({
            "entry_key": f"ref:{r.sid}", "name": r.sid, "category": REFERENCE_CATEGORY, "placement": r.supports,
            "lifecycle": "REFERENCE_ONLY", "identity_status": catalog.STATED, "source_kind": r.source_kind,
            "urls": list(r.urls), "source_refs": [r.sid], "evidence_level": r.evidence, "relevance": r.supports,
            "claims": [], "limitations": ("Diskusi komunitas: sinyal/hipotesis, bukan bukti terverifikasi (E0)."
                                          if r.evidence == "E0" else
                                          "Dokumentasi/sumber publik: mendukung kemampuan yang didokumentasikan (E1), "
                                          "bukan bukti implementasi DLY AEOS."),
            "section_refs": ["§52"], "research_date": catalog.MASTER_RESEARCH_DATE, "release_date": r.source_date,
            "identity": _identity_for(r.urls, r.sid), "hints": [], "group": "",
        })
    return rows


def seed_registry(session: Session, *, platform_repo_slug: str | None = None) -> dict[str, Any]:
    """Idempotent. Existing rows keep their live fields (lifecycle, identity_status, upstream,
    checked_at); only descriptive text from the master is refreshed."""
    created = updated = 0
    for row in _seed_rows(platform_repo_slug):
        existing = session.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == row["entry_key"]))
        if existing is None:
            e = ResearchEntry(
                entry_key=row["entry_key"], name=row["name"], identity=row["identity"], category=row["category"],
                urls=row["urls"], source_refs=row["source_refs"], release_date=row["release_date"],
                research_date=row["research_date"], claims=row["claims"], evidence_level=row["evidence_level"],
                relevance=row["relevance"], placement=row["placement"], limitations=row["limitations"],
                lifecycle=row["lifecycle"], source_kind=row["source_kind"], identity_status=row["identity_status"],
                upstream_status="NOT_CHECKED", upstream={"component_hints": row["hints"], "group": row["group"]},
                section_refs=row["section_refs"], mode="DESIGN", added_by="seed",
            )
            session.add(e)
            created += 1
            continue
        changed = False
        for f in _DESCRIPTIVE:
            if getattr(existing, f) != row[f]:
                setattr(existing, f, row[f])
                changed = True
        if (row["entry_key"] == PLATFORM_ENTRY_KEY and existing.upstream_status == "NOT_CHECKED"
                and (existing.urls or []) != row["urls"]):
            # The platform repo comes from owner configuration; follow it until a live check exists.
            existing.urls, existing.identity = row["urls"], row["identity"]
            existing.identity_status = row["identity_status"]
            changed = True
        if changed:
            updated += 1
    session.flush()
    linked = link_components(session)
    return {"created": created, "updated": updated, "linked_components": linked,
            "total": session.scalar(select(func.count()).select_from(ResearchEntry))}


def link_components(session: Session) -> int:
    """Link entries to inventory component ids when names/identities match (never invent ids)."""
    comps = list(session.scalars(select(ComponentReference)))
    if not comps:
        return 0
    by_id = {c.component_id: c for c in comps}
    by_name = {c.name.strip().lower(): c for c in comps}
    by_identity: dict[str, ComponentReference] = {}
    for c in comps:
        if c.source_url:
            try:
                by_identity.setdefault(canonical_identity(c.source_url), c)
            except IdentityError:
                pass
    linked = 0
    for e in session.scalars(select(ResearchEntry).where(ResearchEntry.component_id.is_(None),
                                                          ResearchEntry.category != REFERENCE_CATEGORY)):
        hit: ComponentReference | None = None
        for h in (e.upstream or {}).get("component_hints", []) or []:
            if h in by_id:
                hit = by_id[h]
                break
        hit = hit or by_name.get(e.name.strip().lower())
        if hit is None and e.identity and not e.identity.startswith("name:"):
            hit = by_identity.get(e.identity)
        if hit is not None:
            e.component_id = hit.component_id
            linked += 1
    session.flush()
    return linked


# --------------------------------------------------------------------------- views


def freshness(e: ResearchEntry) -> str:
    if e.checked_at is None:
        return "NOT_CHECKED"
    checked = ensure_aware(e.checked_at)
    assert checked is not None
    return "STALE" if utcnow() - checked > STALE_AFTER else "FRESH"


def entry_view(e: ResearchEntry, *, detail: bool = False) -> dict[str, Any]:
    v: dict[str, Any] = {
        "id": e.id, "entry_key": e.entry_key, "name": e.name, "identity": e.identity, "category": e.category,
        "urls": e.urls or [], "source_refs": e.source_refs or [], "evidence_level": e.evidence_level,
        "lifecycle": e.lifecycle, "placement": e.placement, "identity_status": e.identity_status,
        "upstream_status": e.upstream_status, "checked_at": e.checked_at.isoformat() if e.checked_at else None,
        "freshness": freshness(e), "research_date": e.research_date, "release_date": e.release_date,
        "component_id": e.component_id, "source_kind": e.source_kind, "mode": e.mode,
        "section_refs": e.section_refs or [], "is_reference": e.category == REFERENCE_CATEGORY,
        "latest_tag": (e.upstream or {}).get("latest_tag"),
        "update_available": (e.upstream or {}).get("update_available"),
        "checkable": (github_repo(e.identity or "") is not None and bool(e.urls)
                      and e.submitted_domain != Domain.OFFICE.value),
        "installed_or_active": "NOT_IMPLIED",  # §38: research lifecycle is not an inventory dimension
    }
    if detail:
        v.update({
            "relevance": e.relevance, "claims": e.claims or [], "limitations": e.limitations,
            "data_terms": e.data_terms, "compatibility": e.compatibility, "test_results": e.test_results or [],
            "maintainer": e.maintainer, "license": e.license, "source_version": e.source_version,
            "upstream": {k: v2 for k, v2 in (e.upstream or {}).items() if k != "component_hints"},
            "history": e.history or [], "owner_decision_id": e.owner_decision_id, "superseded_by": e.superseded_by,
            "added_by": e.added_by, "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    return v


def _visible(q: Any, domain: str | None) -> Any:
    return q.where(or_(ResearchEntry.submitted_domain.is_(None), ResearchEntry.submitted_domain == domain))


def list_entries(session: Session, *, domain: str | None, lifecycle: str | None = None, category: str | None = None,
                 evidence: str | None = None, q: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    stmt = _visible(select(ResearchEntry), domain).order_by(ResearchEntry.category, ResearchEntry.name)
    if lifecycle:
        stmt = stmt.where(ResearchEntry.lifecycle == lifecycle.upper())
    if category:
        stmt = stmt.where(ResearchEntry.category == category)
    if evidence:
        stmt = stmt.where(ResearchEntry.evidence_level == evidence.upper())
    if kind == "reference":
        stmt = stmt.where(ResearchEntry.category == REFERENCE_CATEGORY)
    elif kind == "candidate":
        stmt = stmt.where(ResearchEntry.category != REFERENCE_CATEGORY)
    rows = list(session.scalars(stmt))
    if q:
        needle = q.strip().lower()
        rows = [e for e in rows if needle in " ".join(
            [e.name, e.identity or "", e.placement, " ".join(e.urls or []), " ".join(e.source_refs or [])]).lower()]
    if kind == "reference":
        rows.sort(key=lambda e: e.name)
    return [entry_view(e) for e in rows]


def get_entry(session: Session, entry_id: str, *, domain: str | None) -> ResearchEntry:
    e = session.get(ResearchEntry, entry_id)
    if e is None or (e.submitted_domain is not None and e.submitted_domain != domain):
        raise NotFound("research entry not found")
    return e


def find_by_identity(session: Session, identity: str, *, domain: str | None) -> ResearchEntry | None:
    """Prefer a candidate entry over a reference entry for the same identity."""
    stmt = _visible(select(ResearchEntry).where(ResearchEntry.identity == identity), domain)
    rows = list(session.scalars(stmt))
    rows.sort(key=lambda e: (e.category == REFERENCE_CATEGORY, e.created_at))
    return rows[0] if rows else None


def resolve_alias(session: Session, identity: str) -> str:
    """Follow a recorded rename/redirect (from Check Upstream) to the observed identity. Used only
    for duplicate linking — never to inherit trust."""
    seen = {identity}
    current = identity
    for _ in range(5):
        row = session.scalar(select(ResearchEntry).where(ResearchEntry.identity == current,
                                                          ResearchEntry.identity_status == "CHANGED"))
        nxt = ((row.upstream or {}).get("observed_identity") if row else None)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        current = nxt
    return current


# --------------------------------------------------------------------------- add by URL


def add_by_url(session: Session, *, url: str, name: str | None, category: str | None, notes: str | None,
               actor_user_id: str, domain: str | None) -> tuple[ResearchEntry, bool]:
    """Create a DISCOVERED entry (no install, no network, no project data). Returns (entry, duplicate)."""
    try:
        identity = canonical_identity(url)
        if identity.startswith("name:"):
            raise IdentityError("a repo/docs URL is required")
        gh = github_repo(identity)
        clean = f"https://github.com/{gh[0]}/{gh[1]}" if gh else safe_public_url(url)
    except IdentityError as exc:
        raise ValidationFailed(str(exc)) from exc
    if (category or "").strip().lower() == REFERENCE_CATEGORY:
        raise ValidationFailed(f"category {REFERENCE_CATEGORY!r} is reserved for the master §52 references")
    existing = find_by_identity(session, identity, domain=domain)
    if existing is not None:
        return existing, True
    display = (name or "").strip() or (gh[1] if gh else identity.split(":", 1)[1][:120])
    e = ResearchEntry(
        entry_key=None, name=redact_text(display)[:200], identity=identity, category=(category or "candidate")[:64],
        urls=[redact_text(clean)], source_refs=[], evidence_level="E0", lifecycle="DISCOVERED",
        placement="Belum ditinjau - ditambahkan manual lewat URL", relevance=redact_text(notes or "")[:4000],
        source_kind="GITHUB_REPO" if gh else "UNKNOWN", identity_status="UNVERIFIED", upstream_status="NOT_CHECKED",
        upstream={}, section_refs=[], submitted_domain=domain, added_by=actor_user_id, mode="LIVE",
        research_date=utcnow().date().isoformat(),
        limitations="Ditambahkan manual; belum ada source review, belum dipasang, tidak ada data proyek yang dikirim.",
    )
    session.add(e)
    session.flush()
    link_components(session)
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="research.entry.add",
                 target_type="research_entry", target_id=e.id, domain=domain,
                 details={"identity": identity, "installed": False, "network": False})
    events.emit(session, source="research", kind="research.entry.added", entity_type="research_entry",
                entity_id=e.id, domain=domain, payload={"identity": identity, "lifecycle": e.lifecycle})
    return e, False


# --------------------------------------------------------------------------- check upstream


class _Fetch:
    def __init__(self, client: httpx.Client):
        self.client = client
        self.redirects: list[str] = []

    def get(self, url: str) -> httpx.Response:
        """GET within api.github.com, following at most 3 redirects manually (host-checked)."""
        current = url
        for _ in range(4):
            resp = self.client.get(current)
            if resp.status_code in (301, 302, 307, 308) and "location" in resp.headers:
                nxt = str(httpx.URL(current).join(resp.headers["location"]))
                if httpx.URL(nxt).host != "api.github.com" or httpx.URL(nxt).scheme != "https":
                    raise Blocked("upstream redirect leaves api.github.com; refused", code="UPSTREAM_REDIRECT_REFUSED")
                self.redirects.append(nxt)
                current = nxt
                continue
            return resp
        raise Blocked("too many upstream redirects", code="UPSTREAM_REDIRECT_LOOP")


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": f"DLY-AEOS-research/{__version__}"},
    )


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _rate_limited(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0")


def check_upstream(session: Session, entry: ResearchEntry, *, actor_type: str, actor_id: str,
                   domain: str | None, client: httpx.Client | None = None) -> dict[str, Any]:
    if entry.submitted_domain == Domain.OFFICE.value or domain == Domain.OFFICE.value:
        raise PolicyDenied("OFFICE context: public upstream lookup is not authorised (no Office data to public research)",
                           code="OFFICE_RESEARCH_DENIED")
    if entry.identity_status == "UNVERIFIED" and not entry.urls:
        raise Blocked("identity UNVERIFIED and no canonical URL; nothing to check (no guessed URLs)",
                      code="IDENTITY_UNVERIFIED")
    gh = github_repo(entry.identity or "")
    if gh is None:
        raise Blocked("automatic upstream check supports GitHub-hosted entries only; check this source manually",
                      code="UPSTREAM_CHECK_NOT_SUPPORTED")
    owner, repo = gh
    now = utcnow()
    own_client = client is None
    http = client or _client()
    fetch = _Fetch(http)
    upstream: dict[str, Any] = {k: v for k, v in (entry.upstream or {}).items() if k in ("component_hints", "group")}
    upstream.update({"checked_at": now.isoformat(), "source": "GitHub REST API (public, unauthenticated)",
                     "compatibility": "UNKNOWN - update tersedia tidak berarti kompatibel; ajukan proposal untuk review"})
    try:
        try:
            resp = fetch.get(f"{GITHUB_API}/repos/{owner}/{repo}")
        except httpx.HTTPError as exc:
            entry.upstream = (entry.upstream or {}) | {"last_error": redact_text(str(exc))[:300],
                                                       "last_attempt_at": now.isoformat()}
            entry.upstream_status = "ERROR"
            return _finish(session, entry, actor_type, actor_id, domain, status="ERROR", touch=False)
        if _rate_limited(resp):
            entry.upstream = (entry.upstream or {}) | {"last_error": "RATE_LIMITED", "last_attempt_at": now.isoformat()}
            entry.upstream_status = "RATE_LIMITED"
            return _finish(session, entry, actor_type, actor_id, domain, status="RATE_LIMITED", touch=False)
        if resp.status_code == 404:
            entry.upstream = upstream | {"http_status": 404}
            entry.upstream_status = "NOT_FOUND"
            entry.identity_status = "NOT_FOUND"
            _demote(entry, reason="upstream repository not found (deleted, private or renamed without redirect)")
            entry.checked_at = now
            return _finish(session, entry, actor_type, actor_id, domain, status="NOT_FOUND")
        if resp.status_code != 200:
            entry.upstream = upstream | {"http_status": resp.status_code}
            entry.upstream_status = "ERROR"
            return _finish(session, entry, actor_type, actor_id, domain, status="ERROR", touch=False)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            entry.upstream = (entry.upstream or {}) | {"last_error": "unparseable GitHub response",
                                                       "last_attempt_at": now.isoformat()}
            entry.upstream_status = "ERROR"
            return _finish(session, entry, actor_type, actor_id, domain, status="ERROR", touch=False)
        full_name = str(data.get("full_name") or "")
        observed = f"github:{full_name.lower()}" if full_name else None
        redirected = bool(fetch.redirects)
        latest_tag: str | None = None
        release: dict[str, Any] | None = None
        try:
            rel = fetch.get(f"{GITHUB_API}/repos/{full_name or f'{owner}/{repo}'}/releases/latest")
            if rel.status_code == 200 and isinstance(rj := _json(rel), dict):
                release = {"tag": rj.get("tag_name"), "name": rj.get("name"), "published_at": rj.get("published_at"),
                           "url": rj.get("html_url"), "prerelease": rj.get("prerelease")}
                latest_tag = rj.get("tag_name")
            elif rel.status_code == 404:
                tags = fetch.get(f"{GITHUB_API}/repos/{full_name or f'{owner}/{repo}'}/tags?per_page=1")
                tj = _json(tags) if tags.status_code == 200 else None
                if isinstance(tj, list) and tj and isinstance(tj[0], dict):
                    latest_tag = tj[0].get("name")
        except httpx.HTTPError as exc:
            upstream["release_error"] = redact_text(str(exc))[:200]
        parent = (data.get("parent") or {}).get("full_name")
        upstream.update({
            "observed_full_name": full_name, "observed_identity": observed, "html_url": data.get("html_url"),
            "archived": bool(data.get("archived")), "disabled": bool(data.get("disabled")),
            "fork": bool(data.get("fork")), "parent": parent, "default_branch": data.get("default_branch"),
            "pushed_at": data.get("pushed_at"), "license_spdx": (data.get("license") or {}).get("spdx_id"),
            "owner": (data.get("owner") or {}).get("login"), "latest_release": release, "latest_tag": latest_tag,
            "redirected": redirected, "redirect_chain": fetch.redirects,
        })
        pinned = entry.source_version
        upstream["update_available"] = (bool(latest_tag) and latest_tag != pinned) if pinned else None
        if not pinned:
            upstream["update_note"] = "Tidak ada versi yang dipin; tag terbaru hanya informasi bertanggal."
        identity_changed = bool(observed) and (observed != entry.identity or redirected)
        if identity_changed:
            status = "IDENTITY_CHANGED"
            entry.identity_status = "CHANGED"
            _demote(entry, reason=f"canonical identity changed: {entry.identity} -> {observed}; trust not inherited")
            entry.history = [*(entry.history or []), {"at": now.isoformat(), "event": "IDENTITY_CHANGED",
                                                       "from": entry.identity, "to": observed,
                                                       "redirected": redirected}]
        elif upstream["fork"]:
            status = "FORK"
            entry.identity_status = "CHANGED"
            _demote(entry, reason=f"repository is a fork of {parent}; not the same as upstream")
        elif upstream["archived"]:
            status = "ARCHIVED"
            entry.identity_status = "CHECKED"
        else:
            status = "OK"
            entry.identity_status = "CHECKED"
        if upstream["license_spdx"] and not entry.license:
            entry.license = str(upstream["license_spdx"])[:64]
        entry.upstream = upstream
        entry.upstream_status = status
        entry.checked_at = now
        return _finish(session, entry, actor_type, actor_id, domain, status=status)
    finally:
        if own_client:
            http.close()


def _cap_history(history: list[Any]) -> list[Any]:
    """Keep every identity/lifecycle/decision event; only routine UPSTREAM_CHECK rows are capped."""
    checks = [i for i, h in enumerate(history) if isinstance(h, dict) and h.get("event") == "UPSTREAM_CHECK"]
    drop = set(checks[:-HISTORY_CHECK_EVENTS_KEPT]) if len(checks) > HISTORY_CHECK_EVENTS_KEPT else set()
    return [h for i, h in enumerate(history) if i not in drop]


def _demote(entry: ResearchEntry, *, reason: str) -> None:
    if entry.lifecycle in TRUSTED_LIFECYCLES:
        entry.history = [*(entry.history or []), {"at": utcnow().isoformat(), "event": "LIFECYCLE_RESET",
                                                   "from": entry.lifecycle, "to": "DISCOVERED", "reason": reason}]
        entry.lifecycle = "DISCOVERED"


def _finish(session: Session, entry: ResearchEntry, actor_type: str, actor_id: str, domain: str | None, *,
            status: str, touch: bool = True) -> dict[str, Any]:
    entry.history = _cap_history([*(entry.history or []),
                                  {"at": utcnow().isoformat(), "event": "UPSTREAM_CHECK", "status": status}])
    session.flush()
    audit.record(session, actor_type=actor_type, actor_id=actor_id, action="research.upstream.check",
                 result="SUCCESS" if status in ("OK", "ARCHIVED", "IDENTITY_CHANGED", "FORK", "NOT_FOUND") else "FAILED",
                 target_type="research_entry", target_id=entry.id, domain=domain,
                 details={"identity": entry.identity, "status": status, "installed": False, "activated": False})
    events.emit(session, source="research", kind="research.upstream.checked", entity_type="research_entry",
                entity_id=entry.id, domain=entry.submitted_domain, payload={"status": status, "identity": entry.identity})
    if (entry.upstream or {}).get("update_available"):
        events.emit(session, source="research", kind="research.upstream.update_detected", entity_type="research_entry",
                    entity_id=entry.id, domain=entry.submitted_domain, payload={"latest_tag": entry.upstream.get("latest_tag"),
                                                  "pinned": entry.source_version,
                                                  "action": "notification only; propose an UPGRADE to review"})
    view = entry_view(entry, detail=True)
    view["check_status"] = status
    view["touched"] = touch
    return view
