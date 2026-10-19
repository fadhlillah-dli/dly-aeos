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
DISK_FLOOR_BYTES = 1 << 30  # provisional operational floor (1 GiB) while the owner has not set a budget (§51 P2)
CLOCK_SKEW_SECONDS = 300
BUDGET_KEY = "doctor.resource_budget"
BUDGET_FIELDS = ("cpu_cores", "ram_gib", "disk_gib", "max_parallel_workers")


def resource_budget(session: Any) -> dict[str, Any] | None:
    """The owner's hardware/OS resource budget (§51 P2), set with ``aeos doctor budget``; None = not set."""
    if session is None:
        return None
    from aeos.models.identity import SystemSetting

    try:
        row = session.get(SystemSetting, BUDGET_KEY)
    except Exception:  # table missing (bootstrap): treated as not set
        return None
    return dict(row.value) if row is not None and isinstance(row.value, dict) else None


def set_resource_budget(session: Any, *, actor: str, reason: str, cpu_cores: int | None = None,
                        ram_gib: float | None = None, disk_gib: float | None = None,
                        max_parallel_workers: int | None = None) -> dict[str, Any]:
    """Record the owner's resource budget (audited, emits ``doctor.resource_budget.changed``, marks the host
    checks STALE). Values are the owner's choice for this machine; nothing is measured or guessed here."""
    from aeos.audit import service as audit
    from aeos.core.errors import ValidationFailed
    from aeos.core.timeutil import utcnow
    from aeos.events import service as events
    from aeos.models.identity import SystemSetting

    values = {"cpu_cores": cpu_cores, "ram_gib": ram_gib, "disk_gib": disk_gib,
              "max_parallel_workers": max_parallel_workers}
    for k, v in values.items():
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            raise ValidationFailed(f"{k} must be a positive number")
    if all(v is None for v in values.values()):
        raise ValidationFailed("set at least one budget value")
    if not (reason or "").strip():
        raise ValidationFailed("reason is required")
    row = session.get(SystemSetting, BUDGET_KEY)
    before = dict(row.value) if row is not None and isinstance(row.value, dict) else None
    value = {**(before or {}), **{k: v for k, v in values.items() if v is not None},
             "set_by": actor, "set_at": utcnow().isoformat(), "reason": reason.strip()[:500]}
    if row is None:
        session.add(SystemSetting(key=BUDGET_KEY, value=value))
    else:
        row.value = value
        row.version = (row.version or 1) + 1
    session.flush()
    audit.record(session, actor_type="user" if actor not in ("cli", "local-operator") else "cli", actor_id=actor,
                 action="doctor.resource_budget.set", target_type="system_setting", target_id=BUDGET_KEY,
                 reason=reason[:500], details={"before": before, "after": {k: value.get(k) for k in BUDGET_FIELDS}})
    events.emit(session, source="doctor", kind="doctor.resource_budget.changed", entity_type="system_setting",
                entity_id=BUDGET_KEY, payload={k: value.get(k) for k in BUDGET_FIELDS})
    from aeos.doctor.capabilities import invalidate

    invalidate(session, invalidator="config:resource_budget", reason="resource budget pemilik berubah")
    return value


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


def budget_remedy() -> Remedy:
    return Remedy(operation="Tetapkan budget resource mesin ini (keputusan pemilik dari mesin nyata, §51 P2)",
                  command=aeos_cmd("doctor budget --cpu <n> --ram <GiB> --disk <GiB> --max-workers <n> --reason <teks>"),
                  target="system setting doctor.resource_budget", privilege="pemilik (operator lokal)",
                  rollback="jalankan ulang dengan nilai sebelumnya", evidence_to_collect="host.platform/host.disk")


