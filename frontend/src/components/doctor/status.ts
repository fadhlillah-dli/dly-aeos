import type { CheckItem } from "./types";

/**
 * A check is OK only when its effective status is in that check's own ok-set: CONFIGURED is not a pass
 * for every check (policy.mci can never pass automatically), and STALE/EXPIRED/UNKNOWN never pass.
 * NOT_APPLICABLE is not a problem to fix (e.g. an unconfigured OFFICE adapter), but it is not a pass either.
 */
export function isOk(check: Pick<CheckItem, "effective_status" | "ok_statuses">): boolean {
  if (check.effective_status === "NOT_APPLICABLE") return true;
  return (check.ok_statuses ?? ["VERIFIED"]).includes(check.effective_status);
}
