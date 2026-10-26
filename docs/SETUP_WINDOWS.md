# DLY AEOS setup on Windows

Step-by-step installation in the bootstrap order of master §07. Each step lists **what to do** and
the **evidence before you continue**. Nothing in this guide is installed for you automatically —
the bootstrap script only reports what is missing. Optional components that are not installed do
not block capabilities that do not depend on them.

Tested shells: Windows PowerShell 5.1, PowerShell 7 and Command Prompt (cmd.exe). Run commands from
the repository root. `.\aeos.ps1 <command>` runs the CLI from the repository virtual environment in
PowerShell; this guide writes commands that way. **In Command Prompt type `aeos <command>` instead**:
it runs `aeos.cmd`, which starts the same CLI. Write `.eos.cmd <command>` if your environment sets
`NoDefaultCurrentDirectoryInExePath`. In cmd.exe, `.\aeos.ps1` opens the script in an editor
instead of running it. VS Code's default terminal may be Command Prompt; the prompt then looks like
`C:\...>` rather than `PS C:\...>`. The bootstrap itself is a PowerShell script; from cmd.exe run it as
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bootstrap.ps1`.

| Order | Component | Required for |
|---|---|---|
| 0 | Repository inspection + data backup | everything |
| 1 | Git, VS Code, Python 3.11+, Node.js 20+ | everything |
| 2 | Claude Code (official, native Max login) | planning/review (CAN_PLAN_*, CAN_REVIEW_WITH_MAX) |
| 3 | OpenBao + OS credential store | any credential-using capability |
| 4 | Worker isolation (process sandbox; Docker Desktop optional) | CAN_RUN_WORKER |
| 5 | OpenCode (first worker engine) + one approved route | CAN_RUN_WORKER |
| 6 | Model gateway (LiteLLM) — only if an approved route needs it | optional |
| 7 | PERSONAL adapters: GitHub + Google Drive/Docs | CAN_PUBLISH_PERSONAL, CAN_PREPARE_PR |
| 8 | Doctor / Control Center / verifier / Claude bridge | dashboard, end-to-end flow |
| 9 | DLY AEOS as first PERSONAL project | self-build |
| 10 | OFFICE connectors (company-approved only) | CAN_USE_OFFICE_INTEGRATIONS |
| 11 | Extra retrieval/arena/observability | optional, after measured benefit |

---

## 0. Inspect the repository and back up existing data

```powershell
git clone https://github.com/fadhlillah-dli/dly-aeos.git
cd dly-aeos
Get-Content AGENTS.md -TotalCount 60
```

* Read `AGENTS.md` and the master (`docs/architecture/DLY_AEOS_Master_Architecture_Final.md`).
* If you already ran DLY AEOS on this machine, the bootstrap script backs up the existing data
  directory (`%LOCALAPPDATA%\DLY-AEOS`) **before** any migration
  (`aeos ops backup create --label pre-bootstrap --if-exists`). It stops if the backup fails.

**Evidence:** the bootstrap output shows `Backup created: <id>` or `No database yet`.

## 1. Git, VS Code, Python, Node.js

```powershell
winget install --id Git.Git -e
winget install --id Microsoft.VisualStudioCode -e
winget install --id Python.Python.3.13 -e
winget install --id OpenJS.NodeJS.LTS -e
```

Open a **new** terminal so PATH changes apply, then:

```powershell
git --version; python --version; node --version
git config --global user.name "Your Name"
git config --global user.email "your-github-noreply@users.noreply.github.com"   # counts as contributions (§06)
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once, if scripts are blocked
.\scripts\bootstrap.ps1
```

The bootstrap creates `.venv`, installs the pinned `requirements.lock`, builds the frontend, runs
`aeos db upgrade` + `aeos seed`, the doctor, and prints the component status list.

**Evidence:** bootstrap ends with "Next steps"; `.\aeos.ps1 version` prints the version;
`.\aeos.ps1 doctor` shows core runtime checks without FAILED.

Create the first account (no default credentials exist):

```powershell
.\aeos.ps1 admin create
.\aeos.ps1 serve
```

Open http://127.0.0.1:8787, sign in, choose **PERSONAL**. The server binds loopback only; if the
port is taken it explains the conflict instead of using another interface (`--port 8790` to change).

## 2. Claude Code (official) with native Max login

Install the official client (one of):

```powershell
winget install --id Anthropic.ClaudeCode -e
# or the official installer, downloaded and REVIEWED before it runs (never piped into a shell, section 07):
#   irm https://claude.ai/install.ps1 -OutFile install-claude.ps1; notepad install-claude.ps1; .\install-claude.ps1
```

Then sign in once and check the auth mode:

```powershell
claude                 # interactive: sign in with your Claude account (Max plan), then /exit
claude --version
claude auth status     # JSON: loggedIn must be true, authMethod "claude.ai", subscriptionType "max"
```

Make sure no API key overrides your subscription (Claude Code prefers an API key when one is set):

```powershell
[Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")      # must be empty
[Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "Machine")   # must be empty
```

DLY AEOS uses only this binary (never `--bare`, never the Claude API); credentials stay with the
official client. **Evidence:** `claude auth status` as above; the doctor's Claude checks show the
binary version and auth mode; the bootstrap component list shows `auth: loggedIn=True`.

**Model discovery (master §12 "`/model` best for account discovery").** In an interactive `claude`
session run `/model` and note which models your Max account offers (the "best" entry and the exact
model IDs). Compare them with the SUPERVISOR routes on *Model & Rute* (`/models`). This is discovery
only: AEOS never uses `best` as an alias and never changes the planning baseline from it — a different
model becomes a route only through a recorded ROUTE_ACTIVATION decision. Record what you saw, then run
`.\aeos.ps1 bridge test` so the resolved model ID is observed evidence.

## 3. OpenBao and the OS credential store

OpenBao is the runtime vault for integration credentials (U-13). The Windows Credential Manager
holds the bootstrap identity used to reach OpenBao.

```powershell
.\aeos.ps1 vault install     # the install-manifest approved_pin; while it is null: --version X.Y.Z (reviewed)
.\aeos.ps1 vault start
.\aeos.ps1 vault init        # prints unseal keys ONCE; the root token is revoked when setup completes
.\aeos.ps1 vault unseal      # after every restart of OpenBao
```

Store the unseal keys **offline** (password manager entry or paper), never in the repository,
config files, chat, screenshots or the AEOS database. They are deliberately excluded from AEOS
backups (§33). Init revokes the root token when setup completes (the controller uses AppRole), so
there is no root token to keep; only when init reports `setup incomplete` is it left valid so you
can finish, and then you revoke it. Official downloads: https://openbao.org/downloads/.

**Evidence:** `GET /api/ops/services` (or the bootstrap list) shows OpenBao `unsealed`; the doctor's
secret checks pass. Recovery: [runbooks/openbao-unseal-recovery.md](runbooks/openbao-unseal-recovery.md).

## 4. Worker isolation

The process sandbox works without extra software. For stronger isolation install Docker Desktop
(optional, WSL 2 backend):

```powershell
winget install --id Docker.DockerDesktop -e
docker version
```

**Evidence:** the doctor's containment checks (filesystem/egress/credential/kill negative tests)
report each result honestly; anything not proven keeps CAN_RUN_WORKER blocked (§25).

## 5. OpenCode (first worker engine) and one approved route

```powershell
npm install -g opencode-ai@1.19.3     # version observed on 2026-10-18; install-manifest/ is authoritative
opencode --version
```

Put the OpenRouter worker key into the secret store (input is not echoed; never paste it into a
file or terminal history):

```powershell
.\aeos.ps1 secrets set workers/openrouter
```

Then activate exactly one route in the dashboard (*Model & Rute*) — activation is your recorded
decision; paid routes and budgets need an explicit decision too (§42).

**Evidence:** doctor shows OpenCode version and the route's eligibility; a bounded test task produces
output/evidence matching its contract.

## 6. Model gateway (only if needed)

LiteLLM is not part of the V1 bootstrap. Add it only when an approved API worker route requires it,
with its own budget configuration and outage/fallback tests (§07 step 6).

## 7. PERSONAL adapters: GitHub and Google Drive/Docs

**GitHub** (repository, issues, PRs):

Primary path — a **fine-grained personal access token** (least privilege, §07 step 7): github.com →
Settings → Developer settings → Fine-grained tokens → *Only select repositories*: the platform
repository; permissions Issues: read/write, Pull requests: read/write, Contents: read/write (the git broker
pushes `aeos/*` task branches with this token; protect `main` with a branch rule so merge stays human),
Metadata: read; no Workflows or Administration permission.
Store it (paste at the hidden prompt twice, nothing is echoed):

```powershell
.\.venv\Scripts\aeos.exe secrets set personal/github
```

Alternative (broader): `gh auth login` then
`gh auth token | .\.venv\Scripts\aeos.exe secrets set personal/github --stdin`. That classic OAuth token
normally carries `repo`/`workflow` over all your repositories; `aeos doctor --functional` shows it as a
residual risk on `adapter.github_token`. Pipe into `aeos.exe` directly: Windows PowerShell does not
forward pipeline input through the `.\aeos.ps1` launcher to the program it starts, so
`gh auth token | .\aeos.ps1 ...` would receive no token. Merge stays human-only.

**Google Drive + Docs** (official project documents, folder per project name):

1. Open https://console.cloud.google.com/ → create (or select) a project, e.g. `dly-aeos`.
2. *APIs & Services → Library*: enable **Google Drive API** and **Google Docs API**.
3. *APIs & Services → OAuth consent screen*: user type *External* (personal account) or *Internal*
   (Workspace), app name `DLY AEOS`, your email as support/developer contact; add yourself as a
   **test user** while the app is in testing.
4. *APIs & Services → Credentials → Create credentials → OAuth client ID*, application type
   **Desktop app**, name `DLY AEOS local`. Download the JSON.
5. Connect and consent:
   ```powershell
   .\aeos.ps1 publish google-connect --client-secret "$env:USERPROFILE\Downloads\client_secret_XXXX.json"
   ```
   Complete the consent in the browser, then delete the downloaded JSON.

**Evidence:** *Integrasi & Akses* shows the integration with credential status CONFIGURED (never the
value); the doctor's adapter checks run a dry-run preview against the exact root folder/repository.

## 8. Doctor, Control Center, verifier, Claude bridge

```powershell
.\aeos.ps1 doctor
.\aeos.ps1 serve
```

In the dashboard: *Kesiapan (Doctor)* lists every capability with its checks and remedies;
*Ringkasan* shows readiness, blockers, decisions, plan, cost/quota and health. For development use
`.\scripts\dev.ps1` (backend `--dev` on 8787 + Vite on 5173).

**Evidence:** one controlled end-to-end workflow without self-approval (§07 step 8).

## 9. DLY AEOS as the first PERSONAL project

`aeos seed` registers DLY AEOS as a PERSONAL project linked to this GitHub repository. In the
dashboard choose PERSONAL → project *DLY AEOS* → create a plan, review it and approve one slice.
Workers change only a candidate workspace (`aeos/*` branches); a human merges; the released
controller is promoted with `aeos ops release promote` ([runbook](runbooks/controller-upgrade-rollback.md)).

## 10. OFFICE connectors

Disabled by default (`external_worker_allowed=false`, no approved routes). Configure Bitbucket,
Jira and Confluence only after a recorded company authorisation of routes, data classes and targets.

## 11. Optional extras

Retrieval, arena, observability exporters and a distributed scheduler are added only after their
benefit is measured (§07 step 11).

---

## Backups after setup

```powershell
.\aeos.ps1 ops backup create --label after-setup
.\aeos.ps1 ops backup restore-test <backup_id>
```

A backup counts as ready only after the restore test passed (§33). See
[runbooks/backup-restore.md](runbooks/backup-restore.md) and [OPERATIONS.md](OPERATIONS.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `python` opens the Microsoft Store | install Python with winget, then disable the *App execution aliases* for python |
| Port 8787 in use | `Get-NetTCPConnection -LocalPort 8787 -State Listen`; stop it or `aeos serve --port 8790` |
| `FRONTEND_NOT_BUILT` | `cd frontend; npm.cmd ci; npm.cmd run build` |
| Characters look garbled in the console | output is ASCII; use Windows Terminal / PowerShell 7 for the UI-only Unicode |
