"""SecretProvider implementations (§26, U-13).

Selection (``get_provider``), fail closed:
1. test override (``set_provider_override``);
2. OpenBao (``settings.openbao_addr``, loopback) when reachable, initialised, unsealed and the
   AppRole bootstrap credentials in the OS keyring log in successfully;
3. ONLY when an ACTIVE human Decision kind EXCEPTION with
   ``details.secret_store_interim_keyring == true`` exists for domain PERSONAL: an OS-keyring
   runtime provider labelled ``os-keyring-interim`` whose residual risk is surfaced in status;
4. otherwise ``Blocked(SECRET_STORE_UNAVAILABLE)``.

Values are returned only to trusted in-process callers whose *purpose* prefix matches the
reference namespace (``personal:...`` may read ``personal/*`` only; OFFICE secrets are never
returned to a personal-scoped caller). ``projects/<id>/...`` is served only for a registered
PERSONAL project (the controller is not bound to the office policy). Every fetched value is
registered for redaction. Status calls never include values. Access is logged
(ref/purpose/actor/result) — never the value.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from aeos.core.config import Settings, get_settings
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.core.redaction import register_secret
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.secrets.facade import NAMESPACES, SecretStatus, validate_ref
from aeos.secrets.openbao import TIMEOUT as OPENBAO_TIMEOUT
from aeos.secrets.openbao import OpenBaoClient, OpenBaoError, OpenBaoUnavailable, logical_addr

log = logging.getLogger("aeos.secrets")

KV_MOUNT = "aeos"
APPROLE_NAME = "aeos-controller"
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INTERIM_NAMESPACES = ("personal", "workers", "platform", "projects")
INTERIM_RESIDUAL_RISK = (
    "Mode sementara OS keyring: tidak ada TTL/lease token, tidak ada audit device vault, tidak ada policy "
    "per namespace; nilai dapat dibaca proses lain yang berjalan sebagai pengguna OS yang sama. OFFICE tidak "
    "didukung. Pasang OpenBao (aeos vault install/init) lalu cabut pengecualian ini."
)


class SecretProvider(Protocol):
    name: str

    def get(self, ref: str, *, purpose: str, actor: str) -> str: ...
    def put(self, ref: str, value: str, *, actor: str) -> SecretStatus: ...
    def status(self, ref: str) -> SecretStatus: ...
    def list_status(self) -> list[SecretStatus]: ...


def strict_ref(ref: str) -> tuple[str, str]:
    """validate_ref + safe path segments (refs become vault/keyring paths)."""
    try:
        ns, name = validate_ref(ref)
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="SECRET_REF_INVALID") from exc
    if not all(_SEGMENT.match(seg) for seg in name.split("/")):
        raise ValidationFailed(f"invalid secret reference: {ref!r}", code="SECRET_REF_INVALID")
    if ns == "projects" and len(name.split("/")) < 2:
        raise ValidationFailed("project secrets are projects/<project-id>/<name>", code="SECRET_REF_INVALID")
    return ns, name


def check_purpose(ref: str, purpose: str) -> None:
    """The caller's purpose scope must equal the reference namespace (office never to personal)."""
    ns, name = strict_ref(ref)
    parts = re.split(r"[:/.]", (purpose or "").strip(), maxsplit=2)
    scope = parts[0].lower() if parts else ""
    if scope not in NAMESPACES:
        raise PolicyDenied(f"secret purpose must start with a scope {NAMESPACES}: got {purpose!r}",
                           code="SECRET_SCOPE_DENIED")
    if scope != ns:
        raise PolicyDenied(f"a {scope}-scoped caller may not read {ns}/ secrets", code="SECRET_SCOPE_DENIED")
    if ns == "projects":
        project = name.split("/", 1)[0]
        if len(parts) < 2 or parts[1] != project:
            raise PolicyDenied("project secrets need purpose 'projects:<project-id>:<use>' for the same project",
                               code="SECRET_SCOPE_DENIED")