@check("host.platform", "HOST", "OS, arsitektur dan resource", ttl_seconds=86400,
       invalidators=("config:resource_budget",))
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
    budget = resource_budget(ctx.session)
    if sys.platform not in SUPPORTED_PLATFORMS:
        return Outcome("FAILED", f"Platform {sys.platform} tidak didukung (Windows/macOS/Linux).", evidence=evidence,
                       error_code="UNSUPPORTED_PLATFORM", severity="HIGH")
    if budget is None:
        evidence["resource_budget"] = "UNKNOWN: batas resource belum ditetapkan pemilik dari mesin nyata (§51 P2)"
        return Outcome("VERIFIED", f"{evidence['os']} {evidence['arch']}, {evidence['cpu_count']} CPU, RAM "
                       f"{evidence.get('ram_total_gib')} GiB (skenario: platform didukung; budget resource UNKNOWN).",
                       evidence=evidence, target_identity=f"{evidence['os']}/{evidence['arch']}", remedy=budget_remedy())
    evidence["resource_budget"] = {k: budget.get(k) for k in BUDGET_FIELDS}
    short: list[str] = []
    if budget.get("cpu_cores") and (evidence["cpu_count"] or 0) < budget["cpu_cores"]:
        short.append(f"CPU {evidence['cpu_count']} < budget {budget['cpu_cores']}")
    if budget.get("ram_gib"):
        if evidence.get("ram_total_gib") is None:
            short.append("RAM tidak terbaca (UNKNOWN)")
        elif evidence["ram_total_gib"] < budget["ram_gib"]:
            short.append(f"RAM {evidence['ram_total_gib']} GiB < budget {budget['ram_gib']} GiB")
    if short:
        return Outcome("FAILED", "Mesin di bawah budget resource pemilik: " + "; ".join(short) + ".",
                       evidence=evidence, error_code="RESOURCE_BELOW_BUDGET", severity="HIGH",
                       target_identity=f"{evidence['os']}/{evidence['arch']}", remedy=budget_remedy())
    return Outcome("VERIFIED", f"{evidence['os']} {evidence['arch']}, {evidence['cpu_count']} CPU, RAM "
                   f"{evidence.get('ram_total_gib')} GiB memenuhi budget pemilik.", evidence=evidence,
                   target_identity=f"{evidence['os']}/{evidence['arch']}")


def _existing_parent(path: Path) -> Path:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


@check("host.disk", "HOST", "Ruang disk data dir", ttl_seconds=3600, severity_fail="HIGH",
       invalidators=("config:resource_budget",))
def host_disk(ctx: CheckContext) -> Outcome:
    base = _existing_parent(ctx.data_dir)
    try:
        usage = shutil.disk_usage(base)
    except OSError as exc:
        return Outcome("UNKNOWN", f"Ruang disk tidak dapat dibaca ({type(exc).__name__}).", error_code="DISK_UNREADABLE")
    free_gib = round(usage.free / (1 << 30), 2)
    budget = resource_budget(ctx.session)
    floor_gib = float(budget["disk_gib"]) if budget and budget.get("disk_gib") else 1.0
    evidence = {"free_gib": free_gib, "total_gib": round(usage.total / (1 << 30), 1), "measured_at": logical(ctx, base),
                "floor_gib": floor_gib,
                "floor_note": "budget disk pemilik" if budget and budget.get("disk_gib")
                else "ambang sementara 1 GiB; budget resource belum ditetapkan pemilik (§51 P2)"}
    if usage.free < floor_gib * (1 << 30):
        return Outcome("FAILED", f"Ruang kosong {free_gib} GiB di bawah ambang {floor_gib} GiB.", evidence=evidence,
                       error_code="DISK_LOW", remedy=Remedy(
                           operation="Kosongkan ruang disk atau pindahkan data dir (AEOS_DATA_DIR)",
                           target=logical(ctx, base), privilege="pengguna lokal",
                           rollback="tidak berlaku", evidence_to_collect=f"ruang kosong >= {floor_gib} GiB"))
    return Outcome("VERIFIED", f"Ruang kosong {free_gib} GiB pada lokasi data.", evidence=evidence)


