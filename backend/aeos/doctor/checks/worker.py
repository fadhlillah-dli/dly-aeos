"""§08 Worker: engine/version, model tool/schema behaviour, request/response contract, resource caps."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, logical
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd, npm_cmd
from aeos.models.evidence import EvidenceArtifact

OPENCODE_URL = "https://github.com/anomalyco/opencode"

# Platform-level rows are visible to every domain: OFFICE-domain evidence is never quoted here (§35).
_NOT_OFFICE = or_(EvidenceArtifact.domain.is_(None), EvidenceArtifact.domain != "OFFICE")


def _opencode_remedy(pinned: str | None) -> Remedy:
    version = pinned or "<versi dari install-manifest>"
    return Remedy(operation="Pasang OpenCode versi yang dipin (worker engine pertama, §07 langkah 5); fungsi dibuktikan "
                  "terpisah oleh uji kontrak worker",
                  command=npm_cmd(f"install -g opencode-ai@{version}"),
                  source=f"S28: {OPENCODE_URL} (paket npm opencode-ai; install-manifest/ otoritatif)",
                  target="PATH atau setelan opencode_binary (AEOS_OPENCODE_BINARY)", privilege="pengguna lokal",
                  cost="tidak ada", rollback=npm_cmd("uninstall -g opencode-ai"),
                  evidence_to_collect="worker.opencode VERIFIED (opencode --version)")


def _approved_pin(ctx: CheckContext) -> str | None:
    """The owner-approved pin of the InstallManifest (§07/§38) wins over the runtime's built-in default."""
    if ctx.session is None:
        return None
    try:
        from aeos.inventory import facade as inventory

        # normalised exactly as the runtime resolves it (opencode.resolve_pin): "v1.20.0" is the pin 1.20.0
        return inventory.approved_pin("opencode", session=ctx.session)
    except Exception:  # inventory tables unavailable: fall back to the runtime default
        return None


@check("worker.opencode", "WORKER", "Engine pekerja OpenCode", component_id="opencode", ttl_seconds=86400,
       invalidators=("binary:opencode", "manifest", "pin:opencode"), ok={"VERIFIED", "CONFIGURED"},
       fingerprint=binary_fingerprint("opencode", "opencode_binary"), severity_fail="HIGH", observation="installation")
