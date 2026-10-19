"""§08 Containment: allowed FS/egress, credential isolation, process kill, protected policy/CI/test paths."""

from __future__ import annotations

from typing import Any

from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, file_fingerprint, resolve_binary, run_version
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd, os_install_command

SANDBOX_DOMAIN = "PERSONAL"  # OFFICE is always BLOCKED by the worker runtime (ADR-0051); the gate re-asks per domain
EXCEPTION_HINT = ("keputusan EXCEPTION pemilik: target worker_sandbox/process, domain PERSONAL, "
                  "details.allow_process_sandbox=true (ADR-0051; residual risk diterima secara eksplisit)")


def _sandbox_fp(ctx: CheckContext) -> dict[str, Any]:
    """Cheap identity of what the sandbox requirement depends on: the docker binary and the owner's
    process-sandbox exception (a revoked/expired/new decision makes the stored result STALE)."""
    out: dict[str, Any] = {"docker": binary_fingerprint("docker")(ctx)}
    if ctx.session is not None:
        from sqlalchemy import select

        from aeos.models.decisions import Decision

        rows = ctx.session.execute(select(Decision.id, Decision.status, Decision.expires_at).where(
            Decision.kind == "EXCEPTION", Decision.target_type == "worker_sandbox")).all()
        out["exceptions"] = sorted((r[0], r[1], str(r[2])) for r in rows)
    return out


def sandbox_readiness_view(session: Any, domain: str) -> dict[str, Any]:
    """``aeos.worker_runtime.facade.sandbox_readiness`` (fail closed: any error -> BLOCKED)."""
    try:
        from aeos.worker_runtime import facade as worker

        sb = worker.sandbox_readiness(session, domain=domain)
    except (ImportError, ModuleNotFoundError):
        return {"status": "BLOCKED", "reasons": ["worker runtime (sandbox) module not available"], "remedies": [],
                "residual_risks": []}
    except Exception as exc:  # fail closed, never READY
        return {"status": "BLOCKED", "reasons": [f"sandbox readiness could not be evaluated ({type(exc).__name__})"],
                "remedies": [], "residual_risks": []}
    return sb if isinstance(sb, dict) else {"status": "BLOCKED", "reasons": ["unrecognised sandbox readiness"],
                                            "remedies": [], "residual_risks": []}


@check("containment.sandbox", "CONTAINMENT", "Sandbox worker memenuhi syarat (ADR-0051)", component_id="worker-runtime",
       ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH",
       invalidators=("binary:docker", "config:policy", "config:sandbox"), fingerprint=_sandbox_fp,
       description="Level isolasi host + syarat policy dari worker_runtime.facade.sandbox_readiness (PERSONAL).")
