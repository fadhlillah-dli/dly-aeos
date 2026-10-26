import { StatusBadge } from "../StatusBadge";

/** Shown once next to TaskSpec verifier policies: what the planner's contract cannot express yet. */
export const PLANNER_CONTRACT_NOTE =
  "Kontrak TaskSpec planner v1 belum dapat mengisi verifier_policy.integration_tests atau contract_breaking: task hasil planner tidak " +
  "menjalankan tes integration-marked, dan perubahan kontrak breaking (facade/OpenAPI) tetap diblokir verifier.";

const str = (v: unknown) => (v === null || v === undefined ? "" : String(v).trim().toLowerCase());

/**
 * TaskSpec verifier_policy opt-ins, read-only (the TaskSpec is immutable and bound by its hash). Absent opt-ins are
 * stated as what the verifier then does — never as a pass.
 */
export function VerifierPolicyFacts({ policy }: { policy: Record<string, unknown> | undefined }) {
  if (!policy) return <span className="text-xs text-ink-3">tidak dinyatakan API</span>;
  const integration = str(policy.integration_tests);
  const breaking = str(policy.contract_breaking);
  const roundtrip = str(policy.migration_roundtrip);
  const isolated = str(policy.isolated_runner);
  const suites = Array.isArray(policy.suites) ? policy.suites.map(String) : [];
  return (
    <div className="flex flex-col gap-1 text-xs" aria-label="Kebijakan verifier (hanya baca)">
      <span className="flex flex-wrap items-center gap-1">
        <span className="text-ink-3">integration_tests</span>
        {integration === "required" ? (
          <StatusBadge status="CONFIGURED" label="wajib (required)" />
        ) : integration === "run" ? (
          <StatusBadge status="CONFIGURED" label="dijalankan (run)" />
        ) : (
          <StatusBadge status="NOT_RUN" label="tidak diaktifkan" />
        )}
      </span>
      <span className="flex flex-wrap items-center gap-1">
        <span className="text-ink-3">contract_breaking</span>
        {breaking === "approved" ? (
          <StatusBadge status="CONFIGURED" label="disetujui (dinyatakan, tidak memblokir)" />
        ) : (
          <StatusBadge status="DISABLED" label="tidak disetujui (breaking diblokir)" />
        )}
      </span>
      {roundtrip === "optional" && <span className="text-warn">migration_roundtrip=optional (round-trip tidak memblokir)</span>}
      {isolated === "required" && <span className="text-ink-2">isolated_runner=required</span>}
      {policy.negative_tests_required === false && <span className="text-warn">negative_tests_required=false</span>}
      {suites.length > 0 && <span className="text-ink-3">suite: {suites.join(", ")}</span>}
    </div>
  );
}