def worker_opencode(ctx: CheckContext) -> Outcome:
    try:
        from aeos.worker_runtime import facade as worker

        status = worker.opencode_status()
    except (ImportError, ModuleNotFoundError):
        found = ctx.runner.which("opencode")
        return Outcome("UNKNOWN", "Modul worker runtime belum tersedia; status engine tidak dapat dinilai "
                       f"({'binary ditemukan di PATH' if found else 'binary tidak ditemukan di PATH'}).",
                       evidence={"binary_on_path": bool(found), "location": logical(ctx, found) if found else None},
                       error_code="WORKER_RUNTIME_NOT_IMPLEMENTED", remedy=None if found else _opencode_remedy(None))
    except Exception as exc:
        return Outcome("UNKNOWN", f"Status OpenCode gagal dibaca ({type(exc).__name__}).", error_code="OPENCODE_STATUS_ERROR")
    if not isinstance(status, dict):
        return Outcome("UNKNOWN", "Status OpenCode tidak dikenali.", error_code="OPENCODE_STATUS_UNRECOGNISED")
    # worker_runtime contract: status INSTALLED|FAILED|MISSING, version, pinned_version, drift, functional NOT_PROVEN
    st = str(status.get("status") or "").upper()
    version = status.get("version")
    approved = _approved_pin(ctx)
    pinned = approved or status.get("pinned_version")
    evidence: dict[str, Any] = {k: status.get(k) for k in ("status", "version", "pinned_version", "drift", "functional",
                                                            "checked_at") if k in status}
    path = status.get("binary") or status.get("path")
    if path:
        evidence["location"] = logical(ctx, str(path))
    # the runtime resolves its pin from the reviewed manifest too (opencode.resolve_pin): quote where it came from
    evidence["pin_source"] = "install-manifest approved_pin" if approved or \
        status.get("pin_source") == "install-manifest approved_pin" else "worker runtime default"
    evidence["pinned_version"] = pinned
    remedy = _opencode_remedy(pinned)
    if st == "MISSING" or status.get("found") is False:
        return Outcome("MISSING", f"OpenCode tidak ditemukan (versi yang dipin: {pinned or 'UNKNOWN'}).", evidence=evidence,
                       error_code="OPENCODE_MISSING", remedy=remedy)
    if st in ("FAILED", "ERROR"):
        return Outcome("FAILED", f"OpenCode ditemukan tetapi `opencode --version` gagal ({status.get('evidence')}).",
                       evidence=evidence, error_code="OPENCODE_FAILED", remedy=remedy,
                       target_identity=evidence.get("location"))
    if st == "INSTALLED" and version:
        if (status.get("drift") and not approved) or (pinned and str(version) != str(pinned)):
            # §38 "Binary/version/route berubah dari pin": admission stops until reconciled (§50); the engine
            # is installed, so the inventory still records INSTALLED with the observed version.
            return Outcome("FAILED", f"OpenCode {version} terpasang tetapi berbeda dari versi yang dipin {pinned} "
                           "(drift): admission worker dihentikan sampai direkonsiliasi.", evidence=evidence,
                           component_version=str(version), target_identity=evidence.get("location"),
                           error_code="OPENCODE_PIN_DRIFT", severity="HIGH", remedy=Remedy(
                               operation="Pasang versi yang dipin, ATAU ubah pin lewat PR yang direview (install-"
                               "manifest + worker runtime) setelah uji kontrak worker pada versi baru",
                               command=npm_cmd(f"install -g opencode-ai@{pinned}"),
                               source=f"S28: {OPENCODE_URL} (install-manifest/ otoritatif)",
                               target="PATH atau setelan opencode_binary (AEOS_OPENCODE_BINARY)",
                               privilege="pengguna lokal (pin baru: reviewer manusia)", cost="tidak ada",
                               rollback=npm_cmd(f"install -g opencode-ai@{version}"),
                               evidence_to_collect="worker.opencode VERIFIED tanpa drift"),
                           observations=[{"component_id": "opencode", "dimension": "installation",
                                          "status": "INSTALLED", "observed_version": str(version)}])
        return Outcome("VERIFIED", f"OpenCode {version} terpasang sesuai pin (skenario: perintah versi; fungsi worker "
                       "NOT-PROVEN).", evidence=evidence, component_version=str(version),
                       target_identity=evidence.get("location"))
    return Outcome("UNKNOWN", f"Status OpenCode tidak konklusif ({st or 'tanpa status'}).", evidence=evidence,
                   error_code="OPENCODE_STATUS_UNRECOGNISED")


IMPLEMENTER_AGENT = "opencode-implementer"


def _agent_fp(ctx: CheckContext) -> dict[str, Any] | None:
    """The stored result goes STALE as soon as the agent definition is enabled/disabled or revised."""
    if ctx.session is None:
        return None
    from aeos.models.workforce import AgentDefinition

    a = ctx.session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == IMPLEMENTER_AGENT))
    return {"agent": IMPLEMENTER_AGENT, "present": a is not None,
            **({"enabled": bool(a.enabled), "harness": a.harness, "revision": a.revision_hash or a.revision} if a else {})}


@check("worker.agent_definition", "WORKER", "AgentDefinition opencode-implementer aktif", component_id="opencode",
       needs_db=True, ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, invalidators=("config:agents",),
       fingerprint=_agent_fp, severity_fail="HIGH")