def project_domain(project_id: str) -> str | None:
    """Immutable domain of a registered project (None when unknown or the DB is unavailable)."""
    try:
        from aeos.core.db import session_factory
        from aeos.models.portfolio import Project

        s = session_factory()()
        try:
            p = s.get(Project, project_id)
            return p.domain if p is not None else None
        finally:
            s.close()
    except Exception:  # DB not initialised -> unknown -> callers deny
        return None


def check_project_scope(ref: str) -> None:
    """``projects/<id>/...`` is served only for a registered PERSONAL project.

    The controller AppRole is bound to the ``aeos-projects`` policy but NOT to ``aeos-office``
    (OFFICE default deny, §23/§26). Without this check an OFFICE project's credential stored under
    ``projects/`` would bypass that boundary, so OFFICE (and unknown) projects fail closed."""
    ns, name = strict_ref(ref)
    if ns != "projects":
        return
    project_id = name.split("/", 1)[0]
    domain = project_domain(project_id)
    if domain is None:
        raise PolicyDenied("project secret for an unknown project (domain cannot be verified)",
                           code="SECRET_SCOPE_DENIED")
    if domain != "PERSONAL":
        raise PolicyDenied("OFFICE project credentials are not served: the office policy is not bound to the "
                           "AEOS controller until a company authorisation is recorded", code="SECRET_SCOPE_DENIED")


def _log_access(provider: str, ref: str, purpose: str, actor: str, result: str) -> None:
    log.info("secret access provider=%s ref=%s purpose=%s actor=%s result=%s", provider, ref, purpose, actor, result)


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


# ------------------------------------------------------------------------------ test provider


class MemorySecretProvider:
    """Test-only provider. Never selected outside tests."""

    name = "memory-test"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[str, object]] = {}

    def get(self, ref: str, *, purpose: str, actor: str) -> str:
        validate_ref(ref)
        with self._lock:
            item = self._data.get(ref)
        if item is None:
            raise Blocked(f"secret {ref} is not configured", code="SECRET_MISSING")
        register_secret(item[0])
        return item[0]

    def put(self, ref: str, value: str, *, actor: str) -> SecretStatus:
        validate_ref(ref)
        register_secret(value)
        with self._lock:
            self._data[ref] = (value, utcnow())
        return self.status(ref)

    def status(self, ref: str) -> SecretStatus:
        with self._lock:
            item = self._data.get(ref)
        return SecretStatus(ref=ref, exists=item is not None, backend=self.name,
                            updated_at=item[1] if item else None)  # type: ignore[arg-type]

    def list_status(self) -> list[SecretStatus]:
        with self._lock:
            refs = sorted(self._data)
        return [self.status(r) for r in refs]

    def delete(self, ref: str, *, actor: str) -> None:
        with self._lock:
            self._data.pop(ref, None)


# ------------------------------------------------------------------------------ OS keyring


def keyring_backend() -> tuple[bool, str]:
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:  # pragma: no cover - keyring is a dependency
        return False, "keyring not installed"
    try:
        kr = keyring.get_keyring()
    except Exception as exc:  # pragma: no cover
        return False, f"keyring error: {type(exc).__name__}"
    name = f"{type(kr).__module__}.{type(kr).__name__}"
    if isinstance(kr, fail.Keyring) or "fail" in name.lower() or "null" in name.lower():
        return False, name
    return True, name


