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

    # §02/§08 canonical MCI_V2 source: owner-supplied and never committed (the platform repository is public). By
    # default it is read from <data dir>/policy/canonical/<file> (policy/mci-reference.yaml ``location:
    # local:<file>``); this absolute path (aeos.yaml ``mci_source_path`` / AEOS_MCI_SOURCE_PATH) overrides it. A
    # path inside the repository is refused when the reference is verified (aeos.policy.snapshot).
    mci_source_path: Path | None = None

    platform_repo_slug: str = "fadhlillah-dli/dly-aeos"
    # §06 + owner rule 2026-10-26 (AGENTS.md invariant 11): the commit identity per code host, keyed by provider -
    # ``{github: {name, email}, bitbucket: {name, email}}`` (the owner's GitHub identity and the company identity,
    # two different addresses). Only in the gitignored aeos.yaml / AEOS_COMMIT_IDENTITIES (JSON), never committed.
    # Without the identity of a project's code host, candidates are committed locally with the runtime identity,
    # marked not pushable, and the broker refuses the push (aeos.core.commit_identity).
    commit_identities: dict[str, Any] = Field(default_factory=dict)
    # Backward-compatible fallback for the github identity only (e.g. the noreply address
    # ``<id>+<user>@users.noreply.github.com``); ``commit_identities.github`` wins when both are set.
    commit_author_name: str | None = None
    commit_author_email: str | None = None

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # §06/§50: never auto-bind 0.0.0.0 or a public interface to make startup succeed.
        if v not in LOOPBACK_HOSTS:
            raise ValueError("AEOS V1 binds loopback only (127.0.0.1/::1/localhost)")
        return v

    @field_validator("data_dir")
    @classmethod
    def _data_dir_outside_repo(cls, v: Path) -> Path:
        # §06: runtime state never lives in the source tree, and its location comes from OS/profile
        # configuration - never from the process cwd (normally the repo) via a relative path.
        v = Path(v).expanduser()
        if not v.is_absolute():
            raise ValueError(f"data_dir must be an absolute path (got relative {str(v)!r}); a relative path "
                             "would resolve against the current directory, normally the repository")
        from aeos.core.paths import repo_root

        repo = repo_root().resolve()
        resolved = v.resolve()
        if (repo / ".git").exists() and (resolved == repo or repo in resolved.parents):
            raise ValueError(f"data_dir {resolved} is inside the source tree {repo}; runtime state must live "
                             "outside the repository (unset AEOS_DATA_DIR to use the OS default)")
        return v

    @field_validator("extra_allowed_origins")
    @classmethod
    def _loopback_origins_only(cls, v: list[str]) -> list[str]:
        # §06/§26: an explicit extra origin may only name a loopback http origin with a port.
        import re

        pattern = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})$")
        bad = [o for o in v if not pattern.match(o) or not 0 < int(pattern.match(o).group(2)) < 65536]  # type: ignore[union-attr]
        if bad:
            raise ValueError(f"extra_allowed_origins accepts only loopback http origins with a port "
                             f"(http://127.0.0.1:<port>, http://localhost:<port>, http://[::1]:<port>): {bad}")
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
        host = f"[{self.host}]" if ":" in self.host else self.host  # IPv6 literal (RFC 3986)
        return f"http://{host}:{self.port}"

    @property
    def allowed_origins(self) -> list[str]:
        hosts = ["127.0.0.1", "localhost", "[::1]"]
        origins = [f"http://{h}:{self.port}" for h in hosts]
        if self.dev_mode:
            origins += [f"http://{h}:{self.dev_frontend_port}" for h in hosts]
        return origins + list(self.extra_allowed_origins)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