def worker_agent_definition(ctx: CheckContext) -> Outcome:
    from aeos.models.workforce import AgentDefinition

    assert ctx.session is not None
    a = ctx.session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == IMPLEMENTER_AGENT))
    if a is None:
        return Outcome("MISSING", f"AgentDefinition {IMPLEMENTER_AGENT} belum ada di katalog.",
                       error_code="AGENT_MISSING", remedy=Remedy(
                           operation="Muat katalog workforce (DESIGN; agent tetap nonaktif sampai diaktifkan pemilik)",
                           command=aeos_cmd("seed"), privilege="pengguna lokal",
                           evidence_to_collect="worker.agent_definition"))
    evidence = {"agent_key": a.agent_key, "harness": a.harness, "enabled": bool(a.enabled), "mode": a.mode,
                "revision": a.revision, "enabled_reason": (a.enabled_reason or "")[:200]}
    if a.harness != "opencode":
        return Outcome("FAILED", f"{IMPLEMENTER_AGENT} bukan agent OpenCode (harness {a.harness}).", evidence=evidence,
                       error_code="AGENT_HARNESS_MISMATCH")
    if not a.enabled:
        return Outcome("MISSING", f"Agent {IMPLEMENTER_AGENT} nonaktif (default katalog); scheduler tidak akan "
                       "menjalankan worker.", evidence=evidence, error_code="AGENT_DISABLED", remedy=Remedy(
                           operation="Aktifkan agent di Control Center (Organisasi & Peran -> Definisi agent -> "
                           f"{IMPLEMENTER_AGENT} -> Aktifkan) atau lewat CLI, setelah OpenCode (atau image worker "
                           "untuk sandbox container), sandbox dan route siap; aksi ditolak dengan daftar prasyarat "
                           "(AGENT_PREREQUISITES) bila belum READY. Pemeriksaan ini dijalankan ulang otomatis saat "
                           "agent diaktifkan.",
                           command=aeos_cmd(f"work agent-enable {IMPLEMENTER_AGENT} --as <SUPER_ADMIN> "
                                            "--domain <PERSONAL|OFFICE> --reason <teks>"),
                           target=f"AgentDefinition {IMPLEMENTER_AGENT}", privilege="SUPER_ADMIN",
                           rollback=f"nonaktifkan agent (Organisasi & Peran -> Nonaktifkan, atau "
                                    f"{aeos_cmd(f'work agent-disable {IMPLEMENTER_AGENT} --reason <teks>')})",
                           evidence_to_collect="worker.agent_definition CONFIGURED"))
    return Outcome("CONFIGURED", f"Agent {IMPLEMENTER_AGENT} aktif ({a.enabled_reason or 'tanpa alasan tercatat'}); "
                   "aktif bukan bukti sedang dipakai.", evidence=evidence, target_identity=f"agent:{IMPLEMENTER_AGENT}")


@check("worker.contract", "WORKER", "Kontrak request/response dan caps pekerja", component_id="opencode",
       needs_db=True, ttl_seconds=86400, invalidators=("binary:opencode", "model:route"), severity_fail="HIGH")
def worker_contract(ctx: CheckContext) -> Outcome:
    assert ctx.session is not None
    ev = ctx.session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "WORKER_CONTRACT_TEST",
                                                           EvidenceArtifact.trusted.is_(True),
                                                           EvidenceArtifact.mode == "LIVE", _NOT_OFFICE)
                            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    remedy = Remedy(operation="Jalankan uji kontrak worker (OpenCode asli terhadap endpoint loopback terskrip; tanpa "
                    "jaringan/kredensial)", command=aeos_cmd("worker contract-test"), privilege="pengguna lokal",
                    cost="tidak ada", evidence_to_collect="worker.contract VERIFIED")
    if ev is None:
        return Outcome("UNKNOWN", "Belum ada bukti tepercaya uji kontrak worker (tool/schema/patch, caps); NOT-PROVEN. "
                       "Hasil worker tetap REVIEW sampai verifier independen membuktikannya.",
                       error_code="WORKER_CONTRACT_NOT_PROVEN", remedy=remedy,
                       evidence={"looked_for": "EvidenceArtifact WORKER_CONTRACT_TEST (trusted, LIVE)",
                                 "residual_risks": ["Kontrak tool/schema/patch worker NOT-PROVEN pada host ini; output "
                                                    "worker hanya REVIEW sampai verifier independen lulus"]})
    inline = ev.inline or {}
    content = inline.get("content") if isinstance(inline.get("content"), dict) else inline.get("summary") or {}
    overall = str((content or {}).get("overall") or "UNKNOWN").upper()
    evidence: dict[str, Any] = {
        "evidence_id": ev.id, "level": ev.evidence_level, "claim": ev.claim[:200], "overall": overall,
        "residual_risks": ["Perilaku tool/schema model pada route nyata NOT-PROVEN (uji memakai model loopback "
                           "terskrip); dibuktikan per route oleh worker.route_behaviour (evaluasi/run terverifikasi)"]}
    if overall == "FAIL":
        return Outcome("FAILED", f"Uji kontrak worker {ev.id[:8]} GAGAL ({ev.created_at.isoformat()}).",
                       evidence=evidence, error_code="WORKER_CONTRACT_FAILED", remedy=remedy)
    if overall != "PASS":
        return Outcome("UNKNOWN", f"Uji kontrak worker {ev.id[:8]} tidak konklusif ({overall}); NOT-PROVEN.",
                       evidence=evidence, error_code="WORKER_CONTRACT_NOT_PROVEN", remedy=remedy)
    # §08 "engine/version": a contract test proves the engine version it ran against, not a later binary.
    recorded = str((content or {}).get("engine_version") or "")
    try:
        from aeos.worker_runtime import facade as worker

        current = worker.opencode_status()
    except Exception:  # worker runtime absent / probe error: the binding cannot be checked (never a pass)
        current = {}
    installed = str(current.get("version") or "") if str(current.get("status") or "").upper() == "INSTALLED" else ""
    evidence.update(recorded_engine_version=recorded or None, installed_engine_version=installed or None)
    if not recorded or not installed or recorded != installed:
        return Outcome("UNKNOWN", f"Uji kontrak worker {ev.id[:8]} tercatat untuk OpenCode {recorded or 'UNKNOWN'}, "
                       f"terpasang sekarang {installed or str(current.get('status') or 'UNKNOWN')}: bukti tidak berlaku "
                       "untuk engine ini; NOT-PROVEN sampai uji kontrak dijalankan ulang.", evidence=evidence,
                       error_code="WORKER_CONTRACT_STALE", remedy=remedy)
    return Outcome("VERIFIED", f"Uji kontrak engine worker {ev.id[:8]} lulus ({ev.created_at.isoformat()}); perilaku "
                   "model route nyata tetap NOT-PROVEN.", evidence=evidence)


