# DLY AEOS setup on macOS

Step-by-step installation in the bootstrap order of master §07. Each step lists **what to do** and
the **evidence before you continue**. Nothing is installed for you automatically — the bootstrap
script only reports what is missing. Optional components that are not installed do not block
capabilities that do not depend on them.

Works with the default zsh and with the bash 3.2 that ships with macOS. Run commands from the
repository root; `./aeos <command>` runs the CLI from the repository virtual environment.

| Order | Component | Required for |
|---|---|---|
| 0 | Repository inspection + data backup | everything |
| 1 | Git, VS Code, Python 3.11+, Node.js 20+ | everything |
| 2 | Claude Code (official, native Max login) | planning/review |
| 3 | OpenBao + macOS Keychain | any credential-using capability |
| 4 | Worker isolation (process sandbox; Docker Desktop optional) | CAN_RUN_WORKER |
| 5 | OpenCode + one approved route | CAN_RUN_WORKER |
| 6 | Model gateway (LiteLLM) — only if needed | optional |
| 7 | PERSONAL adapters: GitHub + Google Drive/Docs | CAN_PUBLISH_PERSONAL, CAN_PREPARE_PR |
| 8 | Doctor / Control Center / verifier / Claude bridge | dashboard, end-to-end flow |
| 9 | DLY AEOS as first PERSONAL project | self-build |
| 10 | OFFICE connectors (company-approved only) | CAN_USE_OFFICE_INTEGRATIONS |
| 11 | Extra retrieval/arena/observability | optional |

---

## 0. Inspect the repository and back up existing data

```bash
git clone https://github.com/fadhlillah-dli/dly-aeos.git
cd dly-aeos
head -60 AGENTS.md
```

Read `AGENTS.md` and the master. If DLY AEOS already ran on this Mac, the bootstrap backs up
`~/Library/Application Support/DLY-AEOS` before any migration and stops if that backup fails.

**Evidence:** bootstrap prints `Backup created: <id>` or `No database yet`.

## 1. Git, VS Code, Python, Node.js

