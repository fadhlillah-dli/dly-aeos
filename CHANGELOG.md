# Changelog

All notable changes to DLY AEOS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Evidence levels (E0-E4) follow [ADR-0061](docs/decisions/ADR-0061-evidence-ledger-levels.md). An entry
describes what was built and how it was proven. NOT-PROVEN means no run on a real system has shown it yet.

## [Unreleased]

### Added

- **Free OpenRouter WORKER routes for PUBLIC data (owner decision C, ADR-0041 amendment 2026-10-26).** New `aeos routes
  set-pool` / `POST /api/routes/{key}/pool` moves an OpenRouter `:free` route between ARENA and WORKER; the move voids
  every earlier decision, so the owner re-activates. For `:free` WORKER routes only (other OpenRouter routes keep the
  existing rules), decision C (catalog price exactly zero, free period not over, PUBLIC only, PERSONAL only) is checked
  at the move, at activation, in routing and on every broker request (an INTERNAL run is refused). They rank first for
  PUBLIC tasks; at the account's free-model limit (`openrouter_free_rpm_limit` 20, `openrouter_free_daily_limit` 50,
  1000 after credits) the task waits (`FREE_TIER_LIMIT_REACHED`), never a Go/paid fallback; a 429 is recorded
  account-wide. Migrations 0009/0010 index what is counted. A run stopped at a provider quota no longer uses up
  a task attempt (bounded: 6 quota stops per owner retry decision). E2 (respx); live NOT-PROVEN.

