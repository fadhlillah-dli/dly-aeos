### Fixed

- **A dry run of an owner script never starts background planning work.** On 2026-10-29 a dry run of a script that sends plan revisions started the real four-stage Opus planning against the throwaway database copy (two `claude -p` processes, the owner's Claude quota). `scripts/owner_dryrun.py` now holds `aeos.planning.runner.start` while the script runs, answers as if it started, and prints what it held; the real starter is restored afterwards (LESSONS L13). Evidence: E2 unit run on macOS arm64; both new tests red without the hold.
