"""OpenCode worker engine adapter (§11 "OpenCode sebagai engine pertama").

Verified against opencode-ai 1.19.3 on 2026-10-18 (scratch install + a local fake
OpenAI-compatible endpoint, no credentials):

* ``opencode run "<message>" --format json --pure --model <provider>/<model> --dir <dir> --file <f>``
  — the message must come BEFORE ``--file`` (``--file`` is an array option that swallows positionals).
* ``--format json`` prints one JSON object per line: ``step_start``, ``text`` (model narration),
  ``tool_use`` (``part.tool``, ``part.state.status`` completed|error, ``state.input``,
  ``state.metadata.filepath``), ``step_finish`` (``part.tokens``, ``part.cost``), and ``error``
  (``error.name``, ``error.data.statusCode``, ``isRetryable``). Non-retryable API errors exit 1.
* A custom provider ``@ai-sdk/openai-compatible`` with ``options.baseURL``/``options.apiKey`` sends
  ``Authorization: Bearer <apiKey>`` to ``<baseURL>/chat/completions`` with ``stream: true``.
* Denied permissions remove the tool from the model's tool list (bash/webfetch/task/skill gone);
  ``external_directory: deny`` makes a read outside ``--dir`` fail with a tool error.
* On HTTP 429 OpenCode keeps retrying with backoff and does not exit — the executor therefore
  enforces its own quota stop and timeout.

The worker's narration (``text`` events) is labelled summary and never counted as progress.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow

PINNED_VERSION = "1.19.3"
PROVIDER_ID = "aeos-broker"
RUN_MESSAGE = "Execute the AEOS TaskSpec in the attached file. Follow its scope and stop conditions exactly."
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_status_lock = threading.Lock()
_status_cache: dict[str, Any] = {"at": 0.0, "value": None}
STATUS_CACHE_SECONDS = 300
DEFAULT_CONTEXT_LIMIT = 65_536
DEFAULT_OUTPUT_LIMIT = 4_096

# Tools that are always allowed inside the worktree; everything else defaults to deny.
BASE_ALLOW = ("read", "glob", "grep", "list", "edit", "todowrite")
ALWAYS_DENY = ("websearch", "task", "skill", "lsp", "question", "external_directory", "doom_loop")


PIN_SOURCE_MANIFEST = "install-manifest approved_pin"
PIN_SOURCE_DEFAULT = "worker runtime default (no approved_pin for opencode in the install-manifest)"


def resolve_pin(session: Any = None) -> dict[str, str]:
    """THE OpenCode pin the runtime holds the engine to (§07 "pins approved by a human"): the owner-approved
    ``approved_pin`` of component ``opencode`` in the reviewed InstallManifest when set (the seeded
    ComponentReference with a *session*, else the reviewed manifest file), otherwise the built-in
    ``PINNED_VERSION`` default, labelled as such. Used by the image tag, the drift flag, the contract test and the
    image build hint, so an approved pin other than the default is what gets built, run and compared. When the
    reviewed manifest cannot be read the default is still the runtime's value, but its label says the approved pin
    is UNKNOWN (never "no approved_pin")."""
    problem: str | None = None
    try:
        from aeos.inventory import facade as inventory

        if session is not None:
            approved = inventory.approved_pin("opencode", session=session)
        else:
            state = inventory.manifest_pin("opencode")
            approved, problem = state["pin"], state["problem"]
    except Exception as exc:  # inventory unavailable: the labelled default, never a guessed pin
        approved, problem = None, f"inventory unavailable ({type(exc).__name__})"
    if approved:
        return {"version": approved, "source": PIN_SOURCE_MANIFEST}
    if problem:
        return {"version": PINNED_VERSION,
                "source": f"worker runtime default (install-manifest unreadable: {problem}; the approved pin is UNKNOWN)"}
    return {"version": PINNED_VERSION, "source": PIN_SOURCE_DEFAULT}


def pinned_version(session: Any = None) -> str:
    return resolve_pin(session)["version"]


def exposed_model_id(route_key: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]", "-", route_key).strip("-.")[:128] or "route"
    return out


def _native_from_shim(shim: Path) -> Path | None:
    base = shim.parent
    names = ["opencode.exe"] if sys.platform == "win32" else ["opencode"]
    for cand_dir in (base / ".." / "opencode-ai" / "bin", base / "node_modules" / "opencode-ai" / "bin"):
        for n in names:
            p = (cand_dir / n).resolve()
            if p.is_file():
                return p
    return None


def resolve_binary(configured: str | None = None) -> Path | None:
    """Native OpenCode executable. npm ``.cmd``/``.ps1`` shims are resolved to the native binary so
    no argument is ever interpreted by cmd.exe."""
    raw = configured or os.environ.get("AEOS_OPENCODE_BINARY") or shutil.which("opencode")
    if not raw:
        return None
    p = Path(raw)
    if p.suffix.lower() in {".cmd", ".bat", ".ps1"} or (sys.platform == "win32" and p.suffix == ""):
        return _native_from_shim(p)
    return p if p.is_file() else None


def _isolated_env(tmp: Path) -> dict[str, str]:
    from aeos.worker_runtime.processes import SandboxDirs, build_sandbox_env

    return build_sandbox_env(SandboxDirs.create(tmp), run_id="opencode-status-probe",
                             extra={"OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1"})


def opencode_status(configured: str | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """Binary path/version or MISSING. INSTALLED is not functional verification (§38)."""
    if configured is None:
        try:
            from aeos.core.config import get_settings

            configured = get_settings().opencode_binary
        except Exception:
            configured = None
    pin = resolve_pin()
    with _status_lock:
        cached = _status_cache["value"]
        if not refresh and cached is not None and cached.get("_cfg") == (configured, pin["version"]) \
                and time.monotonic() - _status_cache["at"] < STATUS_CACHE_SECONDS:
            return {k: v for k, v in cached.items() if k != "_cfg"}
    checked = utcnow().isoformat()
    binary = resolve_binary(configured)
    pinned = {"pinned_version": pin["version"], "pin_source": pin["source"]}
    if binary is None:
        value: dict[str, Any] = {
            "status": "MISSING", "binary": None, "version": None, **pinned,
            "checked_at": checked, "functional": "NOT_PROVEN",
            "evidence": "opencode not found (set AEOS_OPENCODE_BINARY or install the pinned version)",
        }
    else:
        with tempfile.TemporaryDirectory(prefix="aeos-oc-") as td:
            try:
                proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30,
                                      env=_isolated_env(Path(td)), cwd=td, check=False)
                version = (proc.stdout or "").strip().splitlines()[-1:] or [""]
                ok = proc.returncode == 0 and bool(version[0])
                value = {
                    "status": "INSTALLED" if ok else "FAILED", "binary": str(binary),
                    "version": version[0] if ok else None, **pinned,
                    "checked_at": checked, "functional": "NOT_PROVEN",
                    "drift": bool(ok and version[0].strip().lstrip("v") != pin["version"]),
                    "evidence": f"`opencode --version` exit {proc.returncode}",
                }
            except (OSError, subprocess.TimeoutExpired) as exc:
                value = {"status": "FAILED", "binary": str(binary), "version": None, **pinned,
                         "checked_at": checked, "functional": "NOT_PROVEN", "evidence": type(exc).__name__}
    with _status_lock:
        _status_cache.update(at=time.monotonic(), value={**value, "_cfg": (configured, pin["version"])})
    return value


def _bash_rules(tools_allowed: list[str], *, runner: str | None = None) -> str | dict[str, str]:
    """``bash`` stays denied unless the TaskSpec allows it; ``bash:<pattern>`` entries allow only
    matching commands (last matching rule wins in OpenCode, so the catch-all comes first). With a ``runner``
    (the AEOS shell runner, container sandbox, §42) every allowed command must be run THROUGH it:
    ``<runner> <pattern>`` instead of ``<pattern>``."""
    patterns = [t.split(":", 1)[1].strip() for t in tools_allowed if t.startswith("bash:") and t.split(":", 1)[1].strip()]
    if not patterns and "bash" in tools_allowed:
        if not runner:
            return "allow"
        patterns = ["*"]
    if not patterns:
        return "deny"
    rules: dict[str, str] = {"*": "deny"}
    for p in patterns:
        rules[f"{runner} {p}" if runner else p] = "allow"
    return rules


def _webfetch_rule(tools_allowed: list[str], egress_allowed: list[str]) -> str:
    # The schema of the pinned version only accepts an action for webfetch; URL scoping is the
    # broker/sandbox's job, so webfetch is allowed only when the TaskSpec allows it explicitly.
    return "allow" if ("webfetch" in tools_allowed and egress_allowed) else "deny"


def build_config(
    *, broker_base_url: str, token: str, model: str, tools_allowed: list[str], egress_allowed: list[str],
    max_output_tokens: int | None = None, context_limit: int | None = None, shell_runner: str | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """``shell_runner`` (additive, wave 7): granted shell commands are allowed only through the AEOS runner.
    ``read_only`` (INVESTIGATION runs, §19): only read/glob/grep/list are allowed - edit/write/patch, todowrite,
    bash and webfetch are denied whatever the TaskSpec asks."""
    if not _MODEL_ID.match(model):
        raise ValueError(f"unsafe model id {model!r}")
    model_entry: dict[str, Any] = {"name": model}
    if context_limit or max_output_tokens:
        # Verified on 1.19.3: a partial ``limit`` is rejected ("Missing key ...limit.context"), so
        # both keys are always written together.
        model_entry["limit"] = {"context": int(context_limit or DEFAULT_CONTEXT_LIMIT),
                                "output": int(max_output_tokens or DEFAULT_OUTPUT_LIMIT)}
    permission: dict[str, Any] = {"*": "deny"}
    if read_only:
        from aeos.worker_runtime.investigation import READ_ONLY_ALLOW

        for t in BASE_ALLOW:
            permission[t] = "allow" if t in READ_ONLY_ALLOW else "deny"
        permission["bash"] = "deny"
        permission["webfetch"] = "deny"
    else:
        for t in BASE_ALLOW:
            permission[t] = "allow"
        permission["bash"] = _bash_rules(tools_allowed, runner=shell_runner)
        permission["webfetch"] = _webfetch_rule(tools_allowed, egress_allowed)
    for t in ALWAYS_DENY:
        permission[t] = "deny"
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": f"{PROVIDER_ID}/{model}",
        "small_model": f"{PROVIDER_ID}/{model}",
        "enabled_providers": [PROVIDER_ID],
        "formatter": False,
        "lsp": False,
        "instructions": [],
        "plugin": [],
        "mcp": {},
        "provider": {
            PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "AEOS capability broker",
                "options": {"baseURL": broker_base_url.rstrip("/"), "apiKey": token},
                "models": {model: model_entry},
            }
        },
        "permission": permission,
    }


def write_config(config_dir: Path, cfg: dict[str, Any]) -> Path:
    path = config_dir / "opencode.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# Repository-level OpenCode configuration a worktree could carry. OpenCode merges project config over
# OPENCODE_CONFIG (documented precedence), so the runtime (1) delivers the security-relevant config inline
# via OPENCODE_CONFIG_CONTENT, the highest-precedence source, (2) disables project config loading, and
# (3) refuses to start when the worktree contains any of these (executor: WORKTREE_AGENT_CONFIG). (1)/(2)
# are NOT verified against the pinned binary on the build host (OpenCode absent); (3) is enforced by AEOS
# itself and the contract test (`aeos worker contract-test`) checks the effective tool set.
PROJECT_CONFIG_FILES = ("opencode.json", "opencode.jsonc", ".opencode")


def inline_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """The config without the capability token (the token stays only in the 0600 file)."""
    out = json.loads(json.dumps(cfg))
    for prov in (out.get("provider") or {}).values():
        if isinstance(prov, dict) and isinstance(prov.get("options"), dict):
            prov["options"].pop("apiKey", None)
    return out


def opencode_env(config_path: Path | str, *, inline: dict[str, Any] | None = None) -> dict[str, str]:
    env = {
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    }
    if inline is not None:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline, separators=(",", ":"))
    return env


def build_command(binary: Path | str, *, model: str, worktree: Path, task_file: Path) -> list[str]:
    if not _MODEL_ID.match(model):
        raise ValueError(f"unsafe model id {model!r}")
    return [str(binary), "run", RUN_MESSAGE, "--format", "json", "--pure", "--model", f"{PROVIDER_ID}/{model}",
            "--dir", str(worktree), "--file", str(task_file)]


def build_container_command(*, model: str, worktree: str, task_file: str) -> list[str]:
    """Arguments after the image name: the worker image's entrypoint is ``tini -- opencode``."""
    if not _MODEL_ID.match(model):
        raise ValueError(f"unsafe model id {model!r}")
    return ["run", RUN_MESSAGE, "--format", "json", "--pure", "--model", f"{PROVIDER_ID}/{model}",
            "--dir", worktree, "--file", task_file]


