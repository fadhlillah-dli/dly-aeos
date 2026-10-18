"""§08 Host: OS/architecture/resource, disk, clock, process/port, filesystem permissions."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from aeos.doctor.checks import check
from aeos.doctor.checks._common import logical
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd
from aeos.inventory.probes import ProbeRefused

SUPPORTED_PLATFORMS = {"win32": "Windows", "darwin": "macOS", "linux": "Linux"}
DISK_FLOOR_BYTES = 1 << 30  # provisional operational floor (1 GiB); resource budget not yet set (§51 P2)
CLOCK_SKEW_SECONDS = 300


def tcp_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts TCP connections on host:port (loopback only)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def can_bind(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


@check("host.platform", "HOST", "OS, arsitektur dan resource", ttl_seconds=86400)
def host_platform(ctx: CheckContext) -> Outcome:
    evidence: dict[str, Any] = {
        "os": SUPPORTED_PLATFORMS.get(sys.platform, sys.platform), "release": platform.release(),
        "arch": platform.machine(), "cpu_count": os.cpu_count(), "python_bits": 64 if sys.maxsize > 2**32 else 32,
    }
    try:
        import psutil

        vm = psutil.virtual_memory()
        evidence["ram_total_gib"] = round(vm.total / (1 << 30), 1)
        evidence["ram_available_gib"] = round(vm.available / (1 << 30), 1)
    except Exception:  # psutil missing/limited: resource facts become UNKNOWN, not guessed
        evidence["ram_total_gib"] = None
    evidence["resource_budget"] = "UNKNOWN: batas resource belum ditetapkan dari mesin nyata (§51 P2)"
    if sys.platform not in SUPPORTED_PLATFORMS:
        return Outcome("FAILED", f"Platform {sys.platform} tidak didukung (Windows/macOS/Linux).", evidence=evidence,
                       error_code="UNSUPPORTED_PLATFORM", severity="HIGH")
    return Outcome("VERIFIED", f"{evidence['os']} {evidence['arch']}, {evidence['cpu_count']} CPU, RAM "
                   f"{evidence.get('ram_total_gib')} GiB (skenario: platform didukung).", evidence=evidence,
                   target_identity=f"{evidence['os']}/{evidence['arch']}")


def _existing_parent(path: Path) -> Path:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


@check("host.disk", "HOST", "Ruang disk data dir", ttl_seconds=3600, severity_fail="HIGH")
def host_disk(ctx: CheckContext) -> Outcome:
    base = _existing_parent(ctx.data_dir)
    try:
        usage = shutil.disk_usage(base)
    except OSError as exc:
        return Outcome("UNKNOWN", f"Ruang disk tidak dapat dibaca ({type(exc).__name__}).", error_code="DISK_UNREADABLE")
    free_gib = round(usage.free / (1 << 30), 2)
    evidence = {"free_gib": free_gib, "total_gib": round(usage.total / (1 << 30), 1), "measured_at": logical(ctx, base),
                "floor_gib": 1, "floor_note": "ambang sementara; budget resource belum ditetapkan (§51)"}
    if usage.free < DISK_FLOOR_BYTES:
        return Outcome("FAILED", f"Ruang kosong {free_gib} GiB di bawah ambang 1 GiB.", evidence=evidence,
                       error_code="DISK_LOW", remedy=Remedy(
                           operation="Kosongkan ruang disk atau pindahkan data dir (AEOS_DATA_DIR)",
                           target=logical(ctx, base), privilege="pengguna lokal",
                           rollback="tidak berlaku", evidence_to_collect="ruang kosong >= 1 GiB"))
    return Outcome("VERIFIED", f"Ruang kosong {free_gib} GiB pada lokasi data.", evidence=evidence)


@check("host.clock", "HOST", "Kewajaran jam sistem", ttl_seconds=3600)
def host_clock(ctx: CheckContext) -> Outcome:
    evidence: dict[str, Any] = {"utc_now": ctx.now.isoformat(), "ntp_compared": False}
    if ctx.session is None:
        return Outcome("UNKNOWN", "Tidak ada database untuk membandingkan waktu tercatat; NTP tidak diperiksa.",
                       evidence=evidence)
    from aeos.models.audit import AuditEvent, Event

    latest = [ctx.session.scalar(select(func.max(AuditEvent.at))), ctx.session.scalar(select(func.max(Event.at)))]
    latest_at = max((t for t in latest if t is not None), default=None)
    if latest_at is not None and latest_at.tzinfo is None:
        from aeos.core.timeutil import ensure_aware

        latest_at = ensure_aware(latest_at)
    evidence["latest_record_at"] = latest_at.isoformat() if latest_at else None
    if latest_at is not None and (latest_at - ctx.now).total_seconds() > CLOCK_SKEW_SECONDS:
        return Outcome("FAILED", "Jam sistem lebih awal dari record terakhir (jam mundur); TTL/expiry tidak dapat "
                       "dipercaya.", evidence=evidence, error_code="CLOCK_BEHIND", severity="HIGH",
                       remedy=Remedy(operation="Sinkronkan jam sistem dengan sumber waktu OS",
                                     privilege="admin OS", evidence_to_collect="jam >= record terakhir"))
    return Outcome("VERIFIED", "Tidak ada tanda jam mundur terhadap record terakhir (NTP tidak dibandingkan).",
                   evidence=evidence)


def _port_fp(ctx: CheckContext) -> dict[str, Any]:
    return {"host": ctx.settings.host, "port": ctx.settings.port}


@check("host.port", "HOST", "Port lokal control plane", ttl_seconds=900, invalidators=("config:port",),
       ok={"VERIFIED", "CONNECTED"}, severity_fail="HIGH", fingerprint=_port_fp)
def host_port(ctx: CheckContext) -> Outcome:
    host, port = ctx.settings.host, ctx.settings.port
    evidence: dict[str, Any] = {"host": host, "port": port, "bind_policy": "loopback saja; tidak pernah 0.0.0.0"}
    find_cmd = (f"Get-NetTCPConnection -LocalPort {port} -State Listen | Select-Object OwningProcess"
                if sys.platform == "win32" else f"lsof -nP -iTCP:{port} -sTCP:LISTEN")
    if tcp_in_use(host, port):
        try:
            hr = ctx.runner.http_get(f"http://{host}:{port}/api/health", timeout=2.0)
        except ProbeRefused:
            hr = None
        body = hr.body if hr is not None and isinstance(hr.body, dict) else {}
        if hr is not None and hr.status == 200 and body.get("status") == "ok" and "version" in body:
            evidence["running_version"] = body.get("version")
            return Outcome("CONNECTED", f"DLY AEOS {body.get('version')} sudah berjalan di {host}:{port}.",
                           evidence=evidence, target_identity=f"{host}:{port}")
        return Outcome("FAILED", f"Port {port} di {host} dipakai proses lain; AEOS tidak akan pindah ke antarmuka "
                       "publik atau mematikan auth.", evidence=evidence, error_code="PORT_IN_USE",
                       target_identity=f"{host}:{port}",
                       remedy=Remedy(operation="Lihat proses yang memakai port (perintah di bawah), hentikan bila "
                                     f"milik Anda, atau jalankan AEOS di port lain: {aeos_cmd(f'serve --port {port + 1}')}",
                                     command=find_cmd, target=f"{host}:{port}", privilege="pengguna lokal",
                                     rollback="kembali ke port semula", evidence_to_collect="port bebas atau milik AEOS"))
    if can_bind(host, port):
        return Outcome("VERIFIED", f"Port {port} di {host} tersedia.", evidence=evidence, target_identity=f"{host}:{port}")
    return Outcome("FAILED", f"Port {port} tidak dapat di-bind di {host} (izin/kebijakan OS).", evidence=evidence,
                   error_code="PORT_BIND_DENIED", target_identity=f"{host}:{port}",
                   remedy=Remedy(operation="Pilih port lain yang diizinkan", command=aeos_cmd(f"serve --port {port + 1}"),
                                 target=f"{host}:{port}", evidence_to_collect="port dapat di-bind"))


def _data_fp(ctx: CheckContext) -> dict[str, Any]:
    return {"data_dir": str(ctx.data_dir)}


@check("host.data_dir", "HOST", "Izin tulis data directory", ttl_seconds=3600, invalidators=("config:data_dir",),
       severity_fail="CRITICAL", fingerprint=_data_fp)
def host_data_dir(ctx: CheckContext) -> Outcome:
    d = ctx.data_dir
    evidence: dict[str, Any] = {"data_dir": "<data>",
                                "separate_from_repo": not d.resolve().is_relative_to(ctx.repo.resolve())}
    if not evidence["separate_from_repo"]:
        return Outcome("FAILED", "Data directory berada di dalam source tree; runtime state tidak boleh di repo (§06).",
                       evidence=evidence, error_code="DATA_DIR_IN_REPO", severity="CRITICAL",
                       remedy=Remedy(operation="Pindahkan data dir ke lokasi OS default (hapus AEOS_DATA_DIR)",
                                     privilege="pengguna lokal", evidence_to_collect="data dir di luar repo"))
    if not d.is_dir():
        return Outcome("MISSING", "Data directory belum dibuat.", evidence=evidence, error_code="DATA_DIR_MISSING",
                       remedy=Remedy(operation="Buat data dir dan database lokal (dengan backup bila sudah ada)",
                                     command=aeos_cmd("db upgrade"), source="aeos.core.migrate",
                                     target="<data>", privilege="pengguna lokal",
                                     rollback="hapus data dir baru (belum berisi data)",
                                     evidence_to_collect="data dir ada dan dapat ditulis"))
    probe = d / ".aeos-doctor-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Outcome("FAILED", f"Data directory tidak dapat ditulis ({type(exc).__name__}).", evidence=evidence,
                       error_code="DATA_DIR_NOT_WRITABLE", remedy=Remedy(
                           operation="Perbaiki izin data dir untuk pengguna saat ini", target="<data>",
                           privilege="pengguna lokal/admin OS", evidence_to_collect="tes tulis berhasil"))
    if os.name == "posix":
        mode = d.stat().st_mode
        evidence["world_writable"] = bool(mode & 0o002)
        if mode & 0o002:
            return Outcome("FAILED", "Data directory dapat ditulis semua pengguna (world-writable).", evidence=evidence,
                           error_code="DATA_DIR_WORLD_WRITABLE", remedy=Remedy(
                               operation="Batasi izin data dir", command="chmod 700 <data dir>",
                               privilege="pengguna lokal", rollback="chmod ke mode sebelumnya",
                               evidence_to_collect="mode tanpa bit world-writable"))
    else:
        evidence["acl_checked"] = False
    return Outcome("VERIFIED", "Data directory ada dan tes tulis berhasil.", evidence=evidence, target_identity="<data>")
