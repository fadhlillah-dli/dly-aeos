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
`~/Library/Application Support/DLY-AEOS` (macOS), `$XDG_DATA_HOME/DLY-AEOS` (Linux, not a tested
host) (§06).

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
11. **Owner identity on code hosts, no AI attribution** (owner rule 2026-10-26, ADR-0121; applies to DLY AEOS
    and to every project it creates or manages): every GitHub activity (commits as author AND committer, pushes, PRs,
    issues, comments) uses the owner's GitHub identity; every Bitbucket activity the owner's company identity (two
    different addresses, configured only in the gitignored `aeos.yaml` as `commit_identities`, never committed).
    No AI may appear on GitHub, Bitbucket or the company's Jira/Confluence: not as author, committer, co-author
    or contributor, and not in any attribution line (`Co-Authored-By:` naming an AI or a bot, "Generated
    with/by <AI tool>" footers, robot emoji, AI vendor noreply addresses, model/route/agent names). Provenance
    (run, agent, route, model) stays in DLY AEOS records. Every outbound text goes through
    `aeos.core.attribution` (refused, never silently stripped); the broker refuses any push whose commits do not
    match the configured identity of the provider (`aeos.core.commit_identity`). The repository is public:
    placeholders only (`you@example.com`), never a real address.

## Protected paths (self-build, §09)

Workers building DLY AEOS may propose changes but the patch is quarantined for special review if
it touches: `policy/**`, `backend/aeos/policy/**`, `backend/aeos/verification/**`,
`backend/aeos/broker/**`, `backend/aeos/identity/**`, `tests/acceptance/**`, `.github/workflows/**`,
`install-manifest/**`, `backend/aeos/migrations/**`, the trust-relevant controller code (router,
tool permissions/sandbox, capability gates, budgets, evaluator, admission, auth/CORS/config,
secrets, supervisor bridge): `backend/aeos/routing/**`, `backend/aeos/worker_runtime/**`,
`backend/aeos/doctor/**`, `backend/aeos/cost/**`, `backend/aeos/evaluation/**`,
`backend/aeos/workforce/**`, `backend/aeos/api/**`, `backend/aeos/core/**`,
`backend/aeos/secrets/**`, `backend/aeos/claude_bridge/**`, the audit/event trail and dashboard
projections (`backend/aeos/audit/**`, `backend/aeos/events/**`, `backend/aeos/control_center/**`), the
master and agent instructions (U-23) and prompts (§25): `docs/architecture/**`,
`backend/aeos/planning/prompts.py`, `AGENTS.md`, `CLAUDE.md`, and the bootstrap/dependency set:
`scripts/bootstrap.*`, `pyproject.toml`, `requirements.lock`.

`backend/aeos/evaluation/**` holds the held-out evaluation suites and gates (§16/§21 "workers never change
graders or held-out tests").

The canonical list lives in `backend/aeos/core/protected_paths.py` (compiler, runtime and verifier
import it; `tests/unit/test_protected_paths.py` keeps it equal to this section). For every managed
project the trusted pipeline/grader/policy/agent-config paths (`opencode.json`, `.opencode/**`,
`AGENTS.md`, `CLAUDE.md`, CI files) and the restricted areas (IaC, IAM, secrets, deploy targets) are
quarantined as well.

## Engineering conventions

* Python ≥3.11, FastAPI, SQLAlchemy 2 typed models, Pydantic v2. Ruff (`ruff check`, line 110).
* Write endpoints commit explicitly (`db.commit()`); services take a `Session` and never commit
  unless documented.
* Every privileged/external action → `aeos.audit.service.record(...)`; every state change the
  dashboard shows → `aeos.events.service.emit(...)`.
* API errors: raise `aeos.core.errors.*`; envelope `{"error": {"code", "message", "details"}}`.
* List endpoints return `{"items": [...], "source": "...", "checked_at": ..., "mode": ...}`; claim
  objects (status/summary) carry the same `source` + `checked_at`. When nothing was ever checked,
  `checked_at` is null and `generated_at` states when the store was read. Every `/api` response carries
  `X-AEOS-Version`, `X-AEOS-Schema-Version` and `X-AEOS-Settings-Hash` (version/config of the claim);
  evidence stays per claim (`evidence_ids`/`evidence_level` on the record, never a page-wide level).
  `tests/unit/test_api_envelope_contract.py` enforces this.
* Cross-platform: `pathlib`, no shell=True, no POSIX-only calls without a Windows path (and vice
  versa); `psutil` for process trees. Host platforms are Windows, macOS arm64 and macOS Intel
  (ADR-0115): CI runs the backend suite and the bootstrap on `windows-latest`, `macos-latest` (arm64)
  and `macos-26-intel`, and mypy for `--platform darwin` and `--platform win32`. `ubuntu-latest`
  runners are used only for the platform-neutral frontend, browser e2e and supply-chain jobs. Linux
  host support is NOT-PROVEN: its code paths stay but no CI job runs them.
* Tests: `pytest` (`tests/`), isolated data dir via fixtures in `tests/conftest.py`. Tests that
  need real tools (claude, opencode, openbao, docker, network) are marked `integration`/`network`
  and skip cleanly when the tool is absent.
* UI copy is Indonesian; technical IDs stay English. Status always = text + icon + colour.
* CLI output and help strings are ASCII-only (legacy Windows consoles).

## Commands

```
./aeos.ps1 <cmd>   (Windows PowerShell)   aeos.cmd <cmd>   (Command Prompt)   ./aeos <cmd>   (macOS)
aeos doctor        bootstrap readiness (no LLM)
aeos serve         http://127.0.0.1:8787
aeos admin create  first SUPER_ADMIN (no default credentials)
pytest -q          backend tests      |  cd frontend && npm test / npm run build
```
