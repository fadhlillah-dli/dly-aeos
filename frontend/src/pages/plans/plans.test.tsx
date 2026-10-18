import { render, screen } from "@testing-library/react";
import { statusSpec } from "../../components/StatusBadge";
import { acceptanceBadge, runtimeBadge } from "../dag/dagStatus";
import { ObjectView } from "./ContentViewer";
import { parseTargets } from "./DecisionPanel";

describe("approval targets", () => {
  it("parses exact identifiers and drops rows without them", () => {
    expect(
      parseTargets([
        { provider: "google-docs", identifiers: "root_id=F1" },
        { provider: "confluence", identifiers: "space=ENG; parent_id=12=3" },
        { provider: "github-repo", identifiers: "" },
      ]),
    ).toEqual([
      { provider: "google-docs", root_id: "F1" },
      { provider: "confluence", space: "ENG", parent_id: "12=3" },
    ]);
  });
});

describe("DAG status honesty (§14, §50)", () => {
  it("never shows acceptance without an ACCEPTED record", () => {
    expect(acceptanceBadge({ status: "NONE", valid: false }).badge).not.toBe("ACCEPTED");
    expect(acceptanceBadge({ status: "UNKNOWN", valid: null }).badge).toBe("UNKNOWN");
    expect(acceptanceBadge({ status: "ACCEPTED", valid: null }).badge).toBe("UNVERIFIED");
    expect(acceptanceBadge({ status: "ACCEPTED", valid: false }).badge).toBe("STALE");
    expect(acceptanceBadge({ status: "ACCEPTED", valid: true }).badge).toBe("ACCEPTED");
  });

  it("runtime states without records are not a pass", () => {
    expect(statusSpec(runtimeBadge("NOT_SCHEDULED").badge).tone).not.toBe("ok");
    expect(statusSpec(runtimeBadge("REVIEW").badge).tone).not.toBe("ok");
    expect(statusSpec(runtimeBadge("READY").badge).tone).not.toBe("ok");
  });
});

describe("content viewer", () => {
  it("renders nested plan content with Indonesian labels", () => {
    render(<ObjectView value={{ vision: "Visi X", objectives: ["A", "B"], acceptance_criteria: [] }} />);
    expect(screen.getByText("Visi")).toBeInTheDocument();
    expect(screen.getByText("Visi X")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
    expect(screen.getByText("Acceptance criteria")).toBeInTheDocument();
  });
});