def containment_sandbox(ctx: CheckContext) -> Outcome:
    try:
        from aeos.worker_runtime import facade as worker

        caps = worker.sandbox_capabilities()
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul sandbox belum tersedia; isolasi pekerja tidak dapat dinilai (worker diblokir).",
                       error_code="SANDBOX_NOT_IMPLEMENTED")
    except Exception as exc:
        return Outcome("UNKNOWN", f"Kapabilitas sandbox gagal dibaca ({type(exc).__name__}).",
                       error_code="SANDBOX_STATUS_ERROR")
    levels = caps.get("levels") if isinstance(caps, dict) else None
    if not isinstance(levels, dict):
        return Outcome("UNKNOWN", "Kapabilitas sandbox tidak dikenali.", error_code="SANDBOX_UNRECOGNISED")
    level_view = {k: {"available": bool(v.get("available")), "strength": v.get("strength")}
                  for k, v in levels.items() if isinstance(v, dict)}
    available = [f"{k} ({v['strength']})" for k, v in level_view.items() if v["available"]]
    evidence: dict[str, Any] = {"levels": level_view, "best_available": caps.get("best_available"),
                                "evaluated_domain": SANDBOX_DOMAIN}
    if ctx.session is None:
        return Outcome("UNKNOWN", f"Level tersedia: {', '.join(available) or 'tidak ada'}; syarat policy (keputusan "
                       "pengecualian) tidak dapat dinilai tanpa database.", evidence=evidence,
                       error_code="DB_NOT_INITIALISED")
    sb = sandbox_readiness_view(ctx.session, SANDBOX_DOMAIN)
    reasons = [str(r) for r in sb.get("reasons") or []]
    risks = [str(r) for r in sb.get("residual_risks") or []]
    # Accepted risks only when the requirement is met; otherwise they are what an exception WOULD accept.
    risk_key = "residual_risks" if sb.get("status") == "READY" else "residual_risks_if_exception"
    evidence.update({"readiness": sb.get("status"), "kind": sb.get("kind"), "reasons": reasons,
                     risk_key: risks, "required": sb.get("required"), "decision_id": sb.get("decision_id")})
    if sb.get("status") == "READY" and sb.get("kind") == "container":
        return Outcome("VERIFIED", "Sandbox container siap: laporan containment container lulus pada host ini.",
                       evidence=evidence, target_identity="sandbox:container")
    if sb.get("status") == "READY":
        return Outcome("CONFIGURED", "Hanya process sandbox (WEAK) yang dipakai, diizinkan oleh keputusan pengecualian "
                       f"pemilik {str(sb.get('decision_id') or '')[:8]}; {len(risks)} residual risk tetap berlaku "
                       "(tanpa batas FS/egress OS).", evidence=evidence, severity="HIGH",
                       target_identity="sandbox:process")
    return Outcome("MISSING", f"Sandbox worker BLOCKED untuk PERSONAL: {'; '.join(reasons) or 'syarat tidak terpenuhi'}. "
                   f"Level tersedia: {', '.join(available) or 'tidak ada'}.", evidence=evidence,
                   error_code="SANDBOX_NOT_READY", remedy=Remedy(
                       operation="Pasang/jalankan Docker lalu buktikan containment container, ATAU catat "
                       + EXCEPTION_HINT + " lewat halaman Keputusan",
                       command=aeos_cmd("worker containment --kind container"),
                       source="ADR-0051 (persyaratan sandbox worker)", target="sandbox worker host ini",
                       privilege="pemilik (keputusan manusia)", cost="tidak ada",
                       rollback="cabut keputusan pengecualian", evidence_to_collect="containment.sandbox CONFIGURED/VERIFIED"))


_REPORT_FAIL = "FAIL"


@check("containment.report", "CONTAINMENT", "Uji negatif containment (FS/egress/credential/kill)",
       component_id="worker-runtime", ttl_seconds=86400, functional=True, severity_fail="CRITICAL",
       observation="functional", invalidators=("binary:docker", "binary:opencode"))
def containment_report(ctx: CheckContext) -> Outcome:
    try:
        from aeos.worker_runtime import facade as worker

        kind = (worker.sandbox_capabilities() or {}).get("best_available") or "process"
        report = worker.containment_report(kind)
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul containment belum tersedia; uji negatif tidak dijalankan (NOT-PROVEN).",
                       error_code="CONTAINMENT_NOT_IMPLEMENTED")
    except Exception as exc:
        return Outcome("FAILED", f"Uji containment error ({type(exc).__name__}).", error_code="CONTAINMENT_ERROR")
    if not isinstance(report, dict):
        return Outcome("UNKNOWN", "Laporan containment tidak dikenali.", error_code="CONTAINMENT_UNRECOGNISED")
    results = [r for r in report.get("results") or [] if isinstance(r, dict)]
    by_status: dict[str, list[str]] = {}
    for r in results:
        by_status.setdefault(str(r.get("status")), []).append(str(r.get("check")))
    overall = str(report.get("overall") or "").upper()
    evidence: dict[str, Any] = {"sandbox_kind": report.get("kind", kind), "overall": overall,
                                "evidence_level": report.get("evidence_level"), "summary": report.get("summary"),
                                "failed": by_status.get(_REPORT_FAIL, []),
                                "not_enforced": by_status.get("NOT_ENFORCED", []),
                                "passed": by_status.get("PASS", []), "not_run": by_status.get("NOT_RUN", [])}
    if not results:
        return Outcome("UNKNOWN", "Laporan containment tanpa hasil uji.", evidence=evidence, error_code="CONTAINMENT_EMPTY")
    if overall == _REPORT_FAIL or evidence["failed"]:
        return Outcome("FAILED", f"Uji containment gagal: {', '.join(evidence['failed']) or overall}.", evidence=evidence,
                       error_code="CONTAINMENT_FAILED")
    if overall == "PASS":
        return Outcome("VERIFIED", f"{len(results)} uji negatif containment lulus pada sandbox {evidence['sandbox_kind']}.",
                       evidence=evidence)
    if overall == "WEAK":
        evidence["residual_risks"] = [f"containment {c}: NOT_ENFORCED (process sandbox tanpa batas OS)"
                                      for c in evidence["not_enforced"]]
        return Outcome("CONFIGURED", f"Process sandbox WEAK: {len(evidence['passed'])} lulus, "
                       f"{len(evidence['not_enforced'])} NOT_ENFORCED ({', '.join(evidence['not_enforced'])}); "
                       "bukan lulus, hanya residual risk yang harus diterima pemilik (ADR-0051).", evidence=evidence,
                       severity="HIGH", error_code="CONTAINMENT_WEAK")
    return Outcome("UNKNOWN", f"Containment {evidence['sandbox_kind']} {overall or 'UNKNOWN'} (NOT-PROVEN).",
                   evidence=evidence, error_code="CONTAINMENT_NOT_PROVEN")


