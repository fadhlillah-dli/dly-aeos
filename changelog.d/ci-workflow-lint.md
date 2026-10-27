### Added

- **Workflow lint on every CI run, shellcheck on main, pull request titles checked.** The `static checks [G1]` job now
  runs `actionlint` (pinned release v1.7.12, tarball compared with a SHA-256 pinned in `ci.yml` before extraction, no
  third-party action) over `.github/workflows`, with a bite control that lints a known-bad fixture (it must exit 1 and
  name the undefined `matrix.oss` property) and a near-miss fixture (it must pass) on every run. `shellcheck` moved from the
  pull-request-only `commit subjects [G1]` job into the same job, so it runs on `main` too, and the three aggregator
  scripts no longer word-split an unquoted variable. A new workflow `pr-title.yml` (`pr title [G1]`, not a required
  check) validates the pull request title as a commit subject through `scripts/commit_subject.py --subject`, with the
  title passed through `env:` only. `scripts/gates.py` mirrors the new steps (SKIPPED with a reason, never PASS, when
  the tools are missing, so the job stays NOT RUN), and `tests/unit/test_ci_gates.py` pins the checksum comparison, the
  SHA pins of every workflow, the title-in-env rule and an allow-list of event-conditional jobs (only `commit-subjects`).
  Evidence: E2 unit run on macOS arm64 for the tests and `gates.py`; actionlint and shellcheck themselves ran on no
  host yet (E3 only after the first CI run on ubuntu-latest), Windows and Linux are NOT-PROVEN for the new tests.
