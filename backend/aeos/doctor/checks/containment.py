"""§08 Containment: allowed FS/egress, credential isolation, process kill, protected policy/CI/test paths."""

from __future__ import annotations

import re
from typing import Any

from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, resolve_binary, run_version
from aeos.doctor.model import CheckContext, Outcome, Remedy


@check("containment.sandbox", "CONTAINMENT", "Level isolasi sandbox yang tersedia", component_id="worker-runtime",
       ttl_seconds=86400, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH", invalidators=("binary:docker",))
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
    if not isinstance(caps, dict):
        return Outcome("UNKNOWN", "Kapabilitas sandbox tidak dikenali.", error_code="SANDBOX_UNRECOGNISED")
    available = [k for k, v in caps.items() if (v.get("available") if isinstance(v, dict) else bool(v))]
    if not available:
        return Outcome("MISSING", "Tidak ada level isolasi yang tersedia; pekerjaan worker BLOCKED.",
                       evidence={"capabilities": caps}, error_code="NO_SANDBOX")
    return Outcome("CONFIGURED", f"Isolasi tersedia: {', '.join(available)} (efektivitas dibuktikan oleh containment.report).",
                   evidence={"available": available, "capabilities": caps})


@check("containment.report", "CONTAINMENT", "Uji negatif containment (FS/egress/credential/kill)",
       component_id="worker-runtime", ttl_seconds=86400, functional=True, severity_fail="CRITICAL",
       observation="functional", invalidators=("binary:docker", "binary:opencode"))
def containment_report(ctx: CheckContext) -> Outcome:
    try:
        from aeos.worker_runtime import facade as worker

        report = worker.containment_report("process")
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul containment belum tersedia; uji negatif tidak dijalankan (NOT-PROVEN).",
                       error_code="CONTAINMENT_NOT_IMPLEMENTED")
    except Exception as exc:
        return Outcome("FAILED", f"Uji containment error ({type(exc).__name__}).", error_code="CONTAINMENT_ERROR")
    if not isinstance(report, dict):
        return Outcome("UNKNOWN", "Laporan containment tidak dikenali.", error_code="CONTAINMENT_UNRECOGNISED")
    tests = report.get("tests") or report.get("results") or {}
    if isinstance(tests, list):
        tests = {str(t.get("name", i)): t for i, t in enumerate(tests) if isinstance(t, dict)}
    failed = [k for k, v in tests.items() if not (v.get("passed") if isinstance(v, dict) else bool(v))]
    evidence = {"sandbox_kind": report.get("sandbox_kind", "process"), "tests": len(tests), "failed": failed}
    if not tests:
        return Outcome("UNKNOWN", "Laporan containment tanpa hasil uji.", evidence=evidence,
                       error_code="CONTAINMENT_EMPTY")
    if failed:
        return Outcome("FAILED", f"Uji containment gagal: {', '.join(failed)}.", evidence=evidence,
                       error_code="CONTAINMENT_FAILED")
    return Outcome("VERIFIED", f"{len(tests)} uji negatif containment lulus pada sandbox {evidence['sandbox_kind']}.",
                   evidence=evidence)


@check("containment.docker", "CONTAINMENT", "Docker (sandbox container opsional)", component_id="docker",
       ttl_seconds=3600, ok={"CONNECTED", "CONFIGURED", "VERIFIED"}, invalidators=("binary:docker",),
       fingerprint=binary_fingerprint("docker"), severity_fail="LOW", observation="installation")
def containment_docker(ctx: CheckContext) -> Outcome:
    path, _ = resolve_binary(ctx, "docker")
    if not path:
        return Outcome("MISSING", "Docker tidak ditemukan (opsional; tidak memblokir capability lain).", severity="LOW",
                       error_code="DOCKER_MISSING", remedy=Remedy(
                           operation="Opsional: pasang Docker Desktop/Engine bila sandbox container dibutuhkan",
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


_PROTECTED_RE = re.compile(r"`([^`]+\*\*?)`")


@check("containment.protected_paths", "CONTAINMENT", "Protected paths self-build terdefinisi",
       component_id="trusted-self-build-seed", ttl_seconds=86400, ok={"CONFIGURED", "VERIFIED"})
def containment_protected_paths(ctx: CheckContext) -> Outcome:
    agents = ctx.repo / "AGENTS.md"
    if not agents.is_file():
        return Outcome("MISSING", "AGENTS.md tidak ada; protected paths tidak terdefinisi.", error_code="AGENTS_MISSING")
    text = agents.read_text(encoding="utf-8", errors="replace")
    start = text.find("## Protected paths")
    section = text[start:text.find("\n## ", start + 5)] if start >= 0 else ""
    paths = _PROTECTED_RE.findall(section)
    if not paths:
        return Outcome("MISSING", "Bagian 'Protected paths' tidak ditemukan di AGENTS.md.", error_code="NO_PROTECTED_PATHS")
    return Outcome("CONFIGURED", f"{len(paths)} protected path terdefinisi; penegakan oleh broker/verifier dibuktikan "
                   "terpisah (NOT-PROVEN di sini).", evidence={"paths": paths, "source": "AGENTS.md"})