@check("containment.docker", "CONTAINMENT", "Docker (sandbox container opsional)", component_id="docker",
       ttl_seconds=3600, ok={"CONNECTED", "CONFIGURED", "VERIFIED"}, invalidators=("binary:docker",),
       fingerprint=binary_fingerprint("docker"), severity_fail="LOW", observation="installation")
def containment_docker(ctx: CheckContext) -> Outcome:
    path, _ = resolve_binary(ctx, "docker")
    if not path:
        return Outcome("MISSING", "Docker tidak ditemukan (opsional; tidak memblokir capability lain).", severity="LOW",
                       error_code="DOCKER_MISSING", remedy=Remedy(
                           operation="Opsional: pasang Docker Desktop/Engine bila sandbox container dibutuhkan",
                           command=os_install_command(windows="winget install --id Docker.DockerDesktop -e",
                                                      macos="brew install --cask docker"),
                           source="docs.docker.com (Docker Desktop; Linux: Docker Engine dari paket resmi)",
                           privilege="admin OS", cost="lisensi Docker Desktop belum diperiksa",
                           rollback="uninstall Docker", evidence_to_collect="docker --version"))
    client, _rr = run_version(ctx, path, ["--version"], regex=r"(\d+\.\d+\.\d+)")
    server, _rr2 = run_version(ctx, path, ["version", "--format", "{{.Server.Version}}"], regex=r"(\d+\.\d+\.\d+)",
                               timeout=20.0)
    evidence: dict[str, Any] = {"client_version": client, "server_version": server,
                                "note": "container standar bukan jaminan isolasi kuat (§25)"}
    if server:
        return Outcome("CONNECTED", f"Docker daemon {server} terjangkau (efektivitas isolasi belum diuji).",
                       evidence=evidence, component_version=client)
    return Outcome("CONFIGURED", f"Docker client {client or 'UNKNOWN'} ada, daemon tidak terjangkau.", evidence=evidence,
                   component_version=client, severity="LOW")


def enforced_protected_lists() -> dict[str, tuple[str, ...]]:
    """The protected-path lists the code actually enforces (worker scope, verifier quarantine, DAG fallback)."""
    from aeos.planning.dag import FALLBACK_PROTECTED_PATHS
    from aeos.verification.scope import PLATFORM_PROTECTED_PATHS as VERIFIER
    from aeos.worker_runtime.scope import PLATFORM_PROTECTED_PATHS as WORKER

    return {"worker_runtime.scope": tuple(WORKER), "verification.scope": tuple(VERIFIER),
            "planning.dag.FALLBACK": tuple(FALLBACK_PROTECTED_PATHS)}


