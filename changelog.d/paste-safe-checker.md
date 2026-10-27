### Added

- **Paste-safety checker for owner commands.** `scripts/paste_safe.py --check FILE ...` (also `-` for standard
  input and `--text 'CMD'`; exit 0 OK, 1 findings, 2 usage; last line `PASTE_SAFE: OK` or `PASTE_SAFE: FAIL`) now
  owns the paste-safety rules that lived inside `tests/unit/test_owner_commands_paste_safe.py` (inline `#`, unquoted
  globs, `rm` after an unproven `cp`, the file-content opt-out; behaviour unchanged), and the test imports it instead
  of keeping a second copy. Three new rules come from two incidents of 2026-10-27 (LESSONS L2, strike 4): a line
  over 80 characters (a chat view wrapped a long command and the paste put a newline inside its quotes, zsh
  `dquote>`); a command that asks for hidden input (any `aeos` command with `--as`, and `admin create`,
  `secrets set`, `vault import-unseal-key`, `vault unseal`) that is not the last line of its block, or two of them
  in one block (the pasted lines after the first prompt were read as the password); and a `<placeholder>` outside
  quotes (zsh reads `<` as a redirection). The list of hidden-prompt commands is checked against the CLI: a test
  derives it from `getpass` and `hide_input` prompts in `backend/aeos` and fails when a command gains one that the
  list lacks. Every owner document that broke the new rules is fixed (long lines split with continuations, each
  password or key prompt moved to the end of its own block, placeholders quoted), mostly `docs/OWNER_ACTIONS.md`,
  plus `README.md`, `docs/SETUP_MACOS.md`, `docs/OPERATIONS.md`, the OpenBao recovery runbook,
  `install-manifest/README.md`, `tests/acceptance/MATRIX.md` and `CONTRIBUTING.md`. LESSONS L2 and rule 10 of the
  delegation brief now say: commands for the owner are drafted into a file and checked with
  `scripts/paste_safe.py --check` before they are shown; one hidden-prompt command per block, last. Evidence: E2 unit
  run on macOS arm64 (violating fixtures red with their message, near misses green); NOT-PROVEN on Windows and
  macOS Intel until CI runs there, and commands shown in chat are checked only when the session runs the checker.
