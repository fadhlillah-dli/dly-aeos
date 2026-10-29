"""Model registry seed from master §23 pools (DESIGN / CANDIDATE — never approved by seeding).

Catalog facts are E1 (documentation/catalog reading dated 2026-10-18): OpenCode Zen model ids and
data statements from S07; OpenRouter ids/context/expiry from the public catalog (S11); Gemini free
tier terms from S09; the local candidate from S60. Provider claims about retention/training are
not proof that project data routing is compliant (§23). Every worker route is PERSONAL-only at
most; OFFICE stays empty until a recorded company authorisation (``approved_worker_routes=[]``).
Seeding never activates a route, never claims installation and never fills ``resolved_version``
(UNKNOWN until the bridge/runtime observes it). Worker routes carry NO ``terms_checked_at``: reading the
provider statement for the master research is not an owner terms review (§23, §45, §51 P2) — the
reading date stays in ``data_terms.read_for_research`` and the owner confirms the terms at activation.

Request shape (ADR-0041 amendment 2026-10-25, E1 documentation reading, not exercised live): S07 documents the Zen
OpenAI-compatible endpoint ``https://opencode.ai/inference/openai/v1/chat/completions`` with the BARE model id
(``opencode/<id>`` is the OpenCode config format, kept as ``model_id`` because ``opencode models`` lists it so);
the bare id is ``mode_params.upstream_model`` and the documented API family ``mode_params.upstream_api``. The Gemini
API documents its OpenAI-compatible base ``https://generativelanguage.googleapis.com/v1beta/openai``. S07 also
states "Free-tier chat models require OpenCode; use paid models or Go from other clients": every free Zen route
carries that in ``data_terms.client_restriction`` and stays non-activatable/non-proxied until the owner decides.

OpenCode Go (ADR-0041 amendment 2026-10-26, owner decision A; E1 documentation reading dated 2026-10-26, not
exercised live): a $10/month subscription with per-model monthly DOLLAR allowances, served on
``https://opencode.ai/inference/go/openai/v1/chat/completions`` with the bare model id and an OpenCode Console API key
(``workers/opencode-go``). Go allows other clients ("Yes, you can use Go with any agent"), so its routes carry no
client restriction. Only chat-completions models are seeded (the broker speaks nothing else): the flash/coding models
as WORKER candidates ranked above the client-restricted Zen pilots, the others as WATCHLIST. Seeding activates nothing:
the terms review, the region and the BUDGET decision stay the owner's at activation. No Extra Usage attestation exists
in code (no decision preset, nothing AEOS can observe): the owner states Extra Usage OFF in the activation terms note;
at runtime a call billed beyond the plan refuses the route until the owner re-activates it (ADR-0080 amendment).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.broker.facade import SUPERSEDED_ENDPOINTS, UPSTREAM_API_CHAT
from aeos.core.errors import PolicyDenied
from aeos.core.model_policy import invariant6_refusal
from aeos.core.timeutil import ensure_aware
from aeos.models.routing import ModelRoute
from aeos.routing.service import REVIEWED_WORKER_TOOLS, compute_config_hash

RESEARCH_DATE = datetime(2026, 10, 18, tzinfo=UTC)
READ_FOR_RESEARCH = "2026-10-18 (master research reading; not an owner terms review)"
SHAPE_READ = "2026-10-25"  # S07 Zen endpoints table + console models page, Gemini OpenAI-compatibility page (E1)
ZEN_OPENAI_BASE = "https://opencode.ai/inference/openai/v1"  # + /chat/completions or /responses (S07)
ZEN_ANTHROPIC_BASE = "https://opencode.ai/inference/anthropic/v1"  # + /messages (S07)
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"  # + /chat/completions (S09 docs)
# S07 endpoint family per seeded model that is not served on the chat-completions endpoint (the broker refuses them)
ZEN_RESPONSES_MODELS = frozenset({"gpt-5.3-codex", "grok-build-0.1", "muse-spark-1.3"})
ZEN_MESSAGES_MODELS = frozenset({"qwen3.8-flash"})
# seeded Zen models the S07 catalog no longer lists (read 2026-10-25); `aeos routes refresh` marks them MISSING
ZEN_NOT_LISTED = frozenset({"longcat-2.5-preview-free", "space-bunny-free", "ll-alpha-free", "exo-free"})
# §23 "mode/thinking params" of worker-runtime routes: the broker sends no reasoning level (a worker-sent
# reasoning_effort is dropped and recorded), so the provider default applies; a stated level is a new configuration
WORKER_REASONING = "PROVIDER_DEFAULT"
ZEN_CLIENT_RESTRICTION = {
    "statement": "Free-tier chat models require OpenCode; use paid models or Go from other clients.",
    "source": f"S07 (https://opencode.ai/docs/zen/ and https://opencode.ai/v2/docs/console/models/, read {SHAPE_READ})",
    "applies_to": "broker-proxied calls: the AEOS broker calls the upstream as its own HTTP client, not OpenCode",
    "claim_level": "E1 provider statement - whether the free route refuses a broker call is NOT-PROVEN (no live call)",
    "owner_decision": "UNDECIDED",
}
WORKER_TOOL_SET = list(REVIEWED_WORKER_TOOLS)  # candidate tool set the owner reviews at activation
LOCAL_BUILD_PARAMS = {"quantization": "UNKNOWN", "inference_backend": "UNKNOWN", "hardware": "UNKNOWN"}
P = "PUBLIC"
I = "INTERNAL"  # noqa: E741 - data class abbreviation
C = "CONFIDENTIAL"

ZERO_RETENTION = {
    "retention": "zero-retention (provider claim, S07)", "training": "not used for training (provider claim, S07)",
    "may_use_inputs_for_training": False, "trial_only_no_confidential": False,
    "claim_level": "E1 provider statement - not proof of compliant routing",
}
MAY_TRAIN = {
    "retention": "UNKNOWN", "training": "during the free period collected data may be used to improve the model (S07)",
    "may_use_inputs_for_training": True, "trial_only_no_confidential": False,
    "claim_level": "E1 provider statement",
}
NVIDIA_TRIAL = {
    "retention": "usage logged for security purposes (S07)", "training": "UNKNOWN",
    "may_use_inputs_for_training": False, "trial_only_no_confidential": True,
    "statement": "Trial use only - do not submit personal or confidential data (S07)",
    "claim_level": "E1 provider statement",
}
BEAM_TERMS = {
    "retention": "retained 30 days for safety (S07)", "training": "not used for training (S07)",
    "may_use_inputs_for_training": False, "trial_only_no_confidential": False,
    "regions": "unavailable in several countries/regions (S07)", "claim_level": "E1 provider statement",
}
ZEN_FREE_PRICING = {"input_per_mtok": 0, "output_per_mtok": 0, "currency": "USD", "basis": "free for a limited time (S07)"}
# Non-OpenRouter OpenAI-compatible upstreams name their broker credential explicitly (§26: the key lives
# in the secrets store under the worker-model namespace; the broker enforces the domain namespace rule).
ZEN_CREDENTIAL_REF = "workers/opencode-zen"
# ---- OpenCode Go (ADR-0041 amendment 2026-10-26, owner decision A; E1 documentation reading, not exercised live) ----
GO_READ = "2026-10-26"
GO_READ_AT = datetime(2026, 10, 26, tzinfo=UTC)
GO_OPENAI_BASE = "https://opencode.ai/inference/go/openai/v1"  # + /chat/completions (Go docs "Endpoints")
GO_CREDENTIAL_REF = "workers/opencode-go"  # a Console API key of the (single-member) workspace subscribed to Go
GO_DOCS = "https://opencode.ai/v2/docs/console/go/"
GO_SOURCES = (GO_DOCS, "https://opencode.ai/go", "https://opencode.ai/v2/docs/console/harnesses/",
              "https://opencode.ai/v2/docs/console/api/inference/", "https://opencode.ai/legal/terms-of-service")
# tool calling is not in the Go docs; models.dev (the OpenCode org's model catalog) states it per Go model
GO_TOOLS_SOURCE = f"models.dev providers/opencode-go (github.com/anomalyco/models.dev, read {GO_READ})"
GO_TERMS: dict[str, Any] = {
    "retention": "0 days (provider claim, OpenCode Go privacy table)",
    "training": "not used for training (provider claim, OpenCode Go privacy table)",
    "may_use_inputs_for_training": False, "trial_only_no_confidential": False,
    # a workspace privacy setting restricts regions: the owner states the reviewed region at activation
    "regions": "workspace privacy setting; allowed regions default Europe & United States (OpenCode Go docs)",
    "training_endpoints": "paid endpoints that train on request data are blocked by default (workspace privacy)",
    # no ``client_restriction``: unlike the free Zen models, Go is documented for other clients
    "client_permission": {
        "statement": "Can I use Go with other coding agents? Yes, you can use Go with any agent.",
        "conditions": "send typical coding agent traffic, identify with its own User-Agent and send a stable "
                      "x-opencode-session per conversation (the broker sends both)",
        "source": f"https://opencode.ai/go (FAQ) and {GO_DOCS} 'Where can I use it?', read {GO_READ}",
        "claim_level": "E1 provider statement - whether Go accepts the broker as a client is NOT-PROVEN (no live call)",
    },
    "provider_paid_fallback": "Extra Usage falls back to the pay-as-you-go balance after a limit: must stay OFF "
                              "(the owner states it in the activation terms note; AEOS cannot observe the setting)",
    "automation_terms": "the ToS clause on automated/programmatic use is an owner risk recorded at activation "
                        "(https://opencode.ai/legal/terms-of-service)",
    "sources": list(GO_SOURCES),
    "claim_level": "E1 provider statement - not proof of compliant routing",
    "read_for_research": f"{GO_READ} (OpenCode Go docs reading for owner decision A; not an owner terms review)",
}
GO_ALLOWANCE_WINDOWS = {"5h": 0.2, "week": 0.5, "month": 1.0}  # share of the per-model monthly allowance (Go docs)
GO_DEEPSEEK_TERMS: dict[str, Any] = {
    **GO_TERMS,
    "retention": "0 days* (provider claim): the DeepSeek ZDR agreement is renewed monthly, the current one is valid "
                 "through 2026-10-31 (OpenCode Go privacy table)",
    "regions": "needs the Global region (Settings > Privacy > Regions); the workspace default Europe & United "
               "States excludes it (OpenCode Go docs)",
}
# (model, display, monthly allowance USD, allowance rate per 1M tokens, supports_tools per GO_TOOLS_SOURCE)
GO_WORKERS: tuple[tuple[str, str, float, dict[str, float], str], ...] = (
    ("glm-5.3-flash", "GLM-5.3-Flash", 60, {"input": 0.15, "output": 0.50, "cached_read": 0.03}, "YES"),
    ("mimo-v2.6-flash", "MiMo-V2.6-Flash", 60, {"input": 0.14, "output": 0.28}, "YES"),
    ("minimax-m3.1-flash", "MiniMax M3.1 Flash", 20, {"input": 0.15, "output": 0.60}, "YES"),
    ("hy3", "Hy3", 60, {"input": 0.14, "output": 0.58}, "UNKNOWN"),  # models.dev states no tool capability
    ("kimi-k2.7-code", "Kimi K2.7 Code", 60, {"input": 0.95, "output": 4.00}, "YES"),
)
GO_WATCH: tuple[tuple[str, str, float, dict[str, float]], ...] = (
    ("longcat-2.0", "LongCat-2.0", 60, {"input": 0.30, "output": 1.20}),
    ("glm-5.2", "GLM-5.2", 60, {"input": 1.40, "output": 4.40}),
    ("glm-5.3", "GLM-5.3", 15, {"input": 1.40, "output": 4.40}),
    ("kimi-k3.1-pro", "Kimi K3.1 Pro", 15, {"input": 2.00, "output": 10.00}),
    ("mimo-v2.6-pro", "MiMo-V2.6-Pro", 15, {"input": 0.435, "output": 0.87}),
)
# ---- OpenCode Go WORKER candidates of owner decision R31 (2026-10-28, ADR-0125; E1 reading, not exercised live) ----
# Allowance and data terms: https://opencode.ai/go (read 2026-10-28: $15/month each, not used for training, 0-day
# retention); API family: models.dev providers/opencode-go (chat-completions, ``tool_call`` true). models.dev's
# tool flag is a catalog claim, not proof: these routes stay supports_tools UNKNOWN until the owner's tool-call probe
# (``aeos broker probe-route --tools``) records a PASS for their configuration.
GO_R31_READ = "2026-10-28"
GO_R31_READ_AT = datetime(2026, 10, 28, tzinfo=UTC)
GO_R31_SOURCES = [
    "S07",
    f"https://opencode.ai/go (monthly allowance and privacy table, read {GO_R31_READ})",
    f"https://models.dev/api.json providers/opencode-go (chat-completions API family, tool_call, rates; read "
    f"{GO_R31_READ})",
    f"{GO_DOCS} (read {GO_R31_READ})",
]
GO_R31_TERMS: dict[str, Any] = {
    **GO_TERMS,
    "read_for_research": f"{GO_R31_READ} (opencode.ai/go privacy table reading for owner decision R31; not an owner "
                         "terms review)",
}
# (model, display, monthly allowance USD, allowance rate per 1M tokens per models.dev)
GO_WORKERS_R31: tuple[tuple[str, str, float, dict[str, float]], ...] = (
    ("kimi-k3", "Kimi K3", 15, {"input": 3.00, "output": 15.00, "cached_read": 0.30}),
    ("step-5-preview", "Step 5 Preview", 15, {"input": 1.00, "output": 2.70, "cached_read": 0.10}),
)
# ---- OpenCode Go full registry of owner decision R34 (2026-10-28, ADR-0129; E1 reading, not exercised live) ----
# Every remaining Go model the owner wants used. Allowance and data terms: https://opencode.ai/go (read 2026-10-28,
# model table and privacy table: "Not used" for training, "0 days" retention for every model below); rates per 1M
# tokens: models.dev providers/opencode-go. All are WORKER CANDIDATEs with tools UNKNOWN until the owner's probes pass.
GO_R34_SOURCES = list(GO_R31_SOURCES)
GO_R34_TERMS: dict[str, Any] = {
    **GO_TERMS,
    "read_for_research": f"{GO_R31_READ} (opencode.ai/go privacy table reading for owner decision R34; not an owner "
                         "terms review)",
}
GO_R34_DEEPSEEK_TERMS: dict[str, Any] = {**GO_DEEPSEEK_TERMS, "read_for_research": GO_R34_TERMS["read_for_research"]}
# Go serves these on the google / anthropic API families (models.dev); the Go docs state that every text model also
# works through the other APIs, which the Console translates. The broker speaks chat-completions only, so they are
# seeded on the chat-completions endpoint and stay unproven until both probes pass for their configuration.
GO_TRANSLATION_STATEMENT = ("served through OpenCode Console API translation - NOT PROVEN until the plain and --tools "
                            "probes pass")
# models.dev providers/opencode-go models/<id>.provider: the native endpoint of each translation candidate's family
GO_NATIVE_API: dict[str, str] = {
    "google": "https://opencode.ai/inference/go/google/v1beta",
    "anthropic": "https://opencode.ai/inference/go/anthropic/v1",
}
# (model, display, monthly allowance USD, allowance rate per 1M tokens per models.dev)
GO_DIRECT_R34: tuple[tuple[str, str, float, dict[str, float]], ...] = (
    ("hy4-preview", "Hy4 preview", 30, {"input": 0.834, "output": 2.501, "cached_read": 0.042}),
)
GO_DEEPSEEK_PRO: tuple[str, str, float, dict[str, float]] = (
    "deepseek-v4-pro", "DeepSeek V4 Pro", 15, {"input": 0.66, "output": 1.98, "cached_read": 0.022})
# Go docs (GO_DOCS, read 2026-10-28): the DeepSeek models have peak rates in the same peak hours; the off-peak rates
# above are the allowance estimate, like its sibling deepseek-v4.1-flash (the peak rates are stated, not estimated)
GO_PEAK_HOURS = "01:00-04:00 and 06:00-10:00 UTC, Mon-Fri"
GO_DEEPSEEK_PRO_PEAK: dict[str, float] = {"input": 1.32, "output": 3.96, "cached_read": 0.044}
GO_DEEPSEEK_PRO_PEAK_SOURCE = (f"{GO_DOCS} (DeepSeek V4 Pro off-peak and peak rates per 1M tokens and peak hours, "
                               "read 2026-10-28)")
# (model, display, monthly allowance USD, rates per 1M tokens, the native API family models.dev states)
GO_TRANSLATION_R34: tuple[tuple[str, str, float, dict[str, float], str], ...] = (
    # the FULL rate: Go lists Argon at a temporary 50% promotion (GO_PROMOTIONS); the allowance estimate stays at the
    # full rate (conservative: it over-counts, never under-counts, the allowance used)
    ("gemini-4-argon", "Gemini 4 Argon", 30, {"input": 4.00, "output": 20.00, "cached_read": 0.20}, "google"),
    ("qwen3.8-max", "Qwen3.8 Max", 15, {"input": 2.00, "output": 6.00, "cached_read": 0.25}, "anthropic"),
    ("qwen3.8-flash", "Qwen3.8 Flash", 30, {"input": 0.15, "output": 0.47, "cached_read": 0.016}, "anthropic"),
    ("qwen3.7-plus", "Qwen3.7 Plus", 60, {"input": 0.40, "output": 1.60, "cached_read": 0.04}, "anthropic"),
    ("minimax-m3", "MiniMax M3", 60, {"input": 0.30, "output": 1.20, "cached_read": 0.06}, "anthropic"),
)
# models.dev states a higher rate above a context size for two of them (tiers[].tier.size)
GO_CONTEXT_TIERS: dict[str, dict[str, float]] = {
    "qwen3.7-plus": {"from_context_tokens": 256000, "input": 1.20, "output": 4.80, "cached_read": 0.12},
    "minimax-m3": {"from_context_tokens": 512000, "input": 0.60, "output": 2.40, "cached_read": 0.12},
}
# (model, display, allowance, rates, successor): superseded, own allowances, lowest priority of the pool (overflow only)
GO_SUPERSEDED_R34: tuple[tuple[str, str, float, dict[str, float], str], ...] = (
    ("mimo-v2.5", "MiMo-V2.5", 60, {"input": 0.14, "output": 0.28, "cached_read": 0.0028}, "mimo-v2.6-flash"),
    ("mimo-v2.5-pro", "MiMo-V2.5-Pro", 15, {"input": 0.435, "output": 0.87, "cached_read": 0.003625},
     "mimo-v2.6-pro"),
)
# temporary Go promotions: the promoted rate is recorded, the allowance is estimated at the full rate (conservative)
GO_PROMOTIONS: dict[str, dict[str, Any]] = {
    "gemini-4-argon": {
        "promotion_rate_per_mtok": {"input": 2.00, "output": 10.00, "cached_read": 0.10},
        "promotion": "temporary 50% off the full rate (Go docs: the 50% discount applies through November 30, 2026)",
        "promotion_ends": "2026-11-30",
        "promotion_source": f"{GO_DOCS} (Gemini 4 Argon rates, '50% off', read 2026-10-28)",
        "allowance_estimate": "FULL rate (allowance_rate_per_mtok), not the promoted rate: conservative while the "
                              "promotion runs",
    },
}
GO_PROMOTION_NOTES: dict[str, str] = {
    "gemini-4-argon": ("Go promotion 50% off (2.00/10.00/0.10 per 1M) through 2026-11-30 per the Go docs (read "
                       "2026-10-28); allowance estimated at the full rate 4.00/20.00/0.20 (conservative)"),
}
GO_R34_TRANSLATION_PRIORITY = 30  # after the Zen pilots (10, 20): unproven upstream shape, not a ranking
GO_R34_SUPERSEDED_PRIORITY = 990  # last of the WORKER pool
# §23 "mode/thinking params" of the native supervisor routes: the bridge passes no --effort/thinking flag, so the
# Claude Code default applies; stated explicitly so that a caller passing a level is a configuration change.
SUPERVISOR_MODE_PARAMS = {"effort": "CLI_DEFAULT", "thinking": "CLI_DEFAULT"}


def _zen_shape(model: str) -> tuple[str, str]:
    """(endpoint base, upstream API) S07 documents for *model*."""
    if model in ZEN_MESSAGES_MODELS:
        return ZEN_ANTHROPIC_BASE, "anthropic.messages"
    if model in ZEN_RESPONSES_MODELS:
        return ZEN_OPENAI_BASE, "openai.responses"
    return ZEN_OPENAI_BASE, UPSTREAM_API_CHAT


def _zen(model: str, display: str, *, pool: str, terms: dict[str, Any], classes: list[str], priority: int,
         identity: str = "PROVIDER_CLAIMED", notes: str = "", fallback: list[str] | None = None,
         paid: dict[str, Any] | None = None) -> dict[str, Any]:
    endpoint, api = _zen_shape(model)
    data_terms: dict[str, Any] = {**terms, "read_for_research": READ_FOR_RESEARCH}
    if not paid:
        data_terms["client_restriction"] = dict(ZEN_CLIENT_RESTRICTION)
    if model in ZEN_NOT_LISTED:
        notes = (notes + " " if notes else "") + (f"Not listed in the S07 model catalog read {SHAPE_READ} (E1): the next "
                                                  "`aeos routes refresh` marks it MISSING if `opencode models` agrees.")
    return {
        "route_key": f"opencode-zen/{model}", "pool": pool, "provider": "opencode-zen",
        "endpoint": endpoint, "model_id": f"opencode/{model}", "display_name": display,
        "harness": "opencode", "tool_set": list(WORKER_TOOL_SET), "is_free": "NO" if paid else "YES",
        "pricing": paid or ZEN_FREE_PRICING, "data_terms": data_terms,
        "eligible_domains": ["PERSONAL"],
        "allowed_data_classes": classes, "supports_tools": "YES" if pool in {"WORKER", "ARENA"} and not paid else "UNKNOWN",
        "identity_status": identity, "priority": priority, "fallback_chain": fallback or [],
        "expires_note": None if paid else "free for a limited time per S07 (no end date published)",
        "source_refs": ["S07", "S08"], "notes": notes, "pricing_checked_at": RESEARCH_DATE,
        "terms_checked_at": None,
        # model_id keeps the OpenCode catalog id; the API takes the bare id (S07) - both are part of the identity
        "mode_params": {"credential_ref": ZEN_CREDENTIAL_REF, "upstream_model": model, "upstream_api": api,
                        "reasoning": WORKER_REASONING},
        "prompt_revision": _worker_prompt_revision(),
    }


def _go_pricing(allowance: float, rates: dict[str, float], *, checked_at: str = GO_READ, **extra: Any) -> dict[str, Any]:
    """A Go subscription is not a per-token price: the rates only say how fast a request consumes the model's monthly
    dollar allowance (``usage.cost`` is 0 within the limits; part 2 of decision A gives the cost module these
    semantics). No ``input_per_mtok``/``output_per_mtok`` keys, so nothing reads them as a metered price."""
    return {"billing": "SUBSCRIPTION", "plan": "OpenCode Go", "monthly_fee_usd": 10, "currency": "USD",
            "allowance_usd_month": allowance, "allowance_windows": dict(GO_ALLOWANCE_WINDOWS),
            "allowance_rate_per_mtok": dict(rates), **extra,
            "basis": "OpenCode Go subscription ($10/month): per-model monthly dollar allowance consumed at the listed "
                     "rates (5h 20%, week 50%, month 100%); requests blocked at a limit while Extra Usage is OFF (Go "
                     "docs; HTTP 429 per OpenCode client test fixture, NOT-PROVEN)",
            "checked_at": checked_at}


def _go(model: str, display: str, *, pool: str, pricing: dict[str, Any], priority: int, tools: str = "UNKNOWN",
        classes: list[str] | None = None, terms: dict[str, Any] | None = None, notes: str = "",
        expires_note: str | None = None, source_refs: list[str] | None = None,
        pricing_checked_at: datetime = GO_READ_AT) -> dict[str, Any]:
    """An OpenCode Go route (ADR-0041 amendment 2026-10-26). Not ``_zen``: Go allows other clients, so no client
    restriction is attached, and its tool support is the documented one, not UNKNOWN for every paid route."""
    return {
        "route_key": f"opencode-go/{model}", "pool": pool, "provider": "opencode-go",
        # model_id keeps the OpenCode config id (``opencode-go/<id>``); the API takes the bare id (Go docs)
        "endpoint": GO_OPENAI_BASE, "model_id": f"opencode-go/{model}", "display_name": f"{display} (OpenCode Go)",
        "harness": "opencode", "tool_set": list(WORKER_TOOL_SET), "is_free": "NO", "pricing": pricing,
        "data_terms": copy.deepcopy(terms or GO_TERMS), "eligible_domains": ["PERSONAL"],
        "allowed_data_classes": classes or [P, I], "supports_tools": tools, "identity_status": "PROVIDER_CLAIMED",
        "priority": priority, "fallback_chain": [],  # never a paid fallback (§42): Go routes carry no chain
        "expires_note": expires_note, "source_refs": list(source_refs or ["S07"]), "notes": notes,
        "pricing_checked_at": pricing_checked_at,
        "terms_checked_at": None,
        "mode_params": {"credential_ref": GO_CREDENTIAL_REF, "upstream_model": model, "upstream_api": UPSTREAM_API_CHAT,
                        "reasoning": WORKER_REASONING},
        "prompt_revision": _worker_prompt_revision(),
    }


def _go_routes() -> list[dict[str, Any]]:
    paid = ("Paid subscription route: inactive until the owner reviews the Go terms (incl. the ToS automation "
            "clause), the region and a BUDGET decision, and keeps Extra Usage OFF (§23, §42).")
    def worker_note(tools: str) -> str:
        tool_note = (f"Tool calling YES per {GO_TOOLS_SOURCE} (E1, not exercised on Go)." if tools == "YES" else
                     f"Tool calling UNKNOWN: {GO_TOOLS_SOURCE} states no tool capability for this model, so the route "
                     "is not eligible for tool-using tasks.")
        return ("OpenCode Go WORKER candidate (owner decision A, 2026-10-26): pilot priority above the "
                f"client-restricted Zen pilots, not a ranking. {tool_note} {paid}")

    out = [
        _go(model, display, pool="WORKER", pricing=_go_pricing(allowance, rates), priority=1 + i, tools=tools,
            notes=worker_note(tools))
        for i, (model, display, allowance, rates, tools) in enumerate(GO_WORKERS)
    ]
    r31_note = ("OpenCode Go WORKER candidate (owner decision R31, 2026-10-28, ADR-0125): priority after the earlier Go "
                f"WORKER pilots, not a ranking. Tool calling UNKNOWN: {GO_TOOLS_SOURCE.replace(GO_READ, GO_R31_READ)} "
                "states tool_call true (a catalog claim); the route serves tool-using tasks only after a PASS of "
                "`aeos broker probe-route --tools` for its configuration. " + paid)
    out += [
        _go(model, display, pool="WORKER", pricing=_go_pricing(allowance, rates, checked_at=GO_R31_READ),
            priority=1 + len(GO_WORKERS) + i, tools="UNKNOWN", terms=GO_R31_TERMS, notes=r31_note,
            source_refs=GO_R31_SOURCES, pricing_checked_at=GO_R31_READ_AT)
        for i, (model, display, allowance, rates) in enumerate(GO_WORKERS_R31)
    ]
    out += _go_r34_routes(paid, first_priority=1 + len(GO_WORKERS) + len(GO_WORKERS_R31))
    out += [
        _go(model, display, pool="WATCHLIST", pricing=_go_pricing(allowance, rates), priority=510 + i,
            notes=f"OpenCode Go chat-completions model on the watchlist (not a WORKER pilot). {paid}")
        for i, (model, display, allowance, rates) in enumerate(GO_WATCH)
    ]
    out.append(_go(
        "deepseek-v4.1-flash", "DeepSeek V4.1 Flash", pool="WATCHLIST", priority=510 + len(GO_WATCH),
        pricing=_go_pricing(80, {"input": 0.15, "output": 0.60}, peak_rate_per_mtok={"input": 0.30, "output": 1.20},
                            peak_hours=GO_PEAK_HOURS),
        terms=GO_DEEPSEEK_TERMS, classes=[P],
        expires_note="DeepSeek ZDR agreement valid through 2026-10-31 per the Go docs (renewed monthly)",
        notes="OpenCode Go watchlist: needs the Global region in the Go workspace (default EU & US excludes it) and "
              "its zero-retention agreement is valid only through 2026-10-31 (renewed monthly, re-check at any "
              f"activation); PUBLIC only until the owner reviews it. {paid}"))
    return out


def go_translation_sources(model: str, family: str) -> list[str]:
    """source_refs of a Console-translation candidate: they cite the native API family models.dev states and the Go
    docs' translation sentence, never a catalog listing on chat-completions (that serving is NOT PROVEN).
    GO_R34_SOURCES (chat-completions family) is for the models Go lists natively on chat-completions only."""
    return [
        "S07",
        f"https://opencode.ai/go (monthly allowance and privacy table, read {GO_R31_READ})",
        f"https://models.dev/api.json providers/opencode-go models/{model} (native {family} API family, "
        f"{GO_NATIVE_API[family]}; tool_call, rates; read {GO_R31_READ}) - not listed on chat-completions",
        f"{GO_DOCS} (every text model also works through the other APIs, which the Console translates; read "
        f"{GO_R31_READ}) - chat-completions serving {GO_TRANSLATION_STATEMENT}",
    ]


def _go_r34_routes(paid: str, *, first_priority: int) -> list[dict[str, Any]]:
    """The OpenCode Go WORKER candidates of owner decision R34 (2026-10-28, ADR-0129): the remaining chat-completions
    models (DeepSeek V4 Pro PUBLIC only, Hy4 preview, the superseded MiMo V2.5 pair last) and the five models Go serves
    on another API family, seeded on chat-completions as Console-translation candidates. New routes only: no key of an
    existing seeded route changes (tests/unit/test_routing_go_registry_probe.py pins every existing hash)."""
    tools = (f"Tool calling UNKNOWN: {GO_TOOLS_SOURCE.replace(GO_READ, GO_R31_READ)} states tool_call true (a catalog "
             "claim); the route serves tool-using tasks only after a PASS of `aeos broker probe-route --tools` for its "
             "configuration.")
    head = "OpenCode Go WORKER candidate (owner decision R34, 2026-10-28, ADR-0129)"

    def pricing(model: str, allowance: float, rates: dict[str, float]) -> dict[str, Any]:
        extra: dict[str, Any] = (
            {"context_tier_rate_per_mtok": dict(GO_CONTEXT_TIERS[model])} if model in GO_CONTEXT_TIERS else {})
        extra.update(copy.deepcopy(GO_PROMOTIONS.get(model, {})))
        return _go_pricing(allowance, rates, checked_at=GO_R31_READ, **extra)

    common: dict[str, Any] = {"pool": "WORKER", "tools": "UNKNOWN", "source_refs": GO_R34_SOURCES,
                              "pricing_checked_at": GO_R31_READ_AT}
    out: list[dict[str, Any]] = []
    model, display, allowance, rates = GO_DEEPSEEK_PRO
    out.append(_go(
        model, display, priority=first_priority, terms=GO_R34_DEEPSEEK_TERMS,
        pricing=pricing(model, allowance, rates) | {"peak_rate_per_mtok": dict(GO_DEEPSEEK_PRO_PEAK),
                                                    "peak_hours": GO_PEAK_HOURS,
                                                    "peak_source": GO_DEEPSEEK_PRO_PEAK_SOURCE},
        classes=[P], expires_note="DeepSeek ZDR agreement valid through 2026-10-31 per the Go docs (renewed monthly)",
        notes=f"{head}: PUBLIC only - the DeepSeek zero-retention agreement is renewed monthly (current one through "
              "2026-10-31) and the model needs the Global region of the Go workspace (default Europe & United States "
              f"excludes it). Priority after the earlier Go WORKER candidates, not a ranking. {tools} {paid}",
        **common))
    for i, (model, display, allowance, rates) in enumerate(GO_DIRECT_R34, start=1):
        out.append(_go(model, display, pricing=pricing(model, allowance, rates), priority=first_priority + i,
                       terms=GO_R34_TERMS,
                       notes=f"{head}: priority after the earlier Go WORKER candidates, not a ranking. {tools} {paid}",
                       **common))
    for i, (model, display, allowance, rates, family) in enumerate(GO_TRANSLATION_R34):
        terms = {**GO_R34_TERMS, "serving": GO_TRANSLATION_STATEMENT,
                 "native_api_family": f"{family} (models.dev providers/opencode-go, read {GO_R31_READ})"}
        out.append(_go(model, display, pricing=pricing(model, allowance, rates),
                       priority=GO_R34_TRANSLATION_PRIORITY + i, terms=terms,
                       expires_note=GO_PROMOTION_NOTES.get(model),
                       notes=f"{head}: {GO_TRANSLATION_STATEMENT}. Go serves it natively on the {family} API family; "
                             "the broker calls the chat-completions endpoint (no new broker code). Priority after the "
                             f"Zen pilots, not a ranking. {tools} {paid}",
                       **{**common, "source_refs": go_translation_sources(model, family)}))
    for i, (model, display, allowance, rates, successor) in enumerate(GO_SUPERSEDED_R34):
        out.append(_go(model, display, pricing=pricing(model, allowance, rates),
                       priority=GO_R34_SUPERSEDED_PRIORITY + i, terms=GO_R34_TERMS,
                       notes=f"{head}: superseded by {successor}; its own monthly allowance is used only as overflow "
                             f"(the lowest priority of the WORKER pool). {tools} {paid}",
                       **common))
    return out


def _worker_prompt_revision() -> str | None:
    """§23: OpenCode worker routes carry the worker prompt revision in their configuration identity."""
    try:
        from aeos.worker_runtime.facade import worker_prompt_revision

        return worker_prompt_revision()
    except (ImportError, ModuleNotFoundError):
        return None


def seed_routes() -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = [
        # ---- SUPERVISOR (native Claude Code, Max login — not routed to workers) ----
        {
            "route_key": "claude-native/fable", "pool": "SUPERVISOR", "provider": "claude-native", "endpoint": None,
            "model_id": "claude-fable-5-1", "alias": "fable", "display_name": "Claude Fable 5.1 (Max, native Claude Code)",
            "harness": "claude-code", "tool_set": ["Read", "Grep", "Glob"], "is_free": "NO",
            "pricing": {"basis": "SUBSCRIPTION (Claude Max) - never converted to dollars (S02, §42)"},
            "data_terms": {"basis": "owner's own consumer account terms (S04)", "office_allowed": False},
            "eligible_domains": ["PERSONAL"], "allowed_data_classes": [P, I, C], "supports_tools": "YES",
            "mode_params": dict(SUPERVISOR_MODE_PARAMS),
            "priority": 10, "source_refs": ["S01", "S02", "S03", "S05"],
            # ADR-0131 (owner decision 2026-10-28): never requested. The row stays as catalog history (deleting it
            # would lose its observations); seeding has no status, a SUPERVISOR route is never activated as a worker
            # route, and the bridge refuses the fable family, so the note (descriptive, not configuration) says so.
            "notes": "NOT USED: DLY AEOS never requests the Claude Fable family (owner decision 2026-10-28, ADR-0131); "
                     "the bridge refuses it (MODEL_FAMILY_REFUSED) and every planning/review stage uses Opus. History: "
                     "Fable weekly limit is part of the total Max weekly limit (S02). 2026-10-18 probe: `--model fable` "
                     "answered via claude-opus-4-8 (fable 0 output tokens, safety_stops=2) -> MODEL_POLICY_MISMATCH "
                     "(ADR-0040).",
            "pricing_checked_at": RESEARCH_DATE, "terms_checked_at": RESEARCH_DATE,
        },
        {
            "route_key": "claude-native/opus", "pool": "SUPERVISOR", "provider": "claude-native", "endpoint": None,
            "model_id": "claude-opus-5-5", "alias": "opus", "display_name": "Claude Opus family (Max, native Claude Code)",
            "harness": "claude-code", "tool_set": ["Read", "Grep", "Glob"], "is_free": "NO",
            "pricing": {"basis": "SUBSCRIPTION (Claude Max) - never converted to dollars (§42)"},
            "data_terms": {"basis": "owner's own consumer account terms (S04)", "office_allowed": False},
            "eligible_domains": ["PERSONAL"], "allowed_data_classes": [P, I, C], "supports_tools": "YES",
            "mode_params": dict(SUPERVISOR_MODE_PARAMS),
            "priority": 20, "source_refs": ["S01", "S03", "S05"],
            "notes": "Planning baseline: all four stages, and the checkpoint and proposal reviews (ADR-0131). The alias "
                     "resolves to the latest Opus; the resolved id is recorded by the bridge and an alias change is a "
                     "new configuration.",
            "pricing_checked_at": RESEARCH_DATE, "terms_checked_at": RESEARCH_DATE,
        },
        # ---- WORKER initial (pilot priority, not a ranking) ----
        _zen("deepseek-v4.1-flash-free", "DeepSeek V4.1 Flash Free", pool="WORKER", terms=ZERO_RETENTION,
             classes=[P, I], priority=10, fallback=["opencode-zen/glm-5.3-flash-free"],
             notes="Pilot priority (§23), not a champion. DeepSeek official Flash alias serves 1017 (S10); the version "
                   "behind the Zen free alias is UNKNOWN until observed."),
        _zen("glm-5.3-flash-free", "GLM-5.3 Flash Free", pool="WORKER", terms=ZERO_RETENTION, classes=[P, I],
             priority=20, fallback=["opencode-zen/deepseek-v4.1-flash-free"],
             notes="Pilot priority (§23); community experience S42 is anecdotal, not an evaluation."),
        # ---- WORKER and WATCHLIST, OpenCode Go (owner decision A): ranked above the client-restricted Zen pilots ----
        *_go_routes(),
        # ---- COMPARATOR ----
        {
            "route_key": "gemini/gemini-3.8-flash-free-tier", "pool": "COMPARATOR", "provider": "gemini",
            # the OpenAI-compatible base the Gemini API documents (read 2026-10-25, E1; not exercised live)
            "endpoint": GEMINI_OPENAI_BASE, "model_id": "gemini-3.8-flash",
            "display_name": "Gemini 3.8 Flash (Free Tier)", "harness": None, "is_free": "YES",
            "pricing": {"input_per_mtok": 0, "output_per_mtok": 0, "basis": "Free Tier (S09)"},
            "data_terms": {"retention": "per Gemini API terms (S09)",
                           "training": "free tier: content may be used to improve Google products (S09)",
                           "may_use_inputs_for_training": True, "trial_only_no_confidential": False,
                           "claim_level": "E1 terms reading", "read_for_research": READ_FOR_RESEARCH},
            "eligible_domains": ["PERSONAL"], "allowed_data_classes": [P], "supports_tools": "UNKNOWN",
            "priority": 10, "source_refs": ["S09"], "notes": "Comparator only; input limited by the free-tier data terms.",
            "pricing_checked_at": RESEARCH_DATE, "terms_checked_at": None,
            "mode_params": {"credential_ref": "workers/gemini", "upstream_api": UPSTREAM_API_CHAT},
        },
        # ---- ARENA (feasibility of route and data) ----
        _zen("longcat-2.5-preview-free", "LongCat 2.5 Preview Free", pool="ARENA", terms=ZERO_RETENTION,
             classes=[P, I], priority=100),
        _zen("mimo-v2.6-flash-free", "MiMo-V2.6 Flash Free", pool="ARENA", terms=MAY_TRAIN, classes=[P], priority=110,
             notes="Serving/tool issues discussed in S43 (community, not an evaluation)."),
        _zen("beam-free", "Beam Free", pool="ARENA", terms=BEAM_TERMS, classes=[P], priority=120),
        _zen("nemotron-3-ultra-free", "Nemotron 3 Ultra Free", pool="ARENA", terms=NVIDIA_TRIAL, classes=[P], priority=130),
        _zen("nemotron-3.5-lightning-free", "Nemotron 3.5 Lightning Free", pool="ARENA", terms=NVIDIA_TRIAL,
             classes=[P], priority=140),
        _zen("nemotron-3.5-super-vl-free", "Nemotron 3.5 Super VL Free", pool="ARENA", terms=NVIDIA_TRIAL,
             classes=[P], priority=150),
        {
            "route_key": "openrouter/poolside/laguna-s-2.1:free", "pool": "ARENA", "provider": "openrouter",
            "endpoint": "https://openrouter.ai/api/v1", "model_id": "poolside/laguna-s-2.1:free",
            "display_name": "Poolside Laguna S 2.1 (free)", "harness": "opencode", "is_free": "YES",
            # same shape the catalog refresh writes, so an unchanged catalog keeps the config hash
            "pricing": {"prompt": "0", "completion": "0", "basis": "OpenRouter catalog", "unit": "USD per token"},
            "data_terms": {"retention": "UNKNOWN", "training": "UNKNOWN", "may_use_inputs_for_training": None,
                           "note": "free-route data policy not reviewed"},
            "eligible_domains": ["PERSONAL"], "allowed_data_classes": [P], "supports_tools": "YES",
            "context_window": 262144, "priority": 160, "source_refs": ["S11", "S12"],
            "free_until": datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
            "expires_note": "free until 2026-10-31 per S11",
            "notes": "Time-limited free route; after the date it is treated as paid (budget decision required).",
            "pricing_checked_at": RESEARCH_DATE, "terms_checked_at": None,
            "prompt_revision": _worker_prompt_revision(),
            "mode_params": {"reasoning": WORKER_REASONING},
        },
        # ---- ARENA, identity not opened (never default, never sensitive data) ----
        _zen("space-bunny-free", "Space Bunny Free", pool="ARENA", terms=ZERO_RETENTION, classes=[P], priority=900,
             identity="UNVERIFIED", notes="Identity not opened: never default, never sensitive data (§23)."),
        _zen("ll-alpha-free", "LL Alpha Free", pool="ARENA", terms={**MAY_TRAIN, "retention": "30 days (S07)"},
             classes=[P], priority=910, identity="UNVERIFIED",
             notes="Identity not opened: never default, never sensitive data (§23)."),
        _zen("exo-free", "Exo Free", pool="ARENA", terms=MAY_TRAIN, classes=[P], priority=920, identity="UNVERIFIED",
             notes="Identity not opened: never default, never sensitive data (§23)."),
        _zen("big-pickle", "Big Pickle", pool="ARENA", terms=MAY_TRAIN, classes=[P], priority=930, identity="UNVERIFIED",
             notes="Identity not opened: never default, never sensitive data (§23)."),
    ]
    # ---- WATCHLIST (paid; inactive unless official access + approved budget) ----
    watch = [
        ("qwen3.8-flash", "Qwen3.8 Flash", {"input_per_mtok": 0.15, "output_per_mtok": 0.47}),
        ("kimi-k2.7-code", "Kimi K2.7 Code", {"input_per_mtok": 0.95, "output_per_mtok": 4.00}),
        ("minimax-m3", "MiniMax M3", {"input_per_mtok": 0.30, "output_per_mtok": 1.20}),
        ("mistral-large-4", "Mistral Large 4", {"input_per_mtok": 0.68, "output_per_mtok": 2.09}),
        ("gpt-5.3-codex", "GPT 5.3 Codex", {"input_per_mtok": 1.75, "output_per_mtok": 14.00}),
        ("grok-build-0.1", "Grok Build 0.1", {"input_per_mtok": 1.00, "output_per_mtok": 2.00}),
        ("muse-spark-1.3", "Muse Spark 1.3", {"input_per_mtok": 1.25, "output_per_mtok": 4.25}),
    ]
    for i, (model, display, price) in enumerate(watch):
        routes.append(_zen(
            model, display, pool="WATCHLIST",
            terms={"retention": "per S07 general statement", "training": "UNKNOWN", "may_use_inputs_for_training": None,
                   "claim_level": "E1"},
            classes=[P], priority=500 + i,
            paid={**price, "currency": "USD", "basis": "paid per token (S07)"},
            notes="Paid route: inactive unless official access and an approved budget decision exist (§23, §42).",
        ))
    # ---- LOCAL / open-weight ----
    routes.append({
        "route_key": "local/qwen3.8-flash-next", "pool": "LOCAL", "provider": "local", "endpoint": None,
        "model_id": "Qwen/Qwen3.8-Flash-Next", "display_name": "Qwen3.8-Flash-Next (open weights, local)",
        "harness": None, "is_free": "UNKNOWN",
        "pricing": {"basis": "local compute cost UNKNOWN; no hosted free endpoint assumed (§23)"},
        "data_terms": {"retention": "local only if served on this host", "may_use_inputs_for_training": False,
                       "license": "per official repository (S60) - not yet reviewed"},
        "eligible_domains": ["PERSONAL"], "allowed_data_classes": [P, I], "supports_tools": "UNKNOWN",
        "priority": 300, "source_refs": ["S60"],
        "notes": "Model/quantization/inference backend/hardware form a separate configuration; GGUF/quantized "
                 "variants never inherit benchmark results.",
        "pricing_checked_at": None, "terms_checked_at": None,
        # part of the identity: a quantized/other-backend build is another configuration (§23)
        "mode_params": dict(LOCAL_BUILD_PARAMS),
    })
    return routes


def invariant6_problems(specs: list[dict[str, Any]]) -> list[str]:
    """AGENTS.md invariant 6 over seed specs (ADR-0129): a Claude-family model off the native bridge or a meta-router
    is never seeded. One finding per refused route, from the shared ``aeos.core.model_policy`` check."""
    out: list[str] = []
    for spec in specs:
        mode = spec.get("mode_params") or {}
        why = invariant6_refusal(spec.get("route_key"), spec.get("model_id"), mode.get("upstream_model"),
                                 provider=spec.get("provider"))
        if why:
            out.append(f"route {spec.get('route_key')}: {why}")
    return out


_NON_CONFIG_UPDATABLE = ("display_name", "notes", "source_refs", "expires_note")
# mode_params keys a seed spec states and an untouched CANDIDATE row gets backfilled when it predates them
_BACKFILLED_MODE_PARAMS = ("credential_ref", "upstream_model", "upstream_api", "reasoning", *LOCAL_BUILD_PARAMS)


def _correct_request_shape(row: ModelRoute, spec: dict[str, Any], mode_params: dict[str, Any]) -> bool:
    """ADR-0041 amendment 2026-10-25 on an untouched CANDIDATE seed row: a superseded seed endpoint becomes the documented
    one, missing request-shape keys and the provider client restriction are stated. Returns True when changed."""
    changed = False
    endpoint = (row.endpoint or "").rstrip("/")
    if endpoint in SUPERSEDED_ENDPOINTS and spec.get("endpoint") and row.endpoint != spec["endpoint"]:
        row.endpoint = spec["endpoint"]
        changed = True
    missing = {k: v for k, v in mode_params.items() if k in _BACKFILLED_MODE_PARAMS and k not in (row.mode_params or {})}
    if missing:
        row.mode_params = {**(row.mode_params or {}), **missing}
        changed = True
    restriction = (spec.get("data_terms") or {}).get("client_restriction")
    if restriction and "client_restriction" not in (row.data_terms or {}):
        row.data_terms = {**(row.data_terms or {}), "client_restriction": dict(restriction)}
        changed = True
    return changed


def run(session: Session) -> dict[str, Any]:
    """Idempotent: creates missing routes; refreshes only descriptive fields of untouched seed rows.

    Integration amendment (ADR-0041): an untouched CANDIDATE seed row that predates the explicit
    ``mode_params.credential_ref`` gets it backfilled. That is a configuration change, but a CANDIDATE
    row carries no approval, so nothing is invalidated; the config hash is recomputed. The same applies to
    the C5 amendment: the placeholder tool set becomes the reviewable candidate tool set, the local build
    parameters are stated, and a seeded research date is no longer kept as a terms check - and to the
    2026-10-25 amendment: the superseded Zen/Gemini endpoints become the documented ones, the bare upstream model
    id / API / reasoning marker are stated and the S07 client restriction is recorded. A TRIAL/APPROVED row is never
    rewritten: rewriting it would silently void the owner's decision. It is listed in ``request_shape_pending`` and
    the broker refuses its superseded endpoint (``ROUTE_ENDPOINT_SUPERSEDED``). The owner's way out: ``aeos routes
    disable <key>`` (which invalidates its decisions), then ``aeos seed`` - a DISABLED seed row on a superseded
    endpoint gets only the request-shape correction (nothing active to void; it stays DISABLED) - then activate the
    corrected configuration."""
    created = updated = existing = backfilled = terms_reset = 0
    corrected: list[str] = []
    pending: list[str] = []
    specs = seed_routes()
    refused = invariant6_problems(specs)
    if refused:
        raise PolicyDenied("seed refused: " + "; ".join(refused), code="MODEL_FAMILY_REFUSED",
                           details={"refused": refused})
    for spec in specs:
        spec = dict(spec)
        mode_params = dict(spec.pop("mode_params", None) or {})
        row = session.scalars(select(ModelRoute).where(ModelRoute.route_key == spec["route_key"])).first()
        if row is None:
            row = ModelRoute(
                status="CANDIDATE", mode="DESIGN", origin="SEED", evidence_level="E1", availability="UNKNOWN",
                resolved_version=None, eval_results={}, mode_params=mode_params, **spec,
            )
            row.config_hash = compute_config_hash(row)
            session.add(row)
            created += 1
            continue
        existing += 1
        superseded = (row.endpoint or "").rstrip("/") in SUPERSEDED_ENDPOINTS
        if row.origin == "SEED" and row.status == "DISABLED" and superseded:
            # the owner's way out of request_shape_pending: a DISABLED row holds no active decision (disabling
            # invalidated them), so correcting its request shape voids nothing; it stays DISABLED until re-activated
            if _correct_request_shape(row, spec, mode_params):
                row.config_hash = compute_config_hash(row)
                corrected.append(row.route_key)
            continue
        if row.origin == "SEED" and row.status != "CANDIDATE" and superseded:
            pending.append(row.route_key)
        if row.origin == "SEED" and row.status == "CANDIDATE":
            changed = False
            # expires_note describes free_until: it is reset to the seed's note only while the stored end date is still the
            # seed's own (a catalog-set or catalog-cleared date keeps its own note, never a contradicting one)
            seed_dated = ensure_aware(row.free_until) == ensure_aware(spec.get("free_until"))
            for f in _NON_CONFIG_UPDATABLE:
                if f == "expires_note" and not seed_dated:
                    continue
                if spec.get(f) is not None and getattr(row, f) != spec[f]:
                    setattr(row, f, spec[f])
                    changed = True
            updated += int(changed)
            config_changed = False
            ref = mode_params.get("credential_ref")
            if ref and not (row.mode_params or {}).get("credential_ref"):
                row.mode_params = {**(row.mode_params or {}), "credential_ref": ref}
                config_changed = True
            if _correct_request_shape(row, spec, mode_params):
                corrected.append(row.route_key)
                config_changed = True
            if set(row.tool_set or []) == {"opencode-default"} and spec.get("tool_set"):
                row.tool_set = list(spec["tool_set"])
                config_changed = True
            if config_changed:
                row.config_hash = compute_config_hash(row)
                backfilled += 1
            rev = spec.get("prompt_revision")
            if rev and row.prompt_revision != rev:
                # A CANDIDATE row carries no approval: binding the current worker prompt revision invalidates nothing.
                row.prompt_revision = rev
                row.config_hash = compute_config_hash(row)
            seeded_check = row.terms_checked_at is not None and spec.get("terms_checked_at") is None \
                and row.terms_checked_at.date() == RESEARCH_DATE.date() and not (row.data_terms or {}).get("checked_by")
            if seeded_check:
                row.terms_checked_at = None  # not part of the identity: nothing to invalidate
                terms_reset += 1
    session.flush()
    return {"created": created, "existing": existing, "updated_descriptive": updated,
            "credential_ref_backfilled": backfilled, "seeded_terms_check_reset": terms_reset,
            "request_shape_corrected": corrected, "request_shape_pending": pending, "mode": "DESIGN",
            "approved": 0}
