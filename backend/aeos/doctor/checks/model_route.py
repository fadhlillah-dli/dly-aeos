"""§08 Model route: provider/key reference, exact route, data terms, free/paid status, caps, freshness.

No model or paid call is made: key presence is a status lookup, never a request.
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
CATALOG_MAX_AGE = timedelta(days=30)


@check("route.registry", "MODEL_ROUTE", "Registry route pekerja", component_id="model-routing", needs_db=True,
       ttl_seconds=3600, invalidators=("config:routes",), ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH")
def route_registry(ctx: CheckContext) -> Outcome:
    from aeos.models.routing import ModelRoute

    assert ctx.session is not None
    rows = ctx.session.execute(select(ModelRoute.pool, ModelRoute.status, func.count())
                               .group_by(ModelRoute.pool, ModelRoute.status)).all()
    counts: dict[str, dict[str, int]] = {}
    for pool, status, n in rows:
        counts.setdefault(str(pool), {})[str(status)] = int(n)
    approved_workers = counts.get("WORKER", {}).get("APPROVED", 0)
    evidence: dict[str, Any] = {"by_pool_status": counts, "approved_worker_routes": approved_workers}
    if not rows:
        return Outcome("MISSING", "Registry model kosong.", evidence=evidence, error_code="ROUTE_REGISTRY_EMPTY",
                       remedy=Remedy(operation="Muat katalog model (DESIGN/CANDIDATE, bukan aktif)",
                                     command=aeos_cmd("seed"), evidence_to_collect="route.registry"))
    if approved_workers == 0:
        return Outcome("MISSING", "Belum ada route pekerja yang APPROVED; kandidat tidak otomatis aktif (§23).",
                       evidence=evidence, error_code="NO_APPROVED_WORKER_ROUTE", remedy=Remedy(
                           operation="Evaluasi lalu setujui satu route pekerja lewat keputusan manusia (ROUTE_ACTIVATION)",
                           target="Models & Routes", privilege="pemilik", cost="sesuai route; paid nonaktif tanpa budget",
                           evidence_to_collect="Decision ROUTE_ACTIVATION + hasil eval"))
    return Outcome("CONFIGURED", f"{approved_workers} route pekerja APPROVED.", evidence=evidence)


# Secret changes are signalled by explicit invalidators (secret:<ref>), not by reading the store.
@check("route.worker_key", "MODEL_ROUTE", "Referensi kunci route pekerja (tanpa panggilan)", component_id="openrouter",
       ttl_seconds=3600, invalidators=(f"secret:{WORKER_KEY_REF}",), ok={"CONFIGURED", "VERIFIED"},
       severity_fail="HIGH", observation="configuration")
def route_worker_key(ctx: CheckContext) -> Outcome:
    state, evidence = secret_ref_status(WORKER_KEY_REF)
    evidence["paid_call_made"] = False
    if state == "PRESENT":
        return Outcome("CONFIGURED", f"Kunci {WORKER_KEY_REF} tersimpan; belum diuji (tidak ada panggilan berbayar).",
                       evidence=evidence)
    if state == "NOT_IMPLEMENTED":
        return Outcome("UNKNOWN", "Modul secrets belum tersedia.", evidence=evidence, error_code="SECRETS_NOT_IMPLEMENTED")
    if state == "STORE_UNAVAILABLE":
        return Outcome("MISSING", "Secret store tidak tersedia; status kunci tidak dapat dibaca (fail closed).",
                       evidence=evidence, error_code="SECRET_STORE_UNAVAILABLE")
    return Outcome("MISSING", f"Kunci {WORKER_KEY_REF} belum disimpan.", evidence=evidence, error_code="WORKER_KEY_MISSING",
                   remedy=set_secret_remedy(WORKER_KEY_REF, "OpenRouter untuk pekerja"))


@check("route.catalog_freshness", "MODEL_ROUTE", "Kesegaran harga/terms katalog route", component_id="model-routing",
       needs_db=True, ttl_seconds=86400, invalidators=("config:routes",), severity_fail="MEDIUM")
def route_catalog_freshness(ctx: CheckContext) -> Outcome:
    from aeos.models.routing import ModelRoute

    assert ctx.session is not None
    rows = list(ctx.session.scalars(select(ModelRoute).where(ModelRoute.pool == "WORKER")))
    stamps = [ensure_aware(t) for r in rows for t in (r.pricing_checked_at, r.terms_checked_at) if t is not None]
    evidence: dict[str, Any] = {"worker_routes": len(rows), "checked_entries": len(stamps)}
    if not stamps:
        return Outcome("UNKNOWN", "Harga/terms route belum pernah dicek bertanggal; biaya dan data terms UNKNOWN.",
                       evidence=evidence, error_code="CATALOG_NEVER_CHECKED")
    oldest = min(s for s in stamps if s is not None)
    evidence["oldest_check"] = oldest.isoformat()
    if ctx.now - oldest > CATALOG_MAX_AGE:
        return Outcome("EXPIRED", f"Sebagian data harga/terms lebih tua dari 30 hari ({oldest.date()}).", evidence=evidence,
                       error_code="CATALOG_STALE", remedy=Remedy(
                           operation="Baca ulang halaman harga/terms resmi untuk route terkait (tanpa auto-update)",
                           target="Models & Routes", privilege="pemilik", evidence_to_collect="pricing_checked_at baru"))
    return Outcome("VERIFIED", f"Harga/terms route dicek dalam 30 hari terakhir (tertua {oldest.date()}).",
                   evidence=evidence)
