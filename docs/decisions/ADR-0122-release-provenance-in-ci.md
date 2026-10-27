# ADR-0122 — Release evidence and build provenance in CI; the GitHub Release stays a human action (§06, §09, §31)

* Status: PROPOSED
* Date: 2026-10-27
* Deciders: owner (Fadhlillah), standing instruction of 2026-10-27: "Keputusan yang lain lakukan rekomendasi yang
  terbaik yang tidak mengganggu atau menghambat operasional (harus efesien dan efektif) tapi sesuai dengan praktek
  industri profesional". Under it the delegating session chose a release CI in limited form (decision D2): CI builds
  evidence only. The owner can still record an ADR decision on this file's content
  (`aeos policy adr-decide ADR-0122 ...`, see the register README).

## Context

* The repository is public on GitHub Free ([ADR-0115](ADR-0115-public-repository-on-github-free.md)): standard
  GitHub-hosted runners cost nothing, and GitHub artifact attestations are available to public repositories on every
  plan.
* `aeos ops release` (build, accept, promote, rollback) is the release authority for the controller
  ([docs/OPERATIONS.md](../OPERATIONS.md), "Controller releases"). The same section says that AEOS never creates a
  GitHub tag or Release and that publishing one is the owner's hand action after a successful promotion.
* CONTRIBUTING.md "Releasing" ended with "push the tag and publish the GitHub Release". Nothing showed, for a file on
  a GitHub Release, which commit and which workflow produced it, and nothing stopped a tag on a commit whose CI was
  not green from getting release files.

## Decision

1. **A tag push builds evidence, nothing else.** `.github/workflows/release.yml` runs only on a pushed tag
   `v*.*.*` (never on pull requests or branches). Its one job, `release evidence [NO-GATE]` on `ubuntu-latest` with
   a 20-minute timeout, runs in this order and stops at the first failure:
   1. `python scripts/release_check.py --tag "$GITHUB_REF_NAME"` on the tagged commit (full history, so the tag must
      sit on `origin/main`);
   2. `python scripts/ci_status.py --sha "$GITHUB_SHA" --required-only --wait --deadline 15`: the required checks of
      the main ruleset must be GREEN for that exact commit; PENDING, RED, INFRA and TIMEOUT stop the job and there is
      no override. The deadline is shorter than the job timeout so the step ends with its own `CI: TIMEOUT` line;
   3. `git archive` of the commit into `dist/dly-aeos-src-<version>.zip` with the prefix `dly-aeos-<version>/`;
   4. the SBOM with the supply-chain job's command (`python scripts/supply_chain.py sbom`, offline, from the
      lockfiles) into `dist/sbom.cdx.json`;
   5. `dist/SHA256SUMS` of those files;
   6. a build-provenance attestation of every file in `dist/` with `actions/attest-build-provenance` v4.2.2
      (commit `4d101475d8b20a2381f78447822ac1eab6504dd8`, released 2026-08-06, the newest release published at
      least 14 days before 2026-10-27);
   7. upload of `dist/` as the workflow artifact `release-evidence-vX.Y.Z`, kept 90 days.
2. **Release creation stays human.** CI never creates, edits or publishes a GitHub Release and never pushes. After
   `aeos ops release promote` the owner, or the delegating session on the owner's explicit instruction, downloads the
   artifact, verifies it and runs
   `gh release create vX.Y.Z --draft --notes-file docs/releases/vX.Y.Z.md <files>`.
3. **Verification.** Anyone can check a downloaded file with
   `gh attestation verify <file> --repo fadhlillah-dli/dly-aeos`; the attestation names the repository, the
   workflow, the tag and the commit.
4. **Least privilege.** The workflow token is `contents: read` at the top. The job declares exactly
   `contents: read`, `actions: read`, `checks: read` (checkout and the CI verdict) and `id-token: write`,
   `attestations: write` (Sigstore signing through the OIDC token and storing the attestation). It is the only
   job-level write scope in any workflow; `tests/unit/test_delivery_ci_secrets.py` keeps it as an explicit
   allow-list and checks the trigger, the runner, the timeout, the step order and that the tag reaches the shell only
   as `GITHUB_REF_NAME`. `tests/unit/test_ci_gates.py` checks that the job reuses ci.yml's action pins and the
   supply-chain job's SBOM command. Release tags are protected by the v* tag ruleset.

## Consequences

* A file on a GitHub Release can be traced to the commit, the tag and the workflow run that built it, and the
  evidence is only built for a tag that passed the release check on a commit with a GREEN CI verdict.
* The tag push costs one short ubuntu job (no dependency install: every script it runs is stdlib only).
* The owner's release steps grow by a download and a verify; the decision to publish does not move to CI.
* If the required checks of the main ruleset cannot be read, `ci_status.py` counts every reported check as
  required, including this job's own pending check, so the job ends in `CI: TIMEOUT` (fail closed, re-run after the
  cause is fixed).

## NOT-PROVEN

* The workflow has not run yet: it is proven by its static tests (E2, macOS arm64) and stays NOT-PROVEN until the
  first real tag. Not proven in particular: that the GITHUB_TOKEN with these scopes can read the rulesets and check
  runs `ci_status.py` needs, and that the attestation is accepted by `gh attestation verify`.
* The CI zip and the zip of `aeos ops release build` are both `git archive` of the same commit, but the CI zip has a
  `dly-aeos-<version>/` prefix and the local one has none, and they come from different git versions on different
  OSes. That their contents are byte-for-byte equal, and that the CI zip is reproducible across OSes, is NOT-PROVEN;
  the two are tied by the commit, not by a file hash.
