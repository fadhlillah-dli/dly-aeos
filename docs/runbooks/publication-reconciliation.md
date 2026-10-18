# Runbook: Publication reconciliation

## Trigger

A publication is PARTIAL, CONFLICT, FAILED or has operations in UNKNOWN/IN_FLIGHT; a provider timed
out after creating a resource (§50 "Provider timeout setelah create resource"); a Google Doc was
edited by a human during an update (§50 "requiredRevisionId conflict"); the SLI `sync_problems` is
non-zero.

## Authority

Owner. Re-publishing uses the **approved** PlanVersion/hash only — never "latest" (§50). A human
edit on the official document is input for a revision, it is never overwritten.

## Steps

1. **Open** *Riwayat & Pemulihan* → *Publikasi* (or *Dokumen & Publikasi*): see per-operation
   status (SUCCEEDED / FAILED / UNKNOWN / CONFLICT) and the bound approval hash.
2. **UNKNOWN or timed-out operations**: do not retry blindly. Reconcile first — look up the target
   by its idempotency key / logical id at the provider (Drive folder, Docs title, GitHub issue
   label) and record the found external id. The publisher's reconciliation does this through its
   outbox; use the publisher CLI (`aeos publish --help`) or the publication page.
3. **CONFLICT (revision changed)**: read the human edit, bring it into a revision request on the
   plan, approve the new version, then publish that version. Never force-overwrite.
4. **FAILED**: fix the cause (expired credential → [credential-rotation](credential-rotation.md);
   missing permission → provider console), then retry only the failed operations. Successful
   operations stay recorded and are not recreated.
5. **Verify** in the provider UI that exactly one resource exists per logical id.

## Verification

* Publication status becomes PUBLISHED only when every operation SUCCEEDED; otherwise it stays
  PARTIAL with the remaining items visible.
* `aeos ops sli --domain PERSONAL`: `sync_problems` back to 0, `sync_freshness_seconds` recent.

## Evidence

* Publication operations with external ids/revisions; audit of each retry; export snapshot
  `GET /api/ops/export?kind=audit` for the incident record if needed.
