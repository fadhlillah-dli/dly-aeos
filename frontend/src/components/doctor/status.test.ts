import { isOk } from "./status";

describe("isOk", () => {
  it("uses the check's own ok-set and never passes UNKNOWN/STALE/EXPIRED", () => {
    expect(isOk({ effective_status: "VERIFIED", ok_statuses: ["VERIFIED"] })).toBe(true);
    expect(isOk({ effective_status: "CONFIGURED", ok_statuses: [] })).toBe(false);
    expect(isOk({ effective_status: "CONNECTED", ok_statuses: ["CONNECTED", "VERIFIED"] })).toBe(true);
    for (const s of ["UNKNOWN", "STALE", "EXPIRED", "FAILED", "MISSING"]) {
      expect(isOk({ effective_status: s, ok_statuses: ["VERIFIED", "CONFIGURED"] })).toBe(false);
    }
    expect(isOk({ effective_status: "NOT_APPLICABLE", ok_statuses: ["VERIFIED"] })).toBe(true);
  });
});
