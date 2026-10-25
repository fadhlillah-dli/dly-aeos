# DLY AEOS — DLY AI Engineering Operating System

DLY AEOS is a **local-first control plane for AI-assisted software engineering**. It runs on your
own machine (Windows or macOS, also Linux) from a clone of this repository, serves one dashboard on
`http://127.0.0.1:8787`, and keeps humans in charge:

* **You choose the domain first** — PERSONAL or OFFICE — before any prompt. The backend refuses
  domain-scoped work without that choice and no model can change it.
* **You approve concrete plan versions.** Approvals are `Decision` records bound to an exact
  target and content hash; agent output is data, never approval.
* **A deterministic controller enforces scope, access, cost and state.** Eligible low-cost worker
  models (via OpenCode) are designed to execute bounded tasks in sandboxes with short-lived
  capabilities — **NOT-PROVEN** on a real machine until OpenCode, a proven container sandbox and the
  broker/verifier pass the Readiness Doctor (`CAN_RUN_WORKER` stays BLOCKED until then, master §09).
* **Claude supervises through the official Claude Code client** with your native Max login — never
  the Claude API, never `--bare`, never `ANTHROPIC_API_KEY`.
* **Merge to `main` and production stay human-only.** Evidence decides acceptance: worker exit 0
  means REVIEW, not DONE; CONNECTED is not VERIFIED; UNKNOWN is never shown as a pass.

The first project DLY AEOS manages is **DLY AEOS itself** (self-build, master §09): it is registered
as a PERSONAL project by `aeos seed`, workers change a candidate workspace, and a human-merged,
built and promoted release replaces the trusted controller.

The authoritative design is the master architecture:
[docs/architecture/DLY_AEOS_Master_Architecture_Final.md](docs/architecture/DLY_AEOS_Master_Architecture_Final.md).
Repository rules for humans and agents: [AGENTS.md](AGENTS.md).

---

## Quick start

### Windows (PowerShell 5.1 or 7)

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.13 -e
winget install --id OpenJS.NodeJS.LTS -e
git clone https://github.com/fadhlillah-dli/dly-aeos.git
cd dly-aeos
.\scripts\bootstrap.ps1          # venv + pinned deps, backup, frontend build, db, doctor, component status
.\aeos.ps1 admin create          # first SUPER_ADMIN (no default credentials exist)
.\aeos.ps1 serve                 # http://127.0.0.1:8787
```

If script execution is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (once).
Full guide: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md).

### macOS (zsh/bash)

```bash
xcode-select --install           # git + compilers
brew install python@3.13 node@22
git clone https://github.com/fadhlillah-dli/dly-aeos.git
cd dly-aeos
./scripts/bootstrap.sh
./aeos admin create
./aeos serve                     # http://127.0.0.1:8787
```

If `./scripts/bootstrap.sh` says `Permission denied` (a ZIP download or copy lost the executable
bit), run `sh scripts/bootstrap.sh` instead: it re-runs itself under bash and restores the bit on
`./aeos` and `./scripts/dev.sh`.
Full guide: [docs/SETUP_MACOS.md](docs/SETUP_MACOS.md).

The bootstrap scripts **never install system software silently**. They print what is missing with
the official install command, back up any existing data before migrating, install the universal
`requirements.lock`, and finish with a status list of the optional components in bootstrap order
(Claude Code + login status, OpenBao, Docker, OpenCode, GitHub CLI).

---

## Daily use

| Task | Windows | macOS / Linux |
|---|---|---|
| Readiness check without server or LLM | `.\aeos.ps1 doctor` | `./aeos doctor` |
| Start the dashboard | `.\aeos.ps1 serve` | `./aeos serve` |
| Development (backend `--dev` + Vite) | `.\scripts\dev.ps1` | `./scripts/dev.sh` |
| Accounts (first admin, viewer, recovery) | `.\aeos.ps1 admin create` / `admin recover <user>` | `./aeos admin ...` |
| Load the design catalog + DLY AEOS project | `.\aeos.ps1 seed` | `./aeos seed` |
| Backup + restore test | `.\aeos.ps1 ops backup create` then `ops backup restore-test <id>` | `./aeos ops backup ...` |
| SLIs for a domain | `.\aeos.ps1 ops sli --domain PERSONAL` | `./aeos ops sli --domain PERSONAL` |
| Emergency stop | `.\aeos.ps1 ops emergency-stop --reason "..."` | `./aeos ops emergency-stop --reason "..."` |

In the dashboard: sign in → **choose PERSONAL or OFFICE** → pick the project (the first one is
**DLY AEOS**) → *Ringkasan* shows capability readiness, blockers, pending decisions, the approved
plan, cost/quota (UNKNOWN when unknown), health and the live activity feed. *Kesiapan (Doctor)*
explains every missing piece with its remedy; *Riwayat & Pemulihan* has the audit timeline, backups,
releases, exports and the emergency stop.

## Architecture in one page

```
VS Code + clone ──> aeos doctor (no LLM) ──> aeos serve on 127.0.0.1:8787 (login, CSRF, RBAC)
                                               │
   domain PERSONAL | OFFICE (human choice) ────┤
                                               ▼
 intake + context ─> planning (Claude Code native: Opus plan / Fable review) ─> human approval (hash-bound)
                                               ▼
 publisher (Google Docs / GitHub Issues; Confluence / Jira for OFFICE) ─> scheduler + model routing
                                               ▼
 OpenCode worker in sandbox ─> independent verifier ─> Claude checkpoint review ─> PR ─> human merge
                                               ▼
 operations: SLIs/alerts, backup + restore test, release promotion/rollback, runbooks, emergency stop