Install the Command Line Tools and Homebrew (https://brew.sh) if you do not have them:

```bash
xcode-select --install
brew install git python@3.13 node@22
brew install --cask visual-studio-code
git --version; python3.13 --version; node --version
git config --global user.name "Your Name"
git config --global user.email "your-github-noreply@users.noreply.github.com"   # contributions (§06)
./scripts/bootstrap.sh
```

`node@22` is keg-only in Homebrew; if `node --version` is not found, follow brew's hint
(`echo 'export PATH="/opt/homebrew/opt/node@22/bin:$PATH"' >> ~/.zprofile`) and open a new terminal.

**Evidence:** bootstrap ends with "Next steps"; `./aeos version`; `./aeos doctor` core checks pass.

```bash
./aeos admin create
./aeos serve
```

Open http://127.0.0.1:8787, sign in, choose **PERSONAL**.

## 2. Claude Code (official) with native Max login

```bash
brew install --cask claude-code                     # package manager (docs: https://code.claude.com/docs/en/setup)
# or the official installer, downloaded and REVIEWED before it runs (never piped into a shell, §07):
#   curl -fsSL https://claude.ai/install.sh -o install-claude.sh && less install-claude.sh && bash install-claude.sh
claude                                              # sign in with your Claude account (Max plan), then /exit
claude --version
claude auth status                                  # loggedIn true, authMethod "claude.ai", subscriptionType "max"
echo "${ANTHROPIC_API_KEY:-<not set>}"              # must print <not set>
```

If `ANTHROPIC_API_KEY` is exported in `~/.zshrc`/`~/.zprofile`, remove it: Claude Code would bill
the API key instead of your subscription, and DLY AEOS never uses the Claude API (§12).

**Evidence:** `claude auth status` as above; doctor Claude checks show version and auth mode.

**Model discovery (master §12 "`/model` best for account discovery").** In an interactive `claude`
session run `/model` and note which models your Max account offers (the "best" entry and the exact
model IDs). Compare them with the SUPERVISOR routes on *Model & Rute* (`/models`). This is discovery
only: AEOS never uses `best` as an alias and never changes the planning baseline from it — a different
model becomes a route only through a recorded ROUTE_ACTIVATION decision. Then run
`./aeos bridge test` so the resolved model ID is observed evidence.

## 3. OpenBao and the macOS Keychain

```bash
./aeos vault install     # the install-manifest approved_pin; while it is null: --version X.Y.Z (a version you reviewed)
                         # alternative: brew install openbao
./aeos vault start
./aeos vault init        # prints unseal keys + root token ONCE - store them offline
./aeos vault unseal      # after every restart of OpenBao
```

The Keychain holds the bootstrap identity used to reach OpenBao. Unseal keys/root token never go
into the repository, config, chat or AEOS backups (§26, §33).

**Evidence:** OpenBao shows `unsealed` in the bootstrap list / `GET /api/ops/services`; doctor secret
checks pass. Recovery: [runbooks/openbao-unseal-recovery.md](runbooks/openbao-unseal-recovery.md).

## 4. Worker isolation

The process sandbox needs nothing extra. Optional container sandbox:

```bash
brew install --cask docker
open -a Docker && docker version
```

**Evidence:** doctor containment checks report each negative test; unproven isolation keeps
CAN_RUN_WORKER blocked.

## 5. OpenCode and one approved route

```bash
npm install -g opencode-ai@1.19.3     # version observed on 2026-10-18; install-manifest/ is authoritative
opencode --version
./aeos secrets set workers/openrouter  # OpenRouter key, not echoed
```

Activate exactly one route in *Model & Rute* (your recorded decision; paid routes need a budget
decision).

## 6. Model gateway (only if needed)

LiteLLM only when an approved API worker route needs it (§07 step 6).

## 7. PERSONAL adapters: GitHub and Google Drive/Docs

Primary path — a **fine-grained PAT** limited to the platform repository (Issues RW, Pull requests RW,
Contents RW because the git broker pushes `aeos/*` task branches with this token, Metadata R; no Workflows
or Administration), pasted at the hidden prompt (twice). Protect `main` with a branch rule so merge stays human:

```bash
./aeos secrets set personal/github
```

Alternative (broader scopes over all repositories, reported as residual risk by
`aeos doctor --functional`):

```bash
brew install gh
gh auth login
gh auth token | ./aeos secrets set personal/github --stdin
```

Google Drive + Docs:

1. https://console.cloud.google.com/ → create/select a project (e.g. `dly-aeos`).
2. *APIs & Services → Library*: enable **Google Drive API** and **Google Docs API**.
3. *OAuth consent screen*: External (personal) or Internal (Workspace), app name `DLY AEOS`, add
   yourself as **test user** while in testing.
4. *Credentials → Create credentials → OAuth client ID* → type **Desktop app** → download the JSON.
5. ```bash
   ./aeos publish google-connect --client-secret ~/Downloads/client_secret_XXXX.json
   rm ~/Downloads/client_secret_XXXX.json      # after consent succeeded
   ```

**Evidence:** *Integrasi & Akses* shows credential status CONFIGURED (never the value); adapter
checks pass a dry-run against the exact folder/repository.

## 8. Doctor, Control Center, verifier, Claude bridge

```bash
./aeos doctor
./aeos serve            # or ./scripts/dev.sh for backend --dev + Vite on 5173
```

## 9. DLY AEOS as the first PERSONAL project

`./aeos seed` registers DLY AEOS (PERSONAL, this GitHub repository). Plan, review and approve one
slice in the dashboard; workers edit only candidate `aeos/*` branches; you merge; promote the built
release with `./aeos ops release promote` ([runbook](runbooks/controller-upgrade-rollback.md)).

## 10. OFFICE connectors

Default deny until a recorded company authorisation exists (§23).

## 11. Optional extras

Only after measured benefit.

---

## Backups after setup

```bash
./aeos ops backup create --label after-setup
./aeos ops backup restore-test <backup_id>
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `xcrun: error: invalid active developer path` | `xcode-select --install` |
| `node: command not found` after brew | add `/opt/homebrew/opt/node@22/bin` to PATH (see step 1) |
| Port 8787 in use | `lsof -nP -iTCP:8787 -sTCP:LISTEN`; stop it or `./aeos serve --port 8790` |
| Keychain prompt on first secret access | allow access for the `dly-aeos` service only |
| `FRONTEND_NOT_BUILT` | `cd frontend && npm ci && npm run build` |