def os_time_sync(ctx: CheckContext) -> dict[str, Any]:
    """OS time-synchronisation status without AEOS contacting a time server itself (--functional only).

    Windows: ``w32tm /query /status`` (source / last successful sync); Linux: ``timedatectl show -p
    NTPSynchronized``; macOS has no unprivileged status command -> UNKNOWN (never guessed)."""
    if sys.platform == "win32":
        exe = ctx.runner.which("w32tm")
        if not exe:
            return {"status": "UNKNOWN", "reason": "w32tm tidak ditemukan", "operation_ref": "w32tm /query /status"}
        rr = ctx.runner.run([exe, "/query", "/status"], timeout=10.0, env=dict(ctx.env), cwd=ctx.probe_cwd)
        text = rr.stdout or ""
        if not rr.ok or not text.strip():
            return {"status": "UNKNOWN", "reason": f"w32tm gagal ({rr.error or rr.returncode})",
                    "operation_ref": "w32tm /query /status"}
        fields = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in text.splitlines()) if v}
        source = fields.get("source")
        last = fields.get("last successful sync time")
        if source is None:
            return {"status": "UNKNOWN", "reason": "output w32tm tidak dikenali (bahasa OS?)",
                    "operation_ref": "w32tm /query /status"}
        free = any(x in source.lower() for x in ("local cmos clock", "free-running"))
        return {"status": "NOT_SYNCED" if free or not last or "unspecified" in (last or "").lower() else "SYNCED",
                "source": source[:80], "last_sync": (last or "")[:60], "operation_ref": "w32tm /query /status"}
    if sys.platform.startswith("linux"):
        exe = ctx.runner.which("timedatectl")
        if not exe:
            return {"status": "UNKNOWN", "reason": "timedatectl tidak ditemukan",
                    "operation_ref": "timedatectl show -p NTPSynchronized"}
        rr = ctx.runner.run([exe, "show", "-p", "NTPSynchronized", "--value"], timeout=10.0, env=dict(ctx.env),
                            cwd=ctx.probe_cwd)
        value = (rr.stdout or "").strip().lower()
        if not rr.ok or value not in ("yes", "no"):
            return {"status": "UNKNOWN", "reason": "status timedatectl tidak terbaca",
                    "operation_ref": "timedatectl show -p NTPSynchronized"}
        return {"status": "SYNCED" if value == "yes" else "NOT_SYNCED",
                "operation_ref": "timedatectl show -p NTPSynchronized"}
    return {"status": "UNKNOWN", "reason": "macOS tidak menyediakan status sinkronisasi waktu tanpa hak admin",
            "operation_ref": None}


@check("host.clock", "HOST", "Kewajaran jam sistem", ttl_seconds=3600)
def host_clock(ctx: CheckContext) -> Outcome:
    evidence: dict[str, Any] = {"utc_now": ctx.now.isoformat(), "ntp_compared": False}
    if ctx.functional:
        sync = os_time_sync(ctx)
        evidence["os_time_sync"] = sync
        evidence["ntp_compared"] = sync["status"] in ("SYNCED", "NOT_SYNCED")
        if sync["status"] == "NOT_SYNCED":
            return Outcome("FAILED", "Jam OS tidak tersinkron dengan sumber waktu (TTL/expiry tidak dapat dipercaya).",
                           evidence=evidence, error_code="CLOCK_NOT_SYNCED", severity="HIGH", remedy=Remedy(
                               operation="Aktifkan/ulangi sinkronisasi waktu OS",
                               command="w32tm /resync" if sys.platform == "win32" else "timedatectl set-ntp true",
                               privilege="admin OS", rollback="tidak berlaku",
                               evidence_to_collect="host.clock --functional VERIFIED"))
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
    synced = (evidence.get("os_time_sync") or {}).get("status") == "SYNCED"
    return Outcome("VERIFIED", "Tidak ada tanda jam mundur terhadap record terakhir" + (
        "; jam OS tersinkron (status OS)." if synced else " (sinkronisasi OS tidak dibandingkan; gunakan --functional)."),
        evidence=evidence)


def _port_fp(ctx: CheckContext) -> dict[str, Any]:
    return {"host": ctx.settings.host, "port": ctx.settings.port}


def port_owner(port: int) -> dict[str, Any]:
    """Which local process listens on *port* (psutil; read-only). Access may be denied without admin rights."""
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                if not c.pid:
                    return {"status": "UNKNOWN", "reason": "pid tidak terlihat (butuh hak admin)"}
                try:
                    name = psutil.Process(c.pid).name()
                except psutil.Error:
                    name = None
                return {"status": "IDENTIFIED", "pid": c.pid, "name": name}
        return {"status": "UNKNOWN", "reason": "pendengar tidak ditemukan di tabel koneksi"}
    except Exception as exc:  # AccessDenied on macOS without root, psutil missing
        return {"status": "UNKNOWN", "reason": f"tabel koneksi tidak terbaca ({type(exc).__name__})"}