class KeyringBootstrapStore:
    """OS credential store for bootstrap material only (AppRole role_id/secret_id, optional unseal).

    Windows Credential Manager / macOS Keychain / Secret Service via ``keyring``. The root token
    and unseal material are never written to config files or the repository (§26).
    """

    ROLE_ID = "openbao-approle-role-id"
    SECRET_ID = "openbao-approle-secret-id"
    UNSEAL_PREFIX = "openbao-unseal-key-"
    UNSEAL_COUNT = "openbao-unseal-key-count"

    def __init__(self, service: str):
        self.service = service

    def available(self) -> tuple[bool, str]:
        return keyring_backend()

    def _get(self, name: str) -> str | None:
        import keyring

        try:
            value = keyring.get_password(self.service, name)
        except Exception as exc:  # keyring.errors.KeyringError and backend errors
            raise Blocked(f"OS keyring unavailable: {type(exc).__name__}", code="SECRET_STORE_UNAVAILABLE") from exc
        register_secret(value)
        return value

    def _set(self, name: str, value: str) -> None:
        import keyring

        register_secret(value)
        try:
            keyring.set_password(self.service, name, value)
        except Exception as exc:  # keyring backend errors: fail closed, never echo the value
            raise Blocked(f"OS keyring write failed: {type(exc).__name__}", code="SECRET_STORE_UNAVAILABLE") from exc

    def _delete(self, name: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(self.service, name)
        except PasswordDeleteError:
            pass

    def approle(self) -> tuple[str, str] | None:
        rid, sid = self._get(self.ROLE_ID), self._get(self.SECRET_ID)
        return (rid, sid) if rid and sid else None

    def set_approle(self, role_id: str, secret_id: str) -> None:
        self._set(self.ROLE_ID, role_id)
        self._set(self.SECRET_ID, secret_id)

    def unseal_keys(self) -> list[str]:
        count = self._get(self.UNSEAL_COUNT)
        if not count or not count.isdigit():
            return []
        keys = [self._get(f"{self.UNSEAL_PREFIX}{i}") for i in range(int(count))]
        return [k for k in keys if k]

    def set_unseal_keys(self, keys: list[str]) -> None:
        for i, k in enumerate(keys):
            self._set(f"{self.UNSEAL_PREFIX}{i}", k)
        self._set(self.UNSEAL_COUNT, str(len(keys)))

    def clear_unseal_keys(self) -> None:
        count = self._get(self.UNSEAL_COUNT)
        for i in range(int(count) if count and count.isdigit() else 0):
            self._delete(f"{self.UNSEAL_PREFIX}{i}")
        self._delete(self.UNSEAL_COUNT)


def bootstrap_store(settings: Settings) -> KeyringBootstrapStore:
    return KeyringBootstrapStore(f"{settings.keyring_service}-bootstrap")


# ------------------------------------------------------------------------------ OpenBao


class OpenBaoProvider:
    name = "openbao"
    HEALTH_TTL = 5.0

    def __init__(self, settings: Settings, bootstrap: KeyringBootstrapStore, *, http: httpx.Client | None = None):
        self.addr = settings.openbao_addr
        self.bootstrap = bootstrap
        # one pooled client for the provider's lifetime (loopback only, proxies ignored)
        self._http = http or httpx.Client(trust_env=False, timeout=OPENBAO_TIMEOUT, follow_redirects=False)
        self._lock = threading.RLock()
        self._token: str | None = None
        self._token_deadline = 0.0
        self._token_ttl = 0
        self._renewable = False
        self._token_policies: list[str] = []
        self._health: tuple[float, dict[str, Any]] | None = None
        self.denied_namespaces: list[str] = []  # namespaces the controller token may not list (office)

    def _client(self, token: str | None = None) -> OpenBaoClient:
        return OpenBaoClient(self.addr, token=token, http=self._http)

    def health(self, *, fresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not fresh and self._health and time.monotonic() - self._health[0] < self.HEALTH_TTL:
                return self._health[1]
        try:
            c = self._client()
            try:
                h = c.health()
            finally:
                c.close()
        except OpenBaoUnavailable as exc:
            h = {"reachable": False, "initialized": None, "sealed": None, "version": None, "error": str(exc)}
        h["addr"] = logical_addr(self.addr) if h.get("reachable") is not None else self.addr
        with self._lock:
            self._health = (time.monotonic(), h)
        return h

    def _login(self) -> str:
        creds = self.bootstrap.approle()
        if not creds:
            raise Blocked("OpenBao AppRole credentials are missing from the OS keyring; run `aeos vault init`",
                          code="SECRET_STORE_UNAVAILABLE")
        c = self._client()
        try:
            auth = c.approle_login(*creds)
        except OpenBaoUnavailable as exc:
            raise Blocked(str(exc), code="SECRET_STORE_UNAVAILABLE") from exc
        except OpenBaoError as exc:
            raise Blocked(f"OpenBao AppRole login failed ({exc.status})", code="SECRET_STORE_UNAVAILABLE") from exc
        finally:
            c.close()
        token = auth.get("client_token")
        if not token:
            raise Blocked("OpenBao AppRole login returned no token", code="SECRET_STORE_UNAVAILABLE")
        self._token = str(token)
        self._token_ttl = int(auth.get("lease_duration") or 0)
        self._renewable = bool(auth.get("renewable"))
        policies = auth.get("token_policies") or auth.get("policies") or []
        self._token_policies = sorted(str(p) for p in policies) if isinstance(policies, list) else []
        self._token_deadline = time.monotonic() + max(self._token_ttl, 1)
        return self._token

    def token_view(self) -> dict[str, Any]:
        """Scope and TTL of the controller token (§26 'scope dan TTL'); never the token itself."""
        with self._lock:
            if not self._token:
                return {"present": False}
            return {"present": True, "ttl_seconds": self._token_ttl, "renewable": self._renewable,
                    "expires_in_seconds": max(0, int(self._token_deadline - time.monotonic())),
                    "policies": list(self._token_policies)}

    def _get_token(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._token_deadline - max(30.0, self._token_ttl * 0.2):
                return self._token
            if self._token and self._renewable and now < self._token_deadline - 5:
                c = self._client(self._token)
                try:
                    auth = c.renew_self("1h")
                    self._token_ttl = int(auth.get("lease_duration") or self._token_ttl)
                    self._token_deadline = time.monotonic() + max(self._token_ttl, 1)
                    return self._token
                except OpenBaoError:
                    pass  # fall through to a fresh login
                finally:
                    c.close()
            return self._login()

    def ensure_usable(self) -> None:
        h = self.health()
        if not h.get("reachable"):
            raise Blocked(f"OpenBao unreachable at {h.get('addr')}; run `aeos vault start`",
                          code="SECRET_STORE_UNAVAILABLE", details={"health": h})
        if not h.get("initialized"):
            raise Blocked("OpenBao is not initialised; run `aeos vault init`", code="SECRET_STORE_UNAVAILABLE")
        if h.get("sealed"):
            raise Blocked("OpenBao is sealed; run `aeos vault unseal`", code="SECRET_STORE_UNAVAILABLE")

    def _call(self, fn: Any) -> Any:
        self.ensure_usable()
        for attempt in range(2):
            c = self._client(self._get_token())
            try:
                return fn(c)
            except OpenBaoUnavailable as exc:
                raise Blocked(str(exc), code="SECRET_STORE_UNAVAILABLE") from exc
            except OpenBaoError as exc:
                if exc.status == 403 and attempt == 0:
                    with self._lock:
                        self._token = None  # revoked/expired token: log in once more
                    continue
                if exc.status == 403:
                    raise PolicyDenied("OpenBao policy denies this path for the AEOS controller",
                                       code="SECRET_SCOPE_DENIED") from exc
                raise Blocked(f"OpenBao error ({exc.status}): {exc}", code="SECRET_STORE_UNAVAILABLE") from exc
            finally:
                c.close()
        raise Blocked("OpenBao call failed", code="SECRET_STORE_UNAVAILABLE")  # pragma: no cover

    def get(self, ref: str, *, purpose: str, actor: str) -> str:
        check_purpose(ref, purpose)
        check_project_scope(ref)
        data = self._call(lambda c: c.kv_read(KV_MOUNT, ref))
        if data is None or not isinstance((data.get("data") or {}).get("value"), str):
            _log_access(self.name, ref, purpose, actor, "MISSING")
            raise Blocked(f"secret {ref} is not configured; run `aeos secrets set {ref}`", code="SECRET_MISSING")
        meta = data.get("metadata") or {}
        custom = meta.get("custom_metadata") or {}
        expires = _parse_time(custom.get("expires_at"))
        if expires is not None and expires <= utcnow():
            _log_access(self.name, ref, purpose, actor, "EXPIRED")
            raise Blocked(f"secret {ref} expired at {expires.isoformat()}", code="SECRET_EXPIRED")
        value = data["data"]["value"]
        register_secret(value)
        _log_access(self.name, ref, purpose, actor, "OK")
        return value

    def put(self, ref: str, value: str, *, actor: str, expires_at: datetime | None = None,
            scope: dict[str, Any] | None = None) -> SecretStatus:
        strict_ref(ref)
        check_project_scope(ref)
        if not value:
            raise ValidationFailed("secret value must not be empty")
        register_secret(value)
        self._call(lambda c: c.kv_write(KV_MOUNT, ref, {"value": value}))
        # custom_metadata values must be non-empty strings (OpenBao rejects empty values)
        custom = {"updated_by": actor[:64] or "unknown"}
        if expires_at:
            custom["expires_at"] = expires_at.isoformat()
        if scope:
            custom["scope"] = json.dumps(scope, sort_keys=True)
        self._call(lambda c: c.kv_write_metadata(KV_MOUNT, ref, custom))
        return self.status(ref)

    def status(self, ref: str) -> SecretStatus:
        strict_ref(ref)
        meta = self._call(lambda c: c.kv_metadata(KV_MOUNT, ref))
        if meta is None:
            return SecretStatus(ref=ref, exists=False, backend=self.name)
        custom = meta.get("custom_metadata") or {}
        try:
            scope = json.loads(custom.get("scope") or "{}")
        except json.JSONDecodeError:
            scope = {}
        return SecretStatus(
            ref=ref, exists=True, backend=self.name, updated_at=_parse_time(meta.get("updated_time")),
            expires_at=_parse_time(custom.get("expires_at")), scope=scope if isinstance(scope, dict) else {},
            note=f"version {meta.get('current_version')}",
        )

    def list_status(self) -> list[SecretStatus]:
        """Metadata of every listable ref. Namespaces the controller policy does not cover (office,
        by design) are skipped and recorded in ``denied_namespaces`` instead of failing the whole
        listing (observed against OpenBao 2.7.1: LIST on office/ -> 403)."""
        refs: list[str] = []
        denied: list[str] = []

        def walk(prefix: str, depth: int) -> None:
            try:
                keys = self._call(lambda c: c.kv_list(KV_MOUNT, prefix))
            except PolicyDenied:
                denied.append(prefix)
                return
            for key in keys:
                if key.endswith("/"):
                    if depth < 2:
                        walk(f"{prefix}/{key.rstrip('/')}", depth + 1)
                else:
                    refs.append(f"{prefix}/{key}")

        self.ensure_usable()
        self._get_token()
        bound = set(self._token_policies)
        for ns in NAMESPACES:
            if bound and f"aeos-{ns}" not in bound and "root" not in bound:
                denied.append(ns)  # not bound to the controller: do not even ask (no re-login churn)
                continue
            walk(ns, 0)
        self.denied_namespaces = denied
        out: list[SecretStatus] = []
        for r in sorted(refs):
            try:
                out.append(self.status(r))
            except PolicyDenied:
                continue
        return out

    def delete(self, ref: str, *, actor: str) -> None:
        strict_ref(ref)
        check_project_scope(ref)
        self._call(lambda c: c.kv_delete_all(KV_MOUNT, ref))
        _log_access(self.name, ref, "delete", actor, "DELETED")

    HEALTHCHECK_REF = "platform/aeos-healthcheck"

    def verify_roundtrip(self) -> dict[str, Any]:
        """Functional proof (beyond CONNECTED): write, read back and delete a random value in the
        platform namespace with the controller's AppRole token. Returns a summary without values."""
        import secrets as _secrets

        probe = "aeos-verify-" + _secrets.token_hex(16)
        register_secret(probe)
        steps: list[dict[str, str]] = []
        status = "VERIFIED"
        try:
            self.put(self.HEALTHCHECK_REF, probe, actor="aeos.vault.verify")
            steps.append({"id": "write", "status": "VERIFIED"})
            back = self.get(self.HEALTHCHECK_REF, purpose="platform:vault.verify", actor="aeos.vault.verify")
            same = back == probe
            steps.append({"id": "read", "status": "VERIFIED" if same else "FAILED"})
            if not same:
                status = "FAILED"
            self.delete(self.HEALTHCHECK_REF, actor="aeos.vault.verify")
            steps.append({"id": "delete", "status": "VERIFIED"})
        except (Blocked, PolicyDenied, ValidationFailed) as exc:
            status = "FAILED"
            steps.append({"id": "error", "status": "FAILED", "detail": f"{exc.code}: {exc.message}"})
        return {"status": status, "steps": steps, "checked_at": utcnow().isoformat(),
                "token": self.token_view(), "ref": self.HEALTHCHECK_REF}


# ------------------------------------------------------------------------------ interim keyring


class InterimKeyringProvider:
    """Owner-approved interim runtime store in the OS keyring (residual risk surfaced in status)."""

    name = "os-keyring-interim"
    # office/* is never served in interim mode: status shows NO_ACCESS, not "missing" (no remedy to run)
    denied_namespaces = ["office"]

    def __init__(self, settings: Settings, *, decision_id: str | None = None):
        self.service = f"{settings.keyring_service}-interim"
        self.index_path = settings.data_dir / "secrets" / "interim-index.json"
        self.decision_id = decision_id
        self._lock = threading.Lock()

    def _ns_allowed(self, ref: str) -> str:
        ns, _ = strict_ref(ref)
        if ns not in INTERIM_NAMESPACES:
            raise PolicyDenied("OFFICE secrets are never stored in the interim OS keyring", code="SECRET_SCOPE_DENIED")
        check_project_scope(ref)  # projects/<id>/... only for PERSONAL projects
        return ns

    def _index(self) -> dict[str, Any]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_index(self, data: dict[str, Any]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.index_path)

    def get(self, ref: str, *, purpose: str, actor: str) -> str:
        check_purpose(ref, purpose)
        self._ns_allowed(ref)
        import keyring

        try:
            value = keyring.get_password(self.service, ref)
        except Exception as exc:
            raise Blocked(f"OS keyring unavailable: {type(exc).__name__}", code="SECRET_STORE_UNAVAILABLE") from exc
        if not value:
            _log_access(self.name, ref, purpose, actor, "MISSING")
            raise Blocked(f"secret {ref} is not configured; run `aeos secrets set {ref}`", code="SECRET_MISSING")
        entry = self._index().get(ref) or {}
        expires = _parse_time(entry.get("expires_at"))
        if expires is not None and expires <= utcnow():
            raise Blocked(f"secret {ref} expired", code="SECRET_EXPIRED")
        register_secret(value)
        _log_access(self.name, ref, purpose, actor, "OK")
        return value

    def put(self, ref: str, value: str, *, actor: str, expires_at: datetime | None = None,
            scope: dict[str, Any] | None = None) -> SecretStatus:
        self._ns_allowed(ref)
        if not value:
            raise ValidationFailed("secret value must not be empty")
        import keyring

        register_secret(value)
        try:
            keyring.set_password(self.service, ref, value)
        except Exception as exc:  # keyring backend errors: fail closed, never echo the value
            raise Blocked(f"OS keyring write failed: {type(exc).__name__}", code="SECRET_STORE_UNAVAILABLE") from exc
        with self._lock:
            idx = self._index()
            idx[ref] = {"updated_at": utcnow().isoformat(), "updated_by": actor[:64],
                        "expires_at": expires_at.isoformat() if expires_at else None, "scope": scope or {}}
            self._write_index(idx)
        return self.status(ref)

    def status(self, ref: str) -> SecretStatus:
        entry = self._index().get(ref)
        if not entry:
            return SecretStatus(ref=ref, exists=False, backend=self.name, note="interim OS keyring")
        return SecretStatus(ref=ref, exists=True, backend=self.name, updated_at=_parse_time(entry.get("updated_at")),
                            expires_at=_parse_time(entry.get("expires_at")), scope=entry.get("scope") or {},
                            note="interim OS keyring (residual risk)")

    def list_status(self) -> list[SecretStatus]:
        return [self.status(r) for r in sorted(self._index())]

    def delete(self, ref: str, *, actor: str) -> None:
        self._ns_allowed(ref)
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(self.service, ref)
        except PasswordDeleteError:
            pass
        with self._lock:
            idx = self._index()
            idx.pop(ref, None)
            self._write_index(idx)


INTERIM_TARGET = ("secret_store", "interim-keyring")


def interim_exception(settings: Settings) -> dict[str, Any] | None:
    """ACTIVE human EXCEPTION decision (domain PERSONAL) allowing the interim keyring store.

    Bound to the exact target ``secret_store/interim-keyring`` (the one ``aeos secrets
    interim-keyring`` tells the owner to record) and made by an active SUPER_ADMIN account —
    an EXCEPTION recorded for anything else never switches the secret store."""
    if not settings.layout.db_path.is_file():
        # DB not initialised yet -> no exception can exist. Opening a session here would create the
        # data dir and database as a side effect of a status read (bootstrap doctor, §08/§50).
        return None
    try:
        from sqlalchemy import select

        from aeos.core.db import session_factory
        from aeos.core.enums import Role
        from aeos.models.decisions import Decision
        from aeos.models.identity import User

        s = session_factory()()
        try:
            rows = list(s.scalars(select(Decision).where(Decision.kind == "EXCEPTION", Decision.status == "ACTIVE",
                                                         Decision.domain == "PERSONAL",
                                                         Decision.target_type == INTERIM_TARGET[0],
                                                         Decision.target_id == INTERIM_TARGET[1])
                                  .order_by(Decision.created_at.desc())))
            admins = {u.id for u in s.scalars(select(User).where(User.role == Role.SUPER_ADMIN.value,
                                                                 User.is_active.is_(True)))}
        finally:
            s.close()
    except Exception:  # DB not initialised yet -> no exception can exist
        return None
    now = utcnow()
    for d in rows:
        if (d.actor_kind or "HUMAN") != "HUMAN" or d.actor_user_id not in admins:
            continue
        if not isinstance(d.details, dict) or d.details.get("secret_store_interim_keyring") is not True:
            continue
        if d.expires_at is not None and ensure_aware(d.expires_at) <= now:  # type: ignore[operator]
            continue
        return {"decision_id": d.id, "expires_at": d.expires_at.isoformat() if d.expires_at else None,
                "actor_user_id": d.actor_user_id, "created_at": d.created_at.isoformat() if d.created_at else None}
    return None


# ------------------------------------------------------------------------------ selection


@dataclass
class Selection:
    provider: Any | None
    backend: str
    reasons: list[str] = field(default_factory=list)
    openbao: dict[str, Any] = field(default_factory=dict)
    interim: dict[str, Any] | None = None


_override: SecretProvider | None = None
_bao_cache: dict[tuple[str, str], OpenBaoProvider] = {}
_cache_lock = threading.Lock()


def set_provider_override(provider: SecretProvider | None) -> None:
    """Tests inject a provider here."""
    global _override
    _override = provider


def reset_provider_cache() -> None:
    with _cache_lock:
        _bao_cache.clear()


def openbao_provider(settings: Settings) -> OpenBaoProvider:
    key = (settings.openbao_addr, settings.keyring_service)
    with _cache_lock:
        p = _bao_cache.get(key)
        if p is None:
            p = OpenBaoProvider(settings, bootstrap_store(settings))
            _bao_cache[key] = p
        return p


def select_backend(settings: Settings | None = None) -> Selection:
    settings = settings or get_settings()
    if _override is not None:
        return Selection(_override, getattr(_override, "name", "override"))
    reasons: list[str] = []
    bao_view: dict[str, Any] = {}
    try:
        bao = openbao_provider(settings)
        bao_view = dict(bao.health())
        bao.ensure_usable()
        bao._get_token()
        bao_view["login"] = "OK"
        bao_view["token"] = bao.token_view()
        return Selection(bao, "openbao", [], bao_view)
    except Blocked as exc:
        reasons.append(exc.message)
        bao_view.setdefault("login", "NOT_OK")
    except OpenBaoUnavailable as exc:
        reasons.append(str(exc))
    exc_info = interim_exception(settings)
    if exc_info is None:
        reasons.append("no ACTIVE owner EXCEPTION decision for the interim OS-keyring store")
        return Selection(None, "none", reasons, bao_view)
    ok, name = keyring_backend()
    if not ok:
        reasons.append(f"OS keyring unavailable for interim mode ({name})")
        return Selection(None, "none", reasons, bao_view, exc_info)
    return Selection(InterimKeyringProvider(settings, decision_id=exc_info["decision_id"]), "os-keyring-interim",
                     reasons, bao_view, exc_info)


def get_provider() -> SecretProvider:
    sel = select_backend()
    if sel.provider is not None:
        return sel.provider
    raise Blocked("no usable secret store (OpenBao / approved interim OS keyring): " + "; ".join(sel.reasons),
                  code="SECRET_STORE_UNAVAILABLE", details={"reasons": sel.reasons})


def provider_delete(ref: str, *, actor: str, session: Any | None = None) -> None:
    from aeos.secrets.readiness import invalidate_secret_checks

    p = get_provider()
    deleter = getattr(p, "delete", None)
    if deleter is None:  # pragma: no cover
        raise Blocked(f"provider {p.name} cannot delete")
    deleter(ref, actor=actor)
    invalidate_secret_checks(ref, reason="credential deleted", session=session)


def provider_put(ref: str, value: str, *, actor: str, expires_at: datetime | None = None,
                 scope: dict[str, Any] | None = None, session: Any | None = None) -> SecretStatus:
    from aeos.secrets.readiness import invalidate_secret_checks

    p = get_provider()
    if isinstance(p, (OpenBaoProvider, InterimKeyringProvider)):
        status = p.put(ref, value, actor=actor, expires_at=expires_at, scope=scope)
    else:
        status = p.put(ref, value, actor=actor)
    invalidate_secret_checks(ref, reason="credential stored/rotated", session=session)
    return status


def verification_file(settings: Settings) -> Any:
    return settings.data_dir / "secrets" / "last-verify.json"


def record_verification(settings: Settings, result: dict[str, Any]) -> None:
    """Keep the last functional roundtrip (status/steps/time, never values) for the health view."""
    path = verification_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: result.get(k) for k in ("status", "steps", "checked_at", "ref")}
    keep["backend"] = result.get("backend", "openbao")
    path.write_text(json.dumps(keep, indent=2, sort_keys=True), encoding="utf-8")


def last_verification(settings: Settings) -> dict[str, Any] | None:
    try:
        data = json.loads(verification_file(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def platform_label() -> str:
    return {"win32": "Windows Credential Manager", "darwin": "macOS Keychain"}.get(sys.platform, "Secret Service")


__all__ = [
    "INTERIM_RESIDUAL_RISK", "InterimKeyringProvider", "KeyringBootstrapStore", "MemorySecretProvider",
    "OpenBaoProvider", "SecretProvider", "Selection", "check_project_scope", "check_purpose", "get_provider",
    "interim_exception", "last_verification", "openbao_provider", "project_domain", "record_verification",
    "select_backend", "set_provider_override", "strict_ref",
]
