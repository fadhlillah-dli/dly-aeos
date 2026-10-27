### Security

- **Tests can no longer reach the owner's real secret stores (one integration exception, named below).** On the
  owner's Mac a test resolved the real GitHub token through the real OpenBao (AppRole login from the macOS
  Keychain) and called the live GitHub API; CI has no vault, so it stayed green (class secret, LESSONS L20,
  AGENTS.md invariant 12). A session-wide SecretGuard in
  `tests/conftest.py` now gives every test an empty in-memory OS keyring, refuses socket connections to the real
  OpenBao loopback ports (default address, `AEOS_OPENBAO_ADDR`, the host's `aeos.yaml`) and drops the provider's
  cached vault client and token, so `get_secret` fails as on a machine without a vault unless the test installs a
  fake (`secrets_memory`, its own keyring, a vault on a free port). Sandboxed child processes (the containment
  probe, workers) get the null keyring through a config file in the sandbox home, so they no longer query the
  macOS Keychain or Windows Credential Manager. The owner's real Claude Code (found on PATH or configured), whose
  `auth status` reads its login from the OS credential store, is refused before it starts; four unit tests ran it on
  the owner's Mac. Integration tests that need it request the `real_claude_cli` fixture and skip unless the owner
  sets `DLY_AEOS_REAL_CLAUDE=1` (or `DLY_AEOS_LIVE_CLAUDE=1`). The opt-in live provider test reads its token
  from the owner's vault through the `real_secret_stores` fixture, which lifts the guard only when the owner sets
  `DLY_AEOS_REAL_SECRETS=1` (it lifts the whole guard for that test, the Claude Code refusal included; the documented
  live-test commands now set it). Not covered: the UI smoke server (integration) still
  looks up the OS keyring, under a random service name. Bite tests in `tests/unit/test_test_isolation.py` serve a
  sentinel secret from a sentinel vault and keyring: it is read without the guard and refused with
  `SECRET_STORE_UNAVAILABLE` with it; a sandboxed child resolves the null keyring only with the guard, and a sentinel
  "real Claude Code" runs only without it. The duplicate unit-only socket guard is gone. The
  memory-cap test no longer races its task timeout under load (timeout 3600 s, no wall-time bound; LESSONS L19
  strike 2, rule: one full suite at a time with -n logical). Evidence E2 on macOS arm64; NOT-PROVEN on Windows and
  macOS Intel until CI runs.
