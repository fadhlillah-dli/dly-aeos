"""Model registry seed from master §23 pools (DESIGN / CANDIDATE — never approved by seeding).

Catalog facts are E1 (documentation/catalog reading dated 2026-10-18): OpenCode Zen model ids and
data statements from S07; OpenRouter ids/context/expiry from the public catalog (S11); Gemini free
tier terms from S09; the local candidate from S60. Provider claims about retention/training are
not proof that project data routing is compliant (§23). Every worker route is PERSONAL-only at
most; OFFICE stays empty until a recorded company authorisation (``approved_worker_routes=[]``).
Seeding never activates a route, never claims installation and never fills ``resolved_version``
(UNKNOWN until the bridge/runtime observes it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.models.routing import ModelRoute
from aeos.routing.service import compute_config_hash

RESEARCH_DATE = datetime(2026, 10, 18, tzinfo=UTC)
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


def _zen(model: str, display: str, *, pool: str, terms: dict[str, Any], classes: list[str], priority: int,
         identity: str = "PROVIDER_CLAIMED", notes: str = "", fallback: list[str] | None = None,
         paid: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "route_key": f"opencode-zen/{model}", "pool": pool, "provider": "opencode-zen",
        "endpoint": "https://opencode.ai/zen", "model_id": f"opencode/{model}", "display_name": display,
        "harness": "opencode", "tool_set": ["opencode-default"], "is_free": "NO" if paid else "YES",
        "pricing": paid or ZEN_FREE_PRICING, "data_terms": terms, "eligible_domains": ["PERSONAL"],
        "allowed_data_classes": classes, "supports_tools": "YES" if pool in {"WORKER", "ARENA"} and not paid else "UNKNOWN",
        "identity_status": identity, "priority": priority, "fallback_chain": fallback or [],
        "expires_note": None if paid else "free for a limited time per S07 (no end date published)",
        "source_refs": ["S07", "S08"], "notes": notes, "pricing_checked_at": RESEARCH_DATE,
        "terms_checked_at": RESEARCH_DATE,
    }


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
            "priority": 10, "source_refs": ["S01", "S02", "S03", "S05"],
            "notes": "Fable weekly limit is part of the total Max weekly limit (S02); when exhausted the queue waits. "
                     "2026-10-18 probe: `--model fable` answered via claude-opus-4-8 (fable 0 output tokens, "
                     "safety_stops=2) -> MODEL_POLICY_MISMATCH (ADR-0040).",
            "pricing_checked_at": RESEARCH_DATE, "terms_checked_at": RESEARCH_DATE,
        },
        {
            "route_key": "claude-native/opus", "pool": "SUPERVISOR", "provider": "claude-native", "endpoint": None,
            "model_id": "claude-opus-5-5", "alias": "opus", "display_name": "Claude Opus family (Max, native Claude Code)",
            "harness": "claude-code", "tool_set": ["Read", "Grep", "Glob"], "is_free": "NO",
            "pricing": {"basis": "SUBSCRIPTION (Claude Max) - never converted to dollars (§42)"},
            "data_terms": {"basis": "owner's own consumer account terms (S04)", "office_allowed": False},
            "eligible_domains": ["PERSONAL"], "allowed_data_classes": [P, I, C], "supports_tools": "YES",
            "priority": 20, "source_refs": ["S01", "S03", "S05"],
            "notes": "Planning baseline primary/synthesis. The alias resolves to the latest Opus; the resolved id is "
                     "recorded by the bridge and an alias change is a new configuration.",
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
        # ---- COMPARATOR ----
        {
            "route_key": "gemini/gemini-3.8-flash-free-tier", "pool": "COMPARATOR", "provider": "gemini",
            "endpoint": "https://generativelanguage.googleapis.com", "model_id": "gemini-3.8-flash",
            "display_name": "Gemini 3.8 Flash (Free Tier)", "harness": None, "is_free": "YES",
            "pricing": {"input_per_mtok": 0, "output_per_mtok": 0, "basis": "Free Tier (S09)"},
            "data_terms": {"retention": "per Gemini API terms (S09)",
                           "training": "free tier: content may be used to improve Google products (S09)",
                           "may_use_inputs_for_training": True, "trial_only_no_confidential": False,
                           "claim_level": "E1 terms reading"},
            "eligible_domains": ["PERSONAL"], "allowed_data_classes": [P], "supports_tools": "UNKNOWN",
            "priority": 10, "source_refs": ["S09"], "notes": "Comparator only; input limited by the free-tier data terms.",
            "pricing_checked_at": RESEARCH_DATE, "terms_checked_at": RESEARCH_DATE,
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
    })
    return routes


_NON_CONFIG_UPDATABLE = ("display_name", "notes", "source_refs", "expires_note")


def run(session: Session) -> dict[str, Any]:
    """Idempotent: creates missing routes; refreshes only descriptive fields of untouched seed rows."""
    created = updated = existing = 0
    for spec in seed_routes():
        row = session.scalars(select(ModelRoute).where(ModelRoute.route_key == spec["route_key"])).first()
        if row is None:
            row = ModelRoute(
                status="CANDIDATE", mode="DESIGN", origin="SEED", evidence_level="E1", availability="UNKNOWN",
                resolved_version=None, eval_results={}, mode_params={}, **spec,
            )
            row.config_hash = compute_config_hash(row)
            session.add(row)
            created += 1
            continue
        existing += 1
        if row.origin == "SEED" and row.status == "CANDIDATE":
            changed = False
            for f in _NON_CONFIG_UPDATABLE:
                if spec.get(f) is not None and getattr(row, f) != spec[f]:
                    setattr(row, f, spec[f])
                    changed = True
            updated += int(changed)
    session.flush()
    return {"created": created, "existing": existing, "updated_descriptive": updated, "mode": "DESIGN",
            "approved": 0}