# ----------------------------------------------------------------------------- §08 model tool/schema behaviour
# W6-021: a real route's tool/schema behaviour is proven only by trusted records of runs on that route: the §43
# reliability measure ``tool_schema_failures`` of a COMPLETED evaluation by a trusted collector on exactly the
# route's current configuration, and independently verified worker runs (runner-observed tool errors). The
# failure-rate threshold is the OWNER's (POLICY decision, target worker_route_behaviour:tool_schema_failures,
# details.max_failure_rate 0..1): without it a measured failure count is reported, never judged (UNKNOWN).
ROUTE_BEHAVIOUR_TARGET = ("worker_route_behaviour", "tool_schema_failures")
WORKER_ROUTE_STATUSES = ("APPROVED", "TRIAL")
TRUSTED_EVAL_SOURCES = frozenset({"verifier", "collector"})
_RANK = {"FAILED": 4, "UNKNOWN": 3, "STALE": 2, "VERIFIED": 1}


def _worker_routes(session: Any) -> list[Any]:
    from aeos.models.routing import ModelRoute

    rows = session.scalars(select(ModelRoute).where(ModelRoute.pool == "WORKER",
                                                    ModelRoute.status.in_(WORKER_ROUTE_STATUSES))
                           .order_by(ModelRoute.route_key))
    # platform view: a route eligible ONLY for OFFICE is never quoted here (§35)
    return [r for r in rows if not (r.eligible_domains and set(r.eligible_domains) <= {"OFFICE"})]


def _threshold(session: Any) -> tuple[float | None, str | None]:
    """(max failure rate, decision id) of the owner's POLICY decision, or (None, None) while UNDECIDED."""
    try:
        from aeos.policy import facade as policy

        d = policy.find_active_decision(session, kind="POLICY", target_type=ROUTE_BEHAVIOUR_TARGET[0],
                                        target_id=ROUTE_BEHAVIOUR_TARGET[1])
    except (ImportError, ModuleNotFoundError):
        return None, None
    if d is None or d.actor_kind != "HUMAN":
        return None, None
    raw: Any = (d.details or {}).get("max_failure_rate")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None, d.id  # an unreadable value is UNDECIDED, never a default
    try:
        value = float(raw)
    except ValueError:
        return None, d.id
    return (value, d.id) if 0.0 <= value <= 1.0 else (None, d.id)


def _route_behaviour_fp(ctx: CheckContext) -> dict[str, Any] | None:
    """STALE as soon as a worker route's status/config/harness, its evaluations or the owner threshold change."""
    if ctx.session is None:
        return None
    from sqlalchemy import func

    from aeos.models.research import EvalRun

    routes = _worker_routes(ctx.session)
    keys = [r.route_key for r in routes]
    evals = ctx.session.scalar(select(func.count()).select_from(EvalRun).where(EvalRun.route_key.in_(keys))) if keys \
        else 0
    _t, decision = _threshold(ctx.session)
    return {"routes": [[r.route_key, r.status, r.config_hash, r.harness_version] for r in routes], "evals": evals,
            "threshold_decision": decision}


