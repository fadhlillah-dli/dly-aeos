"""OpenBao initialisation for DLY AEOS (§26, S24).

1. ``sys/init`` (key shares/threshold) -> unseal keys + root token. By default (ADR-0123) both go to the OS
   keyring at once, before any other step can fail; the root token entry is removed again once it is revoked.
   With ``store_unseal_in_keyring=False`` the caller shows the keys to the owner ONCE (and the root token only if
   setup stays incomplete), and share(s) of an earlier vault still in the OS keyring are removed;
2. unseal with the threshold keys;
3. with the root token: KV v2 at ``aeos``; ACL policies ``aeos-personal``/``aeos-office``/
   ``aeos-workers``/``aeos-platform``/``aeos-projects`` (one per namespace); AppRole auth with role
   ``aeos-controller`` bound to personal/workers/platform/projects — NOT office (OFFICE default
   deny: binding the office policy needs a recorded company authorisation);
4. role_id/secret_id stored in the OS keyring bootstrap store;
5. AppRole login + lookup-self proves the controller identity works, then the root token is
   revoked (and removed from the keyring). If any step fails the root token is NOT revoked so the owner can
   finish/retry; in keyring mode it stays in the keyring and is never printed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from aeos.secrets.openbao import OpenBaoClient, OpenBaoError
from aeos.secrets.provider import APPROLE_NAME, KV_MOUNT, KeyringBootstrapStore

POLICY_NAMESPACES = {
    "aeos-personal": "personal", "aeos-office": "office", "aeos-workers": "workers", "aeos-platform": "platform",
    "aeos-projects": "projects",
}
CONTROLLER_POLICIES = ["aeos-personal", "aeos-workers", "aeos-platform", "aeos-projects"]
TOKEN_TTL = "1h"
TOKEN_MAX_TTL = "24h"


def policy_hcl(namespace: str) -> str:
    return (
        f'# DLY AEOS namespace "{namespace}" (KV v2 mount "{KV_MOUNT}")\n'
        f'path "{KV_MOUNT}/data/{namespace}/*" {{\n  capabilities = ["create", "read", "update", "delete"]\n}}\n'
        f'path "{KV_MOUNT}/metadata/{namespace}/*" {{\n  capabilities = ["read", "list", "update", "delete"]\n}}\n'
    )


@dataclass
class InitResult:
    keys: list[str] = field(default_factory=list)
    root_token: str | None = None
    threshold: int = 1
    steps: list[str] = field(default_factory=list)
    approle_stored: bool = False
    unseal_stored: bool = False
    root_stored: bool = False
    root_revoked: bool = False
    store_error: str | None = None
    error: str | None = None


def _wait_active(c: OpenBaoClient, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        h = c.health()
        if h.get("initialized") and not h.get("sealed") and not h.get("standby"):
            return
        time.sleep(0.5)
    raise OpenBaoError("OpenBao did not become active after unseal")


def _clear_earlier_shares(bootstrap: KeyringBootstrapStore, res: InitResult) -> None:
    """``--show-unseal-once``: share(s) of an earlier vault left in the OS keyring cannot unseal the vault this init
    just created (OpenBao reported it uninitialised), and auto-unseal would apply them and report a rejected key.
    Removed right after a successful ``sys/init``; a failure to remove them is a step note, never a failed setup."""
    try:
        stale = bootstrap.unseal_key_count()
        if stale:
            bootstrap.clear_unseal_keys()
            res.steps.append(f"{stale} unseal key share(s) of an earlier vault removed from the OS keyring")
    except Exception as exc:  # the new vault exists; the note names the leftover, never a value
        res.steps.append(f"unseal key share(s) of an earlier vault NOT removed from the OS keyring "
                         f"({type(exc).__name__}): aeos serve would apply them and report a rejected key")


def run_init(addr: str, *, shares: int, threshold: int, bootstrap: KeyringBootstrapStore,
             store_unseal_in_keyring: bool = False, http: httpx.Client | None = None) -> InitResult:
    if not 1 <= threshold <= shares <= 10:
        raise ValueError("require 1 <= threshold <= shares <= 10")
    ok, backend = bootstrap.available()
    if not ok:
        raise OpenBaoError(f"OS keyring unavailable ({backend}); AppRole credentials could not be stored")
    c = OpenBaoClient(addr, http=http)
    if c.health().get("initialized"):
        raise OpenBaoError("OpenBao is already initialised (use `aeos vault unseal` / `aeos vault status`)")
    data = c.init(shares, threshold)
    res = InitResult(keys=list(data.get("keys_base64") or data.get("keys") or []),
                     root_token=data.get("root_token"), threshold=threshold, steps=["initialised"])
    # From here on nothing may escape: the unseal keys/root token exist only in `res` (and, in keyring mode, in
    # the OS keyring) and must reach the owner even when a later step fails.
    try:
        if len(res.keys) < threshold or not res.root_token:
            raise OpenBaoError("init returned no keys/root token")
        if store_unseal_in_keyring:
            # first, so the material is safe before any later step can fail (ADR-0123)
            try:
                bootstrap.set_unseal_keys(res.keys)
                res.unseal_stored = True
                bootstrap.set_root_token(res.root_token)
                res.root_stored = True
            except Exception as exc:  # the caller falls back to showing the material once
                res.store_error = type(exc).__name__
                raise
            res.steps.append(f"{len(res.keys)} unseal key share(s) and the root token stored in the OS keyring")
        else:
            _clear_earlier_shares(bootstrap, res)
        st: dict = {}
        for k in res.keys[:threshold]:
            st = c.unseal(k)
        if st.get("sealed"):
            raise OpenBaoError("still sealed after applying the threshold keys")
        res.steps.append("unsealed")
        _wait_active(c)
        rc = OpenBaoClient(addr, token=res.root_token, http=http)
        rc.enable_kv2(KV_MOUNT)
        res.steps.append(f"kv-v2 mounted at {KV_MOUNT}/")
        for name, ns in POLICY_NAMESPACES.items():
            rc.write_policy(name, policy_hcl(ns))
        res.steps.append("policies " + ", ".join(POLICY_NAMESPACES))
        rc.enable_approle()
        rc.write_approle_role(APPROLE_NAME, policies=CONTROLLER_POLICIES, token_ttl=TOKEN_TTL, token_max_ttl=TOKEN_MAX_TTL)
        res.steps.append(f"approle {APPROLE_NAME} -> {', '.join(CONTROLLER_POLICIES)} (ttl {TOKEN_TTL}/{TOKEN_MAX_TTL})")
        role_id = rc.approle_role_id(APPROLE_NAME)
        secret_id = rc.approle_secret_id(APPROLE_NAME)
        bootstrap.set_approle(role_id, secret_id)
        res.approle_stored = True
        res.steps.append("approle role_id/secret_id stored in the OS keyring")
        auth = c.approle_login(role_id, secret_id)
        OpenBaoClient(addr, token=auth.get("client_token"), http=http).lookup_self()
        res.steps.append("approle login verified")
        rc.revoke_self()
        res.root_revoked = True
        res.steps.append("root token revoked")
        if res.root_stored:
            try:
                bootstrap.clear_root_token()
                res.root_stored = False
                res.steps.append("root token removed from the OS keyring")
            except Exception as exc:  # revoked already: a leftover entry is useless, not a failed setup
                res.steps.append(f"revoked root token NOT removed from the OS keyring ({type(exc).__name__})")
    except Exception as exc:  # see comment above: never lose the init material
        res.error = f"{type(exc).__name__}: {exc}"
    return res
