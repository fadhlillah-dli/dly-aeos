# Commit id map (history normalisation, 2026-10-26)

On 2026-10-26 the commit messages on `main` were normalised by owner decision: attribution trailer
lines were removed from 93 of the 122 commits, and `main` was force-pushed once, before the ruleset
on `main` was enabled. Nothing else changed: every commit keeps its file tree, author, committer and
dates, and the parent structure is the same. All 122 pairs were checked before the push. Only the
commit ids changed.

Evidence and decision records written before that date cite the old ids. For example,
[docs/evidence/first-plan](first-plan/README.md) ran at `959bed284ac4`, and
[doctor-sample-windows.txt](doctor-sample-windows.txt) and [ADR-0060](../decisions/ADR-0060-independent-verifier-trusted-suite.md)
cite other old ids. Those records stay as they were recorded. Use this table to find the commit with
the same content on `main`.

| Old id | New id | Subject |
|---|---|---|
| `96196cbf8cce` | `76b9108b1ea2` | docs: add master architecture as platform source artifact (M0 baseline) |
| `40bd5719bbb8` | `7846944e2245` | feat: M0/M1 foundation - core contracts, auth, audit, events, app shell |
| `9a5270f4d649` | `f575818d30b7` | Merge pull request #1 from fadhlillah-dli/feat/m1-foundation |
| `e6deee3b876b` | `0fdb99dd8df9` | chore: freeze remaining cross-module facades and lifecycle hooks |
| `82936d4fa563` | `32fd15bf0ea0` | chore: auto-register module-owned model files |
| `0c5228e7b7a1` | `bf45d922c275` | Merge pull request #2 from fadhlillah-dli/chore/module-facades |
| `2646f4dd7ea6` | `3d415658162d` | fix(core): precise credential-key redaction, shared status vocabulary, lifecycle hooks |
| `fc509c96bc30` | `1715daf77021` | feat(portfolio,intake,knowledge): domain-first portfolio, isolated intake, context packs and memory |
| `fddbf383c4fb` | `0da71fb85eb3` | feat(projects-ui): portfolio, new-project wizard and project detail pages |
| `79fbbdf49a7c` | `79c57dac3291` | fix(B-portfolio-intake-knowledge): completeness review |
| `1042e32cb5bb` | `7d22fab2723b` | fix(B-portfolio-intake-knowledge): completeness review |
| `767d1942889f` | `db088f3674ea` | Merge branch 'worktree-wf_d7d5d63f-ef7-2' into integration/wave1 |
| `2633a5f4fdec` | `a79fba5d764f` | feat(planning,policy): planning pipeline, decision registry, interlock and decision queue |
| `cdb5e2a418a3` | `eb484ecb3921` | fix(C-planning-policy): completeness review |
| `0d3f9e8b4e8e` | `d47198188b3d` | Merge branch 'worktree-wf_d7d5d63f-ef7-3' into integration/wave1 |
| `a40253c751af` | `147ba19479b4` | feat(bridge,routing,secrets): Claude native bridge, model routing and OpenBao secrets (group D) |
| `70897fed8a9c` | `9ccd887947cf` | fix(D-claude-routing-secrets): completeness review |
| `02636c1f210b` | `9209fb7994b0` | Merge branch 'worktree-wf_d7d5d63f-ef7-4' into integration/wave1 |
| `f1570dc2df94` | `97b1cb977f45` | feat(cost,evaluation,proposals,research): module group H - budgets, eval leaderboard, change proposals, research registry |
| `a03fcf74a758` | `8f3acfc40b65` | fix(H-cost-eval-proposals-research): completeness review |
| `4f9f27397143` | `5a115ced92ca` | Merge branch 'worktree-wf_d7d5d63f-ef7-8' into integration/wave1 |
| `e85db8c099c6` | `1251c0e92a46` | feat(doctor,inventory): Readiness Doctor, InstallManifest, inventory reconciliation and System Map |
| `4cc552df3250` | `11236ec0d54b` | fix(A-doctor-inventory): completeness review |
| `1389a0f5280d` | `ecf746419891` | Merge branch 'worktree-wf_d7d5d63f-ef7-1' into integration/wave1 |
| `e6ee2d994159` | `bf5e9f818828` | feat(workforce): scheduler, capability broker, worker runtime and Workforce Console (module group E) |
| `ed8404b38254` | `a992a2af371e` | fix(E-workforce-broker-runtime): completeness review |
| `6f8353b3fab4` | `d6e8582f4a5e` | Merge branch 'worktree-wf_d7d5d63f-ef7-5' into integration/wave1 |
| `c4f10df77ffc` | `e42ec600f68f` | feat(verification,delivery): independent verifier, evidence ledger, review, acceptance, gates G0-G8 |
| `e89f358273cf` | `740e2b2f2422` | fix(F-verification-delivery): completeness review |
| `a52c7977350b` | `d8f298c6ca30` | Merge branch 'worktree-wf_d7d5d63f-ef7-6' into integration/wave1 |
| `1c91447e36dc` | `bad50a50a338` | feat(publisher,docs): publication outbox, Product 1 renderer and adapters (WIP checkpoint) |
| `959b07839df8` | `4b866f1e69c8` | feat(publisher,docs): Product 1 docs engine and publication outbox (module group G) |
| `652bb72251bd` | `28e239e5a838` | fix(G-publisher-docs): completeness review |
| `d16f51d9250a` | `db41b2cb5331` | Merge branch 'worktree-wf_d7d5d63f-ef7-7' into integration/wave1 |
| `4173bd415978` | `c6d3c8b7146c` | feat(ops): operations & reliability, control center projections, setup docs and lockfile |
| `5fc73d4abf58` | `bc88e24525be` | fix(I-ops-control-center-docs): completeness review |
| `c62d180fc27d` | `337bd6dca9bb` | Merge branch 'worktree-wf_d7d5d63f-ef7-9' into integration/wave1 |
| `aae83df68b23` | `559b69ccc8c8` | chore(integration): wire wave-1 modules together |
| `c07370dbde71` | `8550432012b8` | fix(security): word-bounded token patterns; scanner accepts only visibly fake tokens in tests |
| `abc258f4496e` | `dbfbe325b411` | fix: binary secret scan uses unbounded token shapes; plain-ASCII CLI under CI |
| `1eb996422a3d` | `d0eb966a683b` | Merge pull request #3 from fadhlillah-dli/integration/wave1 |
| `52740601b070` | `eb65714c9237` | feat(integration): W1 planning-bridge-policy-publisher-portfolio-cost seam end to end |
| `f67fde33c751` | `695ae0424a8d` | fix(W1-planning-publication): review |
| `4bd2e802d283` | `1ee7eaebf5f3` | Merge branch 'worktree-wf_48a549c3-5c7-1' into integration/wave2 |
| `ccd0247cdcc5` | `fe967f58588e` | feat(integration-w2): end-to-end execution chain scheduler->cost->routing->broker->runtime->verifier->review->acceptance->delivery |
| `7d797c990f71` | `9bc67f8800d5` | fix(W2-execution-delivery): review |
| `ea8706adcc9b` | `6d4e39928bbf` | Merge branch 'worktree-wf_48a549c3-5c7-2' into integration/wave2 |
| `e145907739fb` | `2626c9ddc982` | feat(doctor): wire readiness gates to the real modules and invalidate on change (W3) |
| `eac9f80782c9` | `2481ff669a2e` | fix(doctor): name the Claude call in --functional output; extra billing as residual risk |
| `fdfd6641e936` | `9d8b8c803f0a` | docs(evidence): Readiness Doctor sample run on Windows (E3, this machine only) |
| `7813aba60353` | `16f027d16a44` | fix(W3-doctor-readiness): review |
| `e9f953ae5a4b` | `b7afba6ff2e6` | Merge branch 'worktree-wf_48a549c3-5c7-3' into integration/wave2 |
| `0142d8b7ad14` | `2cd3e6fe22c0` | feat(ui): Control Center end to end in a real browser + UX completeness (W4) |
| `b2d3546ec30c` | `81bba212addd` | fix(W4-ui-e2e): review |
| `5fc1b3c40aa8` | `ca4d5fd88f57` | Merge branch 'worktree-wf_48a549c3-5c7-4' into integration/wave2 |
| `1f00a457ffcb` | `4f331cfbe96c` | test(acceptance): WIP integrated acceptance suite for master s50 (bootstrap, input, publication, workforce, routing, self-build) |
| `135fc064d9a8` | `c4d0a4ca9248` | test(acceptance): trusted suite for every master s50 row with MATRIX and seam amendments |
| `867a0a37ea9b` | `c98c87f78793` | fix(W5-acceptance-suite): review |
| `7ca60212f8a4` | `4c437be66b08` | Merge branch 'worktree-wf_48a549c3-5c7-5' into integration/wave2 |
| `e84b574d38c4` | `8be85cdca7ef` | fix(integration): reconcile wave-2 seam contracts |
| `148db01b4db6` | `59fc926f40d0` | feat: PII minimisation, approved-pin drift, initial Alembic migration |
| `199995d32d81` | `bb18874ca9d3` | style: ruff fixes in initial migration header |
| `de890a073134` | `967301407b62` | fix: cross-platform findings from CI |
| `2d314ccea951` | `7689010581f6` | fix(ci,windows): long paths, PID reuse, full-tree verifier candidate; leaner CI matrix |
| `2f58195b57d4` | `6b514c3594fe` | Merge pull request #4 from fadhlillah-dli/integration/wave2 |
| `689f4546083a` | `e90044c82ea0` | feat(worker-runtime): container sandbox, no host shell, risk tiers, contract test, worker blocker channel |
| `a9ce5536229c` | `47fccc1723d3` | fix(C1-worker-runtime): review |
| `6f580729754a` | `399c5a0ddb03` | Merge branch 'worktree-wf_3d56b625-166-1' into integration/wave3 |
| `4195cb729c30` | `917ba2f3763b` | feat(proposals,research): close audited §39-§41/§47 gaps (C2) |
| `d5930481a42d` | `8608127e4f69` | test(proposals): PROMOTE needs linked task, named owner, restore-tested backup; docs dimension follows publications |
| `f19db290c399` | `f5566ef77787` | fix(C2-proposals-research): review |
| `0886033d6d6e` | `3a60510abac4` | Merge branch 'worktree-wf_3d56b625-166-2' into integration/wave3 |
| `8cbed1e9069e` | `0e8095b8f88e` | feat(docs,publisher): render Product 1 from schema-v1 plans; keep human-governed content on publish |
| `92cd7caf8b0d` | `fea29472118d` | fix(C3-docs-publisher): review |
| `e8d1399e4127` | `83c942fa01dc` | Merge branch 'worktree-wf_3d56b625-166-3' into integration/wave3 |
| `373eb769a401` | `2978299f8f16` | feat(inventory,doctor): close C4 audit gaps (decision-side dimensions, usage, project-scoped readiness) |
| `3dfa7ac21f60` | `edf964969c5a` | test(doctor): OFFICE project targets stay UNKNOWN behind the office gate |
| `94d70602211d` | `dad54b4cc52b` | feat(inventory): paginate the component relations list |
| `30c7107d60ad` | `470b72f107ec` | fix(C4-inventory-doctor): review |
| `ce9abb7bcd9b` | `e3d8100db61d` | Merge branch 'worktree-wf_3d56b625-166-4' into integration/wave3 |
| `e3d09241d644` | `518a8e5b546d` | feat(routing,evaluation,cost): production routes only for tested configurations |
| `35c7d4d02bae` | `dde1dcd992f0` | fix(C5-routing-evaluation): review |
| `f3fcef62ff62` | `837466e59685` | Merge branch 'worktree-wf_3d56b625-166-5' into integration/wave3 |
| `7918003c432a` | `095f939eb23e` | fix(verification,delivery,broker): close C6 audit gaps with honest status |
| `5858e64abf43` | `a2fcae9c1534` | fix(C6-verification-delivery-broker): review |
| `84bf74b01704` | `57b10c77df3c` | Merge branch 'worktree-wf_3d56b625-166-6' into integration/wave3 |
| `059963b1a36f` | `3011c2512e12` | fix(workforce,planning,policy): close C7 audit gaps (admission answers, plan governance, retirement) |
| `d52a5f2af8e6` | `7f7a0879d1b9` | fix(C7-workforce-planning-policy): review |
| `6c998fc2a509` | `ff86910537ba` | Merge branch 'worktree-wf_3d56b625-166-7' into integration/wave3 |
| `dee3727617fe` | `56f8080db33f` | fix(core,operations): uuid-safe PII minimisation, run the verified active release, acceptance-gated builds |
| `dadafbb7b0b6` | `17e2bc362069` | feat(operations,identity): incident records, retirement workflow, criticality SLOs and error budget |
| `b4305ff9d88b` | `f7a76ccc4630` | feat(ci,knowledge,secrets,claude_bridge): CI gates, context-pack contract, pinned installs, supervisor rules |
| `cbb75fc1fd47` | `f5e8e8b264eb` | feat(portfolio,frontend,intake,docs): environment targets, integration health, Bitbucket verify, uploads |
| `d4ab0024e993` | `a66143b561be` | feat(control_center,frontend,tests,claude_bridge): attempts timeline, platform readiness gate, page-specific decisions, guards |
| `240ff48b5292` | `f8741900323e` | feat(claude_bridge): explain Doctor results as narration; report OS code signature of the binary |
| `c37165b8b253` | `1821537779a0` | feat(frontend,tests,docs): breadcrumbs, data-flow view, explain buttons, role assignment, proposal candidates |
| `2514da4672e2` | `5e11d2099c40` | test(tests): drop weaker duplicates of identity, master-independence and volume-metric guards |
| `1db2fd04d31b` | `33b5e081b6c0` | test(tests,operations): isolate serve env leaks and fake-module targets found by the full suite |
| `318e556f8a72` | `b8038e0fc1de` | fix(C8-platform-misc): review |
| `db6b507915d9` | `a010ac9c73cf` | Merge branch 'worktree-wf_3d56b625-166-8' into integration/wave3 |
| `f96e54f6ae90` | `aa8f010dc155` | fix(integration): resolve cross-cluster failures after merging wave 3 (C1-C8) |
| `32f7e1faabe5` | `92972a8b7615` | fix(types): resolve mypy gate errors (incl. real bugs found) |
| `959bed284ac4` | `8870181c7f1a` | fix(bootstrap): mark shell scripts and the ./aeos launcher executable |
| `47f8629fbed2` | `fc90715cd376` | Merge pull request #5 from fadhlillah-dli/integration/wave3 |
| `ad770115f99a` | `79a7943db604` | feat: first DLY AEOS plan produced by DLY AEOS (real 4-stage run) + CLI schema fix |
| `d98c1a8fdf69` | `b4bcd860e695` | test: remove timing race in expired-trial proxy test |
| `f6b7ebafcfb8` | `300e273bd02f` | Merge pull request #6 from fadhlillah-dli/feat/first-plan-evidence |
| `9e5f9f0b6b49` | `aedc9b668f35` | feat(office-delivery): Bitbucket Cloud push/PR/Pipelines behind company authorisation, route feasibility probe, publication fidelity |
| `df5eb2740d27` | `164d3c99fed6` | fix(K1-office-delivery): review |
| `71507cd6e95a` | `b6e52a3740ac` | Merge branch 'worktree-wf_3bebb053-e2c-1' into integration/wave5 |
| `fc0a8111b03c` | `7b2707daf5f4` | fix(k3): close workforce/routing/evaluation/proposals re-audit gaps |
| `0fe1732a0648` | `5e98d8724f38` | fix(K3-workforce-routing-misc): review |
| `5f698dc6f19f` | `d08705b8e0d2` | Merge branch 'worktree-wf_3bebb053-e2c-3' into integration/wave5 |
| `5e809e5f35cb` | `478d4af81815` | feat(K4-platform-docs): close re-audit gaps in platform, docs engine, doctor and operations |
| `22eb3508d4b3` | `68353715f5d2` | fix(K4-platform-docs): review |
| `c8994a1b3e7d` | `5a7a15030ae1` | Merge branch 'worktree-wf_3bebb053-e2c-4' into integration/wave5 |
| `1cfa234c7ae0` | `42633c2efa1f` | feat(K2-isolation): isolated verifier runner, env hardening, candidate-code tamper detection |
| `24652f650016` | `d1278a38c939` | fix(K2-isolation): review |
| `88cd5e2a7798` | `a338fe50a843` | Merge branch 'worktree-wf_3bebb053-e2c-2' into integration/wave5 |
| `be502017c3e3` | `f58b4d0a20cf` | fix(doctor): verifier runner view fails closed without a database session |
| `0b409ba72798` | `f199d8ce6f1f` | fix(worker_runtime): mypy on Linux - ctypes.get_last_error is Windows-only |
| `32652a1961e9` | `ecf8b87713f5` | Merge pull request #7: Wave 5 second gap-closing round |
