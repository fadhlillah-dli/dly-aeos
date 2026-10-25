"""§08 Local auth: login/session/RBAC/CSRF/bootstrap/interlock; no default credential in normal use.

``auth.http_guard`` and ``auth.interlock`` are controlled self-tests (E2): the real ``create_app``
middleware/dependencies and the real interlock run in-process against a throw-away in-memory database
with synthetic users — no network, nothing written to the operator's database. They are VERIFIED only
when every negative scenario is refused and a legitimate request still passes.
"""

from __future__ import annotations

import secrets as pysecrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import func, select

from aeos.core.enums import Role
from aeos.core.redaction import register_secret
from aeos.doctor.checks import check
from aeos.doctor.checks._common import module_present
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd

# Weak defaults an installer might have used; checked by hash verification only, never printed.
DEFAULT_PASSWORDS = ("admin", "password", "changeme", "changeme1234", "adminadmin12")
MAX_USERS_CHECKED = 10
PROBE_ADMIN = "doctor-probe-admin"
PROBE_VIEWER = "doctor-probe-viewer"
FOREIGN = "evil.example"
PROBE_ROUTERS = ["aeos.identity.router", "aeos.api.system", "aeos.inventory.router"]
WRITE_PATH = "/api/inventory/snapshots"


@check("auth.bootstrap", "LOCAL_AUTH", "Bootstrap SUPER_ADMIN selesai", component_id="identity-authorization",
       needs_db=True, ttl_seconds=3600, severity_fail="CRITICAL")
def auth_bootstrap(ctx: CheckContext) -> Outcome:
    from aeos.identity import facade as identity

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
    from aeos.identity.facade import verify_password
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
    evidence = {"super_admins": admins, "viewers": counts.get(Role.VIEWER.value, 0),
                "rbac_enforcement": "dibuktikan oleh auth.http_guard (VIEWER write ditolak)"}
    if admins == 0:
        return Outcome("MISSING", "Tidak ada SUPER_ADMIN aktif.", evidence=evidence, error_code="NO_ADMIN",
                       remedy=Remedy(operation="Buat SUPER_ADMIN", command=aeos_cmd("admin create"),
                                     privilege="operator lokal", evidence_to_collect="auth.admins VERIFIED"))
    return Outcome("VERIFIED", f"{admins} SUPER_ADMIN, {evidence['viewers']} VIEWER (penegakan read-only dibuktikan "
                   "auth.http_guard).", evidence=evidence)


@check("auth.sessions", "LOCAL_AUTH", "Sesi aktif", component_id="identity-authorization", needs_db=True,
       ttl_seconds=900, ok={"CONFIGURED", "VERIFIED"})
def auth_sessions(ctx: CheckContext) -> Outcome:
    from aeos.models.identity import AuthSession

    assert ctx.session is not None
    active = ctx.session.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.revoked_at.is_(None), AuthSession.expires_at > ctx.now)) or 0
    revoked = ctx.session.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.revoked_at.is_not(None))) or 0
    # Counting rows proves the session store, not CSRF/cookie enforcement: that is auth.http_guard's scenario.
    return Outcome("CONFIGURED", f"{active} sesi aktif (server-side, token hanya hash); {revoked} sesi dicabut. "
                   "Penegakan CSRF/cookie dibuktikan oleh auth.http_guard.",
                   evidence={"active_sessions": int(active), "revoked_sessions": int(revoked),
                             "enforcement_check": "auth.http_guard"})


# ---------------------------------------------------------------- in-process probes (no network)


