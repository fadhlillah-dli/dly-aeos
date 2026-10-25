"""Structured scope of the OFFICE company authorisation (§01 U-03, §04, §07 step 10, §23, §51 P1).

A company authorisation is more than a provider list. The company approves *which* tenants/targets AEOS may use,
*which* data classes may reach an approved model route or Claude Max, and (optionally) *which* identity the
delegated ``office/`` credential must act as. These are recorded by the human owner on the company-authorisation
decision (``recording.record_generic_decision``) and read here; nothing is inferred or defaulted:

* ``approved_targets`` - one scope per approved target: Confluence/Jira ``{provider, tenant[, root_id][, space_id]}``,
  Bitbucket ``{provider, workspace[, root_id]}``. Every key a scope names must equal the observed target; a
  provider-only scope is refused (too broad to bind anything).
* ``approved_data_classes`` - the exact data classes (``DataClassification``) the company approved for AI
  processing of OFFICE data (worker routes, Claude Max). Exact membership; no implied hierarchy.
* ``approved_identities`` - optional, per OFFICE credential reference (``office/atlassian``, ``office/bitbucket``):
  the account id / email / workspace user the company issued. The Doctor check ``secret.office_identity`` compares
  it with what the provider reports for the stored credential.

Absent means UNDECIDED and fails closed: no approved target -> no OFFICE target is authorised; no approved data
class -> no OFFICE data reaches a model. Leaf module (policy catalog + providers vocabulary only).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aeos.core.enums import DataClassification
from aeos.core.errors import ValidationFailed
from aeos.portfolio import providers as pv

# provider -> (required scope keys, optional scope keys)
TARGET_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    pv.CONFLUENCE: (("tenant",), ("space_id", "root_id")),
    pv.JIRA: (("tenant",), ("root_id",)),
    pv.BITBUCKET: (("workspace",), ("root_id",)),
}
DATA_CLASSES: tuple[str, ...] = tuple(c.value for c in DataClassification)
ATLASSIAN_IDENTITY_REF = "office/atlassian"
BITBUCKET_IDENTITY_REF = "office/bitbucket"
IDENTITY_REFS: tuple[str, ...] = (ATLASSIAN_IDENTITY_REF, BITBUCKET_IDENTITY_REF)
IDENTITY_REF_OF_PROVIDER: dict[str, str] = {pv.CONFLUENCE: ATLASSIAN_IDENTITY_REF, pv.JIRA: ATLASSIAN_IDENTITY_REF,
                                            pv.BITBUCKET: BITBUCKET_IDENTITY_REF}
_IDENTITY_ALIASES = {"atlassian": ATLASSIAN_IDENTITY_REF, "confluence": ATLASSIAN_IDENTITY_REF,
                     "jira": ATLASSIAN_IDENTITY_REF, "bitbucket": BITBUCKET_IDENTITY_REF}
MAX_TARGETS = 50

_TENANT = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_WORKSPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,61}$")
_ROOT: dict[str, re.Pattern[str]] = {
    pv.CONFLUENCE: re.compile(r"^\d{1,20}$"),
    pv.JIRA: re.compile(r"^[A-Z][A-Z0-9_]{1,19}$"),
    pv.BITBUCKET: re.compile(r"^[a-z0-9][a-z0-9_-]{0,61}/[a-z0-9._-]{1,100}$"),
}
_SPACE = re.compile(r"^[A-Za-z0-9_~-]{1,64}$")
_IDENTITY = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


def normalize_tenant(value: Any) -> str:
    """``https://Acme.atlassian.net/`` -> ``acme.atlassian.net`` (the Integration Registry stores no scheme)."""
    v = str(value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    return v.rstrip("/")


def _norm_root(provider: str, value: Any) -> str:
    v = str(value or "").strip()
    if provider == pv.JIRA:
        return v.upper()
    if provider == pv.BITBUCKET:
        return v.lower()
    return v


def observed_identity(provider: str, *, root_id: Any = None, tenant: Any = None,
                      space_id: Any = None) -> dict[str, str]:
    """The comparable identity of a configured/used OFFICE target (missing parts are left out)."""
    p = pv.normalize(provider)
    out: dict[str, str] = {}
    root = _norm_root(p, root_id)
    if root:
        out["root_id"] = root
    if p == pv.BITBUCKET:
        if "/" in root:
            out["workspace"] = root.split("/", 1)[0]
    else:
        t = normalize_tenant(tenant)
        if t:
            out["tenant"] = t
        if p == pv.CONFLUENCE and str(space_id or "").strip():
            out["space_id"] = str(space_id).strip()
    return out


def describe(scope: Mapping[str, Any]) -> str:
    keys = [k for k in ("tenant", "workspace", "space_id", "root_id") if scope.get(k)]
    return f"{scope.get('provider', '?')}(" + ", ".join(f"{k}={scope[k]}" for k in keys) + ")"


def parse_target_spec(text: str) -> dict[str, str]:
    """``confluence:tenant=acme.atlassian.net,space_id=98765`` (also ``;`` or spaces between pairs) -> a dict.

    The CLI and the Control Center send this text form; the API also accepts the dict form directly."""
    raw = str(text or "").strip()
    provider, sep, rest = raw.partition(":")
    if not sep or not provider.strip():
        raise ValidationFailed(f"approved target {raw!r} must look like provider:key=value,key=value",
                               code="APPROVED_TARGET_INVALID")
    out: dict[str, str] = {"provider": provider.strip()}
    for part in re.split(r"[,;\s]+", rest.strip()):
        if not part:
            continue
        k, eq, v = part.partition("=")
        if not eq or not k.strip():
            raise ValidationFailed(f"approved target {raw!r}: expected key=value, got {part!r}",
                                   code="APPROVED_TARGET_INVALID")
        out[k.strip().lower()] = v.strip()
    return out


def normalize_target(item: Any, *, allowed_providers: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Validate one approved target scope (dict or text form). Raises ``ValidationFailed``."""
    spec = parse_target_spec(item) if isinstance(item, str) else item
    if not isinstance(spec, Mapping):
        raise ValidationFailed("approved target must be an object or provider:key=value text",
                               code="APPROVED_TARGET_INVALID")
    provider = pv.normalize(str(spec.get("provider") or ""))
    if provider not in TARGET_KEYS:
        raise ValidationFailed(f"approved target provider {provider or '(none)'!r} has no target scope",
                               code="APPROVED_TARGET_INVALID", details={"allowed": sorted(TARGET_KEYS)})
    if provider not in allowed_providers:
        raise ValidationFailed(f"approved target for {provider} needs {provider} in allowed_providers",
                               code="APPROVED_TARGET_PROVIDER_NOT_ALLOWED")
    required, optional = TARGET_KEYS[provider]
    unknown = sorted(set(spec) - {"provider", *required, *optional})
    if unknown:
        raise ValidationFailed(f"approved {provider} target: unknown keys {unknown}", code="APPROVED_TARGET_INVALID",
                               details={"required": list(required), "optional": list(optional)})
    out: dict[str, str] = {"provider": provider}
    for key in (*required, *optional):
        value = spec.get(key)
        if value in (None, ""):
            if key in required:
                raise ValidationFailed(f"approved {provider} target needs {key} (a provider-only scope is too broad)",
                                       code="APPROVED_TARGET_INVALID")
            continue
        if key == "tenant":
            v = normalize_tenant(value)
            ok = bool(_TENANT.match(v))
        elif key == "workspace":
            v = str(value).strip().lower()
            ok = bool(_WORKSPACE.match(v))
        elif key == "space_id":
            v = str(value).strip()
            ok = bool(_SPACE.match(v))
        else:
            v = _norm_root(provider, value)
            ok = bool(_ROOT[provider].match(v))
        if not ok:
            raise ValidationFailed(f"approved {provider} target: {key} {value!r} has an invalid format",
                                   code="APPROVED_TARGET_INVALID")
        out[key] = v
    if provider == pv.BITBUCKET and out.get("root_id") and out["root_id"].split("/", 1)[0] != out["workspace"]:
        raise ValidationFailed("approved bitbucket target: root_id must be <workspace>/<repo_slug> of that workspace",
                               code="APPROVED_TARGET_INVALID")
    return out


def normalize_targets(items: Any, *, allowed_providers: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    if items in (None, ""):
        return []
    if not isinstance(items, (list, tuple)):
        raise ValidationFailed("approved_targets must be a list", code="APPROVED_TARGET_INVALID")
    if len(items) > MAX_TARGETS:
        raise ValidationFailed(f"at most {MAX_TARGETS} approved targets", code="APPROVED_TARGET_INVALID")
    out: list[dict[str, str]] = []
    for item in items:
        t = normalize_target(item, allowed_providers=allowed_providers)
        if t not in out:
            out.append(t)
    return out


def normalize_data_classes(items: Any) -> list[str]:
    if items in (None, ""):
        return []
    if not isinstance(items, (list, tuple)):
        raise ValidationFailed("approved_data_classes must be a list", code="APPROVED_DATA_CLASS_INVALID")
    given = {str(c).strip().upper() for c in items if str(c).strip()}
    unknown = sorted(given - set(DATA_CLASSES))
    if unknown:
        raise ValidationFailed(f"unknown data classes {unknown}", code="APPROVED_DATA_CLASS_INVALID",
                               details={"allowed": list(DATA_CLASSES)})
    return [c for c in DATA_CLASSES if c in given]


def normalize_identities(items: Any) -> dict[str, str]:
    """``{"office/atlassian": "<account id or email>"}`` or ``["office/bitbucket=<uuid>"]`` -> dict."""
    if items in (None, "", {}, []):
        return {}
    pairs: list[tuple[Any, Any]]
    if isinstance(items, Mapping):
        pairs = list(items.items())
    elif isinstance(items, (list, tuple)):
        pairs = []
        for it in items:
            k, eq, v = str(it).partition("=")
            if not eq:
                raise ValidationFailed(f"approved identity {it!r} must look like office/<provider>=<identity>",
                                       code="APPROVED_IDENTITY_INVALID")
            pairs.append((k, v))
    else:
        raise ValidationFailed("approved_identities must be an object", code="APPROVED_IDENTITY_INVALID")
    from aeos.core.redaction import contains_secret

    out: dict[str, str] = {}
    for k, v in pairs:
        key = str(k or "").strip().lower()
        ref = key if key in IDENTITY_REFS else _IDENTITY_ALIASES.get(key)
        if ref is None:
            raise ValidationFailed(f"approved identity key {key!r} must be one of {list(IDENTITY_REFS)}",
                                   code="APPROVED_IDENTITY_INVALID")
        value = str(v or "").strip()
        if not _IDENTITY.match(value):
            raise ValidationFailed(f"approved identity for {ref} must be 1-200 printable characters",
                                   code="APPROVED_IDENTITY_INVALID")
        if contains_secret(value):
            raise ValidationFailed(f"approved identity for {ref} looks like a credential; record the account id or "
                                   "email, never a token", code="SECRET_IN_FIELD")
        out[ref] = value
    return out


# ---------------------------------------------------------------- reading the effective scope


def effective_scope(details: Mapping[str, Any] | None, authorized_providers: list[str]) -> dict[str, Any]:
    """The structured scope stored on an ACTIVE company authorisation, re-checked (malformed entries dropped)."""
    d = details or {}
    targets: list[dict[str, str]] = []
    for t in d.get("approved_targets") or []:
        try:
            nt = normalize_target(t, allowed_providers=authorized_providers)
        except ValidationFailed:
            continue
        if nt not in targets:
            targets.append(nt)
    try:
        classes = normalize_data_classes(d.get("approved_data_classes"))
    except ValidationFailed:
        classes = []
    try:
        identities = normalize_identities(d.get("approved_identities"))
    except ValidationFailed:
        identities = {}
    return {"approved_targets": targets, "approved_data_classes": classes, "approved_identities": identities}


def target_check(policy: Mapping[str, Any], provider: str, *, root_id: Any = None, tenant: Any = None,
                 space_id: Any = None) -> tuple[bool, str]:
    """(authorised, reason) for one OFFICE target against the company-approved targets (fail closed)."""
    p = pv.normalize(provider)
    if policy.get("office_authorization") is not True:
        return False, "no ACTIVE company authorisation for OFFICE (§23 default deny)"
    if p not in TARGET_KEYS:
        return False, f"{p or '(none)'} has no company-approved target scope"
    observed = {"provider": p, **observed_identity(p, root_id=root_id, tenant=tenant, space_id=space_id)}
    scopes = [t for t in policy.get("approved_targets") or [] if isinstance(t, Mapping) and t.get("provider") == p]
    if not scopes:
        return False, (f"the company authorisation names no approved {p} target (approved_targets is UNDECIDED; "
                       "record it on the company-authorisation decision)")
    for scope in scopes:
        keys = [k for k in scope if k != "provider"]
        if keys and all(observed.get(k) and str(observed[k]) == str(scope[k]) for k in keys):
            return True, f"{describe(observed)} lies within company-approved {describe(scope)}"
    return False, (f"{describe(observed)} is outside the company-approved targets "
                   f"({'; '.join(describe(s) for s in scopes)})")


def data_class_check(policy: Mapping[str, Any], classification: Any) -> tuple[bool, str]:
    """(authorised, reason) for sending data of *classification* to an approved OFFICE model route/Claude Max."""
    if policy.get("office_authorization") is not True:
        return False, "no ACTIVE company authorisation for OFFICE (§23 default deny)"
    cls = str(classification or "").strip().upper() or "UNKNOWN"
    approved = [c for c in policy.get("approved_data_classes") or [] if isinstance(c, str)]
    if not approved:
        return False, ("the company authorisation approves no data class (approved_data_classes is UNDECIDED; "
                       "§51 P1) - OFFICE data is not sent to a model")
    if cls not in approved:
        return False, f"data classification {cls} is not company-approved (approved: {', '.join(approved)})"
    return True, f"data classification {cls} is company-approved"
