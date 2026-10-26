"""Print the status of DLY AEOS components in bootstrap order (master §07), without installing anything.

Used by scripts/bootstrap.ps1 and scripts/bootstrap.sh after the virtual environment exists. Standard
library only, ASCII output, read-only probes with timeouts. Missing tools are reported with the
official install hint for the owner to run - this script NEVER installs system software.

  python scripts/component_status.py            # human-readable
  python scripts/component_status.py --json     # machine-readable
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
OPENCODE_DEFAULT_PIN = "1.19.3"  # worker runtime default (aeos.worker_runtime.opencode.PINNED_VERSION)


def manifest_pin(component_id: str, root: Path = ROOT) -> str | None:
    """The owner-approved ``approved_pin`` of *component_id* in the reviewed install-manifest (None when unset).
    Standard library only: the component block is located textually (read-only, never guessed)."""
    try:
        text = (root / "install-manifest" / "manifest.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    marker = f"- component_id: {component_id}\n"
    start = text.find(marker)
    if start < 0:
        return None
    end = text.find("- component_id:", start + len(marker))
    block = text[start:end if end > 0 else len(text)]
    for line in block.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "approved_pin":
            value = value.split("#", 1)[0].strip().strip("'\"")
            return value.lstrip("v") or None if value.lower() not in ("", "null", "~") else None
    return None


# Section 07: the reviewed install-manifest pin wins; the runtime default is used (and labelled) only without one.
OPENCODE_PIN = manifest_pin("opencode") or OPENCODE_DEFAULT_PIN
OPENCODE_PIN_SOURCE = "install-manifest approved_pin" if manifest_pin("opencode") else "worker runtime default"

HINTS: dict[str, dict[str, str]] = {
    "git": {"win": "winget install --id Git.Git -e", "mac": "xcode-select --install  (or: brew install git)",
            "linux": "use your distribution package manager (e.g. apt install git)"},
    "code": {"win": "winget install --id Microsoft.VisualStudioCode -e", "mac": "brew install --cask visual-studio-code",
             "linux": "https://code.visualstudio.com/download"},
    "node": {"win": "winget install --id OpenJS.NodeJS.LTS -e", "mac": "brew install node@22",
             "linux": "https://nodejs.org/en/download (LTS)"},
    # Section 07: never pipe a remote install script into a shell; use a package manager or the official
    # setup page (the same rule the Readiness Doctor's claude.binary remedy follows).
    "claude": {"win": "winget install --id Anthropic.ClaudeCode -e",
               "mac": "brew install --cask claude-code   (docs: https://code.claude.com/docs/en/setup)",
               "linux": "official setup: https://code.claude.com/docs/en/setup (review the installer before running it)"},
    "bao": {"win": "aeos vault install   (official releases: https://openbao.org/downloads/)",
            "mac": "aeos vault install   (or: brew install openbao)",
            "linux": "aeos vault install   (official releases: https://openbao.org/downloads/)"},
    "docker": {"win": "optional: winget install --id Docker.DockerDesktop -e", "mac": "optional: brew install --cask docker",
               "linux": "optional: https://docs.docker.com/engine/install/"},
    "opencode": {"win": f"npm install -g opencode-ai@{OPENCODE_PIN}", "mac": f"npm install -g opencode-ai@{OPENCODE_PIN}",
                 "linux": f"npm install -g opencode-ai@{OPENCODE_PIN}"},
    "gh": {"win": "winget install --id GitHub.cli -e", "mac": "brew install gh", "linux": "https://cli.github.com/"},
}


def _os_key() -> str:
    return "win" if IS_WIN else "mac" if IS_MAC else "linux"


def _run(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    exe = shutil.which(cmd[0])
    if exe is None:
        return 127, ""
    try:
        proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout, check=False,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, type(exc).__name__
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def _first_line(text: str) -> str:
    return text.splitlines()[0][:120] if text else ""


def _tool(name: str, cmd: str, version_args: list[str], step: int, required: bool, purpose: str) -> dict[str, Any]:
    code, out = _run([cmd, *version_args])
    present = code != 127
    return {"step": step, "name": name, "command": cmd, "required": required, "purpose": purpose,
            "status": ("OK" if code == 0 else "ERROR") if present else "MISSING",
            "version": _first_line(out) if present and code == 0 else None,
            "install_hint": None if present else HINTS.get(cmd, {}).get(_os_key())}


def _claude() -> dict[str, Any]:
    item = _tool("Claude Code (supervisor, native Max login)", "claude", ["--version"], 2, False,
                 "planning/review via the official client; never the Claude API (§12)")
    warnings: list[str] = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        warnings.append("ANTHROPIC_API_KEY is set in this shell: Claude Code would bill the API key instead of "
                        "your Max subscription. Unset it (DLY AEOS never uses the Claude API).")
    if item["status"] == "OK":
        item["auth"] = claude_auth_summary()
        logged_in = item["auth"].get("loggedIn")
        if logged_in is None:
            warnings.append("auth status UNKNOWN (could not read `claude auth status`); run it yourself and "
                            "sign in with `claude` if needed")
        elif logged_in is not True:
            warnings.append("not logged in: run `claude` once and sign in with your Claude account (Max plan)")
        elif item["auth"].get("authMethod") not in ("claude.ai", None):
            warnings.append(f"auth method is {item['auth'].get('authMethod')!r}; DLY AEOS expects the native "
                            "claude.ai login")
    item["warnings"] = warnings
    return item


AUTH_FIELDS = ("loggedIn", "authMethod", "apiProvider", "subscriptionType")


def claude_auth_summary() -> dict[str, Any]:
    """Non-identifying fields of ``claude auth status`` (JSON). Email/org/account ids are never printed.

    Tries ``--json`` first and the plain command second (older clients print JSON by default);
    anything unreadable is ``loggedIn: None`` (UNKNOWN), never reported as logged in or out."""
    for args in (["claude", "auth", "status", "--json"], ["claude", "auth", "status"]):
        code, out = _run(args, timeout=20)
        if code not in (0, 1) or not out.lstrip().startswith("{"):
            continue
        try:
            data = json.loads(out)
        except ValueError:
            continue
        if isinstance(data, dict):
            return {k: data.get(k) for k in AUTH_FIELDS}
    return {"loggedIn": None, "note": "auth status could not be read"}


def _openbao() -> dict[str, Any]:
    item = _tool("OpenBao (runtime secret vault)", "bao", ["version"], 3, False,
                 "credential source of truth; secrets never live in .env/config (U-13)")
    addr = os.environ.get("AEOS_OPENBAO_ADDR", "http://127.0.0.1:8200").rstrip("/")
    url = f"{addr}/v1/sys/health?standbyok=true&sealedcode=200&uninitcode=200"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - fixed loopback health URL
            data = json.loads(resp.read().decode("utf-8") or "{}")
        item["server"] = ("sealed" if data.get("sealed") else "uninitialized" if not data.get("initialized")
                          else "unsealed (CONNECTED, not verified)")
    except (urllib.error.URLError, OSError, ValueError):
        item["server"] = "not running"
    return item


def _docker() -> dict[str, Any]:
    item = _tool("Docker Desktop (optional container sandbox)", "docker", ["--version"], 4, False,
                 "stronger worker isolation; optional - process sandbox is used otherwise")
    if item["status"] == "OK":
        code, out = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        item["daemon"] = f"running ({_first_line(out)})" if code == 0 else "not running"
    return item


def _opencode() -> dict[str, Any]:
    item = _tool("OpenCode (first worker engine)", "opencode", ["--version"], 5, False,
                 f"bounded worker tasks; pin opencode-ai@{OPENCODE_PIN} ({OPENCODE_PIN_SOURCE}; see install-manifest/)")
    if item["status"] == "OK" and item.get("version") and OPENCODE_PIN not in str(item["version"]):
        item["warnings"] = [f"installed version differs from the reviewed pin {OPENCODE_PIN}; the doctor will "
                            "report drift until the pin is reviewed"]
    return item


def _gh() -> dict[str, Any]:
    item = _tool("GitHub CLI (PERSONAL repo/issues adapter helper)", "gh", ["--version"], 7, False,
                 "token for the PERSONAL GitHub adapter: gh auth token | aeos secrets set personal/github --stdin")
    if item["status"] == "OK":
        code, _ = _run(["gh", "auth", "status"], timeout=15)
        item["auth"] = "logged in" if code == 0 else "not logged in (run: gh auth login)"
    return item


def collect() -> list[dict[str, Any]]:
    items = [
        {"step": 0, "name": "Repository + data backup", "required": True, "status": "OK" if (ROOT / "AGENTS.md").is_file()
         else "ERROR", "purpose": "inspect AGENTS.md / master; bootstrap backs up existing data first"},
        _tool("Git", "git", ["--version"], 1, True, "source control"),
        _tool("VS Code", "code", ["--version"], 1, False, "workspace (U-01)"),
        {"step": 1, "name": "Python", "required": True, "status": "OK" if sys.version_info >= (3, 11) else "ERROR",
         "version": sys.version.split()[0], "purpose": "control plane runtime (>= 3.11)"},
        _tool("Node.js", "node", ["--version"], 1, True, "Control Center build (>= 20)"),
        _claude(),
        _openbao(),
        _docker(),
        _opencode(),
        {"step": 6, "name": "Model gateway (LiteLLM)", "required": False, "status": "NOT_REQUIRED",
         "purpose": "only if an approved API worker route needs it; not part of V1 bootstrap"},
        _gh(),
        {"step": 7, "name": "Google Drive/Docs (PERSONAL docs)", "required": False, "status": "MANUAL",
         "purpose": "OAuth Desktop client + consent: aeos publish google-connect --client-secret <file>"},
        {"step": 8, "name": "Control Center frontend build", "required": True,
         "status": "OK" if (ROOT / "frontend" / "dist" / "index.html").is_file() else "MISSING",
         "purpose": "dashboard served on http://127.0.0.1:8787", "install_hint": "cd frontend && npm ci && npm run build"},
        {"step": 9, "name": "DLY AEOS as first PERSONAL project", "required": False, "status": "MANUAL",
         "purpose": "registered by `aeos seed`; plan/approve one slice in the dashboard"},
        {"step": 10, "name": "OFFICE connectors", "required": False, "status": "NOT_CONFIGURED",
         "purpose": "default deny until a recorded company authorisation exists (§23)"},
    ]
    return items


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    items = collect()
    if "--json" in sys.argv:
        print(json.dumps({"platform": platform.platform(), "items": items}, indent=2))
        return 0
    print("Component status in bootstrap order (master section 07). Nothing is installed by this script.")
    for it in items:
        version = f" {it['version']}" if it.get("version") else ""
        print(f"  [{it['step']:>2}] {it['status']:<14} {it['name']}{version}")
        for key in ("auth", "server", "daemon"):
            if it.get(key) is not None:
                val = it[key]
                if isinstance(val, dict):
                    val = ", ".join(f"{k}={v}" for k, v in val.items())
                print(f"                      {key}: {val}")
        for w in it.get("warnings") or []:
            print(f"                      WARNING: {w}")
        if it.get("install_hint") and it["status"] in ("MISSING", "ERROR"):
            print(f"                      install: {it['install_hint']}")
    print("Detailed readiness: aeos doctor   |   dashboard: aeos serve -> http://127.0.0.1:8787")
    return 0


if __name__ == "__main__":
    sys.exit(main())
