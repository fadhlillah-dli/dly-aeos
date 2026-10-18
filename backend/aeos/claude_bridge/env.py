"""Child-process environment for the official Claude Code binary (§12, U-13).

The child environment is built from an explicit ALLOW-list; everything else is dropped. A second
DENY check runs over the result so that a future allow-list edit can never leak a provider switch
or credential: ``ANTHROPIC_*`` (API key, auth token, base URL — S03: an API key takes priority over
the Max login and bills the API), ``CLAUDE_CODE_*`` (incl. Bedrock/Vertex/Foundry switches and the
OAuth-token variable), ``CLAUDE_AGENT_SDK_*``/``CLAUDECODE`` (this controller may itself run inside
a Claude Code session), cloud credentials, other model-provider keys and every ``AEOS_*`` variable.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping

# Variables the official binary needs to find the user's native login, temp dirs and the network.
ENV_ALLOWLIST: tuple[str, ...] = (
    # Windows
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "TEMP", "TMP",
    # macOS / Linux
    "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "__CF_USER_TEXT_ENCODING",
    # locale / terminal
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
    # Where the official client keeps its native login (a location, not a credential).
    "CLAUDE_CONFIG_DIR",
    # TLS trust for corporate networks (paths only).
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
)
# Proxy settings pass only when they carry no embedded credentials (user:pass@host).
PROXY_VARS: tuple[str, ...] = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")

FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "AWS_", "OPENROUTER_", "OPENAI_", "AEOS_",
    "BAO_", "VAULT_", "AZURE_OPENAI_", "GEMINI_", "GOOGLE_GENAI_", "VERTEX_", "LITELLM_",
)
FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "OPENCODE_API_KEY",
})
# Variables whose mere presence in the *server* environment deserves a visible warning: an
# interactive `claude` started from the same shell would bill the API / a third-party provider.
HYGIENE_WARN_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN",
)
_CRED_IN_URL = re.compile(r"//[^/@\s]+@")


def is_forbidden(name: str) -> bool:
    upper = name.upper()
    return name in FORBIDDEN_NAMES or upper in FORBIDDEN_NAMES or upper.startswith(FORBIDDEN_PREFIXES)


def child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the scrubbed environment for a Claude Code child process."""
    src = dict(os.environ if source is None else source)
    if sys.platform == "win32":
        # Windows env names are case-insensitive; normalise lookups.
        upper = {k.upper(): v for k, v in src.items()}
        env = {k: upper[k] for k in ENV_ALLOWLIST if k in upper}
    else:
        env = {k: src[k] for k in ENV_ALLOWLIST if k in src}
    for k in PROXY_VARS:
        v = src.get(k)
        if v and not _CRED_IN_URL.search(v):
            env[k] = v
    # Defence in depth: the allow-list must never let a forbidden variable through.
    for k in list(env):
        if is_forbidden(k):
            del env[k]
    return env


def assert_clean(env: Mapping[str, str]) -> None:
    leaked = sorted(k for k in env if is_forbidden(k))
    if leaked:
        raise RuntimeError(f"forbidden variables in Claude child environment: {leaked}")


def parent_hygiene(source: Mapping[str, str] | None = None) -> dict[str, object]:
    """Names (never values) of provider/credential switches present in the controller process."""
    src = os.environ if source is None else source
    names = {k.upper() for k in src}
    present = [n for n in HYGIENE_WARN_NAMES if n in names]
    nested = "CLAUDECODE" in names or any(n.startswith("CLAUDE_CODE_SESSION") for n in names)
    return {
        "status": "WARN" if present else "OK",
        "forbidden_present": present,
        "running_inside_claude_session": nested,
        "child_env_scrubbed": True,
        "note": (
            "Variabel ini dihapus dari proses anak bridge, tetapi `claude` interaktif dari shell yang sama "
            "akan memakai API/penyedia lain alih-alih login Max. Hapus dari profil shell."
            if present else "Tidak ada variabel API/penyedia Claude di lingkungan server."
        ),
    }