# ----------------------------------------------------------------------------- event parsing
def _obj(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """``parent[key]`` when it is a JSON object, else an empty one (engine events are untrusted input)."""
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


@dataclass
class ObservedAction:
    kind: str  # edit_file|read_file|search|run_command|fetch|tool_error|error|narration|step|unknown
    label: str
    tool: str | None = None
    path: str | None = None
    status: str | None = None
    detail: str = ""
    observed_at: str = ""
    is_narration: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "tool": self.tool, "path": self.path, "status": self.status,
                "detail": self.detail, "observed_at": self.observed_at,
                "source": "opencode --format json (observed tool event)" if not self.is_narration
                else "AI narration (summary, not evidence)"}


@dataclass
class EventParser:
    worktree: Path
    # Inside a container OpenCode sees the worktree at this POSIX path (e.g. /workspace).
    container_root: str | None = None
    touched: list[str] = field(default_factory=list)
    outside_attempts: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    worker_cost: float = 0.0
    steps: int = 0
    last_narration: str = ""
    events_seen: int = 0
    unparsable: int = 0

    def _rel(self, raw: str | None) -> tuple[str | None, bool]:
        if not raw:
            return None, False
        if self.container_root:
            croot = self.container_root.rstrip("/")
            norm = raw.replace("\\", "/")
            if norm == croot or norm.startswith(croot + "/"):
                raw = norm[len(croot):].lstrip("/") or "."
        is_abs = PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute()
        root = self.worktree.resolve()
        candidate = Path(raw) if is_abs else root / raw.replace("\\", "/")
        try:
            # Relative paths are resolved too: ``../../x`` from the worktree is an outside attempt.
            rel = candidate.resolve().relative_to(root)
            return (rel.as_posix() or "."), False
        except (ValueError, OSError):
            return raw, True

    def parse_line(self, line: str) -> ObservedAction | None:
        line = line.strip()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            self.unparsable += 1
            return None
        if not isinstance(ev, dict):
            self.unparsable += 1
            return None
        self.events_seen += 1
        ts = ev.get("timestamp")
        at = utcnow().isoformat() if not isinstance(ts, int | float) else \
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts / 1000)) + "Z"
        etype = ev.get("type")
        part = _obj(ev, "part")
        if etype == "tool_use":
            return self._tool(part, at)
        if etype == "text":
            text = redact_text(str(part.get("text", "")))[:500]
            self.last_narration = text
            return ObservedAction(kind="narration", label="Ringkasan AI (bukan bukti)", detail=text,
                                  observed_at=at, is_narration=True)
        if etype == "step_finish":
            self.steps += 1
            tokens = _obj(part, "tokens")
            self.tokens_input += int(tokens.get("input") or 0)
            self.tokens_output += int(tokens.get("output") or 0)
            try:
                self.worker_cost += float(part.get("cost") or 0)
            except (TypeError, ValueError):
                pass
            return ObservedAction(kind="step", label="Langkah model selesai", status=str(part.get("reason") or ""),
                                  observed_at=at)
        if etype == "step_start":
            return None
        if etype == "error":
            err = _obj(ev, "error")
            data = _obj(err, "data")
            info = {"name": err.get("name"), "status_code": data.get("statusCode"),
                    "retryable": data.get("isRetryable"), "message": redact_text(str(data.get("message", "")))[:300]}
            self.errors.append(info)
            return ObservedAction(kind="error", label="Error dari engine", status=str(info["status_code"] or ""),
                                  detail=f"{info['name']}: {info['message']}", observed_at=at)
        return ObservedAction(kind="unknown", label=f"Event {str(etype)[:40]}", observed_at=at)

    def _tool(self, part: dict[str, Any], at: str) -> ObservedAction:
        tool = str(part.get("tool") or "")
        state = _obj(part, "state")
        status = str(state.get("status") or "")
        inp = _obj(state, "input")
        meta = _obj(state, "metadata")
        raw_path = inp.get("filePath") or meta.get("filepath") or inp.get("path")
        path, outside = self._rel(raw_path if isinstance(raw_path, str) else None)
        if outside and path:
            self.outside_attempts.append(redact_text(path)[:300])
        if status == "error":
            return ObservedAction(kind="tool_error", label=f"Tool {tool} ditolak/gagal", tool=tool, path=path,
                                  status=status, detail=redact_text(str(state.get("error", "")))[:300], observed_at=at)
        if tool in ("write", "edit", "patch", "multiedit"):
            if path and not outside and status == "completed" and path not in self.touched:
                self.touched.append(path)
            return ObservedAction(kind="edit_file", label=f"Mengubah file {path}", tool=tool, path=path,
                                  status=status, observed_at=at)
        if tool == "read":
            return ObservedAction(kind="read_file", label=f"Membaca file {path}", tool=tool, path=path,
                                  status=status, observed_at=at)
        if tool in ("glob", "grep", "list"):
            pattern = inp.get("pattern") or inp.get("path") or ""
            return ObservedAction(kind="search", label=f"Mencari ({tool})", tool=tool, status=status,
                                  detail=redact_text(str(pattern))[:200], observed_at=at)
        if tool == "bash":
            cmd = redact_text(str(inp.get("command", "")))[:300]
            return ObservedAction(kind="run_command", label="Menjalankan perintah", tool=tool, status=status,
                                  detail=cmd, observed_at=at)
        if tool == "webfetch":
            return ObservedAction(kind="fetch", label="Mengambil URL", tool=tool, status=status,
                                  detail=redact_text(str(inp.get("url", "")))[:300], observed_at=at)
        return ObservedAction(kind="tool", label=f"Tool {tool}", tool=tool, status=status, observed_at=at)

    def summary(self) -> dict[str, Any]:
        return {"events": self.events_seen, "unparsable": self.unparsable, "steps": self.steps,
                "touched": list(self.touched), "outside_attempts": list(self.outside_attempts),
                "errors": list(self.errors), "worker_reported_tokens": {"input": self.tokens_input,
                                                                        "output": self.tokens_output},
                "worker_reported_cost": self.worker_cost, "last_narration": self.last_narration}
