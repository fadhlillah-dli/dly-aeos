# Owner actions: what only the owner can do

This is the ordered list of every step that only you, the owner, can do: logins, consent, secrets, installs on
your own machines, and decisions. Do them in this order. Afterwards the only gaps left are the external ones in
[section 4](#4-external-items-company-tenant-provider): company authorisation, a company tenant, a live provider
bypass test and real deployment targets.

The list is written for the move to the MacBooks (Apple Silicon M4 and Intel i9). Windows stays supported:
[section 5](#5-windows-notes) gives the Windows form of every command. It complements
[SETUP_MACOS.md](SETUP_MACOS.md), [SETUP_WINDOWS.md](SETUP_WINDOWS.md) and [OPERATIONS.md](OPERATIONS.md). It does
not replace them.

## 0. How to read this list

**Commands.** Run them from the repository root. macOS commands are written `./aeos <command>`. On Windows,
type `aeos <command>` in Command Prompt (cmd.exe) or `.\aeos.ps1 <command>` in PowerShell (section 5). `<admin>`
is your SUPER_ADMIN username. Commands with `--as <admin>` ask for your password. Never type a secret on a
command line: `secrets set` prompts for it without echo. The macOS shell (zsh) rejects the `# ...` comments in
these code blocks as extra arguments unless you run `setopt interactivecomments` once (or add it to
`~/.zshrc`); otherwise copy each command without its comment.

**Scope tags.**

* **[per machine]**: each machine has its own data directory, database, vault, Keychain and Docker images, so
  do the step on every machine where you run DLY AEOS. In-app decisions (`policy decide`, `routes activate`, SLI
  targets, ADR decisions) are stored in that machine's database, so they are per machine too.
* **[once]**: the step changes the repository, for example `install-manifest/manifest.yaml` or `policy/*.yaml`,
  through a reviewed pull request. Every machine picks it up after `git pull`.

Pick one Mac as your primary machine (the M4 is the natural choice) and do everything there. On the second Mac,
do only the [per machine] steps, and only if you will run DLY AEOS on it.

**Status words.**

| Word | Meaning |
|---|---|
| OWNER | Only you can do it (login, consent, secret, install, run on your hardware). |
| DECISION | You choose. The line "Record:" says exactly where. Until then it shows as UNDECIDED or OWNER_DECISION_REQUIRED. |
| EXTERNAL | Needs your company, a company tenant or a provider. You can prepare for it, but not finish it alone. |
| NOT-PROVEN | No real run on your machine shows it yet. The steps below produce that proof. |

The Doctor reports each check as VERIFIED, CONFIGURED, CONNECTED, MISSING, FAILED, EXPIRED, STALE, UNKNOWN or
NOT_APPLICABLE. It reports each capability (`CAN_*`) as READY, DEGRADED, BLOCKED or UNKNOWN. CONFIGURED is not
VERIFIED: a value is set, but no probe has confirmed it.

**Where each item shows up.** `./aeos doctor` prints every check, `./aeos doctor -g <GROUP>` prints one group and
`./aeos doctor -c <CAPABILITY>` prints one capability (add `--functional` for the heavier probes). In the
Control Center (`./aeos serve`, then http://127.0.0.1:8787) the same results are on *Kesiapan (Doctor)*
(`/readiness`), and open items are in the decision queue on *Keputusan* (`/decisions`).

| Step | Doctor group / check | Control Center page |
|---|---|---|
| 1.2 bootstrap | `HOST`, `REPOSITORY`, `RUNTIME` | Kesiapan (Doctor) |
| 1.3 admin | `LOCAL_AUTH`: `auth.bootstrap`, `auth.default_credentials` | sign-in page |
| 1.4 Claude Code | `CLAUDE_NATIVE`: `claude.auth`, `claude.binary`, `claude.capability_test`, `claude.env`; `CAN_PLAN_PERSONAL`, `CAN_REVIEW_WITH_MAX` | Model & Rute → Pengawas |
| 1.5 commit identity | `REPOSITORY`: `git.commit_identity.github`, `git.commit_identity.bitbucket`; `CAN_PREPARE_PR` | Kesiapan (Doctor) |
| 1.6 Docker + containment | `CONTAINMENT`: `containment.docker`, `containment.sandbox`, `containment.report`; `EVIDENCE_REVIEW`: `evidence.verifier` | Kesiapan (Doctor); Pekerjaan Live |
| 1.7 OpenBao | `SECRET`: `secret.openbao_binary`, `secret.openbao_health`, `secret.openbao_auth`, `secret.keyring` | Integrasi & Akses |
| 1.8 secrets | `SECRET`: `secret.status`; `MODEL_ROUTE`: `route.worker_key`; `PROJECT_ADAPTERS`: `adapter.github_token`; `DELIVERY`: `delivery.branch_protection` | Integrasi & Akses; Bukti → PR & provider |
| 1.9 OpenCode | `WORKER`: `worker.opencode`, `worker.contract` | Kesiapan (Doctor) |
| 1.10 Google | `PROJECT_ADAPTERS`: `adapter.google_oauth`, `adapter.project_drive_root`, `adapter.write_enablement`; `CAN_PUBLISH_PERSONAL` | Integrasi & Akses |
| 1.11 routes and budget | `MODEL_ROUTE`: `route.registry`, `route.budget_admission`; `HOST`: `host.resource_budget`; `WORKER`: `worker.agent_definition`; `CAN_RUN_WORKER` | Model & Rute; Organisasi & Peran |
| 1.12 pins | `REPOSITORY`: `repo.install_manifest`; `claude.binary` (signer pin) | Tools & Repos → component |
| 1.13 first plan | `CAN_PLAN_PERSONAL`, `CAN_RUN_WORKER`, `CAN_PUBLISH_PERSONAL` | Rencana; Pekerjaan Live; Dokumen & Publikasi |
| 1.14 backup | `RECOVERY`: `recovery.backups`, `recovery.restore_test`, `recovery.config_rollback` | Riwayat & Pemulihan → Backup & pemulihan |

---

## 1. Mac setup, in order

### 1.1 Clone [per machine]

`xcode-select --install` provides git and the compilers. The Homebrew prefix is `/opt/homebrew` on the M4 and
`/usr/local` on the i9.

```bash
xcode-select --install
brew install git python@3.13 node@22
git clone https://github.com/fadhlillah-dli/dly-aeos.git
cd dly-aeos
```

Work on `main`. If `node --version` is not found, follow the PATH hint in
[SETUP_MACOS.md step 1](SETUP_MACOS.md#1-git-vs-code-python-nodejs) (`node@22` is keg-only).

### 1.2 Bootstrap [per machine]

If `./scripts/bootstrap.sh` answers "Permission denied", run `sh scripts/bootstrap.sh` instead.

```bash
./scripts/bootstrap.sh
./aeos version
./aeos doctor
```

The bootstrap creates the venv from `requirements.lock`, backs up any existing data, builds the frontend, migrates
the database, seeds the catalog and the DLY AEOS project, and lists the optional components. It installs no system
software. Data lives in `~/Library/Application Support/DLY-AEOS`. Start each Mac with its own fresh data
directory: there is no sync between machines, and restoring a Windows backup on a Mac has never been tested.

*macOS evidence (optional, closes "never run on a physical Mac"):* run `./aeos doctor > ~/aeos-doctor-macos.txt`.
Redact your user name and home paths the way [doctor-sample-windows.txt](evidence/doctor-sample-windows.txt) is
redacted, then hand the file to the builder as `docs/evidence/doctor-sample-macos-arm64.txt` (M4) or
`...-x86_64.txt` (i9).

### 1.3 First admin [per machine]

`admin create` prompts for your login name and a password of at least 12 characters. `doctor -g LOCAL_AUTH` must
show `auth.bootstrap` and `auth.default_credentials` VERIFIED. `serve` runs at http://127.0.0.1:8787: sign in and
choose PERSONAL.

```bash
./aeos admin create
./aeos doctor -g LOCAL_AUTH
./aeos serve
```

Lost password: `./aeos admin recover <username>`.

### 1.4 Claude Code login and bridge test [per machine]

Running `claude` the first time asks you to sign in with your Claude Max account; then type `/exit`.
`claude auth status` must show `loggedIn true`, authMethod `claude.ai` and subscriptionType `max`. The `echo`
must print `<not set>`; remove any `ANTHROPIC_API_KEY` export from `~/.zshrc` and `~/.zprofile`. The `fable` bridge
test must resolve inside the Fable family, with no MODEL_POLICY_MISMATCH. `doctor --functional` is the Claude
capability test and uses a little Max quota.

```bash
brew install --cask claude-code
claude
claude auth status
echo "${ANTHROPIC_API_KEY:-<not set>}"
./aeos bridge test --model opus
./aeos bridge test --model fable
./aeos bridge status
./aeos doctor --functional
```

You can run the same capability test from the Control Center: *Model & Rute* (`/models`) → *Pengawas* →
*Uji kemampuan*. If `fable` keeps resolving outside the Fable family, that is a DECISION: choose another
supervisor through a recorded POLICY decision ([2.12](#212-smaller-open-decisions)). Never switch silently.

### 1.5 Commit identity in the local `aeos.yaml` [per machine]

Every commit, push, PR and issue that DLY AEOS sends uses your identity, never an AI identity
([ADR-0121](decisions/ADR-0121-code-host-identity-and-no-ai-attribution.md)). The real addresses live only in the
local file on each machine. The repository is public, so never commit them.

```bash
CFG="$HOME/Library/Application Support/DLY-AEOS/aeos.yaml"
test -f "$CFG" || cp config/aeos.example.yaml "$CFG"
code "$CFG"
```

Use `nano "$CFG"` instead of `code "$CFG"` if you prefer a terminal editor.

Uncomment the `commit_identities` block and replace every placeholder. A value left as `<...>` is refused.

```yaml
commit_identities:
  github:
    name: <Your Name>
    email: <id>+<login>@users.noreply.github.com   # GitHub noreply address recommended
  bitbucket:                                       # only for OFFICE work (section 4)
    name: <Your Name>
    email: <you>@<company-domain>
```

Find `<id>` and `<login>` with `gh api user --jq '.id, .login'` or on github.com → Settings → Emails. Restart
`./aeos serve`, then run `./aeos doctor -g REPOSITORY`: `git.commit_identity.github` must be CONFIGURED.
`git.commit_identity.bitbucket` stays NOT_APPLICABLE until a Bitbucket repository is registered. Also set the
same identity for your own manual commits: `git config --global user.name "<Your Name>"` and
`git config --global user.email "<id>+<login>@users.noreply.github.com"`.

Before the first push, check two things. Push or rewrite yourself any local history of a project that was
authored with another address. The broker refuses to push it (`COMMIT_IDENTITY_MISMATCH`). Re-run any candidate
committed before this rule: those commits still carry `AEOS-*` trailers and are refused
(`AI_ATTRIBUTION_IN_COMMIT`).

### 1.6 Docker Desktop, images and containment [per machine]

Each Mac builds its own images, natively: linux/arm64 on the M4, linux/amd64 on the i9. Each Mac also records its
own containment report. A report is bound to the image id and the host, so a report from one Mac never admits
the container sandbox on another.

Homebrew picks the right Docker build. Or download it from docker.com: "Mac with Apple chip" (M4) or "Mac with
Intel chip" (i9). Expected results: `worker containment` reports overall=PASS for the PERSONAL profile and for
the OFFICE profile (OFFICE stays blocked, section 4). `worker status` shows "requirement for PERSONAL: READY
(kind=container)" and "integrity hold: CLEAR". `doctor -g CONTAINMENT` shows `containment.sandbox` and
`containment.report` no longer BLOCKED / NOT_PROVEN, and `doctor -g EVIDENCE_REVIEW` shows `evidence.verifier`
reporting the CONTAINER runner, not WEAK.

```bash
brew install --cask docker
open -a Docker && docker version
docker build --build-arg OPENCODE_VERSION=1.19.3 -t dly-aeos-worker:1.19.3 sandbox/
docker build -f sandbox/verifier.Dockerfile -t dly-aeos-verifier:0.1.0 .
./aeos worker containment --kind container
./aeos worker containment --kind container --domain OFFICE
./aeos worker status --domain PERSONAL
./aeos doctor -g CONTAINMENT
./aeos doctor -g EVIDENCE_REVIEW
.venv/bin/python -m pytest -m integration -q -p no:cacheprovider tests/acceptance/test_containment.py
```

The verifier tag follows `./aeos version`. If the pinned OpenCode version changes, use the exact `docker build`
command that `./aeos worker status` prints. These checks must pass: `read_only_root`, `mounts_allowlisted`,
`write_outside_worktree`, `read_outside_worktree`, `docker_socket_absent`, `real_ssh_dir`,
`home_ssh_redirected`, `keyring_unreachable`, `env_secrets`, `credential_helpers_absent`, `read_data_db`,
`egress_non_broker`, `broker_gateway_reachable` and `gateway_path_filter`. If `egress_non_broker` fails on Docker
Desktop for Mac, keep workers blocked and report it. Do not fall back to the process sandbox for that work.

A report is valid for 7 days and only for the image it was recorded with. Re-run the containment after every
image rebuild and at least weekly ([1.15](#115-recurring-owner-actions)).

### 1.7 OpenBao: install, init, unseal [per machine]

`<X.Y.Z>` is an exact release you reviewed at github.com/openbao/openbao/releases; the installer picks
darwin_arm64 or darwin_amd64 and shows the SHA256. `vault init` keeps the unseal key share(s) in the macOS
Keychain (the same `dly-aeos-bootstrap` service as the AppRole credentials) and never prints them
([ADR-0123](decisions/ADR-0123-openbao-unseal-key-in-os-keyring.md)). `vault export-recovery` writes your offline
recovery copy to a new owner-only file and prints only its path. `doctor -g SECRET` must show the
`secret.openbao_*` checks passing and says where the unseal key lives, and `doctor --functional -g SECRET` must
show `secret.keyring` VERIFIED (allow the Keychain prompt for the dly-aeos service only).

```bash
./aeos vault install --version <X.Y.Z>
./aeos vault start
./aeos vault init
./aeos vault export-recovery "$HOME/aeos-openbao-recovery.txt"
./aeos vault status --verify
./aeos doctor -g SECRET
./aeos doctor --functional -g SECRET
```

Move the recovery file offline right away (encrypted USB drive or your password manager entry), then delete it
from the disk; the runbook below has the exact commands. It holds the unseal key share(s) and the AppRole
credentials the controller logs in with, and it is the only way back if the Keychain entries are lost: the runbook
section "Lost keyring" restores both with `./aeos vault import-unseal-key --with-approle`. A copy that says
`approle: NOT in the OS keyring` (or a key you stored before ADR-0123) restores the unseal key only.
The root token is revoked when setup completes, so none is kept anywhere; only a `setup incomplete` run keeps it,
in the Keychain only, and the recovery copy then contains it so you can finish. Never paste or screenshot
`vault init` output, the recovery file or a key into a chat or an AI assistant. To prove the step, paste only
`./aeos doctor --functional -g SECRET`, which contains no secret. If an unseal key was exposed right after
`vault init`, before any `secrets set`, reset the empty vault: `./aeos vault stop`, delete
`~/Library/Application Support/DLY-AEOS/openbao/raft`, `./aeos vault start`, `./aeos vault init`. Once a secret
is stored, follow the runbook below instead.

After a restart, run `./aeos vault start`; `./aeos serve` (or `./aeos vault unseal`) then unseals OpenBao from the
Keychain. The residual risk, stated by Doctor too: anyone who acts as your OS user on this Mac can read the unseal
key, as they can the AppRole credentials already there. Unseal keys never go into the repository, a config file,
chat or AEOS backups. Each Mac has its own vault with its own unseal keys.

**Already initialised before ADR-0123** (the key is only in your offline storage): migrate once with
`./aeos vault import-unseal-key` (hidden prompt, entered twice; paste it from your password manager, never into a
chat or the command line), then `./aeos vault export-recovery "$HOME/aeos-openbao-recovery.txt"` and move that file
offline. Step by step, with the proof that the key works:
[runbooks/openbao-unseal-recovery.md](runbooks/openbao-unseal-recovery.md), "One-time migration". To keep the
former flow instead (keys shown once on a temporary screen, a manual unseal after every restart), run
`./aeos vault init --show-unseal-once`.

Optional live proof on a throw-away vault:
`DLY_AEOS_TEST_BAO="$HOME/Library/Application Support/DLY-AEOS/bin/bao" .venv/bin/python -m pytest -m integration tests/integration/test_secrets_openbao_live.py -q`.

If you cannot run OpenBao on a machine, `./aeos secrets interim-keyring` explains the weaker interim OS-keyring
EXCEPTION. You record it on *Keputusan* with the preset "Secret store sementara di OS keyring (OpenBao belum
tersedia)". Never combine it with the process-sandbox exception ([2.7](#27-process-sandbox-exception-per-machine)).

### 1.8 Secrets: OpenRouter and the GitHub token [per machine]

The OpenRouter key should have a spend limit; it is prompted without echo. `personal/github` takes the fine-grained
PAT and its expiry date (see below). `delivery capabilities --record` records the GitHub provider capability
probe as evidence. `doctor -g DELIVERY` must show `delivery.branch_protection`: the main ruleset reads CONFIGURED.

```bash
./aeos secrets set workers/openrouter
./aeos secrets set personal/github --expires-at <YYYY-MM-DD>
./aeos secrets status
./aeos delivery capabilities --record
./aeos doctor -g DELIVERY
./aeos doctor --functional -g PROJECT_ADAPTERS
```

To create the GitHub token, open github.com → Settings → Developer settings → Fine-grained tokens → "Only select
repositories" and choose `dly-aeos`. Grant Contents RW, Issues RW, Pull requests RW and Metadata R. Grant no
Workflows and no Administration. Give it an expiration (for example 90 days) and pass the same date as
`--expires-at`. The Doctor check `adapter.github_token` warns 14 days ahead: `./aeos doctor` from that recorded
date, `./aeos doctor --functional` also from the date GitHub reports for the token. You can also record the capability probe from *Bukti* → *PR & provider* →
*Catat sebagai bukti*. Other route keys are needed only for the routes you choose in 1.11:
`./aeos secrets set workers/opencode-zen` and `./aeos secrets set workers/gemini`.

**OpenCode Go key (decision A, ADR-0041 amendment 2026-10-26).** Only after you subscribe to Go:

1. Open the OpenCode Console and select the **Personal** workspace. Go works only in single-member workspaces.
2. Create an API key with permission **Restricted → Models: Request**. That is the least privilege the docs give for
   inference.
3. Store it, prompted without echo, under its own ref `workers/opencode-go` (never the Zen key's ref):

   ```bash
   ./aeos secrets set workers/opencode-go
   ./aeos secrets status
   ```

4. On the Console's Go page, keep **Extra Usage OFF**. With it ON, Go falls back to your pay-as-you-go balance after
   a usage limit, and AEOS cannot see that setting.

AEOS counts only the Go calls made through this AEOS instance. If you also use the same Go workspace from your own
OpenCode or from the other Mac, Go's limits are reached sooner than `aeos cost summary` shows; Go's own limit stays
authoritative.

The Integrasi & Akses card "OpenCode Go (langganan, rute pekerja)" then reads CONFIGURED. That means stored, not
verified.

### 1.9 OpenCode and the contract test [per machine]

`install-manifest/` is authoritative for the version. `opencode --version` must print 1.19.3 and
`worker contract-test` must report overall=PASS.

```bash
npm install -g opencode-ai@1.19.3
opencode --version
./aeos worker contract-test
./aeos doctor -g WORKER
```

Optional: `.venv/bin/python -m pytest -m integration tests/integration/test_worker_runtime_opencode_e2e.py -q -p no:cacheprovider`.

### 1.10 Google OAuth client and `google-connect` [per machine]

1. In the Google Cloud Console, create a project. Under *APIs & Services → Library*, enable the **Google Drive
   API** and the **Google Docs API**.
2. Set up the *OAuth consent screen* as External and add your own account as a test user. While the app is in
   "Testing", Google expires the refresh token after 7 days. Either re-run `google-connect` weekly, or set the
   publishing status to "In production" (AEOS requests only `drive.file`).
3. Under *Credentials → Create credentials → OAuth client ID*, choose type **Desktop app** and download the JSON.

Delete the downloaded client secret file (the `rm` below) only after the consent succeeded. `doctor --functional -c
CAN_PUBLISH_PERSONAL` must end READY.

```bash
./aeos publish google-connect --client-secret ~/Downloads/client_secret_<...>.json
rm ~/Downloads/client_secret_<...>.json
./aeos publish google-status
./aeos policy decide --kind WRITE_ENABLEMENT --provider google-drive --enable --domain PERSONAL --as <admin> --rationale "Enable PERSONAL docs publication"
./aeos policy decide --kind WRITE_ENABLEMENT --provider github-issues --enable --domain PERSONAL --as <admin> --rationale "Enable platform GitHub issues"
./aeos doctor --functional -c CAN_PUBLISH_PERSONAL
```

Leave the Drive root empty for the first publication: AEOS creates the project folder in My Drive. Or enter a
folder id that AEOS created under *Portofolio* → DLY AEOS → *Integrasi* → Google Drive / Google Docs. Optional
rendered diagrams: `npm install -g @mermaid-js/mermaid-cli`, then `./aeos docs visuals-status`.

### 1.11 Route activation, budget and the worker agent [per machine, DECISION]

Activating a route is your explicit decision. Nothing is active by default.

Copy the route key and its config hash from the `routes list` output.

```bash
./aeos routes refresh
./aeos routes list --pool WORKER
./aeos routes activate <route_key> --as <admin> --mode TRIAL --expected-hash <hash> --terms-reviewed --terms-note "<what you read>" --rationale "<why this route>"
```

* **In the UI:** *Model & Rute* (`/models`) → route → *Aktifkan rute…*.
* **Paid route:** first record a BUDGET decision on *Keputusan* → *Catat keputusan manusia* (Jenis keputusan:
  BUDGET), then add `--budget-decision <id>`.
* **Free OpenCode Zen routes:** they need the client-terms decision in [2.12](#212-smaller-open-decisions) first.
* **OpenCode Go routes (decision A, ADR-0041 amendment 2026-10-26):** the WORKER candidates are
  `opencode-go/glm-5.3-flash` (first), `opencode-go/mimo-v2.6-flash`, `opencode-go/minimax-m3.1-flash`,
  `opencode-go/hy3` and `opencode-go/kimi-k2.7-code`. hy3 has no documented tool support, so it never serves
  tool-using tasks. **A WORKER route activated as TRIAL is eligible for real worker tasks at once.** If you want the
  probe (step 6) first, activate it before you enable the worker agent (below), or pause scheduling around it
  (`./aeos work pause --reason "Go probe"`, then `./aeos work resume`). To activate one:
  1. Store the Go key (1.8), then run `./aeos routes refresh`. The `opencode_go_catalog` line must read OK, and its
     `available:` line must list the route. A `missing:` line means Go does not list that model.
  2. Read the Go terms: <https://opencode.ai/v2/docs/console/go/>, <https://opencode.ai/go> and the terms of service
     <https://opencode.ai/legal/terms-of-service>. The ToS clause on automated or programmatic use is your risk to
     accept: write what you read into `--terms-note`.
  3. Check on the Console's Go page that **Extra Usage is OFF**. With it ON, Go bills your pay-as-you-go balance after
     a limit, and AEOS cannot see that setting. A call Go does bill beyond the plan refuses the route (below).
  4. Record a BUDGET decision for the **monthly subscription fee** ($10/month), one per route you activate. It covers
     the plan fee, not per-token spend. Usage inside the plan costs $0 and only uses up plan allowance:

     ```bash
     ./aeos policy decide --kind BUDGET --as <admin> --domain PERSONAL --target-type model_route --target-id opencode-go/glm-5.3-flash --days 30 --rationale "OpenCode Go subscription, \$10/month plan fee for the TRIAL"
     ```

     The command prints the decision id. In the UI the same decision is *Keputusan* → *Catat keputusan manusia*, with
     Jenis keputusan BUDGET and target `model_route` / the route key. In PowerShell, use single quotes
     (`'OpenCode Go subscription, $10/month plan fee for the TRIAL'`) or escape the dollar sign with a backtick
     (`` `$10 ``): inside double quotes PowerShell reads `$10` as a variable.
  5. Activate it as TRIAL with that BUDGET decision. The provider restricts regions, so state the region(s) your
     workspace allows: `--region EU-US` for the workspace default (Europe and United States). If you want one region
     only, restrict the workspace first (Console → Settings → Privacy → Regions), then state that one. Name the ToS
     automation-clause risk in the terms note. `--days 30` keeps the TRIAL as long as the BUDGET decision (a TRIAL
     lasts 14 days by default):
     `./aeos routes activate opencode-go/glm-5.3-flash --as <admin> --mode TRIAL --expected-hash <hash> --terms-reviewed --terms-note "Read Go docs, FAQ and ToS on <date>: ToS forbids automated/programmatic extraction and processes running while not logged in; I accept that risk for this TRIAL. Extra Usage OFF." --region EU-US --days 30 --budget-decision <id> --rationale "<why>"`.
     No client-terms flag: Go allows other clients, and passing one is refused with `CLIENT_TERMS_NOT_APPLICABLE`.
  6. Probe, before real work (see the warning above): `./aeos broker probe-route opencode-go/glm-5.3-flash` sends one
     synthetic PUBLIC request (E3 evidence). On a subscription route the probe uses allowance, not money.
  7. **Each billing cycle** (and before the TRIAL expires): record a new BUDGET decision (step 4) and run step 5
     again with `--budget-decision <new id>`. The TRIAL keeps the id of the BUDGET decision it was activated with, so
     a new BUDGET decision alone does not renew the route. See [1.15](#115-recurring-owner-actions).

  **What AEOS does with a Go route** (ADR-0080 amendment 2026-10-26):

  * **No cost limit per task.** A TaskSpec needs no `max_cost_usd`. The marginal cost of each run is $0, and the
    run's cost cap is $0.
  * **AEOS counts only its own calls.** Each call records the allowance it used (`usage.opencode.allowance`; estimated
    from the tokens, or from the request's upper bound when Go reports no usage). AEOS sums the calls made **through
    this AEOS instance** per route. Your own OpenCode with the same Go workspace, or the other Mac, is invisible to
    it, so Go can refuse requests before AEOS's count is used up. Go's own limit stays authoritative, and Extra Usage
    OFF stays your only guard against a bill.
  * **Windows.** 5 hours = 20%, week = 50%, month = 100% of the model's monthly allowance (glm-5.3-flash: $12 / $30 /
    $60). Only the 5-hour window blocks new tasks. The week and month windows are advisory (`WARN_EXHAUSTED`): AEOS
    cannot know when Go resets them, and with Extra Usage OFF going past them only gets refusals from Go, never a
    charge. A refusal from Go is RATE_LIMITED (a quota wait, no fallback to another route) only if Go answers HTTP
    429, which is NOT-PROVEN; any other status is UPSTREAM_ERROR and records no quota wait.
  * **Paid usage beyond the plan.** If Go reports a charge, or bills a call from another source than the plan
    (`billing_source` such as `balance`, i.e. Extra Usage ON), AEOS flags the call (`cost.subscription.beyond_plan`),
    refuses the run's next request and refuses the route for new tasks and probes. A reported charge also counts
    against your budgets.
  * **Admission codes and remedies:**

    | Code | Shown as | What to do |
    |---|---|---|
    | `SUBSCRIPTION_ALLOWANCE_EXHAUSTED` | WAITING_QUOTA | Nothing: the 5h window frees up as calls age out. |
    | `SUBSCRIPTION_ALLOWANCE_UNKNOWN` | WAITING_QUOTA | Nothing: a call in the 5h window could not be counted; it blocks for at most 5 hours. |
    | `SUBSCRIPTION_BEYOND_PLAN` | BLOCKED | Turn Extra Usage OFF on the Console's Go page, then activate the route again (step 5 with a valid BUDGET decision). A TRIAL newer than the flagged call clears it. If Go reported a charge, that charge also counts against the GLOBAL $0 hard stop: every route, free ones included, waits with BUDGET_EXCEEDED until the budget period ends or you record a GLOBAL budget that covers it (Biaya page); re-activation alone is not enough. |
    | `COST_UNKNOWN` | BLOCKED | The route's subscription terms are incomplete: run `./aeos seed` (it corrects an untouched CANDIDATE row) or report it. |
    | `ROUTE_INELIGIBLE` naming the budget decision or the TRIAL | BLOCKED | The BUDGET decision or the TRIAL expired: step 7. |

  * **Where to see it:** `./aeos cost summary --domain PERSONAL` lists "Worker subscriptions": the plan, the state and
    the allowance used, limit and remaining per window, the advisory windows marked. `GET /api/costs/summary`, the
    route view and the task admission answer (`GET /api/tasks/<id>/admission`) carry the same figures for a user who
    may see every domain. `./aeos routes list` only flags `billing=SUBSCRIPTION`, and the Control Center pages do not
    show the windows yet.
* **Free OpenRouter WORKER routes (decision C, ADR-0041 amendment 2026-10-26).** Free OpenRouter models (ids ending in
  `:free`) may serve worker tasks for **PUBLIC data only**, PERSONAL only. INTERNAL data stays on the OpenCode Go routes;
  CONFIDENTIAL never goes to a worker route. Other OpenRouter routes keep the rules above (a paid route needs a BUDGET
  decision); `set-pool` moves only `:free` routes. A free route ranks first for PUBLIC tasks. When the free tier is at its limit, the
  task waits (`FREE_TIER_LIMIT_REACHED`, shown as WAITING_QUOTA); it never moves to a Go route or to any paid route. Do
  these steps in order:
  1. **Credits (once, on openrouter.ai).** Buy at least $10 of credits. This raises the free-model daily cap of the
     account from 50 to 1000 requests. The per-minute cap stays 20.
  2. **Privacy (once, on openrouter.ai).** In the privacy settings, enable the free endpoints that may train on inputs.
     That is why these routes take PUBLIC data only. Write what you enabled into the terms notes below.
  3. **Daily cap in `aeos.yaml` [per machine].** Only after step 1. AEOS stops at 50 requests per UTC day until you
     raise it. The startup refuses a value above 1000:

     ```bash
     CFG="$HOME/Library/Application Support/DLY-AEOS/aeos.yaml"
     test -f "$CFG" || cp config/aeos.example.yaml "$CFG"
     grep -q '^openrouter_free_daily_limit:' "$CFG" || printf '\nopenrouter_free_daily_limit: 1000\n' >> "$CFG"
     grep '^openrouter_free_daily_limit:' "$CFG"
     ```

     Restart `./aeos serve`. The limits are per OpenRouter account. If both Macs run DLY AEOS with the same account,
     split the cap (for example 500 on each), because each machine counts only its own requests.
  4. **Key and catalog.** Store the key (1.8: `./aeos secrets set workers/openrouter`), then run `./aeos routes refresh`.
     The `openrouter_catalog` line lists newly `discovered:` free routes in ARENA. The key info also records the daily
     limit OpenRouter reports for the account; if it still reports 50, the lower value wins until the purchase shows.
  5. **G1, the ARENA TRIAL (evaluation only).** For each of the five routes
     `openrouter/cohere/north-mini-code:free`, `openrouter/poolside/laguna-xs-2.1:free`,
     `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`, `openrouter/nvidia/nemotron-3.5-lightning:free` and
     `openrouter/google/gemma-4-31b-it:free`, copy its hash from `./aeos routes list --pool ARENA` and run:

     ```bash
     ./aeos routes activate openrouter/cohere/north-mini-code:free --as <admin> --mode TRIAL --expected-hash <hash> --data-class PUBLIC --days 30 --terms-reviewed --terms-note "Read the OpenRouter model page and privacy settings on <date>: training-capable free endpoints enabled, PUBLIC data only" --rationale "G1: evaluate this free model in ARENA"
     ```

  6. **Decision C, the move into WORKER.** For each route you want as a worker, move it. The move is a new
     configuration: its ARENA TRIAL from step 5 stops applying, and it does not come back if you move the route back.
     Only a `:free` OpenRouter route moves (`ROUTE_POOL_MOVE_UNSUPPORTED` otherwise).

     ```bash
     ./aeos routes set-pool openrouter/cohere/north-mini-code:free --pool WORKER --as <admin> --rationale "Decision C: free worker tier for PUBLIC data"
     ./aeos routes list --pool WORKER
     ```

     The line of the route must show `decision-c=PUBLIC-ONLY`. Copy its new hash. A move is refused
     (`FREE_WORKER_ROUTE_INELIGIBLE`) for a catalog price that is not exactly zero, an ended free period, data classes
     other than PUBLIC alone, or an OFFICE domain.
  7. **Re-activate the WORKER configuration.** **A WORKER TRIAL is eligible for real PUBLIC worker tasks at once, ahead
     of the Go routes.** If you want the probe (step 8) first, pause scheduling around it
     (`./aeos work pause --reason "OpenRouter probe"`, then `./aeos work resume`).

     ```bash
     ./aeos routes activate openrouter/cohere/north-mini-code:free --as <admin> --mode TRIAL --expected-hash <new hash> --data-class PUBLIC --days 30 --terms-reviewed --terms-note "Decision C: OpenRouter free endpoints may train on inputs, PUBLIC data only, credits bought on <date>" --rationale "Decision C: first-tier worker for PUBLIC tasks"
     ```

     No BUDGET decision: the route is free. A `--data-class INTERNAL` is refused (`FREE_WORKER_ROUTE_INELIGIBLE`).
  8. **Probe (optional, one synthetic PUBLIC request, E3 evidence):** `./aeos broker probe-route openrouter/cohere/north-mini-code:free`.
  9. **Every 30 days** (before the TRIAL expires): run step 7 again with the hash `./aeos routes list --pool WORKER` shows.

  **What AEOS does with a free WORKER route:**

  * **Limits.** AEOS counts the requests it sent to `:free` models (workers, evaluations, every probe that reached
    OpenRouter): at most 20 in a minute and the daily cap per UTC day. A 429 from OpenRouter stops every free route of
    the account until its Retry-After or `X-RateLimit-Reset` (else one minute, or 00:00 UTC for the daily limit). A run
    stopped at a quota does not use up an attempt; the task runs again after the reset. Your own use of the same account
    elsewhere is invisible to AEOS, so OpenRouter can refuse earlier; its 429 stays authoritative and is never a
    success.
  * **No paid fallback.** A task never moves from the free route to a Go route or any other paid route. Over the limit
    it waits, even if a Go route has allowance left.
  * **Repricing.** `./aeos routes refresh` marks a repriced model `is_free=NO` (its TRIAL stops applying) and a dropped
    model MISSING. A call OpenRouter reports with a cost refuses the route until you re-activate it. The route keeps its
    WORKER pool; the catalog never moves it.
  * **Cost.** A free call is recorded at $0 with the token usage OpenRouter reports.
  * **Admission codes and remedies:**

    | Code | Shown as | What to do |
    |---|---|---|
    | `FREE_TIER_LIMIT_REACHED` | WAITING_QUOTA | Nothing: it clears after a minute, after the Retry-After or at 00:00 UTC. After you buy credits: step 3. |
    | `NEEDS_DECISION` naming "provider quota" | BLOCKED | The task was stopped at a quota 6 times since your last retry decision: it does not fit the limit. Retry it with a rationale (*Pekerjaan Live* -> retry) once the limit allows it: that allows 6 more quota stops and adds no attempts. Or plan it on another route. |
    | `FREE_WORKER_ROUTE_INELIGIBLE` | BLOCKED (broker 403) | The route is no longer free, or the task is not PUBLIC. Run `./aeos routes refresh` and check the route. Move a paid one back with `./aeos routes set-pool <route_key> --pool ARENA --as <admin> --rationale "<why>"` or `./aeos routes disable <route_key> --reason "<why>"`. |
    | `ROUTE_INELIGIBLE` naming the TRIAL | BLOCKED | The TRIAL expired or the configuration changed: step 7. |

  * **Where to see it:** the route view (`GET /api/routes/<route_key>`, field `free_tier`): limits, this machine's
    counts and the decision C status. `./aeos routes list` flags `decision-c=`.
* **Prompt revision change:** a route approved under an older worker prompt revision stops with
  `PROMPT_REVISION_CHANGED`. Approve it again.

Then set this machine's resource budget and enable the worker agent:

`agent-prereqs` is repeated until it reports READY; `doctor -c CAN_RUN_WORKER` must end READY.

```bash
./aeos doctor budget --cpu <n> --ram <GiB> --disk <GiB> --max-workers <n> --reason "<measured on this Mac>"
./aeos work agent-prereqs opencode-implementer --domain PERSONAL
./aeos work agent-enable opencode-implementer --as <admin> --domain PERSONAL --reason "<why>"
./aeos doctor -c CAN_RUN_WORKER
```

The M4 and the i9 have different hardware, so measure each one. In the UI the agent controls are on *Organisasi &
Peran* → agent card → *Periksa prasyarat* / *Aktifkan*.

### 1.12 Approve the pins [once per platform, through a reviewed PR]

**Status.** Reviewed on `darwin_arm64` (the M4), 2026-10-27, under the owner decision of 2026-10-27 delegated as
D13. The `approved_pin` values (`openbao` 2.7.1, `opencode` 1.19.3) have no per-platform form, so they apply on every
platform, Windows and the i9 included. Digests and signers are per platform: a digest per `<os>_<arch>`, a signer
per OS. The `claude-code` `darwin` signer `Q6L2SF6YDW` is recorded (read on the M4; the i9 confirms it with the
`codesign` line below). No digest is recorded yet: the `darwin_arm64` OpenBao digest is deferred (below), and the
`windows` signer and every `windows_amd64` and `darwin_amd64` value stay NOT_RECORDED until those machines.

**Rule: pin what protects without blocking a routine update.** Pin a value only when drift from it is caught without
blocking a capability on a routine update, or when it records the pin the runtime already holds (so it adds no new
block). `tests/unit/test_inventory_pins.py` checks this rule.

| Component | Pin | Why |
|---|---|---|
| `claude-code` | publisher only: `signature.approved_signer` `darwin: Q6L2SF6YDW` | Claude Code updates itself often, so a version or digest pin would drift on every update. The publisher pin survives updates, and an unsigned or foreign-signed binary is BLOCKED (`claude.binary`). |
| `opencode` | `approved_pin: "1.19.3"`, equal to the worker runtime default (`PINNED_VERSION`) | The runtime already holds 1.19.3, and drift from it fails `worker.opencode`, which blocks `CAN_RUN_WORKER` (master §38), with or without the manifest pin. So the pin changes no runtime behaviour. It makes the pin human-approved (master §07): *Tools & Repos* shows MATCH or MISMATCH and a version drift finding instead of NO_PIN or UNKNOWN. OpenCode updates itself, so turn its self-update off on each machine (below). `tool_schema_hash`: none observed yet. |
| `openbao` | `approved_pin: "2.7.1"` | `aeos vault install` installs it, and it never updates itself. Drift is a finding, not a block. |
| `git`, `nodejs-runtime`, `python-runtime` | none | `approved_pin` applies on every platform, but these versions differ per platform. The OS or package manager ships their security updates. |

**OpenCode self-update.** A self-update of the host OpenCode (for example after you run `opencode` yourself) moves
it off 1.19.3, and `./aeos doctor -c CAN_RUN_WORKER` then reports `OPENCODE_PIN_DRIFT` and BLOCKED. AEOS already runs
the worker with self-update off. Turn it off for your own shell too, once per machine (on Windows, see section 5):

```zsh
grep -qx 'export OPENCODE_DISABLE_AUTOUPDATE=1' ~/.zshrc || echo 'export OPENCODE_DISABLE_AUTOUPDATE=1' >> ~/.zshrc
source ~/.zshrc
opencode --version
```

`opencode --version` must print 1.19.3. If it does not, run `npm install -g opencode-ai@1.19.3` (1.9). Move to a new
OpenCode version only through a reviewed PR that changes the pin (the manifest `approved_pin` and the runtime
`PINNED_VERSION` together) after the worker contract test passes on it.

The `openbao` digest for `darwin_arm64` is
`sha256:50c3588e64271171cc2147bddeace8ec2b6742f94fd3fe53baa60f6d938cda0c`, but it is not recorded yet. A digest pin
makes every OpenBao change proposal on that platform need a candidate digest, and the trusted acceptance suite
(`tests/acceptance/test_governance.py`, a protected path) does not supply one. Record the digest in the same reviewed
PR that updates that test.

To collect the values on another platform:

For `claude-code` note `versions.observed_version` and `versions.observed_digest`; for `opencode` also the
contract test's `tool_schema_hash` once it records one (none observed yet). The `codesign` line shows the macOS
signer of claude-code.

```bash
./aeos inventory reconcile
./aeos inventory show claude-code --json
./aeos inventory show opencode --json
./aeos inventory show openbao --json
./aeos inventory show git --json
./aeos inventory show python-runtime --json
./aeos inventory show nodejs-runtime --json
codesign -dv --verbose=2 "$(which claude)" 2>&1 | grep TeamIdentifier
```

Then make the change in `install-manifest/manifest.yaml` through a reviewed PR (see
[install-manifest/README.md](../install-manifest/README.md)):

* `approved_pin` is the exact version, never `latest`.
* `approved_digest` holds one `sha256:` value per platform: `{windows_amd64: ..., darwin_arm64: ..., darwin_amd64: ...}`.
  The M4 gives `darwin_arm64` and the i9 gives `darwin_amd64`. A platform without an entry shows NOT_RECORDED.
* `claude-code` `signature.approved_signer` is `{windows: <Authenticode subject>, darwin: <TeamIdentifier>}`. After
  that pin, an unsigned or foreign-signed Claude binary is BLOCKED on that OS.
* `opencode` gets `tool_schema_hash` only after the worker contract test records one, and only while drift from it
  blocks nothing.
* The top-level `reviewed_by` names you.

After the merge, on each machine:

```bash
git pull --ff-only
./aeos seed --only aeos.inventory.seed
./aeos inventory reconcile
```

*Tools & Repos* (`/tools`) → `openbao` and `opencode` must show the approved pin instead of "pin belum disetujui
(UNKNOWN)", with no `resolved-version-mismatch` drift. `claude-code` keeps "UNKNOWN" for its version by design, and its authenticity
is OK (`./aeos doctor -c CAN_PLAN_PERSONAL`). When the pinned OpenCode version changes, rebuild the worker image (1.6) so
it carries the new `aeos.opencode_version` label.

### 1.13 First plan approval, first run, first publication [primary machine]

`plan status` must show READY_FOR_HUMAN_REVIEW, bridge proof PROVEN and four verified stage runs.

```bash
./aeos plan create --project dly-aeos --domain PERSONAL --title "<title>" --prompt-file <idea.md> --actor <admin>
./aeos plan run <plan_id> --actor <admin>
./aeos plan status <plan_id>
```

1. **Approve.** On *Rencana* (`/plans`) → plan, review the version, then press *Approve*. Press *Approve &
   Publish* instead if you want targets Google Drive/Docs and GitHub Issues.
2. **Execute.** On *Rencana* → plan → *Jalankan rencana*, or
   `./aeos work execute <plan_id> --as <admin> --domain PERSONAL --reason "<why>"`.
3. **Run.** On *Pekerjaan Live* (`/work`) → *Aktifkan loop scheduler*, or `./aeos work run-once`. Then run
   `./aeos verify run <run_id>` and `./aeos verify review <run_id>`, which is the native checkpoint review.
4. **Publish.** On *Dokumen & Publikasi* (`/publications`), review the preview, tick the confirmation and
   execute. Then run `./aeos publish list` and `./aeos publish show <publication_id>`: every operation must be
   SUCCEEDED with an external id. The DOCX fidelity verdict is in
   `http://127.0.0.1:8787/api/publications/<publication_id>` (`operations[].response.fidelity`).
5. **M2 pilot evidence.** Edit the published Google Doc by hand, approve a new plan version and publish again.
   That document must show CONFLICT in `./aeos publish show <new id>` and must not be overwritten. For an
   UNKNOWN or FAILED operation, run `./aeos publish reconcile <id>`, then `./aeos publish run <id>`. Keep the
   ids and the `publish show` output for `docs/evidence/`.
6. **Optional live fidelity test.**
   `DLY_AEOS_NETWORK_TESTS=1 DLY_AEOS_GOOGLE_FIDELITY_FOLDER=<AEOS-created folder id> .venv/bin/python -m pytest tests/integration/test_google_docx_fidelity_live.py -q -s -p no:cacheprovider`.
7. **First broker-created PR.** On GitHub, open the PR's Actions run → job step "Set up job" → "GITHUB_TOKEN
   Permissions". It must list read access only. Settings → Secrets and variables → Actions must list no secret
   the CI could expose.

### 1.14 Backup, restore test, first release [per machine]

Note the `backup_id` from `backup list`. `doctor -g RECOVERY` must show `recovery.restore_test` VERIFIED.

```bash
./aeos ops backup create --label first
./aeos ops backup list
./aeos ops backup restore-test <backup_id>
./aeos doctor -g RECOVERY
```

The same controls are on *Riwayat & Pemulihan* (`/history`) → *Backup & pemulihan*. Copy one restore-tested
backup to encrypted external storage. Backups exclude OpenBao data and the Keychain: the offline recovery copy of
the unseal key from 1.7 is your recovery path for those.

Optional but needed for `recovery.config_rollback`: promote the first controller release
([runbook](runbooks/controller-upgrade-rollback.md)).

The first `release build` answers RELEASE_NOT_ACCEPTED when no trusted CI PASS is recorded for the commit; the
`release accept` line records it. Note the `release_id` printed by the second build.

```bash
(cd frontend && npm ci && npm run build)
./aeos ops release build --ref HEAD
./aeos ops release accept --commit "$(git rev-parse HEAD)" --operator <admin> --reason "GitHub Actions CI green on main for this commit"
./aeos ops release build --ref HEAD
./aeos ops release promote <release_id> --operator <admin> --reason "first release"
```

AEOS never creates a GitHub tag or Release. To publish one, use
`gh release create v0.1.0 --target <commit> --title "DLY AEOS 0.1.0" --notes-file docs/releases/v0.1.0.md`.

### 1.15 Recurring owner actions

| When | Action |
|---|---|
| After every reboot | `./aeos vault start`, then `./aeos vault unseal` |
| Every 7 days and after every Claude Code upgrade | `./aeos doctor --functional` (Claude capability test) |
| Every 7 days and after every image rebuild | `./aeos worker containment --kind container` (and `--domain OFFICE` if used) |
| Weekly, while the Google app is in "Testing" | `./aeos publish google-connect --client-secret <file>` |
| Before every upgrade, and at a fixed interval | `./aeos ops backup create`, then `./aeos ops backup restore-test <id>` |
| When the Doctor warns that the GitHub token expires (14 days ahead) | create a new fine-grained PAT with the same permissions (1.8), then `./aeos secrets set personal/github --expires-at <YYYY-MM-DD>` |
| When Claude Code reports a usage or weekly limit | at once, save `./aeos bridge status` and `./aeos cost summary --domain PERSONAL` (no account email) for `docs/evidence/claude-quota-<date>.txt`; after the reset, quota must read UNKNOWN, not OK. Never provoke a limit with paid usage on |
| When an integrity hold is raised | review the findings on *Pekerjaan Live* (or `./aeos worker status`), restore the checkout and data, then *Catat rekonsiliasi*. Each hold needs its own reconciliation |
| After a process-sandbox exception is no longer needed | *Keputusan* → *Cabut* ([2.7](#27-process-sandbox-exception-per-machine)) |
| Each OpenCode Go billing cycle, and before a Go TRIAL expires | a new BUDGET decision, then `./aeos routes activate <route> ... --days 30 --budget-decision <new id>` ([1.11](#111-route-activation-budget-and-the-worker-agent-per-machine-decision) step 7) |
| Before a free OpenRouter WORKER TRIAL expires (30 days) | `./aeos routes refresh`, then `./aeos routes activate <route> ... --mode TRIAL --data-class PUBLIC --days 30` with the hash `./aeos routes list --pool WORKER` shows ([1.11](#111-route-activation-budget-and-the-worker-agent-per-machine-decision), free OpenRouter WORKER routes, step 9) |

---

## 2. Owner decisions still UNDECIDED

Each decision has a "Record:" line that says where it goes. Until you record it, the product shows it as
UNDECIDED (or NO_TARGET, RETAIN, REPORT_ONLY) and fails closed.

### 2.1 SLO/SLI targets [per machine]

Every SLI shows NO_TARGET until you set one. Choose targets per project criticality.

Record:

```bash
./aeos ops sli criticality <project_key> HIGH --rationale "<why>"
./aeos ops sli target queue_age_seconds --max <seconds> --rationale "<why>"
./aeos ops sli target stale_leases --max 0 --rationale "<why>"
./aeos ops sli target recovery_point_age_seconds --max <seconds> --rationale "<why>"
./aeos ops sli target sync_freshness_seconds --max <seconds> --rationale "<why>"
./aeos ops sli budget-policy --window-days 28 --objective <objective> --action alert --rationale "<why>"
./aeos ops sli --domain PERSONAL --raise-alerts
```

The SLI keys are listed in [OPERATIONS.md](OPERATIONS.md#slis-and-alerts). Results are on *Riwayat & Pemulihan* →
*SLI, alert & flag*.

### 2.2 Retention durations [once]

Every `retain_days` in `policy/retention.yaml` is `null`, so everything is retained and nothing is ever reported as
expired. Decide days per data class (`audit_events`, `evidence`, `attachments`, `external_references`, `backups`)
and per domain. OFFICE values follow company policy.

Record: replace `null` with days in `policy/retention.yaml` through a reviewed PR (protected path). If a promoted
release is ACTIVE, the new values apply after the next release build and promote. Check the result on *Riwayat &
Pemulihan* → *Retensi*, and on *Bukti* → *Ledger bukti* (Retensi column).

### 2.3 Supply-chain licence and threshold policy [once]

`policy/supply-chain.yaml` is `status: UNDECIDED`, so the SBOM, licence, vulnerability and SAST reports are
REPORT_ONLY (`[NO-GATE]`).

Record, through a reviewed PR:

* In `policy/supply-chain.yaml`, set `licenses.allow`, `vulnerabilities.python/npm.fail_at_or_above`,
  `sast.bandit.fail_at_or_above` and `min_confidence`, `status: DECIDED` and a `decision_ref`.
* Accept [ADR-0095](decisions/ADR-0095-ci-gates-supply-chain-and-security-tooling.md) ([2.8](#28-adr-registrations-of-proposed-adrs-per-machine)).
* Change the CI job tag `supply-chain report [NO-GATE]` to G1.

### 2.4 Staging/production environments and rollback criteria [per machine]

For each PERSONAL project, decide whether it has a staging and a production environment, and if so, the exact
target.

Record: *Portofolio* → project → *Integrasi* → "Target deployment: pemeriksaan, observasi & keputusan
lingkungan (G6/G8)". Record the environment decision there: staging AUTHORISED with the exact target URL, or
staging NONE / production NONE for a project without that environment. Then set the target checks with *Ubah
pemeriksaan target*: health path, smoke checks, SLI thresholds, *Kriteria rollback* (`rollback_on`) and the
artifact-digest requirement.

CLI alternative for the checks:
`./aeos delivery target-checks <project_id> --environment production --file <checks.json>`, where the JSON holds
`health_path`, `smoke_checks`, `sli_checks`, `rollback_on` and `require_artifact_digest`. Until you record these,
the UI shows UNDECIDED / NO_TARGET and G6/G8 stay NOT_PROVEN. Registering a real target is in
[section 4](#4-external-items-company-tenant-provider).

### 2.5 `worker.route_behaviour` failure-rate threshold [per machine]

Record: in a PERSONAL session, *Kesiapan (Doctor)* → check `worker.route_behaviour` → *Tetapkan ambang
pemilik*. The form shows the exact `max_failure_rate` before you confirm. CLI alternative, the same POLICY decision
(target `worker_route_behaviour:tool_schema_failures`); it prints the exact value before it asks for your password:

The value is between 0 and 1 (0.05 = 5%).

```bash
./aeos policy route-threshold --max-failure-rate 0.05 --domain PERSONAL --as <admin> --rationale "<why>"
```

A newer decision replaces the threshold. It never makes a route pass without trusted evaluation.

### 2.6 Authenticated screenshot capture [per machine]

Decide whether the screenshot collector may make an authenticated, read-only capture of pages behind a login,
such as the Control Center's own routes. The status (UNDECIDED until you decide) is shown on *Bukti* →
*Tangkapan layar UI*.

Record: *Bukti* → *Tangkapan layar UI* → *Catat keputusan pemilik (ALLOWED/DENIED)*, which opens *Keputusan* on the
preset "Capture screenshot terautentikasi read-only". Choose ALLOWED with the scope it covers (for example "Control
Center routes, read-only"), or DENIED. Or run:

```bash
./aeos policy decide --kind POLICY --authenticated-capture ALLOWED --capture-scope "<pages it covers>" --domain PERSONAL --as <admin> --rationale "<why>"
./aeos policy decide --kind POLICY --authenticated-capture DENIED --domain PERSONAL --as <admin> --rationale "<why>"
```

It is PERSONAL only, and `--days` is optional. Either way the collector stays unauthenticated in this build:
authenticated capture is not built yet, so ALLOWED records your decision and the status says so (capture mode
"tanpa login"). Until it is built, only a target's public pages can become SCREENSHOT evidence. UNDECIDED and DENIED
never log in.

### 2.7 Process-sandbox exception [per machine]

Recommended: do not record it on a machine where the container proof (1.6) passes. The process sandbox is WEAK.
Candidate code runs as your OS user and can reach the OS keyring and the data directory, and there is no egress
enforcement. It is never allowed for OFFICE, shell or egress work.

Record, only if a machine has no Docker: *Keputusan* → *Catat keputusan manusia* → preset "Worker di process
sandbox (WEAK) untuk PERSONAL". Or run:

```bash
./aeos policy decide --kind EXCEPTION --exception worker_sandbox_process --no-allow-bash --days 7 --domain PERSONAL --as <admin> --rationale "<why>"
```

To revoke it: *Keputusan* → highlighted EXCEPTION row → *Cabut*, or
`./aeos policy revoke <decision_id> --as <admin> --reason "container sandbox proven"`.

### 2.8 ADR registrations of PROPOSED ADRs [per machine]

These ADRs are PROPOSED: [0004](decisions/ADR-0004-first-admin-bootstrap-hardening.md),
[0043](decisions/ADR-0043-openbao-direct-approle-instead-of-agent.md),
[0070](decisions/ADR-0070-publication-outbox-idempotency-reconciliation.md),
[0071](decisions/ADR-0071-product1-document-model-and-delta-rendering.md),
[0072](decisions/ADR-0072-google-drive-file-scope-docx-import.md),
[0073](decisions/ADR-0073-office-atlassian-adapters-disabled-by-default.md),
[0074](decisions/ADR-0074-publication-approval-decision-binding.md),
[0075](decisions/ADR-0075-diagram-contract-references-and-checkpoint-handoff.md),
[0095](decisions/ADR-0095-ci-gates-supply-chain-and-security-tooling.md),
[0106](decisions/ADR-0106-verification-delivery-broker-audit-closure.md),
[0107](decisions/ADR-0107-verifier-isolated-runner.md),
[0111](decisions/ADR-0111-office-code-delivery-and-route-feasibility.md),
[0114](decisions/ADR-0114-platform-docs-readiness-and-evidence-producers.md) and
[0120](decisions/ADR-0120-verifier-impact-selected-steps.md). The full list is in
[docs/decisions/README.md](decisions/README.md).

Record:

The outcome is ACCEPTED, or REJECTED / SUPERSEDED (with `--superseded-by ADR-NNNN`).

```bash
./aeos policy adrs --load
./aeos policy adr-decide ADR-0004 --outcome ACCEPTED --as <admin> --rationale "<why>"
```

Or use *Keputusan* → *Register ADR*. Your decision is bound to the ADR's content hash: a later edit of the file
makes it STALE until you decide again. Two of these ADRs carry larger choices:

* ADR-0043 is direct AppRole instead of OpenBao Agent. Rejecting it means Agent mode has to be built.
* ADR-0107 decides whether the container is required for PERSONAL.

### 2.9 End of the ADR-0003 seed-build delegation [per machine]

The delegation lets the builder commit, push and merge on the platform repository while DLY AEOS is being built.
When you judge the build done (M4 delivery enforcement reached), revoke it.

Record: open *Keputusan* (`/decisions`) with PERSONAL selected. The queue item "Delegasi owner:ADR-0003 masih
aktif" links to the EXCEPTION row (actor `owner:ADR-0003`). Press *Cabut* with a reason such as "seed reached M4".

CLI alternative: find the id with `./aeos policy decisions --kind EXCEPTION --status ACTIVE` (each row starts with
the decision id; the delegation row shows `actor=owner:ADR-0003`), then run
`./aeos policy revoke <decision_id> --as <admin> --reason "seed reached M4"`. A re-seed does not re-create a
revoked delegation. A REJECTED or SUPERSEDED decision on ADR-0003 (2.8) also revokes it.

### 2.10 Claude extra usage disabled [per machine]

No official API reports this account setting, so `claude.extra_billing` stays UNKNOWN by design. First, on
claude.ai with the Max account, open Settings → Usage and switch paid extra usage off, with no auto-reload.

Record the attestation: *Keputusan* → preset "Atestasi pemilik: extra usage berbayar Claude NONAKTIF di pengaturan
akun claude.ai". Or run:

```bash
./aeos policy decide --kind POLICY --policy-preset claude_extra_usage_off --domain PERSONAL --as <admin> --days <N> --rationale "<what you checked>"
```

It is shown as an owner attestation (E0), never VERIFIED.

### 2.11 MCI source location [per machine, plus once]

The canonical MCI_V2 is owner-supplied and never lives in the repository, which is public. Decide whether you
are entitled to keep a copy on each machine and where.

Record, on each machine:

1. Put the file at `~/Library/Application Support/DLY-AEOS/policy/canonical/MCI_V2_SOURCE_OF_TRUTH.md`. Or set an
   absolute `mci_source_path` outside the checkout in `aeos.yaml`. A path inside the repository is refused.
2. Hash it: `echo "sha256:$(shasum -a 256 '<path>' | cut -d' ' -f1)"`.

Then, once, through a reviewed PR: in `policy/mci-reference.yaml` set `status: VERIFIED`, `version`,
`content_hash`, `location: local:MCI_V2_SOURCE_OF_TRUTH.md` and `verified_at`. Every machine must hold the
byte-identical file.

`doctor -g CANONICAL_POLICY` must show `policy.mci` CONFIGURED; compliance stays NOT-PROVEN.

```bash
./aeos policy snapshot --refresh
./aeos doctor -g CANONICAL_POLICY
```

After that, three follow-up decisions open up, all from MCI_V2 only:

* **Pack mapping.** Copy `blueprints/mci-pack-mapping.template.yaml` to
  `<data dir>/policy/canonical/mci-pack-mapping.yaml`, fill it from MCI_V2 only and never commit the filled copy.
  Approve each pack with
  `./aeos policy decide --kind POLICY --target-type mci_pack_mapping --target-id <PACK> --target-hash <binding hash shown on /decisions> --domain PERSONAL --as <admin> --rationale "<why>"`.
* **Risk classes.** Decide which MCI thresholds map to R0-R4, as a POLICY decision.
* **Off-host audit store.** Decide whether MCI requires a WORM or off-host audit store. The builder records it as
  an ADR.

### 2.12 Smaller open decisions

Each item below is recorded either as an ADR, through the builder, or with the command given.

* **Free OpenCode Zen routes** (ADR-0041 amendment 2026-10-25). The provider documents "Free-tier chat models
  require OpenCode; use paid models or Go from other clients", and the AEOS broker is its own client.
  **Decided 2026-10-26: option A, OpenCode Go** (ADR-0041 amendment 2026-10-26). The `opencode-go/*` routes are
  seeded as CANDIDATE. Activate them through [1.8](#18-secrets-openrouter-and-the-github-token-per-machine) and
  [1.11](#111-route-activation-budget-and-the-worker-agent-per-machine-decision). The options were:
  * (A) OpenCode Go. **Chosen.** Still yours to do: the Go key, the terms review including the ToS automation
    clause, the region, a BUDGET decision and Extra Usage OFF.
  * (B) A paid Zen route: a BUDGET decision, then `./aeos routes activate ... --budget-decision <id>`.
  * (C) Another route. Correction: the seeded OpenRouter route is ARENA and the local route is LOCAL. Only the WORKER
    pool serves worker tasks through the broker, so neither one serves worker tasks in this build. They can only be
    probed (LOCAL through `./aeos routes set-endpoint <local route_key> --endpoint http://127.0.0.1:<port>/v1 --as <admin> --rationale "<why>"`).
  * (D) Proxy anyway: `./aeos routes activate ... --accept-client-terms --client-terms-note "<what you read>"`. Still
    possible per free Zen route. Decision A leaves the Zen client restriction UNDECIDED.
* **Supervisor model.** If Fable resolves to another family (1.4), choose another supervisor, or pin a stage
  model with
  `./aeos policy decide --kind POLICY --stage-model <STAGE>=<exact-model-id> --eval-run <id> --rollback-target <old> --domain PERSONAL --as <admin> --rationale "<why>"`,
  or on *Keputusan* with the preset that starts "Pin model supervisor per tahap perencanaan". Also decide whether a
  supervisor version change must stop planning until it is evaluated.
* **CAN_PLAN_PERSONAL = UNKNOWN.** Decide whether the bridge may still run automatically, or must use the manual
  package. Record it as an ADR. Until then, run `./aeos doctor --functional` before planning.
* **CAN_PUBLISH_PERSONAL.** Decide whether to adopt full READY enforcement (ADR-0094, open item). Until then, run
  `./aeos doctor --functional -c CAN_PUBLISH_PERSONAL` before each publication.
* **NO_TRUSTED_SUITE.** Decide whether admission must refuse a non-platform project without a trusted suite. Or
  register suites (section 3).
* **Role eligibility.** Decide whether a role becomes eligible only after a PASS evaluation:
  * Adopt a role's PASS run with `./aeos work adopt-role <role_key> --eval-run <id> --as <admin> --rationale "<why>" --option "<a>" --option "<b>" --goal "<goal>" --tradeoff "<tradeoff>" --invalidate-if "<condition>"`,
    or on *Organisasi & Peran*.
  * Tighten the default R4 pilot threshold on *Organisasi & Peran* → "Ambang evaluasi peran (§46)" → *Ubah ambang
    evaluasi peran*, which shows the risk classes it covers before you confirm, or with
    `./aeos work role-eval-threshold R2 --as <admin> --rationale "<why>"`. R2 means R2, R3 and R4 tasks need an
    adopted PASS evaluation or an owner EXCEPTION.
* **Memory acceptance.** Decide whether memory acceptance must require EVALUATED. Author the memory fixture suite
  first (ADR-0060, ADR-0081).
* **Verifier coverage:**
  * Which planner-compiled tasks must run integration (real-tool) tests.
  * The ASVS level for the applications AEOS builds, per data class.
  * Which high-trust black-box checks a separate trusted process should run.
* **`requirements.lock` hashes.** Approve regenerating the lock with `--generate-hashes` (protected file). Without
  it, the platform's own dependency changes keep `dependency_env` NOT_RUN.
* **Governance handoff.** Say whether a downstream governance harness or pipeline exists that AEOS must hand off
  to. If none exists, an ADR makes NOT_CONFIGURED the accepted state.
* **Earlier project.** Tell the builder which repository, branch or archive, if any, is the earlier DLY AEOS
  (`gh repo list <your-login> --limit 50`). The builder records a read-only inventory with
  `./aeos project inventory <project_key> --domain PERSONAL`. If none exists, say so and ADR-0001 records it.
* **Earlier specification.** Say whether alignment with the earlier Phase-1 and gateway specifications matters.
  The repository is public, so commit a sanitised copy only if you may publish it. If any credential example in it
  was ever real, rotate it first.
* **GitHub profile (optional).** Contributions on the public repository count once commits land on `main` with
  your verified or noreply address.

---

## 3. Catalog activation (master §45)

The catalog is a design watchlist, not a list of installed components. Status comes from the observed inventory:
`./aeos inventory list --requirement REQUIRED|OPTIONAL|CANDIDATE|REFERENCE_ONLY`, or *Tools & Repos* (`/tools`).

### 3.1 Core components must be active

After section 1, run `./aeos inventory reconcile`, then `./aeos inventory list --requirement REQUIRED`. Every
REQUIRED row should be installed, connected and functional. That includes Claude Code, OpenCode, OpenBao, the OS
credential store, Git, Python, Node.js, VS Code, GitHub, GitHub Issues, GitHub Actions, Google Drive and Google Docs.
The exceptions are the OFFICE integrations (Bitbucket, Bitbucket Pipelines, Jira, Confluence), which stay disabled
until section 4. A
REQUIRED row that is not active points back to its step in section 1.

### 3.2 Token-saving candidates: TRIAL, measured benefit, then ACCEPTED

Serena, Context7, RTK, ccusage, Promptfoo, ToolHive, Hermes and Langfuse/OpenTelemetry are adopted only through
the Usulan Upgrade flow. Nothing is installed or promoted automatically. Go through them one at a time. A sensible
order: ccusage first (a measurement baseline), then Serena and Context7, RTK, Promptfoo, ToolHive (only once an MCP
server is adopted), Hermes, and Langfuse/OpenTelemetry. Langfuse and OpenTelemetry send traces off the machine,
so choose a trace destination, and for OFFICE get company approval.

1. **Propose.** Open *Tools & Repos* → component (for example `serena`) → *Usulkan perubahan*, which opens
   `/proposals?component=serena`. Or open *Usulan Upgrade* (`/proposals`) → *Usulkan perubahan* with kind ADD.
   Or open *Riset* (`/research`) → entry → *Usulkan*.
2. **Review package.** On the proposal → *Buat paket review*.
3. **Benefit criteria, before the trial.** In "Rencana pemilik (ditetapkan sebelum trial, terikat candidate
   hash)", add criteria with metric, threshold and baseline. Examples: tokens per accepted task, cost per
   accepted outcome, accepted outcomes, review time. Then press *Simpan rencana (review ulang otomatis)*.
4. **Trial decision.** In "Keputusan manusia yang diperlukan", choose *Setujui TRIAL terisolasi (bukan promosi)*
   and press *Catat keputusan terikat hash*.
5. **Trial.** In "Trial terisolasi (di bawah keputusan TRIAL)", press *Luncurkan trial*. Run `./aeos work run-once`
   and `./aeos verify run <run_id>`, then press *Lampirkan bukti trial*. For a baseline-versus-candidate
   measurement, plan runs on *Evaluasi Model* → *Rencanakan run evaluasi*. Then run
   `./aeos eval start <run_id> --as <admin>`, bind tasks before executing them with
   `./aeos eval bind <run_id> <slot_id> <task_id> --as <admin>`, and finish with
   `./aeos eval collect <run_id> --as <admin>` and `./aeos eval finish <run_id> --reason "<why>" --as <admin>`.
   Use at least 3 repetitions. A two-worker topology needs
   `./aeos work experiment-grant <eval_run> --as <admin> --domain PERSONAL --days <1-30> --rationale "<why>"`,
   or *Pekerjaan Live* → *Beri hibah eksperimen*.
6. **Checkpoint.** In "Checkpoint Claude atas bukti trial (sebelum PROMOTE)", press *Minta checkpoint Claude*. Or
   use *Buat paket manual*, or *Catat NOT-PROVEN secara eksplisit*.
7. **Decision.** If the measured benefit met the criteria, choose *Setujui PROMOSI (keputusan terpisah dari
   trial)*. If not, choose *Pertahankan baseline (KEEP)* or *Tolak (REJECT)*.
8. **Accept.** Merge the reviewed `install-manifest` PR (requirement and `approved_pin`). Then run
   `./aeos seed --only aeos.inventory.seed` and `./aeos inventory reconcile`. Link the ADR and the publication
   under "Catatan tertaut". Finally, in "Hasil setelah implementasi", press *Catat hasil* with *ACCEPTED —
   diterima*. Finalisation needs inventory DONE and docs PUBLISHED.

Read-only CLI: `./aeos proposal list --domain PERSONAL` and `./aeos proposal show <proposal_id> --domain PERSONAL`.
Creating and deciding proposals is UI-only. The proposal timeline is the M3 exit evidence.

For ccusage, install a version you reviewed with `npm install -g ccusage@<version>`. Then open *Biaya & Kuota*
(`/costs`) → *Jalankan estimator ccusage (ESTIMATE)*. The result is an estimate, never an invoice or a quota.

Trusted suites for evaluation: register each project's suite on *Portofolio* → project → *Suite tepercaya*, or with
`./aeos verify suite-register <project_id> --as <admin> --domain PERSONAL --rationale "<why>" --commit <reviewed sha>`.
Check it with `./aeos verify suite-status <project_id> --domain PERSONAL`. Role evaluation cases come with
`./aeos seed`, and their fixtures are yours to author (ADR-0060).

### 3.3 References stay catalogued

Paperclip, OpenAI Symphony, Ralph, MetaGPT, ChatDev, CrewAI, Spec Kit, OpenSpec, BMAD and GSD are REFERENCE_ONLY.
They stay in the catalog and are not installed. If a proposal is opened for one, choose *Simpan sebagai referensi
saja*.

### 3.4 Stays unused (§45)

* **9Router:** routes based on session or subscription credentials are not used.
* **Infisical:** an alternative vault; OpenBao is the decided one.
* **Docker MCP Gateway:** an alternative to ToolHive, not a second gateway authority.
* **DDorchestrator:** its repository identity is UNVERIFIED.

You need to do nothing here. Do not open proposals for them. If one appears, choose *Tolak (REJECT)* or *Simpan
sebagai referensi saja*.

---

## 4. External items (company, tenant, provider)

These items stay open after sections 1 to 3. The software is complete and fails closed: OFFICE stays BLOCKED and
G6/G8 stay NOT_PROVEN. Below is what to prepare for each one.

### 4.1 Company authorisation for OFFICE

Get a written authorisation from the company, with a reference number. It should name:

* whether personal Claude Max may process office data;
* the approved external model routes;
* the allowed data classes (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED);
* the trace destinations;
* the exact tenant and project targets;
* the company-issued identities;
* the OFFICE environments (staging, production), the approved deploy mechanism, who may trigger it, and the
  read-only health endpoint AEOS may call.

Record it in an OFFICE session: *Keputusan* → *Catat keputusan manusia* → Jenis keputusan POLICY. Tick "Ini
otorisasi perusahaan untuk OFFICE", enter the company reference, tick the providers, and put only the approved
keys in the OFFICE override. Or run:

```bash
./aeos policy decide --kind POLICY --domain OFFICE --office-authorization --company-reference "<ref>" --allowed-provider bitbucket --allowed-provider jira --allowed-provider confluence --approved-target 'bitbucket:workspace=<ws>,root_id=<ws>/<repo>' --approved-data-class <CLASS> --approved-identity office/atlassian=<account id> --office-policy atlassian_deployment=CLOUD --as <admin> --rationale "<what the company approved>"
```

Keep the single quotes around values with commas: Windows PowerShell splits them otherwise. Once the
authorisation exists, OFFICE targets, data classes and identities fail closed until they are recorded.

Next, make each approved route OFFICE-eligible with
`./aeos policy office-route <route_key> --as <admin> --credential-ref office/<provider> --rationale "<why>"`, then
activate it with only the approved data classes:
`./aeos routes activate <route_key> --as <admin> --expected-hash <hash> --data-class <CLASS> --terms-reviewed --terms-note "<what you read>" --rationale "<why>"`.
The OFFICE container profile must pass on that machine (1.6).

The deploy mechanism needs a reviewed adapter contract (an ADR, milestone M5). Hand the description over to the
builder: environments, pipeline, triggers and consumers, with no credentials. Until then the project page shows
"Belum tersedia (M5)".

### 4.2 Atlassian and Bitbucket tenant

* **Deployment type.** Ask company IT which Atlassian deployment is in use. Cloud is `<site>.atlassian.net`.
  For Cloud, record `atlassian_deployment=CLOUD` (4.1); no adapter needs to be built. Data Center has no adapter:
  record it and commission one, tested against a DC instance the company authorises.
* **Secrets.** With OpenBao running, store `./aeos secrets set office/atlassian` (JSON `{"email": ..., "api_token": ...}`
  at the hidden prompt) and `./aeos secrets set office/bitbucket` (`{"access_token": ...}`).
* **Project.** In an OFFICE session, create the project at *Portofolio* → *Proyek baru* (`/projects/new`). Under
  *Integrasi*, set the Bitbucket repository (workspace/repo_slug, tick *Aktifkan adapter*), optionally Bitbucket Pipelines, the
  Confluence numeric space id and the Jira project key. Then verify the repository identity with *Verifikasi
  identitas*. Record a Bitbucket identity as its uuid or account id, not a nickname.
* **Check.** Run `./aeos doctor -g PROJECT_ADAPTERS --functional` and
  `./aeos doctor -c CAN_USE_OFFICE_INTEGRATIONS --functional`.
* **Pilot (M5 exit evidence).** Run one scoped pilot. Check that no OFFICE item appears in a PERSONAL session
  (Projects, Decisions, Evidence, History) and keep the evidence.

### 4.3 Live provider bypass test (GitHub)

The `main` ruleset is CONFIGURED ([ADR-0115](decisions/ADR-0115-public-repository-on-github-free.md)), but
`enforcement_tested` stays NOT-PROVEN until a real bypass attempt is refused. Choose one option.

* **Option 1.** With a non-bypass credential, `git push origin HEAD:main` must be refused (GH013). Then run
  `./aeos delivery enforcement --repo <your-login>/dly-aeos --branch main` (the token needs Administration: read),
  then `./aeos delivery capabilities --record`.
* **Option 2.** Add `aeos-probe/**` to the ruleset's branch targets. Store a low-privilege token with
  `./aeos secrets set <ref>`, then run
  `DLY_AEOS_REAL_SECRETS=1 DLY_AEOS_LIVE_BYPASS_REPO=<your-login>/dly-aeos DLY_AEOS_LIVE_BYPASS_BRANCH=aeos-probe/<name> DLY_AEOS_LIVE_BYPASS_TOKEN_REF=<ref> .venv/bin/python -m pytest tests/acceptance/test_delivery.py::test_provider_enforces_human_only_merge_on_the_real_repository -q`.

Optional fork check: allow fork PR workflows with no write token and no secrets. From a second account, open a
fork PR that prints only env-var names, keep the run URL and close it unmerged. Hand the run URL to the builder.

### 4.4 Real deployment targets

For each project that is actually deployed:

1. **Register the target.** *Portofolio* → project → *Integrasi* → *Atur integrasi lain*. Provider:
   Deployment target. Purpose: staging or production. Root ID: `https://<host>`, whose GET health path returns
   JSON with `status` and `commit`.
2. **Record the environment decision and checks** (2.4).
3. **Back up and check.** Run backup and restore-test (1.14) and `./aeos doctor -c CAN_VALIDATE_STAGING`.
4. **Observe.** Run `./aeos delivery observe-target <project_id> --environment staging --sha <deployed commit>`
   and `./aeos delivery gates <task_id>`.
5. **Screenshots.** `./aeos docs screenshot <project_id> --environment staging --commit <sha> --route /`.
6. **Artifact digests.** Have each product's CI publish artifact digests or attestations, and its health endpoint
   report `artifact_digest`. Verifying attestation signatures needs a signer pin from you.
7. **DLY AEOS itself.** After promoting a release (1.14), register its production target and set
   `./aeos delivery target-checks <platform project id> --environment production --health-path /api/health`.

Optional vulnerability evidence for the platform:

* Python: `.venv/bin/python -m pip install pip-audit==2.7.3`, then `./aeos inventory va-scan`. This is the remedy
  the product prints, and it needs network access.
* npm: upload an `npm audit --json` report with
  `./aeos intake upload <project_key> <npm-audit.json> --domain PERSONAL --actor <admin>`, then
  `./aeos docs register-va-report <project_id> --attachment <id> --assessor "<who>" --performed-on <YYYY-MM-DD> --scope "frontend/package-lock.json dependencies" --tools "npm audit"`.

---

## 5. Windows notes

Windows stays a supported host. Everything above applies, with these differences.

| macOS | Windows Command Prompt (cmd.exe) | Windows PowerShell |
|---|---|---|
| `./scripts/bootstrap.sh` | run it from PowerShell | `.\scripts\bootstrap.ps1` |
| `./aeos <command>` | `aeos <command>` | `.\aeos.ps1 <command>` |
| `.venv/bin/python ...` | `.venv\Scripts\python.exe ...` | `.\.venv\Scripts\python.exe ...` |
| `~/Library/Application Support/DLY-AEOS` | `%LOCALAPPDATA%\DLY-AEOS` | `$env:LOCALAPPDATA\DLY-AEOS` |

In cmd.exe, `.\aeos.ps1` opens in an editor instead of running. Use `aeos`, which runs `aeos.cmd`. If PowerShell
blocks scripts, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. Environment variables for the
pytest lines: in PowerShell write `$env:NAME="value"; <command>`, in cmd.exe `set NAME=value` first.

* **Docker:** `winget install -e --id Docker.DockerDesktop`, then start it. The `docker build` lines are the same.
  The container boundary was proven on the Windows build host in a throw-away data directory. Your real data
  directory still needs its own `aeos worker containment --kind container` report.
* **Claude Code:** `winget install --id Anthropic.ClaudeCode -e`. To remove `ANTHROPIC_API_KEY`, run
  `Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue` and delete it under System Properties →
  Environment Variables.
* **Signer for the pin (1.12):**
  `(Get-AuthenticodeSignature (Get-Command claude.exe).Source).SignerCertificate.Subject`. The `windows` entry of
  `approved_signer`.
* **OpenCode self-update off (1.12):** once per machine. Both forms set it for your user, for windows opened
  afterwards (not the current one). In Command Prompt (cmd.exe):

  ```cmd
  setx OPENCODE_DISABLE_AUTOUPDATE 1
  ```

  Or in PowerShell:

  ```powershell
  [Environment]::SetEnvironmentVariable('OPENCODE_DISABLE_AUTOUPDATE','1','User')
  ```

  Then open a new window and run `opencode --version`. It must print 1.19.3.
* **Commit identity (1.5):** edit `%LOCALAPPDATA%\DLY-AEOS\aeos.yaml` (copy `config\aeos.example.yaml` there if it
  does not exist).
* **OpenBao:** `vault install` picks the `windows_amd64` release. The bootstrap identity and the unseal key share(s)
  are in Windows Credential Manager. `vault export-recovery` refuses on Windows (an owner-only ACL for the file is
  NOT-PROVEN), so on Windows a plain `vault init` also shows the recovery material (unseal key share(s) and AppRole
  credentials) ONCE on a temporary screen: store it offline there, then type `stored`. Skip the
  `vault export-recovery` line of 1.7. For an install initialised before ADR-0123 run
  `.\aeos.ps1 vault import-unseal-key` and keep your existing offline copy. The live test is
  `$env:DLY_AEOS_TEST_BAO="$env:LOCALAPPDATA\DLY-AEOS\bin\bao.exe"; .\.venv\Scripts\python.exe -m pytest -m integration tests/integration/test_secrets_openbao_live.py -q`.
* **MCI hash (2.11):** `"sha256:" + (Get-FileHash -Algorithm SHA256 -LiteralPath '<path>').Hash.ToLower()`. The file
  goes to `%LOCALAPPDATA%\DLY-AEOS\policy\canonical\`.
* **Several machines:** if you keep using the Windows machine next to the Macs, it is one more machine. It has its
  own data directory, vault, unseal keys, secrets, decisions and containment report. Repository-level steps
  ([once]) apply to all machines after `git pull`.

Related: [OWNER_TARGET_CAPABILITY_MATRIX.md](OWNER_TARGET_CAPABILITY_MATRIX.md) (owner, target and capability per
milestone) and [IMPLEMENTATION_COVERAGE.md](IMPLEMENTATION_COVERAGE.md) (what is built and what is proven).
