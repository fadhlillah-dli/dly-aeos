"""Identity & Authorization records (§10, §26)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.enums import Role
from aeos.core.timeutil import utcnow
from aeos.models.base import IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default=Role.VIEWER.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Optional scoping: None = all projects the role allows; list restricts (§20 SUPER_ADMIN is not
    # automatically AUTH over every office resource).
    project_scope: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    domain_scope: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # §26 recovery: the temporary credential is single-use and time-limited. Set by `aeos admin recover`,
    # cleared by a successful password change.
    temp_password_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    temp_password_used_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AuthSession(IdMixin, Base):
    """Server-side opaque session; only a hash of the token is stored (§26)."""

    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(256), default="")
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    # Selected working context is part of the session, never inferred (§05 step 2-3).
    selected_domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    selected_project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class LoginAttempt(IdMixin, Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_user_time", "username", "at"),)

    username: Mapped[str] = mapped_column(String(64), nullable=False)
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class SystemSetting(Base):
    """Small key/value store for bootstrap state and feature flags (never secrets)."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
