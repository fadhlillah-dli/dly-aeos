"""§12 manual fallback: a package the owner runs in official Claude Code (never the Claude API).

Layout under ``DataLayout.manual_packages/<package_id>/``::

    PROMPT.md        prompt (refused when it contains a credential)
    schema.json      compact JSON Schema for --json-schema
    context/         read-only copies of the context files
    INSTRUCTIONS.md  exact commands for Windows PowerShell 5.1 / PowerShell 7 / macOS & Linux
    package.json     metadata + hashes (prompt/schema) used to detect tampering
    result.json      written by the owner: raw `claude -p --output-format json` output, or the
                     bare structured object from an interactive session

``read_manual_result`` validates ``result.json`` against the schema and returns a ClaudeResult with
mode MANUAL_FALLBACK. A bare object carries no model provenance, so its identity stays UNVERIFIED.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aeos.claude_bridge.facade import ClaudeRequest, ClaudeResult
from aeos.claude_bridge.parsing import (
    REFUSAL_BASIS,
    parse_output,
    redact_strings,
    refused_family,
    schema_errors,
)
from aeos.claude_bridge.parsing import cli_schema as _cli_schema
from aeos.claude_bridge.workspace import WorkspaceError, copy_context, remove_tree
from aeos.core.config import get_settings
from aeos.core.errors import AeosError, Conflict, NotFound, PolicyDenied, ValidationFailed
from aeos.core.hashing import content_hash, sha256_text
from aeos.core.paths import UnsafePathError, safe_segment
from aeos.core.timeutil import utcnow

RESULT_FILE = "result.json"
_FLAGS_TEMPLATE = (
    "--model {model} --restricted --tools \"{tools}\" --strict-mcp-config --permission-prompts none "
    "--no-session-persistence"
)


def package_dir(package_id: str) -> Path:
    try:
        return get_settings().layout.manual_packages / safe_segment(package_id)
    except UnsafePathError as exc:
        raise ValidationFailed(str(exc)) from exc


def _instructions(request: ClaudeRequest, pkg: Path) -> str:
    tools = ",".join(request.allowed_tools)
    flags = _FLAGS_TEMPLATE.format(model=request.model, tools=tools)
    if request.effort:
        flags += f" --effort {request.effort}"
    return f"""# Paket manual Claude Code resmi

Tujuan: `{request.purpose}` - peran `{request.role}` - model diminta `{request.model}`.
Domain: `{request.domain or 'platform'}`. Jangan pernah memakai `--bare` dan jangan memakai Claude API.

## Sebelum menjalankan

1. Buka terminal BARU di luar repository mana pun.
2. Pastikan login resmi: `claude auth status` harus menampilkan `"authMethod": "claude.ai"` dan
   `"apiProvider": "firstParty"`.
3. Pastikan tidak ada variabel API:
   * Windows PowerShell: `Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue`
   * macOS/Linux: `unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL`
4. Masuk ke folder paket ini:
   * Windows: `cd "{pkg}"`
   * macOS/Linux: `cd '{pkg.as_posix()}'`

## Opsi A - non-interaktif (disarankan)

Windows PowerShell 5.1 (bawaan Windows; tanda kutip JSON perlu di-escape):

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -Raw -Encoding UTF8 PROMPT.md | claude -p --output-format json --json-schema ((Get-Content -Raw schema.json) -replace '"','\\"') {flags} | Out-File -Encoding utf8 {RESULT_FILE}
```

PowerShell 7.3 atau lebih baru:

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -Raw -Encoding UTF8 PROMPT.md | claude -p --output-format json --json-schema (Get-Content -Raw schema.json) {flags} | Out-File -Encoding utf8 {RESULT_FILE}
```

macOS / Linux (bash/zsh):

```bash
claude -p --output-format json --json-schema "$(cat schema.json)" {flags} < PROMPT.md > {RESULT_FILE}
```

## Opsi B - interaktif

1. Jalankan `claude --model {request.model}` di folder ini (tanpa `--bare`).
2. Tempelkan isi `PROMPT.md`; berkas pendukung ada di `context/`.
3. Minta jawaban berupa SATU objek JSON yang sesuai `schema.json`.
4. Simpan objek JSON itu sebagai `{RESULT_FILE}` di folder ini.

Catatan: jawaban interaktif tidak membawa bukti model yang dipakai; DLY AEOS menandainya
identitas model UNVERIFIED.

## Setelah selesai

