# ADR-0023 — ContextPack retrieval at a known commit; human-accepted, partitioned memory

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Knowledge & Context module

## Context

§24 starts retrieval with git search/symbol retrieval and metadata search; repository context is a
file/symbol subset of a known commit; policy context is read-only with version/hash and is never
reconstructed; research context stays DATA; retrieval from an old base is stale; memory stores
accepted facts with scope/AUTH/expiry/supersession, never secrets or untrusted instructions, and is
not used across projects for sensitive data. §50: "Memory lama/cross-project mencoba menjadi
kebijakan" must be rejected.

## Decision

1. **ContextPack** (`aeos.knowledge.packs.build_pack`) per task/role: role profiles choose item
   kinds; TaskSpec `allowed_paths`/`forbidden_paths` and request patterns apply on top of default
   forbidden globs (`.env*`, keys, credentials, DB files, `.git/`). Repository items come from
   `git ls-tree`/`git grep -n`/`git cat-file blob` at the resolved commit; symlinks and submodules
   are never read and files over 512 KiB are skipped with a warning (never a truncated prefix). Metadata items come from the domain-scoped search, filtered to the project and global
   records. Policy items are the trusted controller's `policy/` files (sha256, `POLICY_READ_ONLY`);
   missing policy is reported. Attachments are `UNTRUSTED_DATA`, research `DATA`, memory
   `MEMORY_DATA`; none grants permissions.
2. Each item records kind/ref/title/provenance/classification/sha256/token estimate (chars/4 + 16)
   and redaction status. Items are added in priority order until the budget (1 024–200 000,
   default 8 000) is used; truncatable items are cut to fit, the rest are listed as omitted.
   Policy and task context are mandatory (`BUDGET_TOO_SMALL` otherwise). The pack classification
   is the highest of project, task and item classifications; the pack hash covers request
   identity and item hashes.
3. **Staleness, TTL and supersession** (§13 ContextPack "TTL/expiry, supersession"): a pack is STALE
   (sticky) when the repository head differs from its base commit, the policy file set snapshotted
   at build time changed (file changed, added or removed), its TTL (`ttl_hours`, 1–720, default
   168) expired, or a newer pack for the same project/TaskSpec/role/repository superseded it
   (`superseded_by`). Ad-hoc packs without a TaskSpec do not supersede each other. Reading a pack
   re-checks; repository observation marks packs of that repository stale; an unreachable checkout
   reports `UNKNOWN`, never `CURRENT`.
4. **Memory**: proposals only from accepted facts — ACTIVE Decision, approved PlanVersion,
   ACCEPTED ADR (platform project) or AcceptanceRecord, or an explicit human statement;
   attachment/worker/model/research sources, secrets and injection markers are refused.
   Classification never drops below the project's. DOMAIN-scope (cross-project) memory only for
   PUBLIC/INTERNAL; partitioned by domain; a project-scoped principal never receives DOMAIN-scope
   memory owned by a project outside its scope. Accept/reject record a Decision via
   `aeos.policy.facade.record_decision` bound to the record hash; accept fails closed
   (`DECISION_REGISTRY_UNAVAILABLE`) when the registry is unavailable, reject proceeds (it grants
   nothing) and is audited. Expiry and supersession keep history; nothing is deleted.

## Consequences

* No embedding/vector index in V1; Serena/Context7/codebase-memory-mcp remain candidates (§24).
* Packs store item content in the metadata DB (bounded by the budget); classification and domain
  scoping apply to every read.