def _verified_runs(session: Any, route: Any) -> dict[str, Any]:
    """Independently verified worker runs on the route (verifier PASS) and the tool errors the runner observed in
    them (``run.action`` events of kind ``tool_error`` written by the runner identity, never by the worker)."""
    from aeos.models.audit import Event
    from aeos.models.workforce import WorkerRun

    runs = list(session.scalars(select(WorkerRun).where(WorkerRun.model_route_key == route.route_key,
                                                        WorkerRun.verifier_status == "PASS",
                                                        or_(WorkerRun.domain.is_(None), WorkerRun.domain != "OFFICE"))
                                .order_by(WorkerRun.created_at.desc()).limit(200)))
    if route.harness_version:
        runs = [r for r in runs if r.engine_version == route.harness_version]
    ids = [r.id for r in runs]
    errors = 0
    if ids:
        errors = sum(1 for ev in session.scalars(select(Event).where(Event.source == "runner", Event.kind == "run.action",
                                                                     Event.run_id.in_(ids)))
                     if (ev.payload or {}).get("kind") == "tool_error")
    return {"verified_runs": len(ids), "tool_errors": errors}


def _route_verdict(session: Any, route: Any, *, threshold: float | None, installed: str | None) -> dict[str, Any]:
    from aeos.models.research import EvalRun

    out: dict[str, Any] = {"route_key": route.route_key, "route_status": route.status,
                           "config_hash": route.config_hash, "harness_version": route.harness_version,
                           **_verified_runs(session, route)}
    evals = [e for e in session.scalars(select(EvalRun).where(EvalRun.route_key == route.route_key,
                                                              EvalRun.status == "COMPLETED")
                                        .order_by(EvalRun.created_at.desc()).limit(50))
             if e.result_source in TRUSTED_EVAL_SOURCES and (e.mode or "LIVE") == "LIVE"
             and e.submitted_domain != "OFFICE"]
    current = [e for e in evals if route.config_hash and e.route_config_hash == route.config_hash]
    if not evals:
        return {**out, "verdict": "UNKNOWN", "why": "belum ada evaluasi tepercaya (COMPLETED, verifier/collector, LIVE) "
                "pada route ini; tool/schema NOT-PROVEN"}
    if not current:
        return {**out, "verdict": "STALE", "eval_run_id": evals[0].id,
                "why": f"evaluasi terakhir dibuat pada konfigurasi {str(evals[0].route_config_hash)[:19]}, route sekarang "
                       f"{str(route.config_hash)[:19]}: bukti tidak berlaku"}
    ev = current[0]
    recorded = str((ev.configuration or {}).get("harness_version") or "") or None
    out.update(eval_run_id=ev.id, sample_size=ev.sample_size, recorded_engine_version=recorded)
    if (route.harness_version and recorded and recorded != route.harness_version) or \
            (installed and recorded and recorded != installed):
        return {**out, "verdict": "STALE", "why": f"evaluasi dibuat dengan OpenCode {recorded}; route/engine sekarang "
                f"{route.harness_version or '-'} / terpasang {installed or 'UNKNOWN'}"}
    failures = ((ev.metrics or {}).get("reliability") or {}).get("tool_schema_failures")
    n = int(ev.sample_size or 0)
    if failures is None or n <= 0:
        return {**out, "verdict": "UNKNOWN", "why": "evaluasi tidak mencatat tool_schema_failures (UNKNOWN, bukan nol)"}
    out.update(tool_schema_failures=int(failures), failure_rate=round(int(failures) / n, 4))
    if int(failures) == 0:
        return {**out, "verdict": "VERIFIED", "why": f"0 kegagalan tool/schema dari {n} sampel"}
    if threshold is None:
        return {**out, "verdict": "UNKNOWN", "why": f"{failures} kegagalan tool/schema dari {n} sampel; ambang batas "
                "pemilik UNDECIDED (dilaporkan, tidak dinilai)"}
    if int(failures) / n > threshold:
        return {**out, "verdict": "FAILED", "why": f"{failures}/{n} kegagalan tool/schema melebihi ambang pemilik "
                f"{threshold}"}
    return {**out, "verdict": "VERIFIED", "why": f"{failures}/{n} kegagalan tool/schema dalam ambang pemilik {threshold}"}


@check("worker.route_behaviour", "WORKER", "Perilaku tool/schema route worker nyata", component_id="opencode",
       needs_db=True, ttl_seconds=3600, invalidators=("model:route", "config:routes", "binary:opencode"),
       fingerprint=_route_behaviour_fp, severity_fail="HIGH",
       description="Ukuran reliability tool_schema_failures (evaluasi tepercaya pada konfigurasi route saat ini) dan "
                   "run worker yang lulus verifier independen, per route worker APPROVED/TRIAL.")
