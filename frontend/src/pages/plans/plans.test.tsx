import { render, screen } from "@testing-library/react";
import { statusSpec } from "../../components/StatusBadge";
import { acceptanceBadge, runtimeBadge } from "../dag/dagStatus";
import { ObjectView, PlanAssumptions } from "./ContentViewer";
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

  it("shows the planner's assumptions, unknowns and material questions as DATA (§05 step 7)", () => {
    render(
      <PlanAssumptions
        content={{
          assumptions: [{ statement: "Pengguna tunggal", source: "prompt" }],
          unknowns: [{ statement: "Target hosting belum dipilih", impact: "Rilis tertunda" }],
          material_questions: [{ question: "Data apa yang sensitif?", category: "security", blocking: true }],
          facts: [],
        }}
      />,
    );
    expect(screen.getByText(/berstatus DATA\/proposal/)).toBeInTheDocument();
    expect(screen.getByText("Pengguna tunggal")).toBeInTheDocument();
    expect(screen.getByText("Target hosting belum dipilih")).toBeInTheDocument();
    expect(screen.getByText("Dampak: Rilis tertunda")).toBeInTheDocument();
    expect(screen.getByText("Data apa yang sensitif?")).toBeInTheDocument();
    expect(screen.getByText("Memblokir")).toBeInTheDocument();
    expect(screen.getByText("Tidak ada fakta tercatat")).toBeInTheDocument();
  });
});
