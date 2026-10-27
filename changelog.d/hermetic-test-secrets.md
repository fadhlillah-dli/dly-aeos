### Security

- **Tests can no longer reach the owner's real secret stores.** On the owner's Mac a test resolved the real GitHub
  token through the real OpenBao (AppRole login from the macOS Keychain) and called the live GitHub API; CI has no
  vault, so it stayed green (class secret, LESSONS L20, AGENTS.md invariant 12). A session-wide SecretGuard in
  `tests/conftest.py` now gives every test an empty in-memory OS keyring, refuses socket connections to the real
  OpenBao loopback ports (default address, `AEOS_OPENBAO_ADDR`, the host's `aeos.yaml`) and drops the provider's
  cached vault client and token, so `get_secret` fails as on a machine without a vault unless the test installs a
  fake (`secrets_memory`, its own keyring, a vault on a free port). Bite tests in
  `tests/unit/test_test_isolation.py` serve a sentinel secret from a sentinel vault and keyring: it is read without
  the guard and refused with `SECRET_STORE_UNAVAILABLE` with it. The duplicate unit-only socket guard is gone. The
  memory-cap test no longer races its task timeout under load (timeout 3600 s, no wall-time bound; LESSONS L19
  strike 2, rule: one full suite at a time with -n logical). Evidence E2 on macOS arm64; NOT-PROVEN on Windows and
  macOS Intel until CI runs.
