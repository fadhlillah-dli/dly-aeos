### Fixed

- **Owner commands are paste-safe in zsh.** `tests/unit/test_owner_commands_paste_safe.py` scans the fenced
  bash, sh, zsh, shell and console blocks of the owner-facing Markdown (`docs/**/*.md`, `README.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `SETUP*.md`) and fails on a `#` comment outside quotes or an unquoted glob
  character (`[ ] ? *`). Fences are recognised the CommonMark way (inside list items and blockquotes, closer at
  least as long as the opener, bare closer only), untagged fences whose first line starts like a shell command
  are scanned too, `<<<` and quoted `<<` no longer start a heredoc, and continuation lines of a console command
  are scanned. This prevents the incident class where a pasted `cmd  # note` hands the comment to the command
  and an unquoted `.[dev]` stops with zsh `no matches found`. Every existing violation in `README.md`,
  `docs/OWNER_ACTIONS.md`, `docs/SETUP_MACOS.md`, `docs/OPERATIONS.md` and `docs/evidence/ui/README.md` is fixed
  by moving the comment into the prose before the block and quoting `".[dev]"` (meaning unchanged). A block that
  shows file contents opts out with `<!-- paste-safe: file-content -->`. Evidence: E1 static plus E2 unit run on
  macOS arm64 (violating fixtures red, near-miss fixtures green); NOT-PROVEN on Windows and Intel macOS until CI
  runs there.

- **Vault/OpenBao init output is detected and redacted.** The shared credential patterns
  (`aeos.core.redaction`, reused by `scripts/secret_scan.py` and the verifier binary scan) now match the labelled
  forms `Unseal Key <n>: <base64>`, `Recovery Key <n>: <base64>`, `Initial Root Token: <token>`, the
  `unseal_keys_b64` / `unseal_keys_hex` JSON arrays, the `hvb.` / `hvr.` token prefixes (`hvs.` now covers the
  full token alphabet) and the short OpenBao recovery token `r.<24+ alphanumerics>` (not after a letter, digit
  or dot). Runtime redaction matches over the whole text, so a pretty-printed `operator init -format=json` is
  redacted; `scripts/secret_scan.py` now also runs the patterns over each whole file (`multiline_findings`),
  while its per-line pass and the verifier added-line scan cannot see a multi-line form. The label is required,
  so bare base64 such as lockfile hashes never matches (0 matches over all tracked files). `AGENTS.md`
  invariant 12 states that a secret seen in chat is burned: never quote or store it, rotate or re-key it per the
  runbook, touch credentials only on explicit instruction. This prevents the incident class where an unseal key
  is pasted into chat. Evidence: E1 static plus E2 unit run on macOS arm64 (synthetic labelled samples red,
  unlabelled near-misses green); NOT-PROVEN on Windows and Intel macOS until CI runs there.