def protected_self_test(paths: list[str]) -> dict[str, Any]:
    """A synthetic diff touching one file under every protected path must be quarantined by the verifier's scope
    check (PROTECTED_PATH violation), and an ordinary docs file must not be."""
    from aeos.verification.scope import PLATFORM_PROTECTED_PATHS, check_scope

    probes = {p: (p.replace("**", "aeos-doctor-probe.txt").replace("*", "aeos-doctor-probe") if "*" in p
                  else p) for p in paths}
    res = check_scope([*probes.values(), "docs/aeos-doctor-probe.md"], allowed=["docs/**"], forbidden=[],
                      protected=list(PLATFORM_PROTECTED_PATHS))
    quarantined = {v.path for v in res.violations if v.rule in ("PROTECTED_PATH", "PROTECTED_PATH_ALLOWED")}
    missed = sorted(p for p, f in probes.items() if f not in quarantined)
    docs_flagged = "docs/aeos-doctor-probe.md" in quarantined
    return {"probes": len(probes), "quarantined": len(quarantined & set(probes.values())), "missed": missed,
            "docs_flagged": docs_flagged, "ok": not missed and not docs_flagged}


@check("containment.protected_paths", "CONTAINMENT", "Protected paths self-build terdefinisi dan ditegakkan",
       component_id="trusted-self-build-seed", ttl_seconds=86400, ok={"CONFIGURED", "VERIFIED"},
       fingerprint=file_fingerprint("AGENTS.md"),
       description="Daftar AGENTS.md harus sama dengan daftar yang ditegakkan kode (worker_runtime.scope, "
                   "verification.scope, planning.dag fallback); uji mandiri: diff sintetis pada setiap protected path "
                   "dikarantina oleh scope check verifier. Isolasi FS/egress sandbox dibuktikan containment.report.")
def containment_protected_paths(ctx: CheckContext) -> Outcome:
    agents = ctx.repo / "AGENTS.md"
    if not agents.is_file():
        return Outcome("MISSING", "AGENTS.md tidak ada; protected paths tidak terdefinisi.", error_code="AGENTS_MISSING")
    text = agents.read_text(encoding="utf-8", errors="replace")
    # The canonical reading of the section (aeos.core.protected_paths: every backticked pattern of its first
    # paragraph - globs and exact files such as AGENTS.md or pyproject.toml; later paragraphs only explain it).
    from aeos.core.protected_paths import parse_agents_md

    paths = list(parse_agents_md(text))
    if not paths:
        return Outcome("MISSING", "Bagian 'Protected paths' tidak ditemukan di AGENTS.md.", error_code="NO_PROTECTED_PATHS")
    evidence: dict[str, Any] = {"paths": paths, "source": "AGENTS.md"}
    try:
        lists = enforced_protected_lists()
    except (ImportError, ModuleNotFoundError) as exc:
        return Outcome("UNKNOWN", f"Daftar yang ditegakkan tidak dapat dibaca ({type(exc).__name__}).",
                       evidence=evidence, error_code="PROTECTED_LISTS_UNREADABLE")
    documented = set(paths)
    diffs = {name: {"missing_in_code": sorted(documented - set(lst)), "extra_in_code": sorted(set(lst) - documented)}
             for name, lst in lists.items() if set(lst) != documented}
    evidence["enforced_lists"] = {k: list(v) for k, v in lists.items()}
    if diffs:
        evidence["differences"] = diffs
        return Outcome("FAILED", "Daftar protected path AGENTS.md berbeda dengan yang ditegakkan kode: "
                       + ", ".join(sorted(diffs)) + ".", evidence=evidence, error_code="PROTECTED_PATHS_DIVERGED",
                       severity="HIGH", remedy=Remedy(
                           operation="Samakan AGENTS.md dan daftar di kode lewat PR yang direview (protected path)",
                           target="AGENTS.md, worker_runtime/scope.py, verification/scope.py, planning/dag.py",
                           privilege="reviewer manusia", evidence_to_collect="containment.protected_paths VERIFIED"))
    test = protected_self_test(paths)
    evidence["self_test"] = test
    if not test["ok"]:
        return Outcome("FAILED", "Uji mandiri: verifier tidak mengkarantina diff pada " + (", ".join(test["missed"])
                       or "path biasa (docs) yang salah ditandai") + ".", evidence=evidence,
                       error_code="PROTECTED_PATH_NOT_QUARANTINED", severity="HIGH")
    return Outcome("VERIFIED", f"{len(paths)} protected path identik di AGENTS.md dan kode; diff sintetis pada setiap "
                   "path dikarantina verifier (skenario: scope check). Isolasi sandbox dibuktikan terpisah.",
                   evidence=evidence)
