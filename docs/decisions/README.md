# Decision register (ADRs)

Architecture decisions for the DLY AEOS platform (master §02, §41). Each ADR is a source artifact;
`aeos seed` loads them into the `adrs` table with a content hash so the dashboard can link
decisions to components, proposals and approvals. Superseding an ADR adds a new file — history is
never rewritten.

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](ADR-0001-master-architecture-baseline.md) | Master architecture is the platform baseline; MCI alignment UNVERIFIED | ACCEPTED |
| [ADR-0002](ADR-0002-stack-and-cross-platform-runtime.md) | Stack and cross-platform local runtime (Windows + macOS) | ACCEPTED |
| [ADR-0003](ADR-0003-seed-build-authority.md) | Seed build by native Claude; owner-authorised commit/push/merge for the platform repo | ACCEPTED |
