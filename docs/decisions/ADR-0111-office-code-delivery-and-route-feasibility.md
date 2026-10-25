# ADR-0111 — OFFICE code delivery on the company code host (Cloud only, disabled by default), route feasibility probe, publication fidelity

* Status: PROPOSED
* Date: 2026-10-19
* Deciders: owner (Fadhlillah) — proposed by the K1 office-delivery re-audit fixer

## Context

The company code host is Bitbucket Cloud (master §04 OFFICE). The title names it generically on purpose: ADR
titles are searchable from the PERSONAL Control Center, which must not surface OFFICE provider names
(`tests/acceptance/test_transparency.py`).

The wave-3 re-audit left OFFICE delivery impossible even after a company authorisation (§01 U-03, §04
"OFFICE CI/PR", §45, §48, §49 M5): no broker-mediated Bitbucket push or PR and no Bitbucket Pipelines CI
reader, only an honest `OFFICE_DELIVERY_NOT_AVAILABLE`. Bitbucket enforcement of merge checks is a Premium
feature (§31, S23) and must never be overclaimed. ARENA/COMPARATOR/LOCAL routes had no controlled execution
path at all (§23, §48). Publication still ran lossy image updates, never checked DOCX→Google Docs import
fidelity and treated invalidated (STALE) readiness as advisory (§08, §29).

## Decision

1. **Bitbucket Cloud delivery, disabled by default.** `aeos.delivery.bitbucket.office_delivery_readiness`
   is AVAILABLE only with an ACTIVE company-authorisation POLICY decision listing `bitbucket` (§23), an
   enabled `bitbucket/code` integration (ADR-0073), deployment **CLOUD** (Data Center → `NOT_SUPPORTED`,
   unknown → BLOCKED), the stored `office/bitbucket` credential and the registered repository whose exact
   identity was VERIFIED. Otherwise prepare-PR refuses (`OFFICE_DELIVERY_NOT_AVAILABLE` /
   `BITBUCKET_DC_NOT_SUPPORTED`), PR sync makes no call and the gates show DISABLED/NOT_SUPPORTED/BLOCKED.
2. **Writes only in the broker** (`aeos.broker.bitbucket_broker`, dispatched by `git_broker.is_bitbucket_run`
   before any GitHub validation or credential read): sha-pinned `<sha>:refs/heads/aeos/*` push from a private
   bare mirror (no force, no deletion, no tags, never main), one draft `POST /pullrequests`
   (`close_source_branch: false`), idempotent outbox + read-only reconciliation, run-bound GIT_BRANCH /
   PR_CREATE grants and the external-write interlock (provider `bitbucket`: company authorisation, write
   enablement, the plan APPROVAL that allows the operation on that repository). The broker re-checks the OFFICE
   target itself (enabled, Cloud, verified, integration root) — it never trusts delivery for it. The
   credentialed git remote is pinned to the repository's own `https://bitbucket.org/<workspace>/<repo_slug>(.git)`:
   any other host or repository is `REMOTE_REJECTED` before the credential is read (besides it only a local bare
   remote, which gets no credential, is accepted). A PR retry after an UNKNOWN/PENDING attempt reads Bitbucket
   first (all PR states) and adopts an existing PR or stays UNKNOWN when unreadable — never a blind second POST
   (§50). Reconciliation reads with the OFFICE credential only for the exact OFFICE target and while the company
   authorisation lists `bitbucket` (`require_office_read`: OFFICE default deny covers reads too, §23). There is
   no merge, decline or approve call anywhere (static tests).
3. **Read side GET only** (`publisher.providers.atlassian.BitbucketProvider`, used by delivery through the
   publisher facade): commit statuses + Pipelines steps of one exact commit → `provider-ci` evidence (E3) for
   G1/G3/G5, mapped by the project's pipeline reference (`bitbucket-pipelines/ci` integration, step names);
   partial reads are NOT_PROVEN, another commit's CI is never attributed, pagination never leaves
   `api.bitbucket.org`. PR observation resolves Bitbucket's abbreviated hashes to full commits (or fails);
   G7 uses `closed_by` and the owner's `release_approvers` decision with `auth_scope.bitbucket_accounts`.
