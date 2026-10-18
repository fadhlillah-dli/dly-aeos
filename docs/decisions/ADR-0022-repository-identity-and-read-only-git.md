# ADR-0022 — Repository identity verification and read-only git access

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Gateway & Portfolio module

## Context

§37: for user repositories store provider, exact repo ID, domain/project, branch/head/base, dirty
state, links and last verified time; never assume a same-named repository is the same target or
guess a private repository from a cloud account. U-02..U-04 map PERSONAL → GitHub and OFFICE →
Bitbucket. §05 step 6 requires an inventory of an existing repository without re-scaffolding.

## Decision

1. Provider must match the domain (PERSONAL→GITHUB, OFFICE→BITBUCKET) or the request is
   `PolicyDenied PROVIDER_DOMAIN_MISMATCH` (audited). Owner/name are validated; `role=platform`
   only on the `is_platform` project.
2. GitHub verification is one read-only `GET https://api.github.com/repos/{owner}/{name}` with
   `personal/github` from `aeos.secrets.facade` when available (else anonymous), 10 s timeout,
   **no redirect following**. VERIFIED only when `full_name` matches exactly and the provider id
   equals any previously stored id. 3xx (rename/move), 401/403/404/429/5xx, network errors, a
   different `full_name` or a changed id leave the record UNVERIFIED with the reason; nothing is
   inferred. VERIFIED means repository identity only — not permissions or delivery readiness.
3. Bitbucket verification is not implemented in this build (OFFICE, M5): it reports UNVERIFIED
   with the reason and makes no network call.
4. Local checkouts must be absolute, existing, the git top-level, outside the AEOS data dir and
   not a UNC/network path. Observation is read-only (`rev-parse`, `status --porcelain`, `remote
   get-url`); the origin must point at the declared provider/owner/name or the checkout is
   `MISMATCH`. Credentials embedded in remote URLs are stripped; views show a logical/redacted
   path. The platform repository uses the logical ref `platform-repo` resolved from
   `aeos.core.paths.repo_root()` at use time.
5. All git calls go through `aeos.portfolio.gitread`: list args, timeouts, output caps, inherited
   `GIT_*` variables removed, `core.fsmonitor=false`, content read from the object DB at an exact
   commit (`git show <sha>:<path>`) so working-tree symlinks cannot escape the repository.
6. `aeos.portfolio.inventory.inventory_repository(path, commit=None)` is the read-only inventory
   contract planning uses for `PlanningContext.repo_inventory` (languages, manifests + lockfile
   pairing, test dirs/frameworks, CI, deployment/IaC, docs, contracts/schemas, symlinks, sensitive
   paths, head/branch/dirty, explicit coverage gaps; tests are never executed).

## Consequences

* Residual risk: repository-local clean/smudge filter drivers can run during `git status`; the
  repositories are chosen by the human admin. A hardened mode (`--no-optional-locks` with a
  filter-free config) can be added if workers ever supply repositories.
* A moved/renamed repository needs an explicit new reference; trust is not inherited.
