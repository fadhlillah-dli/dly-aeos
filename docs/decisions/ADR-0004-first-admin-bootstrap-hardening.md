# ADR-0004 — First-admin bootstrap hardening and local recovery

* Status: ACCEPTED
* Date: 2026-10-19
* Deciders: owner (Fadhlillah)

## Context

Master §02 requires the bootstrap hardening to be recorded as an ADR and tested together with
recovery; §26 (auth lokal) requires no default credentials, Argon2id password hashes, a local and
authorised recovery with a one-time temporary credential plus session revocation (never a public
bootstrap credential), and audited authentication events.

## Decision

1. **No default credentials.** The UI never creates the first account. The first SUPER_ADMIN is
   created only by a local CLI action on the machine that owns the data directory
   (`aeos admin create`); `bootstrap_admin` refuses while an active SUPER_ADMIN exists. The login
   page only learns `bootstrap_complete` (no secret, no account name).
2. **Password policy.** At least 12 characters, at least 6 distinct characters, not a common
   password and not containing the username (`identity/passwords.py`).
3. **Hashing.** Argon2id (`argon2-cffi`, `type=Type.ID`) with pinned RFC 9106 low-memory parameters
   `t=3, m=64 MiB, p=4`, 16-byte salt, 32-byte hash. A hash with weaker or different parameters is
   re-hashed at the next successful login.
4. **Sessions.** Server-side opaque sessions; the cookie carries a random token, the database
   stores only its SHA-256; HttpOnly + SameSite=Strict; absolute and idle expiry; CSRF token bound
   to the session for every unsafe method; loopback Host/Origin allow-lists.
5. **Throttling.** Per username and per client address within `login_lockout_seconds`; a throttled
   attempt is refused before the password is checked.
6. **Recovery** (`aeos admin recover <user>`, local CLI only, no HTTP route): issues a random
   temporary password that is valid for **one** successful login within **30 minutes**, revokes
   every session of that user and forces a password change (the session is limited to
   self-service until then). A second login with the same temporary password is refused
   (`TEMP_PASSWORD_USED`), as is any login after expiry (`TEMP_PASSWORD_EXPIRED`); a new recovery
   is then required. A successful password change clears the temporary credential.
7. **Audit.** `identity.login` (SUCCESS; DENIED with `invalid credentials`, `throttled`,
   `temporary password already used/expired`), `identity.logout`, `identity.user.recover`,
   `identity.password.change` (SUCCESS / DENIED, the denial is committed before the error is
   returned), `session.domain.select`, `identity.user.create`, `identity.bootstrap.complete`.

## Threat model (summary)

| Threat | Control |
|---|---|
| Default/guessable admin on a fresh install | No default account; CLI-only bootstrap; password policy |
| Offline cracking of a stolen database | Argon2id with pinned memory-hard parameters; token hashes only |
| Online guessing | Throttling per user and per client; audited denials |
| Recovery credential reused or leaked later | Single use, 30-minute expiry, all sessions revoked, forced change |
| CSRF / DNS rebinding from a browser tab | Session-bound CSRF header; Host and Origin allow-lists (loopback) |
| Recovery through the network | No HTTP route; recovery requires local access to the data directory |

## Consequences

* Losing every SUPER_ADMIN password requires local CLI access, by design.
* Evidence: `tests/unit/test_identity.py`, `tests/unit/test_identity_hardening.py`
  (Argon2id parameters, audited events, single-use/expiring recovery),
  `tests/acceptance/test_auth_boundary.py` (no default credentials, boundary).