4. **Merge-check entitlement is never overclaimed.** Without an observed `enforce_merge_checks` restriction
   (Premium) the merge gate is `NOT_ENFORCEABLE` with `entitlement: NOT_OBSERVED`; with it, `CONFIGURED` and
   still `enforcement_tested: NOT-PROVEN`. Bitbucket build statuses are not bound to a reporter identity.
5. **OFFICE capability gate** for prepare-PR is `CAN_USE_OFFICE_INTEGRATIONS` (CAN_PREPARE_PR is PERSONAL).
6. **Route feasibility probe** (`broker.facade.probe_route`, `POST /api/broker/routes/{key}/feasibility`,
   `aeos broker probe-route`): ONE fixed synthetic PUBLIC prompt (16 tokens, no tools, no retry, no fallback)
   to a free, PERSONAL, TRIAL/APPROVED route of `PROBE_POOLS` (ARENA/COMPARATOR/LOCAL/WORKER) whose decision is
   bound to the current configuration hash. Recorded as PROBE evidence (E3) + ledger + audit; 429 records the
   quota state. The worker proxy still serves only `SERVED_POOLS = ("WORKER",)`.
7. **Publication.** A Google update that carries rendered images is never run as a lossy text update: a new
   DOCX snapshot (`<logical>@snapshot-<hash>`, images embedded) is planned as its own `create_doc` under the
   publication's human decision; the old document is not overwritten; HUMAN_GOVERNED content in the official
   document stops it (CONFLICT). Every DOCX import is read back (`documents.get`) and compared with the sent
   DOCX (headings, tables, links, images) → CONSISTENT / MISMATCH / UNKNOWN on the operation and the document.
   A CAN_PUBLISH_PERSONAL dependency that passed and was then invalidated (STALE) refuses execute/retry.
8. **Doctor.** Fine-grained PATs are judged by what they reach (`/user/repos` vs registered repos) and by the
   token's own Administration grant. GitHub's repository `permissions.admin` is the *account's* role (the owner
   of a personal repository always shows it), so it is never read as the token's permission: with an admin role
   the Doctor observes the grant read-only (`GET .../branches/<default>/protection`: 403 "Resource not accessible
   by personal access token" → NOT_GRANTED, 200 / 404 "Branch not protected" → GRANTED → not least privilege,
   anything else → UNKNOWN → least privilege NOT proven); `delivery.branch_protection` exposes and evaluates status-check identity, deploy restrictions and
   entitlement (GitHub: OBSERVED with an enforcing rule, NOT_ENTITLED when GitHub answers "upgrade required" or a
   private Free repository 404s, AVAILABLE on "Branch not protected", else UNKNOWN; Bitbucket: OBSERVED /
   NOT_OBSERVED / UNKNOWN as in 4); `delivery.ci_workflow` shows the latest provider-ci observation; `recovery.route_rollback`
   checks that every active route keeps a restorable rollback target (never claims a rollback ran).

## Consequences / still NOT-PROVEN

* Live Bitbucket Cloud behaviour (push over HTTPS with the chosen token kind, PR fields, Pipelines filters,
  branch-restriction kinds) is exercised with respx and local bare remotes only: it needs the company
  authorisation and a tenant (owner, M5). Data Center needs its own adapter.
* DOCX→Google Docs fidelity on the owner's account runs only through the owner-gated
  `tests/integration/test_google_docx_fidelity_live.py`.
* Full READY enforcement of CAN_PUBLISH_PERSONAL (never-run checks blocking) changes the protected acceptance
  suite: owner decision (ADR-0094). Tables in incremental Google updates still become text (disclosed).
* EvalRuns reaching ARENA routes and routing's view of ARENA TRIAL activations belong to the evaluation and
  routing modules; the probe is their controlled execution path to build on.