```

* One control plane: FastAPI + SQLite (WAL, append-only audit/event/evidence tables), React
  Control Center served from the same origin. Modules map to master §10 (`backend/aeos/<module>`).
* Secrets live in OpenBao (runtime vault) and the OS credential store — never in `.env`, config,
  logs, the UI, prompts or worker sandboxes.
* Runtime state lives outside the repository: `%LOCALAPPDATA%\DLY-AEOS` (Windows),
  `~/Library/Application Support/DLY-AEOS` (macOS), `$XDG_DATA_HOME/DLY-AEOS` (Linux).
* Decisions: [docs/decisions](docs/decisions) (ADRs). Operations: [docs/OPERATIONS.md](docs/OPERATIONS.md)
  and [docs/runbooks](docs/runbooks).

## Implementation status

Status per capability, with evidence level and what is still NOT-PROVEN, is tracked in
[docs/IMPLEMENTATION_COVERAGE.md](docs/IMPLEMENTATION_COVERAGE.md). Milestones follow master §49:

| Milestone | Scope | Status |
|---|---|---|
| M0 Baseline & seed | AGENTS/master, doctor CLI, install manifest, decision register | done — [owner/target/capability matrix](docs/OWNER_TARGET_CAPABILITY_MATRIX.md) |
| M1 Foundation | local auth, domain-first gateway, Control Center, inventory, mock labels | done (see coverage matrix) |
| M2 Planning & Docs | native Claude bridge, Product 1 renderer, PERSONAL publisher | partial — real publication NOT-PROVEN |
| M3 Workforce & self-build | sandbox + broker + OpenCode, verifier, live console | partial — container sandbox / OpenCode NOT-PROVEN |
| M4 Delivery | CI/PR evidence, human-only merge/production enforcement | partial — ruleset on `main` configured (public repo on GitHub Free, ADR-0115); provider bypass test NOT-PROVEN; no deployment target |
| M5 OFFICE | company-approved adapters/routes, isolated runtime | not started (default deny) |
| M6 Optimization & Ops | arena/scorecards, SLO/runbook/restore/incident loop | partial (see coverage matrix) |

Nothing in this repository claims a capability works unless the Readiness Doctor or a recorded test
proves it. MCI alignment remains **UNVERIFIED** until the canonical source can be read (§51).

## Manual steps only the owner can do

These cannot (and must not) be automated. The dashboard and `aeos doctor` show which are missing.

1. **Create the first admin** — `aeos admin create` (no default credentials; password ≥ 12 chars).
2. **Claude Code login check** — install the official Claude Code, run `claude` once, sign in with
   your Claude account (Max plan), then `claude auth status` must show `loggedIn: true` and
   `authMethod: claude.ai`. Make sure `ANTHROPIC_API_KEY` is **not** set in your environment.
3. **OpenRouter key into the secret store** — create a key with a spend limit at openrouter.ai,
   then `aeos secrets set workers/openrouter` (input is not echoed). Never put it in a file.
4. **OpenBao** — `aeos vault install` (installs the install-manifest `approved_pin`; while none is
   recorded pass `--version X.Y.Z` for a version you reviewed - `latest` is never implied),
   `aeos vault start`, `aeos vault init` (store the unseal keys
   and root token **offline**, never in the repo or chat), `aeos vault unseal` after each restart.
5. **Google OAuth client + consent** (PERSONAL docs) — in Google Cloud Console create a project,
   enable the Google Drive API and Google Docs API, configure the OAuth consent screen, create an
   OAuth client of type **Desktop app**, download the JSON, then
   `aeos publish google-connect --client-secret <file>` and complete consent in the browser. Delete
   the downloaded JSON afterwards (it is git-ignored, but do not keep it around).
6. **GitHub token (least privilege)** — create a **fine-grained PAT** on github.com (Settings → Developer
   settings → Fine-grained tokens) limited to *only* the platform repository, with Issues: read/write,
   Pull requests: read/write, Contents: read/write (the git broker pushes `aeos/*` task branches with it;
   protect `main` with a branch rule so merge stays human), Metadata: read, no Workflows/Administration; then
   `./aeos secrets set personal/github` and paste it at the hidden prompt (Windows:
   `.\.venv\Scripts\aeos.exe secrets set personal/github`). `gh auth token` works too, but that
   classic OAuth token usually carries `repo`/`workflow` over **all** your repositories:
   `aeos doctor --functional` reports it as a residual risk (`adapter.github_token`, token privilege).
7. **Route activation decision** — enabling a worker model route (and any paid route or budget) is
   your explicit decision in *Model & Rute*; nothing is activated by default.
8. **Optional: Docker Desktop** — for container sandboxes (stronger worker isolation).
9. **Backups** — after the first real work: `aeos ops backup create` and
   `aeos ops backup restore-test <id>`; copy backups to encrypted external storage if you want
   off-machine recovery.
10. **OFFICE** — stays disabled until your company authorises routes, data classes and targets.

## Development

```bash
python -m pytest -q                 # backend tests (isolated temp data dirs)
python -m ruff check backend tests  # lint
cd frontend && npx tsc -b --noEmit && npx vitest run
```

CI (`.github/workflows/ci.yml`) runs on Windows, macOS and Linux. Dependency pins:
[requirements.lock](requirements.lock) (universal, regenerated as described in
[docs/OPERATIONS.md](docs/OPERATIONS.md#dependency-lockfile)).