def _owner_text(owner: dict[str, Any]) -> str:
    if owner.get("status") == "IDENTIFIED":
        return f"pid {owner.get('pid')} {owner.get('name') or ''}".strip()
    return "pemilik UNKNOWN: " + str(owner.get("reason") or "")


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
        evidence["owner"] = port_owner(port)
        return Outcome("FAILED", f"Port {port} di {host} dipakai proses lain ({_owner_text(evidence['owner'])}); AEOS "
                       "tidak akan pindah ke antarmuka publik atau mematikan auth.", evidence=evidence,
                       error_code="PORT_IN_USE",
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


def _posix() -> bool:
    return os.name == "posix"


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
    if _posix():
        mode = d.stat().st_mode
        evidence["world_writable"] = bool(mode & 0o002)
        if mode & 0o002:
            return Outcome("FAILED", "Data directory dapat ditulis semua pengguna (world-writable).", evidence=evidence,
                           error_code="DATA_DIR_WORLD_WRITABLE", remedy=Remedy(
                               operation="Batasi izin data dir", command="chmod 700 <data dir>",
                               privilege="pengguna lokal", rollback="chmod ke mode sebelumnya",
                               evidence_to_collect="mode tanpa bit world-writable"))
    else:
        from aeos.doctor.checks.evidence import windows_acl

        broad, err = windows_acl(ctx, d)
        evidence["acl_checked"] = err is None
        if err:
            evidence["acl_error"] = err
        if broad:
            evidence["broad_write_aces"] = broad
            return Outcome("FAILED", "Data directory dapat ditulis grup luas (" + ", ".join(
                sorted({b["principal"] for b in broad})) + ").", evidence=evidence,
                error_code="DATA_DIR_ACL_TOO_OPEN", remedy=Remedy(
                    operation="Hapus hak tulis Everyone/Users/Authenticated Users dari data dir",
                    command="icacls <data dir> /remove:g *S-1-1-0 *S-1-5-32-545 *S-1-5-11 /T",
                    privilege="pengguna lokal/admin OS", rollback="icacls <data dir> /reset /T",
                    evidence_to_collect="host.data_dir VERIFIED"))
    return Outcome("VERIFIED", "Data directory ada dan tes tulis berhasil" + (
        "; ACL Windows tanpa hak tulis grup luas." if not _posix() and evidence.get("acl_checked")
        else ("; ACL Windows tidak terbaca (UNKNOWN)." if not _posix() else ".")),
        evidence=evidence, target_identity="<data>")


VSCODE_CHANNELS = (("code", "stable"), ("code-insiders", "insiders"))


@check("host.vscode", "HOST", "VS Code (permukaan kerja): identitas distribusi", component_id="vscode",
       ttl_seconds=86400, invalidators=("binary:code",), severity_fail="LOW", observation="installation",
       description="Advisory, tidak memblokir: versi, commit dan arsitektur dari `code --version`, lokasi logis dan "
                   "kanal (stable/insiders). Keaslian distribusi tidak diverifikasi.")
def host_vscode(ctx: CheckContext) -> Outcome:
    from aeos.doctor.checks._common import run_version

    for name, channel in VSCODE_CHANNELS:
        path = ctx.runner.which(name)
        if not path:
            continue
        version, rr = run_version(ctx, path, ["--version"], regex=r"(\d+\.\d+\.\d+)", timeout=20.0)
        lines = [ln.strip() for ln in (rr.stdout or "").splitlines() if ln.strip()]
        evidence = {"channel": channel, "location": logical(ctx, path), "operation_ref": f"{name} --version",
                    "commit": lines[1][:40] if len(lines) > 1 else None, "arch": lines[2][:16] if len(lines) > 2 else None,
                    "distribution_verified": False}
        if version is None:
            return Outcome("UNKNOWN", f"VS Code ({channel}) ditemukan tetapi versi tidak terbaca.", evidence=evidence,
                           error_code="VERSION_PROBE_FAILED", target_identity=evidence["location"])
        return Outcome("VERIFIED", f"VS Code {version} ({channel}) ditemukan; identitas distribusi tercatat (keaslian "
                       "tidak diverifikasi).", evidence=evidence, component_version=version,
                       target_identity=evidence["location"])
    return Outcome("MISSING", "VS Code tidak ditemukan di PATH (opsional bagi server; permukaan kerja U-01).",
                   severity="LOW", error_code="VSCODE_MISSING", remedy=Remedy(
                       operation="Pasang VS Code dari distribusi resmi dan aktifkan perintah 'code' di PATH",
                       source="code.visualstudio.com (distribusi resmi)", privilege="pengguna lokal",
                       rollback="uninstall VS Code", evidence_to_collect="code --version"))