- **OpenCode Go WORKER routes (owner decision A).** The owner chose option A of ADR-0041 on 2026-10-26: OpenCode Go,
  a $10/month subscription, for broker-served worker routes. Part 1 adds the routes and the code paths. Part 2 adds the
  subscription accounting.
  - **Routes.** `aeos seed` adds PERSONAL-only CANDIDATE routes for provider `opencode-go`:
    - WORKER, ranked above the client-restricted free Zen pilots: glm-5.3-flash, mimo-v2.6-flash,
      minimax-m3.1-flash, hy3 and kimi-k2.7-code.
    - WATCHLIST: longcat-2.0, glm-5.2, glm-5.3, kimi-k3.1-pro and mimo-v2.6-pro, plus deepseek-v4.1-flash
      (PUBLIC only: Global region, zero-retention agreement valid through 2026-10-31).

    Each route records the Go terms: no training, 0-day retention, the region setting and Go's permission for other
    clients, with no client restriction. Its pricing is a subscription: $10/month, a dollar allowance per model, 5h
    20% / week 50% / month 100% windows and allowance rates per 1M tokens, never a metered price.
  - **Broker.** It sends the Go key (`workers/opencode-go`) only to https://opencode.ai, with the bare model id, its
    own User-Agent and a stable opaque `x-opencode-session` per worker run.
  - **Availability.** `aeos routes refresh` reads the Go model list with that key and now prints the `available:`
    routes too. A listed model is AVAILABLE and an absent one MISSING. No key, a rejected key or an unreachable list is
    UNKNOWN, never MISSING.
  - **Secrets, Doctor and inventory.** The secret ref and its "OpenCode Go" integrations card warn that Extra Usage
    must stay OFF. Doctor `route.worker_key` now follows every `workers/*` key, and its remedy names the route's own
    key. The inventory CONFIG lens maps Go routes to the `opencode-console` component.
  - **Evidence.** The facts are E1: the OpenCode Go docs, FAQ, ToS and models.dev for tool calling, read 2026-10-26.
    The AEOS side is E2: unit tests against mocked upstreams for the request shape, host pin, headers, catalog
    AVAILABLE/MISSING/UNKNOWN, activation (region + BUDGET, no client terms) and the separate credential. No live
    call was made.
  - **Subscription accounting (part 2; ADR-0080 amendment 2026-10-26).** A route whose `pricing.billing` is
    SUBSCRIPTION is a third billing class beside free and per-token.
    - **Activation.** It still needs a BUDGET decision, for the monthly plan fee. The activation and eligibility copy
      say "subscription route (OpenCode Go, $10/month plan fee ...)".
    - **Broker.** It records `usage.cost` as the marginal charge (0 within the plan). `usage.opencode.allowance` and
      `billing_source` go to the ledger entry's details as `allowance_usd`. The basis is REPORTED, else ESTIMATED from
      tokens x the allowance rates, or from the request's upper bound (clamped `max_tokens` plus prompt size) when Go
      reports no usage, so a late answer without usage never locks the route. An unreported cost is charged $0, never
      the reservation. A provider dollar figure above 1e9 USD is unreported, and `money()` turns every decimal
      arithmetic error (e.g. 1e25) into a validation error instead of crashing the ledger write.
    - **Counted scope.** AEOS counts only the calls made through this AEOS instance. Usage of the same Go workspace
      from other clients or machines (your own OpenCode, the other Mac) is invisible, so Go's own limit stays
      authoritative: the docs say requests are blocked at a limit; HTTP 429 comes from OpenCode's client test fixture
      (NOT-PROVEN). Extra Usage OFF stays the only guard against a provider-side bill.
    - **Admission.** It needs no TaskSpec `max_cost_usd`: the marginal estimate is $0 and the run's grant cost cap is
      $0. The allowance is counted per route over trailing 5 h / 7 day / 31 day windows (20% / 50% / 100% of the
      monthly allowance). Only the 5h window blocks: `SUBSCRIPTION_ALLOWANCE_EXHAUSTED`, or
      `SUBSCRIPTION_ALLOWANCE_UNKNOWN` for at most 5 hours while a call in it cannot be counted (both WAITING_QUOTA).
      The week and month windows are advisory (`WARN_EXHAUSTED`, never blocking): Go's reset anchors are unknown, and
      with Extra Usage OFF exceeding them only yields Go's refusals. Wait reasons carry no allowance figures.
    - **Paid usage beyond the plan fails closed.** A reported charge, a `billing_source` other than `go` / `go-plus`
      (e.g. `balance`) or an unreadable stated cost is flagged `beyond_plan_usd` and emitted as
      `cost.subscription.beyond_plan`. The route is refused (`SUBSCRIPTION_BEYOND_PLAN`, BLOCKED) for new admissions,
      probes and every broker request until the owner turns Extra Usage OFF and re-activates it.
    - **Probe.** One synthetic PUBLIC feasibility probe is allowed on an activated subscription route (ADR-0111
      amendment 2026-10-26), refused with the same codes; a per-token paid route is still refused (`ROUTE_NOT_FREE`).
    - **Views.** `aeos cost summary`, `GET /api/costs/summary` (`subscriptions`, `by_route[].billing`), the route view
      and the task admission answer show the plan and the allowance per window (advisory windows marked), never
      per-token spend; the figures are hidden from a principal who cannot see every domain, also in
      `GET /api/tasks/{id}/admission`. `aeos routes list` only flags `billing=SUBSCRIPTION`; the Costs page ("Langganan
      worker") and the route drawer show the plan, the allowance per window and the beyond-plan state (UI contract
      fixtures refreshed). Doctor `route.registry` lists `subscription_candidates` apart from paid or unknown-price
      candidates.
    - **No migration.** Existing JSON columns hold the new figures.
    - **Evidence.** E2: unit tests with mocked upstreams cover allowance recording (reported, estimated, upper bound,
      stream), the blocking 5h window, the advisory week/month windows, the beyond-plan refusal and its clearing by
      re-activation (cost, scheduler, probe, broker), no reservation charge, absurd provider figures, the admission
      visibility, the cost summary and CLI, and Doctor.
    - **Open.** Per-token paid routes keep the `COST_UNKNOWN` gap (no TaskSpec cost limit from the planner).
  - **NOT-PROVEN:** whether Go accepts the broker as a client, tool calling on Go (hy3 UNKNOWN), the model-list shape,
    whether Go reports the allowance on every answer, whether the allowance is per model or pooled and the unit of
    `usage.opencode.allowance` (to compare with tokens x rate on the first probe or run), the 429 status at a limit,
    where Go anchors its windows, and the owner's subscription, key, region and Extra Usage setting.
  - **Not changed.** The free Zen pilots keep their S07 client restriction. ADR-0041 records the decision and its
    risks (the ToS automation clause and Extra Usage are owner risks). OWNER_ACTIONS 1.8, 1.11, 1.15 and 2.12 and
    OPERATIONS give the owner steps: a TRIAL WORKER route serves real tasks at once (probe first or pause
    scheduling), each billing cycle needs a new BUDGET decision and a new `routes activate ... --budget-decision`, and
    the admission codes with their remedies.