@contextmanager
def probe_database() -> Iterator[Any]:
    """A throw-away in-memory database with the full schema (never the operator's database).

    Events the probe's synthetic users/logins emit stay in that database: they are dropped before commit, so
    the process-wide broadcaster never pushes probe events (with the probe DB's own seq numbers) to the live
    dashboard stream."""
    from sqlalchemy import create_engine
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from aeos.core.db import Base, import_all_models, install_append_only_triggers
    from aeos.events.service import _PENDING_KEY

    def _drop_probe_events(session: Any) -> None:
        session.info.pop(_PENDING_KEY, None)

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}, future=True)
    try:
        import_all_models()
        Base.metadata.create_all(engine)
        install_append_only_triggers(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        sa_event.listen(factory, "before_commit", _drop_probe_events)
        yield factory
    finally:
        engine.dispose()


def _probe_users(factory: Any) -> dict[str, str]:
    from aeos.identity import facade as identity

    creds = {"admin": pysecrets.token_urlsafe(24), "viewer": pysecrets.token_urlsafe(24)}
    for v in creds.values():
        register_secret(v)
    with factory() as s:
        identity.bootstrap_admin(s, username=PROBE_ADMIN, password=creds["admin"])
        identity.create_user(s, username=PROBE_VIEWER, password=creds["viewer"], role=Role.VIEWER)
        s.commit()
    return creds


def http_guard_probe(settings: Any) -> dict[str, Any]:
    """Run the real app's Host/Origin/CSRF/RBAC guards in-process. Returns scenario -> {ok, status, code}."""
    import warnings

    with warnings.catch_warnings():  # starlette's notice about its httpx-based client is not actionable here
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

    from aeos.api.app import create_app
    from aeos.api.deps import CSRF_HEADER, get_db

    scenarios: dict[str, dict[str, Any]] = {}
    with probe_database() as factory:
        creds = _probe_users(factory)
        app = create_app(settings, include=PROBE_ROUTERS, testing=False, init_db=False)

        def _db() -> Iterator[Any]:
            s = factory()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = _db
        # with secure_cookies the session cookie is only sent over https: probe with that scheme, otherwise the
        # client would drop the cookie and every scenario would wrongly look like "no session" (false FAILED)
        scheme = "https" if getattr(settings, "secure_cookies", False) else "http"
        base = f"{scheme}://{settings.host}:{settings.port}"
        origin = settings.allowed_origins[0]

        def record(name: str, resp: Any, *, status: int, code: str | None = None) -> None:
            try:
                got = ((resp.json() or {}).get("error") or {}).get("code")
            except ValueError:
                got = None
            scenarios[name] = {"ok": resp.status_code == status and (code is None or got == code),
                               "status": resp.status_code, "code": got, "expected": [status, code]}

        def login(client: Any, who: str) -> tuple[Any, str | None]:
            r = client.post("/api/auth/login", json={"username": PROBE_ADMIN if who == "admin" else PROBE_VIEWER,
                                                     "password": creds[who]}, headers={"origin": origin})
            csrf = (r.json() or {}).get("csrf_token") if r.status_code == 200 else None
            return r, csrf

        anon = TestClient(app, base_url=base)
        record("foreign_host", anon.get("/api/health", headers={"host": FOREIGN}), status=421, code="HOST_NOT_ALLOWED")
        record("foreign_origin", anon.post("/api/auth/login", json={"username": "x", "password": "y"},
                                           headers={"origin": f"https://{FOREIGN}"}), status=403,
               code="ORIGIN_NOT_ALLOWED")
        record("no_session_write", anon.post(WRITE_PATH, json={}, headers={"origin": origin}), status=401)
        admin = TestClient(app, base_url=base)
        r, admin_csrf = login(admin, "admin")
        cookie = (r.headers.get("set-cookie") or "").lower()
        scenarios["session_cookie_flags"] = {"ok": r.status_code == 200 and "httponly" in cookie
                                             and "samesite=strict" in cookie, "status": r.status_code,
                                             "code": None, "expected": ["HttpOnly", "SameSite=Strict"]}
        record("write_without_csrf", admin.post(WRITE_PATH, json={}, headers={"origin": origin}), status=403,
               code="CSRF_FAILED")
        record("write_with_wrong_csrf", admin.post(WRITE_PATH, json={}, headers={"origin": origin,
                                                                                   CSRF_HEADER: "x" * 32}),
               status=403, code="CSRF_FAILED")
        viewer = TestClient(app, base_url=base)
        _r, viewer_csrf = login(viewer, "viewer")
        record("viewer_write", viewer.post(WRITE_PATH, json={}, headers={"origin": origin,
                                                                          CSRF_HEADER: viewer_csrf or ""}),
               status=403, code="READ_ONLY_ROLE")
        # a legitimate request still passes (the guard is not just "deny everything")
        record("legitimate_logout", admin.post("/api/auth/logout", headers={"origin": origin,
                                                                            CSRF_HEADER: admin_csrf or ""}),
               status=200)
    return scenarios


def _guard_fp(ctx: CheckContext) -> dict[str, Any]:
    s = ctx.settings
    return {"host": s.host, "port": s.port, "origins": sorted(s.allowed_origins), "dev_mode": s.dev_mode,
            "secure_cookies": s.secure_cookies}


@check("auth.http_guard", "LOCAL_AUTH", "Penegakan Host/Origin/CSRF/RBAC (uji in-process)",
       component_id="identity-authorization", ttl_seconds=86400, invalidators=("config:port", "config:settings"),
       severity_fail="CRITICAL", fingerprint=_guard_fp,
       description="Middleware dan dependency create_app yang sebenarnya dijalankan in-process terhadap database "
                   "sementara di memori (tanpa jaringan): Host asing 421, Origin asing 403, tanpa sesi 401, tanpa/salah "
                   "CSRF 403, VIEWER write 403, cookie HttpOnly+SameSite=Strict, request sah tetap lolos.")
def auth_http_guard(ctx: CheckContext) -> Outcome:
    try:
        scenarios = http_guard_probe(ctx.settings)
    except ImportError as exc:
        return Outcome("UNKNOWN", f"Probe guard HTTP tidak dapat dijalankan ({type(exc).__name__}).",
                       error_code="HTTP_GUARD_UNAVAILABLE")
    failed = sorted(k for k, v in scenarios.items() if not v["ok"])
    evidence = {"scenarios": scenarios, "database": "in-memory (sementara)", "network": False}
    if failed:
        return Outcome("FAILED", f"Guard HTTP tidak menolak/menerima sesuai kontrak: {', '.join(failed)}.",
                       evidence=evidence, error_code="HTTP_GUARD_FAILED", severity="CRITICAL",
                       remedy=Remedy(operation="Jangan jalankan server sampai guard diperbaiki (bug keamanan)",
                                     target="aeos.api.security / aeos.api.deps", privilege="reviewer manusia",
                                     evidence_to_collect="auth.http_guard VERIFIED"))
    return Outcome("VERIFIED", f"{len(scenarios)} skenario guard HTTP lulus (skenario: in-process, database sementara; "
                   "bukan pentest jaringan).", evidence=evidence)


def interlock_self_test() -> dict[str, Any]:
    """An unbound external-write intent (no approved version/hash/decision/capability) must be refused."""
    from aeos.models.portfolio import Project
    from aeos.policy import facade as policy

    with probe_database() as factory:
        _probe_users(factory)
        with factory() as s:
            from aeos.models.identity import User

            admin = s.scalar(select(User).where(User.username == PROBE_ADMIN))
            project = Project(project_key="doctor-probe", name="doctor probe", domain="PERSONAL")
            s.add(project)
            s.flush()
            intent = policy.WriteIntent(
                actor_user_id=admin.id if admin else "", project_id=project.id, domain="PERSONAL",
                provider="github-issues", operation="create_issue",
                target={"provider": "github-issues", "root_id": "doctor/probe"}, approved_plan_version_id=None,
                approved_hash=None, decision_id=None)
            res = policy.check_external_write(s, intent)
            codes = sorted({r.split(":", 1)[0] for r in res.reasons})
            s.rollback()
    return {"allowed": res.allowed, "reason_codes": codes}


@check("auth.interlock", "LOCAL_AUTH", "External-write interlock menolak write tanpa ikatan",
       component_id="policy-decision-registry", ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH",
       level="E2")
def auth_interlock(ctx: CheckContext) -> Outcome:
    present, placeholder = module_present("aeos.policy.interlock")
    if not present or placeholder:
        return Outcome("MISSING", "Implementasi external-write interlock belum ada; semua write eksternal ditolak "
                       "(fail closed).", evidence={"module": "aeos.policy.interlock", "placeholder": placeholder},
                       error_code="INTERLOCK_NOT_IMPLEMENTED")
    try:
        result = interlock_self_test()
    except Exception as exc:  # the self-test could not run: never green
        return Outcome("UNKNOWN", f"Uji mandiri interlock gagal dijalankan ({type(exc).__name__}).",
                       evidence={"module": "aeos.policy.interlock"}, error_code="INTERLOCK_SELF_TEST_ERROR")
    evidence = {"module": "aeos.policy.interlock", "self_test": result, "database": "in-memory (sementara)"}
    if result["allowed"]:
        return Outcome("FAILED", "Interlock MENGIZINKAN write tanpa versi/hash/keputusan/capability yang disetujui.",
                       evidence=evidence, error_code="INTERLOCK_ALLOWED_UNBOUND_WRITE", severity="CRITICAL")
    return Outcome("VERIFIED", f"Interlock menolak write tanpa ikatan ({len(result['reason_codes'])} alasan: "
                   f"{', '.join(result['reason_codes'][:4])}...) (skenario: uji mandiri in-process).", evidence=evidence)
