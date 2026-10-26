### Fixed

- **Owner commands are paste-safe in zsh.** `tests/unit/test_owner_commands_paste_safe.py` scans the fenced
  bash, sh, zsh, shell and console blocks of every Markdown file of the repository and fails on a `#` comment outside quotes or an unquoted glob
  character (`[ ] ? *`). Fences are recognised the CommonMark way (inside list items and blockquotes, also nested ones such as a list inside a list or a quote inside a list item, closer at
  least as long as the opener, bare closer only), untagged fences whose first line starts like a shell command
  are scanned too, `<<<` and quoted `<<` no longer start a heredoc, and continuation lines of a console command
  are scanned. This prevents the incident class where a pasted `cmd  # note` hands the comment to the command
  and an unquoted `.[dev]` stops with zsh `no matches found`. Every existing violation in `README.md`,
  `CONTRIBUTING.md`, `changelog.d/README.md`, `install-manifest/README.md`, `tests/acceptance/MATRIX.md`,
  `docs/OWNER_ACTIONS.md`, `docs/SETUP_MACOS.md`, `docs/OPERATIONS.md` and `docs/evidence/ui/README.md` is fixed
  by moving the comment into the prose before the block and quoting `".[dev]"` (meaning unchanged). A block that
  shows file contents opts out with `<!-- paste-safe: file-content -->`. Evidence: E1 static plus E2 unit run on
  macOS arm64 (violating fixtures red, near-miss fixtures green); NOT-PROVEN on Windows and Intel macOS until CI
  runs there.