- **GitHub token expiry warning (#16).** The Doctor check `adapter.github_token` reads the expiry GitHub reports for
  the token (`GitHub-Authentication-Token-Expiration`) and the date recorded with `aeos secrets set
  personal/github --expires-at`, and warns 14 days ahead with the rotation command, without turning the check
  red while the token still works. The probe runner keeps that header (and still drops every other one). Unit
  tests cover both header formats, an unreadable header, the recorded date and the header filter (E2); a live
  GitHub response with the header was observed on the owner's Mac (E3, that host only).

- **Local gate runner, pre-push hook and commit-subject gate.** `python scripts/gates.py` runs the G1 static
  commands of `ci.yml` (ruff, mypy darwin and win32, secret scan) plus frontend gates when `frontend/` changed.
  `scripts/git-hooks/pre-push` (`git config core.hooksPath scripts/git-hooks`) runs them on the tip commit of each pushed
  ref in a temporary worktree outside the repo (intermediate commits get only the message check; the hook finds
  the main checkout's `.venv` from a linked worktree, or `AEOS_GATES_PYTHON`). `scripts/commit_subject.py` and a `commit subjects [G1]` job (pull
  requests only) enforce Conventional Commits and the no-attribution rule after a baseline (reverts accepted); `.editorconfig` added. A drift test pins
  `gates.py` to `ci.yml`. Proven by static checks and unit runs on macOS arm64 (E1/E2); the hook, the CI job and
  every Windows path are NOT-PROVEN until a real push and a CI run.

### Changed

- **CI in about ten minutes, with the same tests.** On each host OS the backend suite now runs as 2 shards in
  parallel, each with one pytest-xdist worker per logical CPU. Shards split by test file (`pytest --shard K/N`
  in `tests/conftest.py`), packed from measured seconds in `tests/ci_durations.json`. Every collected file lands
  in exactly one shard, so every test still runs on Windows, macOS arm64 and macOS Intel (ADR-0115). G1 static
  checks run once per OS in shard 1. The real trusted-suite verifier run has its own `macos-latest` job. The
  required check names `backend [G1,G2] (<os>)` are now aggregator jobs: each passes only when every backend
  job of its OS succeeded, and a skipped or cancelled job fails it. The pip, npm and Playwright browser downloads
  are cached. Every CI job now has a `timeout-minutes` (the default is 360, and a hung macOS job would block one
  of the 5 macOS slots of GitHub Free). Each shard uploads its JUnit XML, and `scripts/ci_durations.py` rebuilds the weights from those
  reports. `pytest-xdist` 3.8.0 (with `execnet` 2.1.2) joins the dev extra, and `requirements.lock` was
  regenerated (ADR-0093 amendment). Proven locally (E2, Apple Silicon, `-n 4`): shards 1/2 and 2/2 ran
  disjoint sets of tests whose union is the full collection (4,325 tests). They took 246 s and 203 s, and
  4,309 tests passed with 7 skipped. The 9 failures depend on the build host: the tests reached a live local
  OpenBao and a GitHub credential, and the same 9 fail on the unchanged code there. Unit tests cover the
  assignment, the workflow wiring and the aggregator behaviour. The expected wall clock of about 10-12
  minutes is NOT-PROVEN until the first CI run, and so is parallel safety on Windows and Intel macOS.

### Fixed

- **A push run on `main` is no longer cancelled by the next merge.** The single concurrency group with
  `cancel-in-progress` cancelled the run of PR #14's merge commit, so that commit never got a CI PASS for
  `aeos ops release accept`. Only pull-request runs are cancelled now, and every push run has its own group.
  Pinned by `tests/unit/test_ci_gates.py` (E1, static). The behaviour on GitHub is NOT-PROVEN until two merges
  land in close succession.

- **Advisory matching in multi-package repositories (#18).** The TRIAL/PROMOTE admission gate compared a candidate's pin
  with the vulnerable ranges of every package a repository publishes, so `langgraph` 1.2.14 was refused for
  advisories of `langgraph-checkpoint` 4.x (a different version line). Advisories that only name other packages
  are now skipped, but only when the repository's advisories name the candidate's package (from `pkg==version`
  in the pin, else the repository name); otherwise every advisory still counts. Skipped advisories are listed.
  Unit tests (E2) and the owner's stored advisory records (E3, that host only: LangGraph 1.2.14 passes,
  ToolHive v0.51.4 and Deep Agents 0.7.24 stay blocked, their patched pins pass).

- **OpenBao found in the data bin; a binary that cannot run is UNKNOWN.** The inventory, the Doctor and the vault
  runtime now find binaries through one resolver (`aeos.core.binaries`): settings override (`~` expanded),
  `<data>/bin` (OpenBao only), PATH; a candidate counts only as a regular executable file, so the Doctor and the
  inventory agree for every binary (`claude`, `opencode`, `node`, `docker` too) and `aeos vault start` names an
  unusable `openbao_binary` setting instead of advising `aeos vault install`. A located binary that times out, fails
  to start, is killed by a signal or (Windows) crashes with an NTSTATUS code is UNKNOWN, no longer INSTALLED, also
  for the REQUIRED `claude`, `git` and `opencode` (before, a TIMEOUT/OS_ERROR was reported INSTALLED; intentional
  evidence honesty). An UNKNOWN installation also leaves an open version/digest drift finding OPEN (not evaluated,
  never resolved).
  Evidence: OpenBao lookup E3 on the owner's Mac arm64 only (reconcile observed INSTALLED 2.7.1); the rest E2 unit
  tests. Windows `bao.exe` and the NTSTATUS path: NOT-PROVEN.

- **Data directory private on POSIX (#14).** The data layout was created with the process umask (0755
  under the usual 022), so the Doctor reported `evidence.dir_permissions` FAILED on every fresh Mac. On the
  default macOS path the 0700 `~/Library` already kept other users out, but a data directory configured
  elsewhere (`AEOS_DATA_DIR`) or the Linux path was readable by other OS users. On POSIX the layout is now
  created as 0700, and a directory left open by an older bootstrap is tightened at the next start. Windows
  keeps its inherited ACLs: Python 3.13 turns a 0o700 `mkdir` into a protected DACL there. Unit tests on CI
  (E2) cover POSIX tightening and the Windows mode; on the owner's Mac (arm64) the Doctor reported
  `evidence.dir_permissions` VERIFIED after the fix (E3, that host only). A real Windows run of the
  unchanged path is covered by the existing Windows CI suite only.

- **Public repositories no longer count as a fine-grained PAT breach (#16).** GitHub lists every public
  repository for a fine-grained PAT ("Also includes public repositories (read-only)"), so `adapter.github_token`
  reported a correctly scoped token as exceeding least privilege. Unregistered public repositories now leave
  least privilege NOT proven, with a note; an unregistered private repository, or one whose visibility is not
  reported, is still a breach. Unit tests (E2) and the owner's token on the Mac (E3, that host only).

### Security

- **Unseal keys no longer stay in the terminal (#15).** `aeos vault init` shows the unseal key(s) on a
  temporary (alternate) terminal screen that closes when the owner types `stored`, without erase-in-display or
  scrolling, so the keys are not written to the scrollback and cannot be copied with the rest of the output.
  Outside a terminal it refuses before touching OpenBao unless `--plain-output` is passed. A revoked root token
  is no longer shown; an incomplete setup asks to store it too. The banner and the docs say never to paste the
  output into a chat or an AI assistant, and the unseal runbook covers an exposed key. Unit tests drive every
  path (E2) and a macOS pty run showed the byte sequence; real iTerm2, Terminal.app and Windows console runs
  are NOT-PROVEN.

## [0.1.0] - 2026-10-26

First release of the local-first control plane. It contains pull requests #1 to #13 (#8 was
closed unmerged and replaced by #9). Anything that needs the owner's credentials, a company tenant or a
tool that was absent on the build host is NOT-PROVEN. The full list and the owner setup steps are
in the [v0.1.0 release notes](docs/releases/v0.1.0.md).

### Added

- **Foundation (#1, #2).** Canonical data model (master §13) with append-only and immutable
  database guards. Local authentication: Argon2id, opaque sessions, CSRF, login throttling, account
  recovery, no default credentials, and a VIEWER role that the backend enforces. Loopback-only
  binding with Host/Origin checks. A persistent event stream with SSE cursor replay. A redacted audit
  trail. Module packages with frozen cross-module facades and lifecycle hooks. Alembic migrations
  that take a backup before upgrading. The Control Center shell with the domain gate and every §35
  route. Bootstrap scripts. ADR-0001 to ADR-0003.
- **Control-plane modules (#3).** Nine module groups covering master §04-§47:
  - Readiness Doctor, install manifest, inventory reconciliation and the System Map.
  - Gateway and portfolio, an isolated intake parser, context packs and memory, and metadata search.
  - The four-stage planning pipeline (Opus plans, Fable reviews), PlanVersion/DAG, hash-bound
    decisions, the external-write interlock and the Human Decision Queue.
  - The Claude native bridge (official Claude Code client, native login), the model registry and
    routing, and the OpenBao secret provider.
  - Scheduler with leases, the capability broker (model proxy, git broker) and the OpenCode worker
    runtime with a sandbox.
  - Independent verifier, evidence ledger, checkpoint review, acceptance, and delivery gates G0-G8.
  - Product 1 docs engine and publication outbox, with Google Drive/Docs and GitHub Issues adapters.
    The Atlassian adapters are off by default.
  - Cost and quota, the evaluation leaderboard, change proposals and the research registry.
  - Backup and restore test, release promotion, SLIs and alerts, the emergency stop, and the setup
    guides.

  NOT-PROVEN in that PR: real Google, GitHub and OpenRouter writes, the container sandbox, OFFICE
  tenants, an interactive macOS run and MCI alignment.
- **End-to-end seams (#4).** Two chains run end to end against scripted fakes: planning to
  publication, and owner decision to pull request (scheduler, cost, routing, broker, runtime,
  verifier, review, acceptance, delivery). The fakes are an official-binary fake, HTTP mocks and a
  local bare repository. The §50 acceptance suite maps all 82 rows: 77 PASS and 5 PASS+BLOCKED. The
  blocked rows need Docker, a real GitHub repository, an Atlassian tenant or a deployment target. A
  Playwright smoke test covers every route at 1440 px and 390 px and in dark mode. A real `aeos doctor` run
  is recorded for one Windows machine (E3, that machine only). The initial schema migration.
- **First gap-closing round (#5).** An audit classified 1,656 master requirements. The PR reports
  416 fixed, each with a test that fails without the fix, 79 that now state their owner or external
  dependency, and 17 that cannot be fixed without the owner or an external system. Notable fixes:
  the Product 1 renderer consumes the real planning schema, a controller-integrity check covers
  every controller runtime file, workers get a blocker channel, and
  the worker contract test is bound to the engine version. Also added: evaluation execution,
  retirement verification, a mypy gate, real-browser and bootstrap CI jobs, and migrations 0001 to
  0005 with a single head.
- **First plan made by DLY AEOS (#6).** A real four-stage planning run for the platform project,
  through the official Claude Code client on the owner's Windows machine (E3, that machine only).
  It ended in `READY_FOR_HUMAN_REVIEW`. Evidence is in `docs/evidence/first-plan`. Owner approval,
  publication and worker execution were not part of that run.
- **Second gap-closing round (#7).** OFFICE delivery to Bitbucket Cloud through the broker: a
  sha-pinned push to `aeos/*` branches only, a draft PR (never merge, decline or approve), and a
  Pipelines/commit-status reader. It is disabled by default and sits behind the interlock and the
  company-authorisation decision. Other additions:
  - The verifier uses the container runner only when a fresh PASS containment report exists.
    Otherwise it reports WEAK with the residual risk.
  - The candidate environment redirects HOME, APPDATA, Known Folders, GitHub CLI, git, keyring
    and SSH.
  - A tamper window covers trust tables and the schema.
  - Per-run limits are enforced.
  - API envelope freshness, docs readiness checks and evidence producers.

  NOT-PROVEN in that PR: a real Docker container boundary, a live Bitbucket or Atlassian tenant
  (company authorisation, milestone M5) and a live OpenCode worker on a real route.
- **Container boundary on the build host (#9).** Docker Desktop on Windows 11. The images were built
  unchanged from `sandbox/`. `aeos worker containment --kind container` passed 20/20 checks for both
  the PERSONAL and OFFICE profiles, and the real-daemon containment test passed (E3, that host only).
  ADR-0115 was added, with a test that keeps every ADR listed in the decision register.
  NOT-PROVEN: a real OpenCode worker run, the verifier container runner on a real candidate, Docker
  on macOS, and a provider bypass test against the ruleset.
- **Host launcher and guides (#10).** `aeos.cmd` starts the CLI from Command Prompt. The macOS guide
  covers both Apple Silicon and Intel.
- **Third gap-closing round (#11).** The PR reports 101 fixed and 13 that now state their dependency
  precisely. By area:
  - **OFFICE integrations.** Bitbucket code and Pipelines sections and a deployment-target section
    in the Control Center. Structured company authorisation (approved targets, data classes,
    identities), an office-route CLI and provider-watch feeds.
  - **Deploy gates.** G6/G8 with smoke and critical-flow checks, and `CAN_VALIDATE_STAGING` is
    enforced. A post-release failure raises an alert and an incident, and recommends a rollback
    that a human performs. An artifact digest reader, ruleset enforcement evidence, and an opt-in
    live bypass test.
  - **Routing.** Documented Zen/Gemini request shapes. Client-restriction terms stay visible until
    the owner decides. An owner path for LOCAL endpoints.
  - **Evaluation.** System-change evidence is bound to the exact change. An ARENA eval-only path
    and verifier-baseline regressions.
  - **Workforce.** Concurrency is capped by the Doctor resource budget. An owner experiment grant,
    controls for enabling and disabling agents, plan execution and suite registration, and
    INVESTIGATION runs.
  - **Verifier.** Impact-selected checks: project baseline, per-candidate venv, frontend, UI/a11y,
    contract diff and an adversarial pack. Result sealing, kill-group survivor detection, residual
    risk per report, and a master-hash Doctor check.
  - **Worker integrity.** A database trust window with quarantine and an integrity hold, `.pyc`
    integrity checks, a Job Object CPU cap, a shell runner wrapper and a task-scoped knowledge pack.
  - **Pins and supply chain.** Digest and signer pins per OS and architecture, a signer gate, an
    OpenCode pin resolver, the candidate digest, and a policy-driven supply-chain evaluator. The
    evaluator is REPORT_ONLY while the policy is undecided.
  - **Docs engine.** Diagrams are checked against each project's own contracts. VA report
    registration, and checkpoints that hand off to a writer.
  - **Policy and other.** The MCI source of truth can be loaded from a local, uncommitted location.
    ADR decisions can be recorded from a live admin session. Research attachments, and release and
    retirement controls.
  - Migrations 0006 to 0008, in one linear chain after 0005.

  Stated in that PR: the verifier's npm, vitest, tsc and vite steps and the per-candidate
  `pip install` run outside a kill group (ADR-0120); two fragile tests are scheduled for the next
  round. Live behaviour that needs owner credentials or tenants (Bitbucket, Atlassian, real
  deployment targets, OpenCode) is NOT-PROVEN.
- **Repository files (this release).** `LICENSE`, this changelog, `SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `CITATION.cff`, `.github/CODEOWNERS`, issue forms, a pull request template,
  and `tests/unit/test_repository_hygiene.py`, which keeps them consistent.

- **Control Center for the wave 7 backend (#12).** Screens and forms for:
  - deployment-target checks (health path, smoke and critical-flow checks, SLI thresholds,
    rollback criteria, artifact digest);
  - the staging/production environment decisions;
  - the promotion block and ruleset enforcement evidence;
  - verifier impact checks, tamper neutralisation and UI-state screenshots;
  - the project test baseline;
  - route request shapes and client terms;
  - the worker integrity hold and DB-trust alerts;
  - the worker.route_behaviour threshold.

  Owner decisions still go through confirmed Decision records. VIEWER sees the screens read-only.
- **Code-host identity and no AI attribution (#12, ADR-0121, owner rule 2026-10-26).** Commits
  for each code host use the owner's identity for that host, from the local `aeos.yaml` (never
  committed). The broker refuses a push whose commits carry another identity or an AI attribution
  trailer. Outbound PR, issue and page text with AI attribution is refused, and the renderers use
  neutral role labels. Provenance stays inside DLY AEOS's own records.

- **Every owner decision has a control (#13).** The owner can now record these through the Control
  Center or the CLI:
  - the authenticated-screenshot decision (a POLICY preset; capture stays unauthenticated);
  - the role-evaluation threshold (`aeos work role-eval-threshold` and a control on *Organisasi &
    Peran*);
  - the worker route-behaviour threshold (`aeos policy route-threshold`).

  `aeos policy decisions` prints decision ids so a delegation can be revoked by id.
- **Owner checklist and coverage matrix (#13).** [docs/OWNER_ACTIONS.md](docs/OWNER_ACTIONS.md) lists
  every remaining owner step in order, macOS first (Apple Silicon and Intel), with every command
  checked against the CLI. [docs/IMPLEMENTATION_COVERAGE.md](docs/IMPLEMENTATION_COVERAGE.md) traces
  all 1,656 master requirements through four audit rounds:
  - 1,486 implemented and tested;
  - 81 waiting on an owner step or decision;
  - 9 externally blocked;
  - 73 design-only by the master's own statement;
  - 7 not applicable.

### Changed

- **Public repository on GitHub Free (#9, ADR-0115, owner decision 2026-10-25).** Pull requests and
  pushes to `main` run the full backend suite and the bootstrap. Before the switch, the whole history
  was scanned and no secrets were found. On 2026-10-26 the owner normalised the commit messages on
  `main` (trees unchanged). The old and new commit ids are in `docs/evidence/commit-id-map.md`.
- **Host platforms are Windows and macOS (#10, ADR-0115 amendment, owner decision 2026-10-26).**
  The backend suite and the bootstrap run on `windows-latest`, `macos-latest` (Apple Silicon) and
  `macos-26-intel` (x86_64). mypy checks the darwin and win32 code paths. The real trusted-suite
  verifier test runs on the macOS arm64 job.
- **Type checking is a full gate (#13).** All 13 modules still exempt from mypy were cleaned (235
  errors to 0 on darwin and win32), and no module is exempt any more. The behaviour is unchanged.
- **Module boundaries (#11).** The identity facade burn-down cut module boundary crossings from 132
  to 110.
- **Licence metadata (this release).** `pyproject.toml` now uses the PEP 639 licence expression
  `LicenseRef-DLY-AEOS-Proprietary` with `license-files = ["LICENSE"]`, and the build needs
  setuptools 77.0.3 or newer. `frontend/package.json` declares `"license": "UNLICENSED"`.

- **Verifier version 5 (#12).** The dependency-set hash covers only the candidate's packages, and the
  venv and pip steps run isolated. Build-tool steps and the project baseline run inside the kill group
  with sealed results. Reports and acceptances from version 4 are STALE and need re-verification.
- **ADR-0003 wording (#12, owner decision 2026-10-26).** The seed-build delegation is stated without
  naming a tool as author; its scope is unchanged.

### Removed

- **Linux backend and bootstrap CI jobs (#10).** Linux host support is now NOT-PROVEN. The code
  paths remain. The frontend, browser and supply-chain jobs still run on Ubuntu runners because they
  check platform-neutral artifacts. They do not claim Linux host support.
- **macOS smoke subset (#9).** The full suite replaced it.

### Fixed

- **Claude Code `--json-schema` (#6).** The CLI rejected a `$schema` meta-schema declaration. The
  bridge now leaves meta-schema keys out of the CLI argument and still validates the output against
  the full schema.
- **Cross-domain leaks (#5, #7).** Closed in proposal snapshots, baselines and outcomes.
- **PII minimisation (#5).** It no longer rewrites UUIDs or hash digests.
- **Container names (#9).** Names cut the run id to 12 characters, so the probe and cancel
  containers collided. Names now carry the whole run id.
- **`aeos worker status` (#9).** It printed `NOT_PROVEN` after admission had already selected the
  proven container. It now shows the per-domain proof.
- **macOS `codesign` (#9).** A `codesign` failure that is not a broken signature was reported as
  INVALID and blocked the native bridge. It is now UNKNOWN with the reason.
- **Detached processes on macOS (#12).** On the macOS Intel CI, a process that left the verifier
  step's process group and session received the 2-second exit grace, acted, and exited unreported. A
  detached member is now a survivor at once and is killed, and an empty first sweep is repeated
  briefly.
- **OpenBao root token guidance (#13).** The README, the setup guides and the `vault init` banner told
  the owner to store the root token. `vault init` actually revokes it once setup completes, so only the
  unseal keys are kept offline.
- **Merge integration (#4, #11).** Secret purpose contract, provider vocabulary, approved-pin
  drift, the migration chain order, facade imports, and the shared health-path rule for the
  registry, the checks API and the observer.

### Security

- **No secrets in the repository (#1, #3, #5).** PR builds get no secrets, and Actions are pinned to
  full commit SHAs. A secret scan runs over every tracked file, and a PII and tenant-host scan over
  `blueprints/`, `contracts/` and `docs/`. Secrets live in OpenBao and the OS
  credential store, never in `.env`, config, logs, the UI, prompts or worker sandboxes.
- **Claude only through the official client (#3).** Never the Claude API, never `--bare`, never
  `ANTHROPIC_API_KEY`.
- **Human-only gates (#1, #3).** Inside DLY AEOS, plan approval, publication, merge to `main` and
  production are human `Decision` records bound to an exact target and hash. Agent output is data,
  never approval, and workers can push only `aeos/*` task branches.
- **Repository settings (#9).** These live in the GitHub settings, not in the code: secret scanning
  with push protection, Dependabot alerts, required SHA-pinned actions and CodeQL default setup. A
  ruleset on `main` requires a pull request and the CI gates, and blocks force-push and deletion. A
  provider bypass test against the ruleset is NOT-PROVEN.
- **Vulnerability reporting (this release).** Report vulnerabilities privately through GitHub
  private vulnerability reporting. See [SECURITY.md](SECURITY.md).

[unreleased]: https://github.com/fadhlillah-dli/dly-aeos/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fadhlillah-dli/dly-aeos/releases/tag/v0.1.0
