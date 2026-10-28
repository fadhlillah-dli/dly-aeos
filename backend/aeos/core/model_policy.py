"""AGENTS.md invariant 6 for model ids: the one check every routing seed, registry and ranking path uses (ADR-0129).

Claude is used only through the official ``claude`` binary with the native Max login (``aeos.claude_bridge``, provider
``claude-native``). A Claude-family model on any other provider (an OpenRouter ``anthropic/*`` id, a ``~anthropic/*``
floating alias, the OpenCode Go ``claude-haiku-*`` model, a Bedrock-style ``us.anthropic.claude-*`` id) would be the
Claude API by another door, so it is refused. A meta-router (``openrouter/auto`` and similar, or a ``~vendor/*-latest``
floating alias) picks the model itself, so neither the configuration hash nor the ranking would name what runs: it is
refused too.

The Claude-family rule is a prefix rule: a token that STARTS WITH ``claude`` or ``anthropic`` (any case, whatever
follows: ``claude3-opus``, ``claude4.5-haiku``, ``Claude-X``) is refused, where a token starts at the beginning of the id
or of a path segment, or after any other non-alphanumeric separator (``.``, ``-``, ``_``, ``~``, ``:``). Only a letter
or digit directly before it makes it part of another name (``myclaude3``, ``xanthropic``): such ids stay allowed.

One check at every door (ADR-0129): seed, catalog discovery, eligibility, the ranking and the agent catalog call
``invariant6_refusal``/``model_family_refusal``; route activation (``aeos.routing.service.activate_route``) and the
broker egress (``aeos.broker.model_proxy.resolve_upstream``, used by every probe and every worker call) call
``route_invariant6_refusal`` on the stored row, so a row that reached the database by another path is never
activated, probed or called (code ``MODEL_FAMILY_REFUSED``).

Stdlib only and free of side effects: ``scripts/role_route_ranking.py`` imports it with the backend on ``sys.path``.
Callers turn a reason into their own refusal (seed: ``PolicyDenied``; catalog discovery: skipped and reported;
eligibility: a hard reason; ranking and the agent catalog: a finding). Nothing here is silently stripped.
"""

from __future__ import annotations

import re
from typing import Any

NATIVE_CLAUDE_PROVIDER = "claude-native"
CLAUDE_FAMILY = "claude"
META_ROUTER = "meta-router"
# the refusal code of activation, egress and seed (one code for every door)
REFUSAL_CODE = "MODEL_FAMILY_REFUSED"

# a token that STARTS WITH claude or anthropic, whatever follows (claude-haiku-5-5, claude3-opus, claude4.5-haiku,
# anthropic/x, ~anthropic/x, us.anthropic.claude-x, opencode-go/claude-y); only a letter or digit directly before it
# makes it part of another name (near misses: myclaude3, deepseek, qwen3.8-max, gemini-4-argon). Matched lower-cased.
_CLAUDE = re.compile(r"(?<![a-z0-9])(?:claude|anthropic)")
# route-key / config prefixes of the providers AEOS knows (a route key is "<provider>/<model id>")
_PROVIDER_PREFIXES = ("openrouter/", "opencode-go/", "opencode-zen/", "opencode/", "gemini/", "local/")
# OpenRouter's own routing models (catalog read 2026-10-28) and the router ids of other gateways
META_ROUTER_IDS = frozenset({
    "auto", "router", "openrouter/auto", "openrouter/auto-beta", "openrouter/free", "openrouter/fusion",
    "openrouter/pareto-code", "openrouter/bodybuilder",
})
_META_NAME = re.compile(r"^(?:auto|auto-.+|.*router|pareto.*|fusion)$")


def _forms(model: str) -> list[str]:
    """The lower-cased id, without a ``:variant`` suffix, as given and with one known provider prefix removed."""
    s = model.strip().lower()
    out = [s]
    for prefix in _PROVIDER_PREFIXES:
        if s.startswith(prefix) and len(s) > len(prefix):
            out.append(s[len(prefix):])
    return [f.split(":", 1)[0] for f in out]


def model_family_refusal(model: str | None) -> str | None:
    """``claude`` or ``meta-router`` when *model* (a model id, an upstream id or a route key) is one, else None."""
    if not model:
        return None
    forms = _forms(model)
    if any(_CLAUDE.search(f) for f in forms):
        return CLAUDE_FAMILY
    for f in forms:
        if f.startswith("~") or f in META_ROUTER_IDS:
            return META_ROUTER
        vendor, _, name = f.partition("/")
        if (name and vendor == "openrouter" and _META_NAME.match(name)) or (name and name.endswith("router")):
            return META_ROUTER
    return None


def invariant6_refusal(*ids: str | None, provider: str | None = None) -> str | None:
    """The refusal reason for a route or model that is a Claude-family model off the native bridge or a meta-router
    (None = allowed). *ids* are the route key, model id and upstream model id; the native ``claude-native`` provider is
    the one allowed Claude path (AGENTS.md invariant 6)."""
    for model in ids:
        family = model_family_refusal(model)
        if family is None:
            continue
        if family == CLAUDE_FAMILY and provider == NATIVE_CLAUDE_PROVIDER:
            continue
        if family == CLAUDE_FAMILY:
            return (f"{model} is a Claude-family model: Claude runs only through the native claude binary with the "
                    "Max login, never through OpenRouter, OpenCode Go/Zen or any API (AGENTS.md invariant 6)")
        return (f"{model} is a meta-router or floating alias: it picks the model itself, so the configuration and "
                "the ranking would not name what runs (AGENTS.md invariant 6, ADR-0129)")
    return None


def route_invariant6_refusal(route: Any) -> str | None:
    """The refusal reason for a stored route row (None = allowed): its route key, model id and upstream model id under
    its provider, through ``invariant6_refusal``. The one helper of route activation and the broker egress boundary;
    *route* is any object with ``route_key``, ``model_id``, ``provider`` and ``mode_params`` (a ``ModelRoute``)."""
    mode = getattr(route, "mode_params", None) or {}
    upstream = mode.get("upstream_model") if isinstance(mode, dict) else None
    return invariant6_refusal(getattr(route, "route_key", None), getattr(route, "model_id", None),
                              upstream if isinstance(upstream, str) else None, provider=getattr(route, "provider", None))
