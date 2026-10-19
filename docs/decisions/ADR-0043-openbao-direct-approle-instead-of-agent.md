# ADR-0043 — Direct AppRole login in the controller instead of OpenBao Agent (local single host)

* Status: PROPOSED
* Date: 2026-10-19
* Deciders: owner (Fadhlillah) - review pending; written by the implementation agent

## Context

Master §45 names "OpenBao / Agent / AppRole" as the default secret architecture (S24: Agent or proxy
for auto-auth, token renewal and templated secrets). ADR-0042 implemented OpenBao + AppRole and noted
that the Agent part is not implemented. The audit asked for either an Agent mode or a recorded decision.

## Decision

1. For the local-first, single-host controller, the **controller process authenticates itself**
   with AppRole (`role_id` + `secret_id` from the OS credential store, ADR-0042) and keeps the token
   in memory only. This replaces the Agent's *auto-auth* and *token renewal* functions:
   `OpenBaoSecretProvider` renews a renewable token before its deadline (`renew_self`) and logs in
   again when renewal fails or the token is revoked (`backend/aeos/secrets/provider.py`).
2. **No Agent sink and no templated secret files.** An Agent would write a token (or rendered
   secrets) to a sink file on disk; on a single host that file adds an at-rest copy of a credential
   without adding isolation. Workers never receive secrets at all (the capability broker proxies
   model/git/PR calls, ADR-0053), so templating secrets into worker sandboxes is not needed.
3. **OpenBao Agent stays a CANDIDATE**, not a REQUIRED component. It becomes relevant when (a) a
   second host or a container runtime must authenticate separately, or (b) an OFFICE deployment
   requires a company-managed Agent/proxy. Adopting it is a change proposal (§39) with its own pin.
4. **OFFICE AppRole** is not created until a company authorisation exists (§23 default deny); the
   OFFICE KV path is denied by policy for the controller AppRole (ADR-0042).

## Consequences

* The "Agent" part of the §45 default is satisfied by an equivalent in-process mechanism for this
  topology; it is NOT-PROVEN against a live OpenBao on this host until `aeos vault install/start/init`
  is run and `tests/integration/test_secrets_openbao_live.py` passes (skips without `bao`).
* No secret or token is ever written by AEOS to a sink file, config file or the repository.
