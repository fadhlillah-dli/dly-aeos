### Changed

- **CI: three backend shards on macOS Intel.** After PR #52 main went red without a failing test: an Intel shard passed every test in 23 minutes and then hit the 25-minute job limit. The Intel runners vary up to about 2x (11 and 16 minutes an hour earlier), so `macos-26-intel` now runs 3 shards like Windows; the time limit is unchanged, so a real hang still fails (ADR-0115 amendment 2026-10-29, LESSONS L15). Evidence: E1 static; the PR's own CI run is the proof on the Intel runner.
