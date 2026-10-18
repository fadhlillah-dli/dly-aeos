"""Minimal OpenBao HTTP API client (S24): health/seal, init/unseal, KV v2, ACL policies, AppRole.

Loopback only (V1, §06): non-loopback addresses are refused. ``trust_env=False`` so that proxy
variables never route vault traffic elsewhere. Tokens travel in ``X-Vault-Token`` (OpenBao keeps the
Vault-compatible header) and every token/secret value seen here is registered for redaction.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from aeos.core.redaction import redact_text, register_secret

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
SLOW_TIMEOUT = httpx.Timeout(120.0, connect=5.0)  # init/unseal: raft bootstrap + key generation


class OpenBaoError(Exception):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(redact_text(message))
        self.status = status


class OpenBaoUnavailable(OpenBaoError):
    """Unreachable / sealed / not initialised — callers fail closed."""


def validate_addr(addr: str) -> str:
    parts = urlsplit(addr)
    if parts.scheme not in {"http", "https"} or (parts.hostname or "") not in LOOPBACK:
        raise OpenBaoUnavailable(f"OpenBao address must be loopback http(s) in V1, got {parts.hostname!r}")
    return addr.rstrip("/")


def logical_addr(addr: str) -> str:
    p = urlsplit(addr)
    return f"{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}"


def kv_path(name: str) -> str:
    return "/".join(quote(seg, safe="._-") for seg in name.split("/"))


class OpenBaoClient:
    def __init__(self, addr: str, *, token: str | None = None, http: httpx.Client | None = None):
        self.addr = validate_addr(addr)
        self.token = token
        self._http = http or httpx.Client(trust_env=False, timeout=TIMEOUT, follow_redirects=False)
        self._own = http is None

    def close(self) -> None:
        if self._own:
            self._http.close()

    def _req(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
             auth: bool = True, ok: tuple[int, ...] = (200, 204), timeout: httpx.Timeout | None = None) -> httpx.Response:
        headers = {"X-Vault-Request": "true"}
        if auth:
            if not self.token:
                raise OpenBaoError("no OpenBao token available")
            headers["X-Vault-Token"] = self.token
        try:
            r = self._http.request(method, f"{self.addr}/v1/{path.lstrip('/')}", json=json, params=params,
                                   headers=headers, timeout=timeout or TIMEOUT)
        except httpx.HTTPError as exc:
            raise OpenBaoUnavailable(f"OpenBao unreachable at {logical_addr(self.addr)}: {type(exc).__name__}") from exc
        if r.status_code == 503 and 503 not in ok:
            raise OpenBaoUnavailable("OpenBao is sealed or unavailable (HTTP 503)", status=503)
        if r.status_code not in ok:
            msg = ""
            try:
                errs = r.json().get("errors") or []
                msg = "; ".join(str(e) for e in errs)[:300]
            except (ValueError, AttributeError):
                msg = r.text[:200]
            raise OpenBaoError(f"OpenBao {method} {path.split('?')[0]} -> HTTP {r.status_code}: {msg}",
                               status=r.status_code)
        return r

    @staticmethod
    def _json(r: httpx.Response) -> dict[str, Any]:
        if r.status_code == 204 or not r.content:
            return {}
        data = r.json()
        return data if isinstance(data, dict) else {}

    # ---- system
    def health(self) -> dict[str, Any]:
        r = self._req("GET", "sys/health", auth=False, ok=(200, 429, 472, 473, 501, 503),
                      params={"standbyok": "true", "sealedcode": "200", "uninitcode": "200"})
        data = self._json(r)
        return {"reachable": True, "initialized": bool(data.get("initialized")), "sealed": bool(data.get("sealed", True)),
                "standby": bool(data.get("standby")), "version": data.get("version")}

    def seal_status(self) -> dict[str, Any]:
        return self._json(self._req("GET", "sys/seal-status", auth=False))

    def init(self, shares: int, threshold: int) -> dict[str, Any]:
        data = self._json(self._req("PUT", "sys/init", auth=False, timeout=SLOW_TIMEOUT,
                                    json={"secret_shares": shares, "secret_threshold": threshold}))
        for k in data.get("keys") or []:
            register_secret(k)
        for k in data.get("keys_base64") or []:
            register_secret(k)
        register_secret(data.get("root_token"))
        return data

    def unseal(self, key: str) -> dict[str, Any]:
        register_secret(key)
        return self._json(self._req("PUT", "sys/unseal", auth=False, json={"key": key}, timeout=SLOW_TIMEOUT))

    def enable_kv2(self, mount: str) -> None:
        self._req("POST", f"sys/mounts/{mount}", json={"type": "kv", "options": {"version": "2"},
                                                        "description": "DLY AEOS runtime secrets"})

    def write_policy(self, name: str, hcl: str) -> None:
        self._req("PUT", f"sys/policies/acl/{name}", json={"policy": hcl})

    def enable_approle(self) -> None:
        self._req("POST", "sys/auth/approle", json={"type": "approle", "description": "DLY AEOS controller"})

    def write_approle_role(self, role: str, *, policies: list[str], token_ttl: str, token_max_ttl: str) -> None:
        self._req("POST", f"auth/approle/role/{role}", json={
            "token_policies": policies, "token_ttl": token_ttl, "token_max_ttl": token_max_ttl,
            "secret_id_num_uses": 0, "secret_id_ttl": "0", "token_no_default_policy": False,
        })

    def approle_role_id(self, role: str) -> str:
        rid = self._json(self._req("GET", f"auth/approle/role/{role}/role-id"))["data"]["role_id"]
        register_secret(rid)
        return str(rid)

    def approle_secret_id(self, role: str) -> str:
        sid = self._json(self._req("POST", f"auth/approle/role/{role}/secret-id", json={}))["data"]["secret_id"]
        register_secret(sid)
        return str(sid)

    def approle_login(self, role_id: str, secret_id: str) -> dict[str, Any]:
        auth = self._json(self._req("POST", "auth/approle/login", auth=False,
                                    json={"role_id": role_id, "secret_id": secret_id})).get("auth") or {}
        register_secret(auth.get("client_token"))
        return auth

    def renew_self(self, increment: str = "1h") -> dict[str, Any]:
        auth = self._json(self._req("POST", "auth/token/renew-self", json={"increment": increment})).get("auth") or {}
        register_secret(auth.get("client_token"))
        return auth

    def lookup_self(self) -> dict[str, Any]:
        return self._json(self._req("GET", "auth/token/lookup-self")).get("data") or {}

    def revoke_self(self) -> None:
        self._req("POST", "auth/token/revoke-self", json={})

    # ---- KV v2
    def kv_read(self, mount: str, name: str) -> dict[str, Any] | None:
        r = self._req("GET", f"{mount}/data/{kv_path(name)}", ok=(200, 404))
        if r.status_code == 404:
            return None
        return self._json(r).get("data") or {}

    def kv_write(self, mount: str, name: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._req("POST", f"{mount}/data/{kv_path(name)}", json={"data": data})).get("data") or {}

    def kv_metadata(self, mount: str, name: str) -> dict[str, Any] | None:
        r = self._req("GET", f"{mount}/metadata/{kv_path(name)}", ok=(200, 404))
        if r.status_code == 404:
            return None
        return self._json(r).get("data") or {}

    def kv_write_metadata(self, mount: str, name: str, custom: dict[str, str]) -> None:
        self._req("POST", f"{mount}/metadata/{kv_path(name)}", json={"custom_metadata": custom})

    def kv_list(self, mount: str, prefix: str) -> list[str]:
        r = self._req("GET", f"{mount}/metadata/{kv_path(prefix)}/", params={"list": "true"}, ok=(200, 404))
        if r.status_code == 404:
            return []
        return [str(k) for k in (self._json(r).get("data") or {}).get("keys") or []]

    def kv_delete_all(self, mount: str, name: str) -> None:
        self._req("DELETE", f"{mount}/metadata/{kv_path(name)}")
