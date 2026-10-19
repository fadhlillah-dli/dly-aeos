"""Catalog refresh (§23, §38, §41): OpenRouter public models, OpenRouter key info, OpenCode models.

* Observations update pricing/context/free flags/availability with ``*_checked_at`` timestamps and
  register newly found ``:free`` models as DISCOVERED candidates (status CANDIDATE, mode LIVE,
  evidence E1). Nothing is ever activated by a refresh (§23 "Penemuan model baru tidak otomatis
  mengaktifkannya").
* A change of any identity field is a configuration change: approvals bound to the old hash stop
  matching, eval baselines are not inherited and ``config:routes`` Doctor checks go STALE.
* Key info: GET /api/v1/key with the key from secret ref ``workers/openrouter`` (falls back to
  ``personal/openrouter``); no key -> quota UNKNOWN. The response ``label`` (it can contain a key
  fragment) is never stored.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.claude_bridge.binary import neutral_cwd
from aeos.claude_bridge.env import child_env
from aeos.core.config import Settings, get_settings
from aeos.core.errors import AeosError
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.models.cost import QuotaState
from aeos.models.routing import ModelRoute
from aeos.models.routing_ext import CatalogRefresh
from aeos.routing.service import WORKER_EXECUTED_POOLS, compute_config_hash, refresh_config_hash

log = logging.getLogger("aeos.routing.catalog")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_KEY_REFS = (("workers/openrouter", "workers:routing.key-info"), ("personal/openrouter", "personal:routing.key-info"))
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# S12 (documentation, E1): free-model daily request caps depend on purchased credits.
FREE_DAILY_LIMIT_NO_CREDITS = 50
FREE_DAILY_LIMIT_WITH_CREDITS = 1000


def _is_zero(v: Any) -> bool:
    try:
        return float(v) == 0.0
    except (TypeError, ValueError):
        return False


def _parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    if len(value) == 10:  # date only: free through the end of that day (UTC)
        d = d.replace(hour=23, minute=59, second=59)
    return d


def fetch_openrouter_models(client: httpx.Client) -> list[dict[str, Any]]:
    r = client.get(OPENROUTER_MODELS_URL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("unexpected OpenRouter catalog shape")
    return [m for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _apply_openrouter(route: ModelRoute, m: dict[str, Any], now: datetime) -> None:
    pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
    keep = {k: pricing.get(k) for k in ("prompt", "completion", "request", "image", "input_cache_read") if k in pricing}
    route.pricing = {**keep, "basis": "OpenRouter catalog", "unit": "USD per token"}
    route.pricing_checked_at = now
    prompt, completion = pricing.get("prompt"), pricing.get("completion")
    route.is_free = "YES" if _is_zero(prompt) and _is_zero(completion) else ("NO" if prompt is not None else "UNKNOWN")
    if isinstance(m.get("context_length"), int):
        route.context_window = m["context_length"]
    params = m.get("supported_parameters")
    if isinstance(params, list):
        route.supports_tools = "YES" if "tools" in params else "NO"
    expiry = _parse_date(m.get("expiration_date"))
    if expiry is not None:
        route.free_until = expiry if route.is_free == "YES" else route.free_until
        route.expires_note = f"catalog expiration_date {m.get('expiration_date')}"
    top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
    terms = dict(route.data_terms or {})
    # Not a data-terms review: the catalog only exposes moderation; terms_checked_at is unchanged.
    terms["catalog"] = {"checked_at": now.isoformat(), "is_moderated": top.get("is_moderated")}
    route.data_terms = terms
    route.availability = "AVAILABLE"
    route.availability_checked_at = now


# §23 "Trial/data restrictions diperiksa": a model family whose provider publishes trial-only terms keeps them
# whichever gateway lists it (a Nemotron discovered on OpenRouter is still "trial use only - no confidential
# data"). Training flags of the serving provider stay UNKNOWN until the owner records the terms.
FAMILY_TERMS: dict[str, dict[str, Any]] = {
    "nvidia": {"trial_only_no_confidential": True,
               "statement": "NVIDIA family: trial use only - do not submit personal or confidential data (S07)",
               "claim_level": "E1 family terms (S07) applied on catalog discovery"},
}


def discovered_terms(author: str) -> dict[str, Any]:
    base: dict[str, Any] = {"retention": "UNKNOWN", "training": "UNKNOWN", "may_use_inputs_for_training": None}
    return {**base, **FAMILY_TERMS.get(author.lower(), {})}


def refresh_openrouter_catalog(session: Session, client: httpx.Client) -> dict[str, Any]:
    now = utcnow()
    models = {m["id"]: m for m in fetch_openrouter_models(client)}
    updated: list[str] = []
    changed: list[str] = []
    missing: list[str] = []
    routes = list(session.scalars(select(ModelRoute).where(ModelRoute.provider == "openrouter")))
    known_ids = {r.model_id for r in routes}
    for route in routes:
        if route.config_hash is None:
            route.config_hash = compute_config_hash(route)
        m = models.get(route.model_id)
        if m is None:
            route.availability = "MISSING"
            route.availability_checked_at = now
            missing.append(route.route_key)
            continue
        _apply_openrouter(route, m, now)
        updated.append(route.route_key)
        if refresh_config_hash(session, route, reason="OpenRouter catalog values changed"):
            changed.append(route.route_key)
    discovered: list[str] = []
    for model_id, m in sorted(models.items()):
        if not model_id.endswith(":free") or model_id in known_ids:
            continue
        author = model_id.split("/", 1)[0]
        route = ModelRoute(
            route_key=f"openrouter/{model_id}", pool="ARENA", provider="openrouter",
            endpoint="https://openrouter.ai/api/v1", model_id=model_id,
            display_name=str(m.get("name") or model_id)[:200], harness="opencode", eligible_domains=["PERSONAL"],
            allowed_data_classes=["PUBLIC"], data_terms=discovered_terms(author),
            status="CANDIDATE", mode="LIVE", origin="CATALOG_DISCOVERED", evidence_level="E1",
            identity_status="UNVERIFIED" if author in {"openrouter", "stealth"} else "PROVIDER_CLAIMED",
            priority=800, source_refs=["S11", "S12"], eval_results={}, mode_params={}, tool_set=[], fallback_chain=[],
            notes=f"Discovered in the OpenRouter catalog on {now.date().isoformat()}; never activated automatically.",
        )
        _apply_openrouter(route, m, now)
        route.config_hash = compute_config_hash(route)
        session.add(route)
        discovered.append(route.route_key)
    session.flush()
    return {"status": "OK", "checked_at": now.isoformat(), "catalog_size": len(models), "updated": updated,
            "config_changed": changed, "missing": missing, "discovered": discovered, "evidence_level": "E1"}


def _openrouter_key() -> tuple[str | None, str | None, str]:
    try:
        from aeos.secrets.facade import get_secret
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - same owner
        return None, None, "secrets module unavailable"
    reasons = []
    for ref, purpose in OPENROUTER_KEY_REFS:
        try:
            return get_secret(ref, purpose=purpose, actor="routing.catalog"), ref, ""
        except AeosError as exc:
            reasons.append(f"{ref}: {exc.code}")
    return None, None, "; ".join(reasons)


def _upsert_quota(session: Session, *, provider: str, scope: str) -> QuotaState:
    row = session.scalars(select(QuotaState).where(QuotaState.provider == provider, QuotaState.scope == scope)).first()
    if row is None:
        row = QuotaState(provider=provider, scope=scope)
        session.add(row)
    return row


def parse_key_info(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("unexpected OpenRouter key info shape")
    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    usage = data.get("usage")
    if isinstance(limit, (int, float)) and remaining is None and isinstance(usage, (int, float)):
        remaining = max(0.0, float(limit) - float(usage))
    if isinstance(remaining, (int, float)) and remaining <= 0:
        state = "EXHAUSTED"
    elif isinstance(remaining, (int, float)) and isinstance(limit, (int, float)) and limit > 0 and remaining / limit < 0.1:
        state = "LOW"
    else:
        state = "OK"
    is_free_tier = data.get("is_free_tier")
    return {
        "credit_state": state, "limit": limit, "limit_remaining": remaining, "usage": usage,
        "usage_daily": data.get("usage_daily"), "limit_reset": data.get("limit_reset"),
        "is_free_tier": is_free_tier, "rate_limit": data.get("rate_limit"),
        "is_provisioning_key": data.get("is_provisioning_key"),
        "free_model_daily_limit_documented": (FREE_DAILY_LIMIT_NO_CREDITS if is_free_tier is True
                                              else FREE_DAILY_LIMIT_WITH_CREDITS if is_free_tier is False else None),
    }


def refresh_openrouter_key_info(session: Session, client: httpx.Client) -> dict[str, Any]:
    now = utcnow()
    key, ref, why = _openrouter_key()
    credits = _upsert_quota(session, provider="openrouter", scope="credits")
    daily = _upsert_quota(session, provider="openrouter", scope="free-daily")
    if key is None:
        credits.state, credits.source, credits.observed_at = "UNKNOWN", "no-key", now
        credits.details = {"reason": why, "remedy": "aeos secrets set workers/openrouter"}
        if daily.state != "EXHAUSTED":
            daily.state, daily.source, daily.observed_at = "UNKNOWN", "no-key", now
        session.flush()
        return {"status": "UNKNOWN", "checked_at": now.isoformat(), "reason": f"no OpenRouter key ({why})",
                "remedy": "aeos secrets set workers/openrouter"}
    r = client.get(OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=HTTP_TIMEOUT)
    if r.status_code in (401, 403):
        credits.state, credits.source, credits.observed_at = "UNKNOWN", "official-key-endpoint", now
        credits.details = {"reason": f"key rejected (HTTP {r.status_code})", "secret_ref": ref}
        session.flush()
        return {"status": "FAILED", "checked_at": now.isoformat(), "reason": f"key rejected (HTTP {r.status_code})",
                "secret_ref": ref}
    r.raise_for_status()
    info = parse_key_info(r.json())
    credits.state = info["credit_state"]
    credits.source = "official-key-endpoint"
    credits.observed_at = now
    credits.details = {k: v for k, v in info.items() if k != "credit_state"} | {"secret_ref": ref}
    if daily.state != "EXHAUSTED" or (daily.resets_at is not None and daily.resets_at <= now):
        daily.state = "UNKNOWN"
        daily.source = "documented-limit"
        daily.observed_at = now
        daily.resets_at = None
        daily.details = {"documented_daily_limit": info["free_model_daily_limit_documented"],
                         "note": "remaining free-model requests are not reported by the API; becomes EXHAUSTED "
                                 "after an observed 429 (S12)"}
    session.flush()
    return {"status": "OK", "checked_at": now.isoformat(), "credit_state": credits.state, "secret_ref": ref,
            "is_free_tier": info["is_free_tier"], "documented_free_daily_limit": info["free_model_daily_limit_documented"]}


def _opencode_binary(settings: Settings) -> str | None:
    if settings.opencode_binary:
        return settings.opencode_binary
    for name in (("opencode.exe", "opencode") if sys.platform == "win32" else ("opencode",)):
        found = shutil.which(name)
        if found:
            return found
    return None


def parse_opencode_models(output: str) -> set[str]:
    out: set[str] = set()
    for line in output.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if "/" in token and not token.startswith(("http", "#")):
            out.add(token)
    return out


def refresh_opencode_catalog(session: Session, settings: Settings) -> dict[str, Any]:
    now = utcnow()
    binary = _opencode_binary(settings)
    if not binary:
        return {"status": "UNKNOWN", "checked_at": now.isoformat(),
                "reason": "opencode binary not found - availability stays UNKNOWN",
                "remedy": "install OpenCode from the reviewed InstallManifest (bootstrap order 5)"}
    harness = observe_harness_version(session, binary)
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # neutral cwd: never the controller's cwd, whose project config (plugins/providers) opencode would load
        proc = subprocess.run([binary, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60, env=child_env(), stdin=subprocess.DEVNULL, check=False,
                              cwd=neutral_cwd(), **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "FAILED", "checked_at": now.isoformat(), "reason": f"opencode models failed: {type(exc).__name__}"}
    if proc.returncode != 0:
        return {"status": "FAILED", "checked_at": now.isoformat(),
                "reason": f"opencode models exit {proc.returncode}: {redact_text(proc.stderr)[:200]}"}
    listed = parse_opencode_models(proc.stdout)
    present, missing = [], []
    for route in session.scalars(select(ModelRoute).where(ModelRoute.provider == "opencode-zen")):
        route.availability = "AVAILABLE" if route.model_id in listed else "MISSING"
        route.availability_checked_at = now
        (present if route.model_id in listed else missing).append(route.route_key)
    session.flush()
    return {"status": "OK", "checked_at": now.isoformat(), "listed": len(listed), "available": present,
            "missing": missing, **harness}


def observe_harness_version(session: Session, binary: str | None) -> dict[str, Any]:
    """§23 "harness version": record the installed OpenCode version on every route the worker runtime runs
    with it. A different version is a configuration change (approvals bound to the old hash stop matching,
    eval baselines are not inherited); the executor also refuses a run whose engine differs (drift)."""
    try:
        from aeos.worker_runtime import opencode

        status = opencode.opencode_status(binary)
    except Exception as exc:  # pragma: no cover - observation is best effort, never a pass
        return {"harness_version": None, "harness_reason": f"version probe failed: {type(exc).__name__}"}
    version = status.get("version") if status.get("status") == "INSTALLED" else None
    if not version:
        return {"harness_version": None, "harness_reason": f"opencode {status.get('status')}: version not observed"}
    changed: list[str] = []
    for route in session.scalars(select(ModelRoute).where(or_(ModelRoute.harness == "opencode",
                                                                ModelRoute.pool.in_(WORKER_EXECUTED_POOLS)))):
        # candidates bind the version at activation; activated or already-bound routes follow the installation
        if route.harness_version == version or (route.harness_version is None
                                                and route.status not in ("APPROVED", "TRIAL")):
            continue
        previous = route.harness_version
        if route.config_hash is None:
            route.config_hash = compute_config_hash(route)
        route.harness_version = version
        if refresh_config_hash(session, route, reason=f"harness opencode {previous or 'UNKNOWN'} -> {version}"):
            changed.append(route.route_key)
    return {"harness_version": version, "harness_changed": changed}


def refresh_all(session: Session, *, actor: str, settings: Settings | None = None,
                client: httpx.Client | None = None) -> CatalogRefresh:
    """Run every catalog source independently; one failing source never blocks the others."""
    settings = settings or get_settings()
    own = client is None
    http = client or httpx.Client(follow_redirects=False, headers={"User-Agent": "dly-aeos-routing"})
    results: dict[str, Any] = {}
    try:
        for name, fn in (("openrouter_catalog", lambda: refresh_openrouter_catalog(session, http)),
                         ("openrouter_key", lambda: refresh_openrouter_key_info(session, http)),
                         ("opencode_catalog", lambda: refresh_opencode_catalog(session, settings))):
            try:
                results[name] = fn()
            except (httpx.HTTPError, ValueError) as exc:
                results[name] = {"status": "FAILED", "checked_at": utcnow().isoformat(),
                                 "reason": redact_text(f"{type(exc).__name__}: {exc}")[:300]}
    finally:
        if own:
            http.close()
    statuses = {v.get("status") for v in results.values()}
    overall = "OK" if statuses <= {"OK", "UNKNOWN"} and "OK" in statuses else ("FAILED" if statuses == {"FAILED"} else "PARTIAL")
    row = CatalogRefresh(actor=actor, status=overall, results=results, mode="LIVE")
    session.add(row)
    session.flush()
    return row


def latest_refresh(session: Session) -> CatalogRefresh | None:
    return session.scalars(select(CatalogRefresh).order_by(CatalogRefresh.created_at.desc())).first()
