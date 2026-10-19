"""Non-secret configuration (master §06, §26).

Settings come from (highest priority first): environment variables prefixed ``AEOS_``, an optional
YAML file (``AEOS_CONFIG`` or ``<config dir>/aeos.yaml``), then defaults. Secrets are NEVER read
from here — credentials live in OpenBao or the OS credential store (U-13, §26).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from aeos.core.paths import DataLayout, default_config_dir, default_data_dir

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _config_file_path() -> Path:
    env = os.environ.get("AEOS_CONFIG")
    return Path(env) if env else default_config_dir() / "aeos.yaml"


class _YamlSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = _config_file_path()
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"config file {path} must contain a mapping")
        forbidden = {k for k in data if any(s in k.lower() for s in ("password", "secret", "token", "api_key"))}
        if forbidden:
            raise ValueError(f"secrets are not allowed in config file {path}: {sorted(forbidden)}")
        return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEOS_", extra="ignore")

    environment: str = "local"
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: Path = Field(default_factory=default_data_dir)
    # Dev mode allows an explicit separate Vite origin (§06); production serves one origin.
    dev_mode: bool = False
    dev_frontend_port: int = 5173
    extra_allowed_origins: list[str] = Field(default_factory=list)

    session_ttl_hours: int = 12
    session_idle_minutes: int = 120
    login_max_failures: int = 5
    login_lockout_seconds: int = 300
    secure_cookies: bool = False  # http://127.0.0.1 has no TLS; cookies stay HttpOnly+SameSite=Strict

    max_upload_bytes: int = 25 * 1024 * 1024
    max_archive_members: int = 2000

    claude_binary: str | None = None
    opencode_binary: str | None = None
    openbao_addr: str = "http://127.0.0.1:8200"
    openbao_binary: str | None = None
    keyring_service: str = "dly-aeos"
    mmdc_binary: str | None = None  # optional mermaid-cli renderer (§29)
    confluence_projects_parent_id: str | None = None  # canonical CONFLUENCE_PROJECTS_PARENT_ID (§04)

    # §23 OFFICE default deny — overridable only by recorded owner/company decisions, not config.
    office_external_worker_allowed: bool = False

    platform_repo_slug: str = "fadhlillah-dli/dly-aeos"
    # §06 commit attribution of worker candidates: the owner's own identity (e.g. the GitHub noreply address
    # ``<id>+<user>@users.noreply.github.com``). Unset -> the runtime identity, reported as not linked.
    commit_author_name: str | None = None
    commit_author_email: str | None = None

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # §06/§50: never auto-bind 0.0.0.0 or a public interface to make startup succeed.
        if v not in LOOPBACK_HOSTS:
            raise ValueError("AEOS V1 binds loopback only (127.0.0.1/::1/localhost)")
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, _YamlSource(settings_cls))

    @property
    def layout(self) -> DataLayout:
        return DataLayout(self.data_dir)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.layout.db_path.as_posix()}"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def allowed_origins(self) -> list[str]:
        hosts = ["127.0.0.1", "localhost"]
        origins = [f"http://{h}:{self.port}" for h in hosts]
        if self.dev_mode:
            origins += [f"http://{h}:{self.dev_frontend_port}" for h in hosts]
        return origins + list(self.extra_allowed_origins)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
