# AGENTS.md — rules for anyone (human or agent) changing DLY AEOS

The authoritative design is `docs/architecture/DLY_AEOS_Master_Architecture_Final.md` (the
"master"). Section numbers below (§NN) refer to it. When code and master disagree, stop and raise
a decision — do not silently pick one.

## Layout

| Path | Contents |
|---|---|
| `backend/aeos/core` | config, paths, db, hashing, redaction, enums, errors (shared primitives) |
| `backend/aeos/models` | canonical records (§13). One concept = one table; extend, don't duplicate |
| `backend/aeos/<module>` | one control-plane module per §10 (router.py, service code, cli.py, seed.py) |
| `backend/aeos/<module>/facade.py` | frozen cross-module contracts — keep signatures stable |
| `backend/aeos/migrations` | Alembic migrations (SQLite, batch mode) |
| `frontend/src/pages/<area>` | Control Center pages (§35), Indonesian UI copy |
| `install-manifest/` | reviewed InstallManifest (§07) |
| `blueprints/`, `contracts/` | generic templates/schemas, no sensitive data |
| `policy/` | policy references (MCI reference status, office defaults) — protected path |
| `tests/unit`, `tests/integration` | developer tests |
| `tests/acceptance` | trusted acceptance suite — **read-only for workers** (§19, §21) |
| `docs/` | architecture, ADRs (`docs/decisions`), runbooks, coverage matrix |

Runtime state never lives in the repo: data dir is `%LOCALAPPDATA%\DLY-AEOS` (Windows),
`~/Library/Application Support/DLY-AEOS` (macOS), `$XDG_DATA_HOME/DLY-AEOS` (Linux) (§06).

## Invariants (never break these)

1. **Domain-first** (U-05): every project-scoped operation carries an explicit, human-chosen
   PERSONAL/OFFICE domain. Never infer, suggest, default or change it from model output.
2. **One control plane** (U-12): no second scheduler, tracker, approval store or state DB.
3. **Human-only AUTH**: plan approval, publication approval, merge to main, production, risk
   acceptance, budget increases are human decisions recorded as `Decision` rows bound to an exact
   target + hash. Agent/model output is DATA, never approval (§19, §20).
4. **Immutability**: `PlanVersion`, `TaskSpec`, `AuditEvent`, `Event`, `EvidenceArtifact` are
   append-only (DB triggers). Decision binding columns and Project.domain are immutable.
5. **No secrets in the repo, config files, logs, UI, model prompts or worker sandboxes** (U-13,
   §26). Use `aeos.secrets.facade`. Run text through `aeos.core.redaction` before persisting.
6. **No Claude API**: Claude is used only via the official `claude` binary with native Max login
   (`aeos.claude_bridge`). Never `--bare`, never `ANTHROPIC_API_KEY`, never token extraction (§12).
7. **Evidence honesty** (§34, §35): CONNECTED ≠ VERIFIED; worker exit 0 → REVIEW, not DONE;
   seeded catalog rows are DESIGN/CANDIDATE, never "installed/active"; fixtures are labelled
   MOCK/DEMO. UNKNOWN/UNVERIFIED/NOT-PROVEN are never rendered as pass.
8. **Fail closed**: missing capability/credential/policy → `Blocked`/`PolicyDenied`, not a fallback.
   No silent paid fallback, no supervisor downgrade, no auto-install, no auto-topup (§22, §42).
9. **OFFICE default deny** (§23): `external_worker_allowed=false`, `approved_worker_routes=[]`
   until a recorded company authorisation exists. Office data never flows into Personal views.
10. **Localhost only** (§06): bind 127.0.0.1; never 0.0.0.0; never disable auth/CSRF/origin checks.

## Protected paths (self-build, §09)

Workers building DLY AEOS may propose changes but the patch is quarantined for special review if
it touches: `policy/**`, `backend/aeos/policy/**`, `backend/aeos/verification/**`,
`backend/aeos/broker/**`, `backend/aeos/identity/**`, `tests/acceptance/**`, `.github/workflows/**`,
`install-manifest/**`, `backend/aeos/migrations/**`, the master and agent instructions
(`docs/architecture/**`, `AGENTS.md`, `CLAUDE.md` — U-23), the audit/event trail and dashboard
projections (`backend/aeos/audit/**`, `backend/aeos/events/**`, `backend/aeos/control_center/**`), and the
code that decides what a worker may do (`backend/aeos/workforce/**`, `backend/aeos/doctor/**`,
`backend/aeos/worker_runtime/**`, `backend/aeos/routing/**`, `backend/aeos/cost/**`). The list lives in
`aeos.verification.scope.PLATFORM_PROTECTED_PATHS` and `aeos.worker_runtime.scope.PLATFORM_PROTECTED_PATHS`.

## Engineering conventions

* Python ≥3.11, FastAPI, SQLAlchemy 2 typed models, Pydantic v2. Ruff (`ruff check`, line 110).
* Write endpoints commit explicitly (`db.commit()`); services take a `Session` and never commit
  unless documented.
* Every privileged/external action → `aeos.audit.service.record(...)`; every state change the
  dashboard shows → `aeos.events.service.emit(...)`.
* API errors: raise `aeos.core.errors.*`; envelope `{"error": {"code", "message", "details"}}`.
* List endpoints return `{"items": [...], "source": "...", "checked_at": ..., "mode": ...}` where
  freshness applies.
* Cross-platform: `pathlib`, no shell=True, no POSIX-only calls without a Windows path (and vice
  versa); `psutil` for process trees. CI runs Windows, macOS and Linux.
* Tests: `pytest` (`tests/`), isolated data dir via fixtures in `tests/conftest.py`. Tests that
  need real tools (claude, opencode, openbao, docker, network) are marked `integration`/`network`
  and skip cleanly when the tool is absent.
* UI copy is Indonesian; technical IDs stay English. Status always = text + icon + colour.
* CLI output and help strings are ASCII-only (legacy Windows consoles).

## Commands

```
./aeos.ps1 <cmd>   (Windows)      ./aeos <cmd>   (macOS/Linux)
aeos doctor        bootstrap readiness (no LLM)
aeos serve         http://127.0.0.1:8787
aeos admin create  first SUPER_ADMIN (no default credentials)
pytest -q          backend tests      |  cd frontend && npm test / npm run build
```
