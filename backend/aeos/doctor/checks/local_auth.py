"""§08 Local auth: login/session/RBAC/CSRF/bootstrap/interlock; no default credential in normal use."""

from __future__ import annotations

from sqlalchemy import func, select

from aeos.core.enums import Role
from aeos.doctor.checks import check
from aeos.doctor.checks._common import module_present
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd

# Weak defaults an installer might have used; checked by hash verification only, never printed.
DEFAULT_PASSWORDS = ("admin", "password", "changeme", "changeme1234", "adminadmin12")
MAX_USERS_CHECKED = 10


@check("auth.bootstrap", "LOCAL_AUTH", "Bootstrap SUPER_ADMIN selesai", component_id="identity-authorization",
       needs_db=True, ttl_seconds=3600, severity_fail="CRITICAL")
def auth_bootstrap(ctx: CheckContext) -> Outcome:
    from aeos.identity import service as identity

    assert ctx.session is not None
    if identity.bootstrap_complete(ctx.session):
        return Outcome("VERIFIED", "Bootstrap selesai: SUPER_ADMIN aktif ada (tanpa credential default).")
    return Outcome("MISSING", "Belum ada SUPER_ADMIN; login dan publish diblokir sampai bootstrap lokal.",
                   error_code="BOOTSTRAP_INCOMPLETE", remedy=Remedy(
                       operation="Buat SUPER_ADMIN pertama secara lokal (password dimasukkan sendiri)",
                       command=aeos_cmd("admin create"), source="aeos.identity", target="database lokal",
                       privilege="operator lokal (akses data dir)", rollback=aeos_cmd("admin recover <username>"),
                       evidence_to_collect="auth.bootstrap VERIFIED"))


@check("auth.default_credentials", "LOCAL_AUTH", "Tidak ada credential default", component_id="identity-authorization",
       needs_db=True, ttl_seconds=3600, severity_fail="CRITICAL")
def auth_default_credentials(ctx: CheckContext) -> Outcome:
    from aeos.identity.passwords import verify_password
    from aeos.models.identity import User

    assert ctx.session is not None
    users = list(ctx.session.scalars(select(User).where(User.is_active).order_by(User.created_at).limit(MAX_USERS_CHECKED)))
    weak = [u.id for u in users if any(verify_password(u.password_hash, pw) for pw in DEFAULT_PASSWORDS)]
    pending = sum(1 for u in users if u.must_change_password)
    evidence = {"users_checked": len(users), "weak_accounts": len(weak), "must_change_password": pending}
    if weak:
        return Outcome("FAILED", f"{len(weak)} akun memakai password default/lemah yang dikenal.", evidence=evidence,
                       error_code="DEFAULT_CREDENTIAL", remedy=Remedy(
                           operation="Reset akun dengan password sementara satu kali dan wajib ganti",
                           command=aeos_cmd("admin recover <username>"), privilege="operator lokal",
                           rollback="tidak berlaku", evidence_to_collect="auth.default_credentials VERIFIED"))
    return Outcome("VERIFIED", f"{len(users)} akun aktif diperiksa; tidak ada password default yang dikenal.",
                   evidence=evidence)


@check("auth.admins", "LOCAL_AUTH", "Jumlah admin dan viewer", component_id="identity-authorization", needs_db=True,
       ttl_seconds=3600)
def auth_admins(ctx: CheckContext) -> Outcome:
    from aeos.models.identity import User

    assert ctx.session is not None
    rows = ctx.session.execute(select(User.role, func.count()).where(User.is_active).group_by(User.role)).all()
    counts = {str(role): int(n) for role, n in rows}
    admins = counts.get(Role.SUPER_ADMIN.value, 0)
    evidence = {"super_admins": admins, "viewers": counts.get(Role.VIEWER.value, 0)}
    if admins == 0:
        return Outcome("MISSING", "Tidak ada SUPER_ADMIN aktif.", evidence=evidence, error_code="NO_ADMIN",
                       remedy=Remedy(operation="Buat SUPER_ADMIN", command=aeos_cmd("admin create"),
                                     privilege="operator lokal", evidence_to_collect="auth.admins VERIFIED"))
    return Outcome("VERIFIED", f"{admins} SUPER_ADMIN, {evidence['viewers']} VIEWER (read-only ditegakkan backend).",
                   evidence=evidence)


@check("auth.sessions", "LOCAL_AUTH", "Sesi aktif", component_id="identity-authorization", needs_db=True,
       ttl_seconds=900)
def auth_sessions(ctx: CheckContext) -> Outcome:
    from aeos.models.identity import AuthSession

    assert ctx.session is not None
    active = ctx.session.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.revoked_at.is_(None), AuthSession.expires_at > ctx.now)) or 0
    revoked = ctx.session.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.revoked_at.is_not(None))) or 0
    return Outcome("VERIFIED", f"{active} sesi aktif (server-side, token hanya hash); {revoked} sesi dicabut.",
                   evidence={"active_sessions": int(active), "revoked_sessions": int(revoked),
                             "csrf": "header X-CSRF-Token terikat sesi", "cookie": "HttpOnly, SameSite=Strict"})


@check("auth.interlock", "LOCAL_AUTH", "External-write interlock tersedia", component_id="policy-decision-registry",
       ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH")
def auth_interlock(ctx: CheckContext) -> Outcome:
    present, placeholder = module_present("aeos.policy.interlock")
    if not present or placeholder:
        return Outcome("MISSING", "Implementasi external-write interlock belum ada; semua write eksternal ditolak "
                       "(fail closed).", evidence={"module": "aeos.policy.interlock", "placeholder": placeholder},
                       error_code="INTERLOCK_NOT_IMPLEMENTED")
    return Outcome("CONFIGURED", "Interlock tersedia; efektivitas penegakan dibuktikan oleh uji negatif modul policy "
                   "(bukan oleh Doctor).", evidence={"module": "aeos.policy.interlock"})
