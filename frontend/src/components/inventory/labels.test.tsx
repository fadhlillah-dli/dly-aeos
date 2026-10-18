import { render, screen } from "@testing-library/react";
import { statusSpec } from "../StatusBadge";
import { Badge, badgeProps } from "./labels";

describe("inventory status aliases", () => {
  it("never renders unproven inventory states with a passing tone", () => {
    for (const s of ["UNREGISTERED", "NO_PIN", "MISMATCH", "DISCOVERED", "DECLARED", "OBSERVED", "HUMAN_ONLY"]) {
      expect(statusSpec(badgeProps(s).status).tone).not.toBe("ok");
    }
    expect(statusSpec(badgeProps("MATCH").status).tone).toBe("ok");
  });

  it("shows Indonesian text for aliased statuses", () => {
    render(<Badge status="UNREGISTERED" />);
    expect(screen.getByText("Tidak terdaftar")).toBeInTheDocument();
  });

  it("passes through known statuses unchanged", () => {
    expect(badgeProps("CONNECTED")).toEqual({ status: "CONNECTED" });
  });
});
