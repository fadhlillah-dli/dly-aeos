import { render, screen } from "@testing-library/react";
import { StatusBadge, statusSpec } from "./StatusBadge";

describe("StatusBadge", () => {
  it("always renders a text label, not only colour (§35)", () => {
    render(<StatusBadge status="CONNECTED" />);
    expect(screen.getByText(/Terhubung/)).toBeInTheDocument();
  });

  it("does not render CONNECTED as verified/ready", () => {
    expect(statusSpec("CONNECTED").tone).not.toBe("ok");
  });

  it("treats unknown/unverified as warning, never as pass", () => {
    expect(statusSpec("UNKNOWN").tone).toBe("warn");
    expect(statusSpec("NOT-PROVEN").tone).toBe("warn");
    expect(statusSpec(undefined).tone).toBe("warn");
  });

  it("labels mock data distinctly", () => {
    expect(statusSpec("MOCK").tone).toBe("mock");
  });
});
