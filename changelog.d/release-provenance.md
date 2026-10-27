### Added

- **Release evidence in CI on a tag push (ADR-0122).** Pushing a tag `vX.Y.Z` runs `release evidence [NO-GATE]`
  (`.github/workflows/release.yml`): the release check of the tagged commit, a GREEN CI verdict for that commit
  (`scripts/ci_status.py --required-only --wait`, no override), then a `git archive` source zip, the SBOM, `SHA256SUMS`
  and a build-provenance attestation, uploaded as a workflow artifact for 90 days. CI never creates or publishes a
  GitHub Release; after `aeos ops release promote` the owner verifies the files with `gh attestation verify` and
  creates a draft release by hand. The job is the only one with write scopes (`id-token`, `attestations`), kept as an
  explicit allow-list in `tests/unit/test_delivery_ci_secrets.py` with bite tests (branch trigger, `contents: write`,
  macOS runner and an interpolated tag are red; the real file is green). Evidence: E2 static tests on macOS arm64; the
  workflow itself is NOT-PROVEN until the first real tag.
