# Changelog

All notable changes to DLY AEOS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Evidence levels (E0-E4) follow [ADR-0061](docs/decisions/ADR-0061-evidence-ledger-levels.md). An entry
describes what was built and how it was proven. NOT-PROVEN means no run on a real system has shown it yet.

## [Unreleased]

### Fixed

- **Data directory private on macOS.** The data layout was created with the default umask (0755), so other
  OS users could read the database and the PERSONAL/OFFICE partitions, and the Doctor reported
  `evidence.dir_permissions` FAILED on every fresh Mac. The layout is now created as 0700 on POSIX, and a
  directory left open by an older bootstrap is tightened at the next start. Unit tests cover both cases
  (skipped on Windows, where the Doctor checks ACLs instead).

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
