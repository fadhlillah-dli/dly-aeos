import { render, screen } from "@testing-library/react";
import backend from "./__fixtures__/backend-status-values.json";
import { StatusBadge, hasStatusSpec, statusSpec } from "./StatusBadge";

// R28: TRIAL, UNTESTED and TRIAL_APPROVED had no spec and rendered as a grey raw code with a question mark, like a
// candidate. The fixture lists every status value the backend enums can return (tests/unit/test_status_vocabulary.py
// keeps it equal to them), so a new backend value without a spec fails here by name.
describe("StatusBadge covers every backend status value", () => {
  const groups = backend as Record<string, string[]>;
  for (const [group, values] of Object.entries(groups)) {
    for (const value of values) {
      it(`${group}: ${value}`, () => {
        if (!hasStatusSpec(value)) throw new Error(`StatusBadge has no spec for ${value} (${group})`);
      });
    }
  }

  it("reports the missing value by name (bite)", () => {
    expect(hasStatusSpec("SOMETHING_NEW_FROM_THE_BACKEND")).toBe(false);
    expect(() => {
      const v = "SOMETHING_NEW_FROM_THE_BACKEND";
      if (!hasStatusSpec(v)) throw new Error(`StatusBadge has no spec for ${v}`);
    }).toThrow("StatusBadge has no spec for SOMETHING_NEW_FROM_THE_BACKEND");
  });

  it("does not inherit Object.prototype keys as specs", () => {
    expect(hasStatusSpec("constructor")).toBe(false);
    expect(statusSpec("constructor").label).toBe("constructor");
  });

  it("an active TRIAL route reads as active, a trial admission as approved-for-trial, never as a candidate", () => {
    expect(statusSpec("TRIAL")).toMatchObject({ tone: "ok", label: "Aktif · TRIAL" });
    expect(statusSpec("TRIAL_APPROVED")).toMatchObject({ label: "Disetujui · TRIAL" });
    expect(statusSpec("TRIAL_APPROVED").tone).not.toBe("muted");
    expect(statusSpec("UNTESTED").tone).toBe("muted");
    render(<StatusBadge status="TRIAL" />);
    expect(screen.getByText("Aktif · TRIAL")).toBeInTheDocument();
  });
});
