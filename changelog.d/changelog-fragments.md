### Added

- **Changelog fragments instead of editing `CHANGELOG.md`.** A pull request now adds one new file
  `changelog.d/<short-slug>.md`; `scripts/changelog.py --check` validates the fragments and the `CHANGELOG.md`
  structure (CI, `browser e2e + a11y smoke [G3]` job, no job renamed) and `--assemble [--dry-run]` moves them into
  `## [Unreleased]` at release time, deleting them (idempotent). This prevents the class of incident seen on
  2026-10-26, when almost every pull request merge hit a `CHANGELOG.md` conflict that forced another full CI run.
  The hygiene test accepts fragments as part of `[Unreleased]`. Bite tests in a temporary git repository: two
  branches adding different fragments merge cleanly while the same two edits to `[Unreleased]` conflict
  (negative control); malformed fragments (no heading, two headings, conflict marker, trailing whitespace, unknown
  section, no bullet) are red and near-misses are green; the assembled result passes the structure checks and
  a second run changes nothing. Evidence E1 (static) and E2 (unit run on macOS arm64); Windows and macOS Intel
  runs and the CI step itself are NOT-PROVEN until the first CI run. Hardened after a blind review, which prevents
  a fragment that passes `--check` from corrupting the next `--assemble`: a `[label]: url` line at column 0 (it
  ended `[Unreleased]` early) and a `##`/`###` line inside a code fence are refused, `[Unreleased]` now ends only at
  the next `## ` heading, hidden junk such as `.DS_Store` is ignored, an unreadable `changelog.d/` exits 2 instead of
  1, `--assemble` keeps the file permissions of `CHANGELOG.md`, and the CI-wiring test has red and green fixtures.
  Evidence E2 on macOS arm64; the permission and chmod tests are skipped on Windows (NOT-PROVEN there).
  The fragment format is defined only in `scripts/changelog.py`: a second checker that had been written beside it
  (it rejected `changelog.d/README.md` and the documented continuation lines) was removed, and the bite tests of
  `tests/unit/test_changelog_fragments.py` cover a heading-only fragment, a heading without `###`, an empty
  directory and a README alone. The ordering of `[Unreleased]` after a hand-edited link definition is asserted
  without going through the module under test.
