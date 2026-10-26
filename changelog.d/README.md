# Changelog fragments

`CHANGELOG.md` is never edited by a pull request. Two pull requests that both edit `## [Unreleased]` conflict on
every merge, and each conflict costs another full CI run. Instead, each pull request adds **one new file** here,
`changelog.d/<short-slug>.md`. Two different new files never conflict.

## Format

```
### Added

- **Short title.** What changed, why (the incident class it prevents), and the evidence level
  (E1 static, E2 unit run, E3 real host; anything not run on a host is NOT-PROVEN there).
```

- Line 1 is `### <Section>` with the section one of `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
  `Security`. Line 2 is empty. The bullets follow, the first one starting with `- **`.
- UTF-8, no other heading, no merge conflict markers, no trailing whitespace, no `[label]: url` link
  definition at column 0 (those live in `CHANGELOG.md`; use inline `[text](url)` links).
- One section per file. A change that belongs under two sections is two files.
- Name the file after the change (`github-token-expiry.md`), not after a number, so that two pull requests
  never pick the same name.

## Checks

```
python scripts/changelog.py --check
```

validates every fragment and the structure of `CHANGELOG.md`. CI runs it on every pull request. Exit codes: 0 ok,
1 violations (each printed), 2 usage or IO error.

## Release

The release pull request runs

```
python scripts/changelog.py --assemble --dry-run   # show what would move
python scripts/changelog.py --assemble
```

which moves every fragment into `## [Unreleased]` of `CHANGELOG.md` (under the right `###` section, created in
Keep-a-Changelog order when missing; fragments in filename order, after the bullets already there) and deletes the
fragments. Running it again changes nothing. Then follow "Releasing" in `CONTRIBUTING.md`.
