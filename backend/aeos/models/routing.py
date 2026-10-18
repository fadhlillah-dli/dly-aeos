"""ModelRoute / AIConfiguration registry and capability-broker grants (§13, §23, §25, §43)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import CreatedMixin, IdMixin, TimestampMixin


class ModelRoute(IdMixin, TimestampMixin, Base):
    __tablename__ = "model_routes"

    route_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    pool: Mapped[str] = mapped_column(String(32), nullable=False)  # SUPERVISOR|WORKER|COMPARATOR|ARENA|WATCHLIST|LOCAL
    provider: Mapped[str] = mapped_column(String(64), nullable=False)  # claude-native|openrouter|opencode-zen|gemini
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    resolved_version: Mapped[str | None] = mapped_column(String(160), nullable=True)  # UNKNOWN when alias hides it
    mode_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    harness: Mapped[str | None] = mapped_column(String(64), nullable=True)
    harness_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_set: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fallback_chain: Mapped[list[str]] = mapped_column(JSON, default=list)  # only already-approved routes
    is_free: Mapped[str] = mapped_column(String(8), default="UNKNOWN")  # YES|NO|UNKNOWN
    pricing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pricing_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    data_terms: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # retention/training/trial limits
    terms_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eligible_domains: Mapped[list[str]] = mapped_column(JSON, default=list)  # OFFICE empty until approved
    allowed_data_classes: Mapped[list[str]] = mapped_column(JSON, default=list)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supports_tools: Mapped[str] = mapped_column(String(8), default="UNKNOWN")
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE")  # CANDIDATE|UNTESTED|TRIAL|APPROVED|DISABLED
    eval_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_note: Mapped[str | None] = mapped_column(String(256), nullable=True)  # e.g. free until date
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="DESIGN")
    # --- routing module extensions (§23, §37, §38, §43) -------------------------------------------
    alias: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "fable" / "opus"
    priority: Mapped[int] = mapped_column(Integer, default=100)  # deterministic tie-break, lower first
    # VERIFIED (resolved identity observed by AEOS) | PROVIDER_CLAIMED (catalog says) | UNVERIFIED
    identity_status: Mapped[str] = mapped_column(String(24), default="PROVIDER_CLAIMED")
    evidence_level: Mapped[str] = mapped_column(String(4), default="E0")  # E1 = catalog/doc reading
    origin: Mapped[str] = mapped_column(String(24), default="SEED")  # SEED|CATALOG_DISCOVERED|OWNER
    availability: Mapped[str] = mapped_column(String(16), default="UNKNOWN")  # AVAILABLE|MISSING|UNKNOWN
    availability_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    free_until: Mapped[datetime | None] = mapped_column(nullable=True)  # after this, is_free is not trusted
    config_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)  # AIConfiguration identity
    config_changed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_observed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CapabilityGrant(IdMixin, CreatedMixin, Base):
    """§25/§26 scoped capability issued by the trusted broker to one run (token stored hashed)."""

    __tablename__ = "capability_grants"

    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # MODEL_PROXY|GIT_BRANCH|PR_CREATE|READ_CONTEXT
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # allowed routes/branches/paths
    max_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    used_requests: Mapped[int] = mapped_column(Integer, default=0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    used_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
