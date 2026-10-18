import { StatusBadge } from "../StatusBadge";
import { planStateSpec, runStatusSpec } from "./planState";

/** Planning state as text + icon + colour; raw §14 state in the tooltip. */
export function PlanStateBadge({ state, className }: { state: string | null | undefined; className?: string }) {
  const spec = planStateSpec(state);
  return (
    <span title={`${state ?? "UNKNOWN"} — ${spec.hint}`} className={className}>
      <StatusBadge status={spec.badge} label={spec.label} />
    </span>
  );
}

export function RunStatusBadge({ status }: { status: string | null | undefined }) {
  const spec = runStatusSpec(status);
  return (
    <span title={status ?? "NOT_RUN"}>
      <StatusBadge status={spec.badge} label={spec.label} />
    </span>
  );
}