def worker_route_behaviour(ctx: CheckContext) -> Outcome:
    assert ctx.session is not None
    routes = _worker_routes(ctx.session)
    threshold, decision = _threshold(ctx.session)
    try:
        from aeos.worker_runtime import facade as worker

        st = worker.opencode_status()
        installed = str(st.get("version") or "") if str(st.get("status") or "").upper() == "INSTALLED" else None
    except Exception:  # engine probe unavailable: the engine binding is simply not checked against it
        installed = None
    evidence: dict[str, Any] = {
        "threshold": {"max_failure_rate": threshold, "decision_id": decision,
                      "status": "DECIDED" if threshold is not None else "UNDECIDED",
                      "target": ":".join(ROUTE_BEHAVIOUR_TARGET)},
        "installed_engine_version": installed,
        "residual_risks": ["Perilaku tool/schema dibuktikan hanya oleh evaluasi/run nyata pada route itu; output worker "
                           "tetap REVIEW sampai verifier independen lulus"]}
    remedy = Remedy(operation="Aktifkan satu route worker lewat keputusan manusia, jalankan satu tugas terbatas dari "
                    "Control Center (/work) dan evaluasi terlindungi route itu agar ada bukti verifier/evaluasi",
                    command=aeos_cmd("routes activate <route_key>"), privilege="SUPER_ADMIN", cost="kuota route",
                    evidence_to_collect="worker.route_behaviour VERIFIED")
    if not routes:
        return Outcome("UNKNOWN", "Belum ada route worker APPROVED/TRIAL; perilaku tool/schema model NOT-PROVEN.",
                       evidence={**evidence, "routes": []}, error_code="NO_WORKER_ROUTE", remedy=remedy)
    verdicts = [_route_verdict(ctx.session, r, threshold=threshold, installed=installed) for r in routes]
    evidence["routes"] = verdicts
    worst = max(verdicts, key=lambda v: _RANK[v["verdict"]])["verdict"]
    lines = "; ".join(f"{v['route_key']}: {v['verdict']} ({v['why']})" for v in verdicts)[:900]
    if worst == "FAILED":
        return Outcome("FAILED", f"Route worker melebihi ambang kegagalan tool/schema pemilik - {lines}",
                       evidence=evidence, error_code="ROUTE_TOOL_SCHEMA_FAILURES", severity="HIGH", remedy=Remedy(
                           operation="Tinjau run/evaluasi yang gagal; nonaktifkan atau uji ulang route tersebut",
                           command=aeos_cmd("routes disable <route_key>"), privilege="SUPER_ADMIN",
                           evidence_to_collect="worker.route_behaviour VERIFIED"))
    if worst == "UNKNOWN":
        undecided = any("UNDECIDED" in v["why"] for v in verdicts)
        return Outcome("UNKNOWN", f"Perilaku tool/schema route worker NOT-PROVEN - {lines}", evidence=evidence,
                       error_code="ROUTE_BEHAVIOUR_THRESHOLD_UNDECIDED" if undecided else "ROUTE_BEHAVIOUR_NOT_PROVEN",
                       remedy=Remedy(
                           operation="Pemilik menetapkan ambang: keputusan POLICY target_type "
                                     f"{ROUTE_BEHAVIOUR_TARGET[0]}, target_id {ROUTE_BEHAVIOUR_TARGET[1]}, "
                                     "details.max_failure_rate (0..1) di Kesiapan -> worker.route_behaviour -> "
                                     "Tetapkan ambang pemilik, atau lewat CLI",
                           command=aeos_cmd("policy route-threshold --max-failure-rate <0..1> --domain PERSONAL "
                                            "--as <admin> --rationale \"<alasan>\""),
                           privilege="SUPER_ADMIN", evidence_to_collect="worker.route_behaviour")
                       if undecided else remedy)
    if worst == "STALE":
        return Outcome("STALE", f"Bukti perilaku tool/schema tidak berlaku untuk konfigurasi/engine sekarang - {lines}",
                       evidence=evidence, error_code="ROUTE_BEHAVIOUR_STALE", remedy=remedy)
    return Outcome("VERIFIED", f"Perilaku tool/schema route worker terbukti oleh evaluasi tepercaya - {lines}",
                   evidence=evidence)
