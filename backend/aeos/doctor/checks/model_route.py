"""§08 Model route: provider/key reference, exact route, data terms, free/paid status, caps, freshness.

No model or paid call is made: key presence is a status lookup, never a request. Eligibility is the
routing module's own hard filter (``aeos.routing.facade.eligible_routes``) — an APPROVED/TRIAL row
without an ACTIVE human decision bound to its current configuration hash is NOT an eligible route.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from aeos.core.timeutil import ensure_aware
from aeos.doctor.checks import check
from aeos.doctor.checks._common import secret_ref_status, set_secret_remedy
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd

WORKER_KEY_REF = "workers/openrouter"
OPENROUTER_PERSONAL_REFS = ("workers/openrouter", "personal/openrouter")  # broker model proxy order (PERSONAL)
# Remedy wording per worker credential reference (an eligible route names its own, e.g. workers/opencode-go)
KEY_LABELS = {"workers/openrouter": "OpenRouter untuk pekerja",
              "workers/opencode-go": "OpenCode Go (langganan) untuk pekerja",
              "workers/opencode-zen": "OpenCode Zen untuk pekerja", "workers/gemini": "Gemini API (pembanding)"}
CATALOG_MAX_AGE = timedelta(days=30)
ROUTE_DOMAIN = "PERSONAL"  # OFFICE routes stay denied by the office policy (§23) and are gated per domain
ROUTE_CLASSIFICATION = "INTERNAL"  # TaskSpec default data classification
# The data classes a worker route can serve, in the order a readiness probe reports them: INTERNAL work, or PUBLIC work
# only (a free OpenRouter WORKER route, decision C, ADR-0041 amendment 2026-10-26). The agent enable probe
# (workforce/agents.py) accepts the same two; every task is still routed by its own data class.
ROUTE_CLASSIFICATIONS = ("INTERNAL", "PUBLIC")


def _routes_fp(ctx: CheckContext) -> dict[str, Any] | None:
    """Cheap DB identity of the worker routes and their decisions: any status/config/decision change makes
    the stored result STALE (the routing hooks additionally invalidate ``config:routes``/``model:route``)."""
    if ctx.session is None:
        return None
    from aeos.models.decisions import Decision
    from aeos.models.routing import ModelRoute

    routes = ctx.session.execute(select(ModelRoute.route_key, ModelRoute.status, ModelRoute.config_hash)
                                 .where(ModelRoute.pool == "WORKER")).all()
    decisions = ctx.session.execute(select(Decision.id, Decision.status, Decision.expires_at).where(
        Decision.target_type == "model_route")).all()
    return {"routes": sorted((r[0], r[1], r[2] or "") for r in routes),
            "decisions": sorted((d[0], d[1], str(d[2])) for d in decisions)}


def eligible_worker_routes(session: Any) -> tuple[list[str], dict[str, list[str]], str | None]:
    """(route keys eligible for PERSONAL tool-using work of any class in ROUTE_CLASSIFICATIONS, rejections of the
    rows eligible for none, error). ``eligible_worker_routes_by_class`` says which class each key serves."""
    by_class, rejected, error = eligible_worker_routes_by_class(session)
    union = [k for c in ROUTE_CLASSIFICATIONS for k in by_class.get(c, [])]
    return list(dict.fromkeys(union)), rejected, error


def eligible_worker_routes_by_class(session: Any) -> tuple[dict[str, list[str]], dict[str, list[str]], str | None]:
    """({data class: eligible route keys}, rejections of the rows eligible for no class, error) for PERSONAL
    tool-using work, per class in ROUTE_CLASSIFICATIONS.

    The routing filter may backfill a missing config hash in memory; autoflush is off during the call and
    those attributes are expired afterwards, so a readiness probe never writes routing state."""
    try:
        from aeos.routing import facade as routing
    except (ImportError, ModuleNotFoundError):
        return {}, {}, "routing module not available (fail closed)"
    from aeos.models.routing import ModelRoute

    unhashed = list(session.scalars(select(ModelRoute).where(ModelRoute.config_hash.is_(None))))
    by_class: dict[str, list[str]] = {}
    rejected_by_class: dict[str, dict[str, list[str]]] = {}
    try:
        with session.no_autoflush:
            for c in ROUTE_CLASSIFICATIONS:
                rd = routing.eligible_routes(session, domain=ROUTE_DOMAIN, data_classification=c,
                                             needs_tools=True, pool="WORKER")
                by_class[c] = list(rd.eligible)
                rejected_by_class[c] = dict(rd.rejected)
    except Exception as exc:  # fail closed, never green
        return {}, {}, f"eligibility could not be evaluated ({type(exc).__name__})"
    finally:
        for r in unhashed:
            session.expire(r, ["config_hash"])
    served = {k for keys in by_class.values() for k in keys}
    rejected: dict[str, list[str]] = {}
    for c in ROUTE_CLASSIFICATIONS:  # a row eligible for no class: the reasons of every class, without repeats
        for key, reasons in rejected_by_class.get(c, {}).items():
            if key not in served:
                rejected.setdefault(key, [])
                rejected[key] += [r for r in reasons if r not in rejected[key]]
    return by_class, rejected, None


@check("route.registry", "MODEL_ROUTE", "Route pekerja APPROVED/TRIAL yang eligible", component_id="model-routing",
       needs_db=True, ttl_seconds=3600, invalidators=("config:routes", "model:route", "config:budget",
                                                      "config:office_policy"),
       ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH", fingerprint=_routes_fp,
       description="Hard filter routing (keputusan manusia terikat hash konfigurasi, domain, data terms, tool support, "
                   "kuota, kesegaran harga/terms, budget) untuk PERSONAL / INTERNAL, atau PUBLIC saja (keputusan C).")
def route_registry(ctx: CheckContext) -> Outcome:
    from aeos.models.routing import ModelRoute

    assert ctx.session is not None
    rows = ctx.session.execute(select(ModelRoute.pool, ModelRoute.status, func.count())
                               .group_by(ModelRoute.pool, ModelRoute.status)).all()
    counts: dict[str, dict[str, int]] = {}
    for pool, status, n in rows:
        counts.setdefault(str(pool), {})[str(status)] = int(n)
    worker = counts.get("WORKER", {})
    decided = worker.get("APPROVED", 0) + worker.get("TRIAL", 0)
    evidence: dict[str, Any] = {"by_pool_status": counts, "approved_or_trial_worker_routes": decided,
                                "evaluated_for": {"domain": ROUTE_DOMAIN,
                                                  "data_classifications": list(ROUTE_CLASSIFICATIONS),
                                                  "needs_tools": True}}
    if not rows:
        return Outcome("MISSING", "Registry model kosong.", evidence=evidence, error_code="ROUTE_REGISTRY_EMPTY",
                       remedy=Remedy(operation="Muat katalog model (DESIGN/CANDIDATE, bukan aktif)",
                                     command=aeos_cmd("seed"), evidence_to_collect="route.registry"))
    by_class, rejected, error = eligible_worker_routes_by_class(ctx.session)
    eligible = list(dict.fromkeys(k for c in ROUTE_CLASSIFICATIONS for k in by_class.get(c, [])))
    decided_keys = {k for (k,) in ctx.session.execute(select(ModelRoute.route_key).where(
        ModelRoute.pool == "WORKER", ModelRoute.status.in_(("APPROVED", "TRIAL"))))}
    evidence["eligible"] = eligible
    evidence["eligible_by_class"] = {c: keys for c, keys in by_class.items() if keys}
    evidence["rejected_decided"] = {k: v[:4] for k, v in rejected.items() if k in decided_keys}
    activate = Remedy(operation="Evaluasi lalu aktifkan satu route pekerja lewat keputusan manusia (ROUTE_ACTIVATION "
                      "atau TRIAL terikat hash konfigurasi; route berbayar butuh keputusan BUDGET, route langganan "
                      "butuh keputusan BUDGET untuk biaya paket bulanannya)",
                      command=aeos_cmd("routes activate <route_key>"), target="Models & Routes", privilege="pemilik",
                      cost="sesuai route; paid nonaktif tanpa budget; langganan = biaya paket + allowance per jendela",
                      rollback=aeos_cmd("routes disable <route_key>"),
                      evidence_to_collect="Decision ROUTE_ACTIVATION/TRIAL + route.registry CONFIGURED")
    if error:
        return Outcome("UNKNOWN", f"Eligibilitas route tidak dapat dinilai: {error}.", evidence=evidence,
                       error_code="ROUTE_ELIGIBILITY_UNKNOWN")
    internal = by_class.get(ROUTE_CLASSIFICATION, [])
    if internal:
        return Outcome("CONFIGURED", f"{len(internal)} route pekerja eligible untuk {ROUTE_DOMAIN}/{ROUTE_CLASSIFICATION}: "
                       f"{', '.join(internal[:5])} (keputusan manusia terikat hash; belum bukti kualitas).",
                       evidence=evidence)
    if eligible:
        # decision C: only PUBLIC work can run; INTERNAL tasks wait (routing refuses them on these routes)
        return Outcome("CONFIGURED", f"{len(eligible)} route pekerja eligible untuk {ROUTE_DOMAIN}/PUBLIC saja "
                       f"(keputusan C): {', '.join(eligible[:5])}; tugas INTERNAL menunggu route yang mengizinkan "
                       "INTERNAL (mis. OpenCode Go) (keputusan manusia terikat hash; belum bukti kualitas).",
                       evidence=evidence)
    # §08: the remedy (activating a worker route) may be a paid write when any candidate is not free. A flat-fee
    # SUBSCRIPTION candidate (ADR-0080 amendment 2026-10-26) is neither free nor an unpriced paid route: activating it
    # is a BUDGET decision for its plan fee (a paid write); incomplete subscription terms stay unknown-price.
    paid, subscription = _non_free_candidates(ctx.session)
    evidence["paid_or_unknown_price_candidates"] = paid[:10]
    evidence["subscription_candidates"] = subscription[:10]
    if decided == 0:
        return Outcome("MISSING", "Belum ada route pekerja APPROVED/TRIAL; kandidat tidak otomatis aktif (§23).",
                       evidence=evidence, error_code="NO_APPROVED_WORKER_ROUTE", remedy=activate,
                       requires_paid_write=bool(paid or subscription))
    first = next(iter(evidence["rejected_decided"].items()), None)
    why = f" (mis. {first[0]}: {first[1][0]})" if first and first[1] else ""
    return Outcome("MISSING", f"{decided} route APPROVED/TRIAL tetapi tidak ada yang eligible{why}.", evidence=evidence,
                   error_code="NO_ELIGIBLE_WORKER_ROUTE", remedy=activate,
                   requires_paid_write=bool(paid or subscription))


def _non_free_candidates(session: Any) -> tuple[list[str], list[str]]:
    """(paid or unknown-price WORKER candidates, flat-fee SUBSCRIPTION candidates with complete terms), not DISABLED.
    A route whose pricing says SUBSCRIPTION is never counted as free, whatever its is_free flag."""
    from aeos.cost import facade as cost
    from aeos.models.routing import ModelRoute

    paid: list[str] = []
    subscription: list[str] = []
    for r in session.scalars(select(ModelRoute).where(ModelRoute.pool == "WORKER", ModelRoute.status != "DISABLED")):
        try:
            terms, problem = cost.subscription_terms(r)
        except (ImportError, ModuleNotFoundError):  # pragma: no cover - cost ships with the doctor; is_free decides
            terms, problem = None, None
        if terms is not None:
            subscription.append(r.route_key)
        elif problem or r.is_free != "YES":
            paid.append(r.route_key)
    return sorted(paid), sorted(subscription)


def _required_key_refs(ctx: CheckContext) -> tuple[list[str], list[tuple[str, tuple[str, ...]]]]:
    """(eligible route keys, [(route_key, acceptable refs)]) for the eligible PERSONAL worker routes, with the
    broker model proxy's credential rules (``mode_params.credential_ref``, else OpenRouter's worker/personal key)."""
    if ctx.session is None:
        return [], []
    from aeos.inventory.probes import is_loopback_url
    from aeos.models.routing import ModelRoute

    eligible, _rejected, _error = eligible_worker_routes(ctx.session)
    out: list[tuple[str, tuple[str, ...]]] = []
    for key in eligible:
        r = ctx.session.scalar(select(ModelRoute).where(ModelRoute.route_key == key))
        if r is None:
            continue
        cred = (r.mode_params or {}).get("credential_ref")
        if cred:
            out.append((key, (str(cred),)))
        elif r.provider == "openrouter":
            out.append((key, OPENROUTER_PERSONAL_REFS))
        elif not (r.endpoint and r.endpoint.startswith("http://") and is_loopback_url(r.endpoint)):
            out.append((key, ()))  # https upstream without credential_ref: the broker proxy refuses it
        # a loopback http endpoint without credential_ref needs no upstream key
    return eligible, out


# Secret changes are signalled by explicit invalidators (secret:<ref>), not by reading the store. ``secret:workers/*``
# covers every worker-model key an eligible route may name (workers/openrouter, workers/opencode-go, ...).
@check("route.worker_key", "MODEL_ROUTE", "Referensi kunci route pekerja (tanpa panggilan)", component_id="openrouter",
       ttl_seconds=3600, invalidators=("secret:workers/*", "secret:personal/openrouter", "config:routes",
                                       "model:route"),
       ok={"CONFIGURED", "VERIFIED", "NOT_APPLICABLE"}, severity_fail="HIGH", observation="configuration")
def route_worker_key(ctx: CheckContext) -> Outcome:
    eligible, required = _required_key_refs(ctx)
    if eligible and not required:
        return Outcome("NOT_APPLICABLE", f"Route eligible ({', '.join(eligible[:5])}) adalah endpoint loopback tanpa "
                       "credential; tidak ada kunci upstream yang dibutuhkan.",
                       evidence={"routes": eligible, "paid_call_made": False})
    # No eligible route yet (route.registry reports why): report the default worker key the first
    # OpenRouter route would need, so the owner can prepare it.
    no_ref = [k for k, refs in required if not refs]
    if no_ref:
        return Outcome("MISSING", f"Route eligible tanpa credential_ref: {', '.join(no_ref)}; broker model proxy menolak "
                       "upstream https tanpa referensi kunci (UPSTREAM_CREDENTIAL_MISSING).",
                       evidence={"routes": no_ref, "paid_call_made": False}, error_code="ROUTE_CREDENTIAL_REF_MISSING",
                       remedy=Remedy(operation="Simpan kunci provider di secret store lalu catat mode_params."
                                     "credential_ref pada route (perubahan konfigurasi = review + aktivasi ulang)",
                                     command=aeos_cmd("secrets set workers/<provider>"), target=", ".join(no_ref),
                                     privilege="pemilik", cost="sesuai route",
                                     evidence_to_collect="route.worker_key CONFIGURED"))
    targets = required or [("<default>", (WORKER_KEY_REF,))]
    missing: list[str] = []
    store_down = False
    states: dict[str, str] = {}
    for route_key, refs in targets:
        found = False
        for ref in refs:
            state, _ev = secret_ref_status(ref)
            states[ref] = state
            if state == "PRESENT":
                found = True
                break
            if state in ("STORE_UNAVAILABLE", "NOT_IMPLEMENTED"):
                store_down = True
        if not found:
            missing.append(f"{route_key}: {' / '.join(refs)}")
    evidence: dict[str, Any] = {"paid_call_made": False, "ref_states": states,
                                "routes": [k for k, _ in targets if k != "<default>"]}
    if not missing:
        return Outcome("CONFIGURED", f"Kunci untuk {len(targets)} route tersimpan ({', '.join(sorted(states))}); belum "
                       "diuji (tidak ada panggilan berbayar).", evidence=evidence)
    if "NOT_IMPLEMENTED" in states.values():
        return Outcome("UNKNOWN", "Modul secrets belum tersedia.", evidence=evidence, error_code="SECRETS_NOT_IMPLEMENTED")
    if store_down:
        return Outcome("MISSING", "Secret store tidak tersedia; status kunci tidak dapat dibaca (fail closed).",
                       evidence=evidence, error_code="SECRET_STORE_UNAVAILABLE")
    ref = (required[0][1][0] if required else WORKER_KEY_REF)
    return Outcome("MISSING", f"Kunci route belum disimpan: {'; '.join(missing)}.", evidence=evidence,
                   error_code="WORKER_KEY_MISSING", remedy=set_secret_remedy(ref, KEY_LABELS.get(ref, ref)))


@check("route.catalog_freshness", "MODEL_ROUTE", "Kesegaran harga/terms route yang diputuskan", component_id="model-routing",
       needs_db=True, ttl_seconds=86400, invalidators=("config:routes", "model:route"), severity_fail="MEDIUM",
       ok={"VERIFIED", "NOT_APPLICABLE"}, fingerprint=_routes_fp)
def route_catalog_freshness(ctx: CheckContext) -> Outcome:
    from aeos.models.routing import ModelRoute

    assert ctx.session is not None
    # Only routes a human decided on matter for cost/terms freshness; DESIGN catalog rows carry the research
    # date of the blueprint, which is not a live check of the provider page.
    rows = list(ctx.session.scalars(select(ModelRoute).where(ModelRoute.pool == "WORKER",
                                                             ModelRoute.status.in_(("APPROVED", "TRIAL")))))
    if not rows:
        return Outcome("NOT_APPLICABLE", "Belum ada route pekerja APPROVED/TRIAL; kesegaran harga/terms dinilai saat "
                       "ada keputusan route.", evidence={"decided_worker_routes": 0})
    stamps = {r.route_key: [ensure_aware(t) for t in (r.pricing_checked_at, r.terms_checked_at)] for r in rows}
    never = sorted(k for k, ts in stamps.items() if any(t is None for t in ts))
    evidence: dict[str, Any] = {"decided_worker_routes": len(rows), "never_checked": never}
    if never:
        return Outcome("UNKNOWN", f"Harga/terms belum pernah dicek bertanggal untuk: {', '.join(never)}; biaya dan data "
                       "terms UNKNOWN.", evidence=evidence, error_code="CATALOG_NEVER_CHECKED")
    oldest = min(t for ts in stamps.values() for t in ts if t is not None)
    evidence["oldest_check"] = oldest.isoformat()
    if ctx.now - oldest > CATALOG_MAX_AGE:
        return Outcome("EXPIRED", f"Sebagian data harga/terms lebih tua dari 30 hari ({oldest.date()}).", evidence=evidence,
                       error_code="CATALOG_STALE", remedy=Remedy(
                           operation="Baca ulang halaman harga/terms resmi untuk route terkait (tanpa auto-update)",
                           command=aeos_cmd("routes refresh"), target="Models & Routes", privilege="pemilik",
                           evidence_to_collect="pricing_checked_at baru"))
    return Outcome("VERIFIED", f"Harga/terms {len(rows)} route yang diputuskan dicek dalam 30 hari terakhir (tertua "
                   f"{oldest.date()}).", evidence=evidence)


def _budget_fp(ctx: CheckContext) -> dict[str, Any] | None:
    if ctx.session is None:
        return None
    from aeos.models.cost import Budget
    from aeos.models.decisions import Decision

    budgets = ctx.session.execute(select(Budget.id, Budget.status, Budget.terms_hash, Budget.decision_id)
                                  .where(Budget.status == "ACTIVE")).all()
    decisions = ctx.session.execute(select(Decision.id, Decision.status).where(Decision.kind == "BUDGET")).all()
    return {"budgets": sorted(tuple(str(x) for x in b) for b in budgets),
            "decisions": sorted((d[0], d[1]) for d in decisions)}


@check("route.budget_admission", "MODEL_ROUTE", "Budget admission dapat dipercaya", component_id="cost-evaluation",
       needs_db=True, ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, invalidators=("config:budget",),
       fingerprint=_budget_fp, severity_fail="HIGH",
       description="Integritas budget aktif (hash terms, keputusan BUDGET manusia) yang dipakai cost.reserve; "
                   "tanpa reservasi, tanpa membuat baris.")
def route_budget_admission(ctx: CheckContext) -> Outcome:
    from collections import Counter

    from aeos.models.cost import Budget

    assert ctx.session is not None
    try:
        from aeos.cost.service import verify_budget_authority
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul cost tidak tersedia; admission ditolak (fail closed).",
                       error_code="COST_NOT_AVAILABLE")
    budgets = list(ctx.session.scalars(select(Budget).where(Budget.status == "ACTIVE")))
    untrusted: list[str] = []
    for b in budgets:
        untrusted += [f"{b.scope_type}:{b.scope_id}:{b.period}: {r}" for r in verify_budget_authority(ctx.session, b)]
    dupes = [k for k, n in Counter((b.scope_type, b.scope_id, b.period) for b in budgets).items() if n > 1]
    untrusted += [f"{':'.join(k)}: lebih dari satu budget ACTIVE (ambigu)" for k in dupes]
    global_month = [b for b in budgets if (b.scope_type, b.scope_id, b.period) == ("GLOBAL", "*", "MONTH")]
    evidence: dict[str, Any] = {"active_budgets": len(budgets), "untrusted": untrusted[:10],
                                "global_month": "ACTIVE" if global_month else "DEFAULT_ON_FIRST_RESERVATION",
                                "reservation_made": False}
    if untrusted:
        return Outcome("FAILED", f"Status budget tidak dapat dipercaya ({len(untrusted)} masalah); cost.reserve akan "
                       "menolak admission (fail closed).", evidence=evidence, error_code="BUDGET_STATE_UNTRUSTED",
                       remedy=Remedy(operation="Tinjau budget di halaman Biaya; perubahan budget hanya lewat keputusan "
                                     "BUDGET pemilik (tidak ada auto-topup)", target="Biaya & Kuota",
                                     privilege="pemilik", cost="keputusan BUDGET dapat mengizinkan pemakaian berbayar",
                                     evidence_to_collect="route.budget_admission CONFIGURED"),
                       # the remedy is a budget decision, i.e. it may authorise paid usage (§08, §42)
                       requires_paid_write=True)
    note = ("budget GLOBAL bulanan aktif" if global_month else
            "belum ada budget GLOBAL; reservasi pertama memakai default $0 hard-stop (route gratis tetap bisa)")
    return Outcome("CONFIGURED", f"{len(budgets)} budget aktif dapat dipercaya; {note}. Tidak ada paid fallback.",
                   evidence=evidence)