Hasil yang diharapkan: `{RESULT_FILE}` di folder ini. Kembali ke dashboard DLY AEOS dan impor hasil
paket `{pkg.name}`. Jangan mengubah `PROMPT.md` atau `schema.json` (hash diperiksa).
"""


_POLICY_CODES = {"OFFICE_POLICY_DENIED", "DOMAIN_MISMATCH", "CONTEXT_DOMAIN_DENIED"}


def _refuse(code: str, message: str) -> AeosError:
    if code in _POLICY_CODES:
        return PolicyDenied(message, code=code)
    if code == "PROJECT_NOT_FOUND":
        return NotFound(message, code=code)
    return ValidationFailed(message, code=code)


def write_manual_package(request: ClaudeRequest, *, package_id: str) -> Path:
    """Write a package for official Claude Code. The same preflight as the bridge applies (§23:
    the Office policy holds from the first planning/review call — a manual package would make the
    owner paste Office data into Claude Max just the same)."""
    from aeos.claude_bridge.bridge import preflight

    problem = preflight(request)
    if problem:
        raise _refuse(*problem)
    pkg = package_dir(package_id)
    if pkg.exists():
        raise Conflict(f"manual package {package_id} already exists")
    pkg.mkdir(parents=True)
    try:
        manifest = _fill_package(pkg, request)
    except BaseException:
        remove_tree(pkg)  # never leave a half-written package (or its context copies) behind
        raise
    meta = {
        "package_id": package_id, "purpose": request.purpose, "role": request.role, "model_requested": request.model,
        "project_id": request.project_id, "domain": request.domain, "created_at": utcnow().isoformat(),
        "prompt_sha256": sha256_text(request.prompt), "schema_hash": content_hash(_cli_schema(request.json_schema)),
        "allowed_tools": list(request.allowed_tools), "effort": request.effort, "context_files": manifest,
        "expected_result": RESULT_FILE, "mode": "MANUAL_FALLBACK",
    }
    (pkg / "package.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return pkg


def _fill_package(pkg: Path, request: ClaudeRequest) -> list[dict[str, Any]]:
    schema_json = json.dumps(_cli_schema(request.json_schema), separators=(",", ":"), ensure_ascii=False)
    (pkg / "PROMPT.md").write_text(request.prompt, encoding="utf-8")
    (pkg / "schema.json").write_text(schema_json, encoding="utf-8")
    try:
        manifest = copy_context(pkg, list(request.context_files), layout=get_settings().layout,
                                domain=request.domain)
    except WorkspaceError as exc:
        raise _refuse(exc.code, str(exc)) from exc
    (pkg / "INSTRUCTIONS.md").write_text(_instructions(request, pkg), encoding="utf-8")
    return manifest


def _load_meta(pkg: Path) -> dict[str, Any]:
    try:
        return json.loads((pkg / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotFound("manual package metadata missing or unreadable") from exc


def read_manual_result(package_id: str) -> ClaudeResult | None:
    pkg = package_dir(package_id)
    if not pkg.is_dir():
        raise NotFound(f"manual package {package_id} not found")
    meta = _load_meta(pkg)
    model = str(meta.get("model_requested") or "")

    def fail(code: str, error: str) -> ClaudeResult:
        return ClaudeResult(ok=False, structured=None, text="", model_requested=model, model_resolved=None,
                            engine_version=None, session_id=None, usage={}, duration_ms=None, error_code=code,
                            error=error, mode="MANUAL_FALLBACK")

    fam = refused_family(model)
    if fam:
        # ADR-0131: a package written for Fable (before the decision) is refused whether or not a result exists yet
        return fail("MODEL_FAMILY_REFUSED",
                    f"manual package {package_id} was written for {model!r} (Claude {fam} family), which DLY AEOS "
                    f"never uses ({REFUSAL_BASIS}); its result is not imported. Run the stage or review again: the new "
                    "package uses opus.")
    result_path = pkg / RESULT_FILE
    if not result_path.is_file():
        return None

    try:
        prompt = (pkg / "PROMPT.md").read_text(encoding="utf-8")
        schema = json.loads((pkg / "schema.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fail("PACKAGE_TAMPERED", "PROMPT.md or schema.json missing/unreadable")
    if sha256_text(prompt) != meta.get("prompt_sha256") or content_hash(schema) != meta.get("schema_hash"):
        return fail("PACKAGE_TAMPERED", "PROMPT.md or schema.json changed after the package was written")
    raw_bytes = result_path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-16"):  # Windows PowerShell may write UTF-16 when redirected with '>'
        try:
            text = raw_bytes.decode(enc)
            json.loads(text)
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = None
    if text is None:
        return fail("OUTPUT_UNPARSEABLE", f"{RESULT_FILE} is not valid JSON")
    data = json.loads(text)
    if isinstance(data, dict) and data.get("type") == "result":
        parsed = parse_output(text, "", returncode=0, schema=schema, model_requested=model)
        return ClaudeResult(
            ok=parsed.ok, structured=parsed.structured, text=parsed.text, model_requested=model,
            model_resolved=parsed.model_resolved, engine_version=None, session_id=parsed.session_id,
            usage=parsed.usage, duration_ms=parsed.duration_ms, error_code=parsed.error_code, error=parsed.error,
            quota_exhausted=parsed.quota_exhausted, mode="MANUAL_FALLBACK",
        )
    if not isinstance(data, dict):
        return fail("SCHEMA_INVALID", "result must be a JSON object")
    errors = schema_errors(schema, data)
    if errors:
        return fail("SCHEMA_INVALID", "; ".join(errors))
    return ClaudeResult(
        ok=True, structured=redact_strings(data), text="", model_requested=model, model_resolved="UNKNOWN",
        engine_version=None, session_id=None,
        usage={"model_identity": "UNVERIFIED", "note": "bare structured object; owner attests official Claude Code"},
        duration_ms=None, error_code=None,
        error="model identity UNVERIFIED (bare object without client provenance)", mode="MANUAL_FALLBACK",
    )
