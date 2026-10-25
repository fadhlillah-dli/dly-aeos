# Decision register (ADRs)

Architecture decisions for the DLY AEOS platform (master §02, §41). Each ADR is a source artifact;
`aeos seed` loads them into the `adrs` table with a content hash so the dashboard can link
decisions to components, proposals and approvals. Superseding an ADR adds a new file — history is
never rewritten. Number ranges per module group: 0001-0009 platform, 0010 doctor/inventory,
0020 portfolio/intake/knowledge, 0030 planning/policy, 0040 bridge/routing/secrets,
0050 workforce/broker/runtime, 0060 verification/delivery, 0070 publisher/docs,
0080 cost/evaluation/proposals/research, 0090 operations/control center.

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](ADR-0001-master-architecture-baseline.md) | Master architecture is the platform baseline; MCI alignment UNVERIFIED | ACCEPTED |
| [ADR-0002](ADR-0002-stack-and-cross-platform-runtime.md) | Stack and cross-platform local runtime (Windows + macOS) | ACCEPTED |
| [ADR-0003](ADR-0003-seed-build-authority.md) | Seed build by native Claude; owner-authorised commit/push/merge for the platform repo | ACCEPTED |
| [ADR-0004](ADR-0004-first-admin-bootstrap-hardening.md) | First-admin bootstrap hardening and local single-use recovery | PROPOSED |
| [ADR-0010](ADR-0010-install-manifest-component-catalog.md) | InstallManifest as the reviewed component catalog (DESIGN lens) | ACCEPTED |
| [ADR-0011](ADR-0011-readiness-doctor-capability-gates.md) | Readiness Doctor checks, fingerprints and capability gates | ACCEPTED |
| [ADR-0012](ADR-0012-inventory-reconciliation-system-map.md) | Inventory reconciliation, drift and the three System Map lenses | ACCEPTED |
| [ADR-0020](ADR-0020-domain-first-project-registry.md) | Domain-first project registry with an immutable domain | ACCEPTED |
| [ADR-0021](ADR-0021-intake-isolation.md) | Intake: allow-list, archive inspection without extraction, isolated parser | ACCEPTED |
| [ADR-0022](ADR-0022-repository-identity-and-read-only-git.md) | Repository identity verification and read-only git access | ACCEPTED |
| [ADR-0023](ADR-0023-context-packs-and-memory.md) | ContextPack retrieval at a known commit; human-accepted, partitioned memory | ACCEPTED |
| [ADR-0024](ADR-0024-domain-scoped-metadata-search.md) | Domain-scoped metadata search on SQLite LIKE | ACCEPTED |
| [ADR-0030](ADR-0030-planning-pipeline-and-manual-fallback.md) | Four-stage planning pipeline as bounded activities with manual fallback | ACCEPTED |
| [ADR-0031](ADR-0031-decision-binding-and-external-write-interlock.md) | Human decisions bound to exact targets, and a fail-closed external-write interlock | ACCEPTED |
| [ADR-0032](ADR-0032-single-human-decision-queue.md) | One Human Decision Queue projected from canonical records | ACCEPTED |
| [ADR-0033](ADR-0033-taskspec-compile-defaults.md) | TaskSpec compile defaults and self-build protected paths | ACCEPTED |
| [ADR-0040](ADR-0040-claude-native-bridge-invocation.md) | ClaudeNativeBridge invocation, output contract and model-policy check | ACCEPTED |
| [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) | Model registry, hard-filter routing and AIConfiguration identity | ACCEPTED |
| [ADR-0042](ADR-0042-openbao-secret-provider-and-bootstrap.md) | OpenBao secret provider, OS-keyring bootstrap and the interim exception | ACCEPTED |
| [ADR-0043](ADR-0043-openbao-direct-approle-instead-of-agent.md) | Direct AppRole login in the controller instead of OpenBao Agent (local single host) | PROPOSED |
| [ADR-0050](ADR-0050-task-execution-state-machine.md) | TaskExecution state machine, admission and leases (§14, §19, §21, §22) | ACCEPTED |
| [ADR-0051](ADR-0051-worker-sandbox-requirement.md) | Worker sandbox requirement and the process-sandbox exception (§11, §25, §50, §51) | ACCEPTED |
| [ADR-0052](ADR-0052-self-build-candidate-workspace.md) | Self-build zones: candidate workspace, runtime commits and quarantine (§09, §21, §50) | ACCEPTED |
| [ADR-0053](ADR-0053-capability-broker.md) | Capability broker: run-bound tokens, model proxy and git/PR broker (§25, §26, §31, §42) | ACCEPTED |
| [ADR-0054](ADR-0054-candidate-git-hardening-and-publication-gate.md) | Candidate git hardening, quarantine release and the publication gate (§09, §14, §21, §31, §50) | ACCEPTED |
| [ADR-0060](ADR-0060-independent-verifier-trusted-suite.md) | Independent verifier runs the controller's trusted suite against an exact head snapshot | ACCEPTED |
| [ADR-0061](ADR-0061-evidence-ledger-levels.md) | Evidence ledger: trust from origin, levels per claim, redaction before hashing | ACCEPTED |
| [ADR-0062](ADR-0062-review-acceptance-binding.md) | Checkpoint review and acceptance: verifier PASS + review verdict, hash-bound, STALE on change | ACCEPTED |
| [ADR-0063](ADR-0063-delivery-gates-provider-governance.md) | Delivery: PR via broker only, G0–G8 from evidence, read-only provider checks, Null governance | ACCEPTED |
| [ADR-0070](ADR-0070-publication-outbox-idempotency-reconciliation.md) | Publication outbox with idempotency keys and reconcile-before-retry | PROPOSED |
| [ADR-0071](ADR-0071-product1-document-model-and-delta-rendering.md) | Product 1 document model, content types and hash-based delta rendering | PROPOSED |
| [ADR-0072](ADR-0072-google-drive-file-scope-docx-import.md) | PERSONAL docs: drive.file scope, DOCX import, requiredRevisionId updates | PROPOSED |
| [ADR-0073](ADR-0073-office-atlassian-adapters-disabled-by-default.md) | OFFICE Atlassian adapters: disabled by default, Cloud-only, real folders or BLOCKED | PROPOSED |
| [ADR-0074](ADR-0074-publication-approval-decision-binding.md) | One human PUBLICATION decision per run, bound to version + hash + preview | PROPOSED |
| [ADR-0075](ADR-0075-diagram-contract-references-and-checkpoint-handoff.md) | Diagram checks against the project's own contracts at a recorded commit; checkpoint hand-off to planning | PROPOSED |
| [ADR-0080](ADR-0080-budget-admission-and-native-usage.md) | Budget admission: $0 default hard stop, serialised reservations, fail-closed state, native usage never in dollars | ACCEPTED |
| [ADR-0081](ADR-0081-evaluation-gates-and-leaderboard.md) | Evaluation: AIConfiguration unit, eligibility gates, honest leaderboard, no automatic route change | ACCEPTED |
| [ADR-0082](ADR-0082-change-proposal-hash-binding.md) | Change proposals: canonical identity, deterministic review package, trial ≠ promotion, hash-bound decisions | ACCEPTED |
| [ADR-0083](ADR-0083-research-registry-and-upstream-checks.md) | Research & Technology Registry: dated catalog, no guessed identities, manual GitHub upstream checks | ACCEPTED |
| [ADR-0090](ADR-0090-backup-restore-test-readiness.md) | Backups count as ready only after a recorded restore test | ACCEPTED |
| [ADR-0091](ADR-0091-controller-release-promotion.md) | Released controller artifacts, human-only promotion and rollback | ACCEPTED |
| [ADR-0092](ADR-0092-sli-targets-and-alerting.md) | SLIs from records, SLO targets as owner decisions, deduplicated alerts, emergency stop | ACCEPTED |
| [ADR-0093](ADR-0093-universal-dependency-lockfile.md) | Universal Python dependency lockfile (requirements.lock) | ACCEPTED |
| [ADR-0094](ADR-0094-product1-schema-seam-and-human-governed-publication.md) | Product 1 from schema-v1 plans; HUMAN_GOVERNED content carried on publication; SUPERSEDED banners; publish gates | ACCEPTED |
| [ADR-0095](ADR-0095-ci-gates-supply-chain-and-security-tooling.md) | CI gates, explicit gate mapping, supply-chain reports and security tooling | PROPOSED |
| [ADR-0106](ADR-0106-verification-delivery-broker-audit-closure.md) | Verification, delivery and broker: closing the audited gaps honestly | PROPOSED |
| [ADR-0111](ADR-0111-office-code-delivery-and-route-feasibility.md) | OFFICE code delivery on the company code host (Cloud only, disabled by default), route feasibility probe, publication fidelity | PROPOSED |
| [ADR-0114](ADR-0114-platform-docs-readiness-and-evidence-producers.md) | Platform, documentation readiness and real evidence producers (K4 audit closure) | PROPOSED |
