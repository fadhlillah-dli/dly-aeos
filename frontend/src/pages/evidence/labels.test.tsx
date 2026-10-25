import { render, screen } from "@testing-library/react";
import { Status } from "./labels";

describe("verifier isolation labels (§20 isolated runner)", () => {
  it("states the WEAK process runner as degraded", () => {
    render(<Status status="WEAK" />);
    expect(screen.getByText("Isolasi verifier lemah (proses, bukan sandbox)")).toBeInTheDocument();
  });

  it("names the container runner instead of showing an unknown raw status", () => {
    render(<Status status="CONTAINER" />);
    expect(screen.getByText("Isolasi verifier: container (boundary terbukti di host ini)")).toBeInTheDocument();
    expect(screen.queryByText("CONTAINER")).not.toBeInTheDocument();
  });
});
