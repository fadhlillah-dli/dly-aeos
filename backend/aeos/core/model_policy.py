"""AGENTS.md invariant 6 for model ids: the one check every routing seed, registry and ranking path uses (ADR-0129).

Claude is used only through the official ``claude`` binary with the native Max login (``aeos.claude_bridge``, provider
``claude-native``). A Claude-family model on any other provider (an OpenRouter ``anthropic/*`` id, a ``~anthropic/*``
floating alias, the OpenCode Go ``claude-haiku-*`` model, a Bedrock-style ``us.anthropic.claude-*`` id) would be the
Claude API by another door, so it is refused. A meta-router (``openrouter/auto`` and similar, or a ``~vendor/*-latest``
floating alias) picks the model itself, so neither the configuration hash nor the ranking would name what runs: it is
refused too.

Stdlib only and free of side effects: ``scripts/role_route_ranking.py`` imports it with the backend on ``sys.path``.
Callers turn a reason into their own refusal (seed: ``PolicyDenied``; catalog discovery: skipped and reported;
eligibility: a hard reason; ranking and the agent catalog: a finding). Nothing here is silently stripped.
"""

from __future__ import annotations

import re

NATIVE_CLAUDE_PROVIDER = "claude-native"
CLAUDE_FAMILY = "claude"
META_ROUTER = "meta-router"

# a token that starts with claude or anthropic, not preceded by a letter or digit: claude-haiku-5-5, anthropic/x,
# ~anthropic/x, us.anthropic.claude-x, opencode-go/claude-y (near misses: deepseek, qwen3.8-max, gemini-4-argon)
_CLAUDE = re.compile(r"(?<![a-z0-9])(?:claude|anthropic)(?![a-z0-9])")
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
