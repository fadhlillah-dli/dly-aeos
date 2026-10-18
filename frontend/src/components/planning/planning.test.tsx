import { render, screen } from "@testing-library/react";
import { statusSpec } from "../StatusBadge";
import { layoutDag } from "./dagLayout";
import { NEEDS_HUMAN_STATES, operationsFor, PLAN_STATES, planStateSpec, runStatusSpec } from "./planState";
import { PlanStateBadge } from "./PlanStateBadge";

const ALL_STATES = [
  "DRAFT", "CONTEXT_READY", "PRIMARY_PLANNING", "CHALLENGE_REVIEW", "SYNTHESIS", "FINAL_REVIEW", "READY_FOR_HUMAN_REVIEW",
  "APPROVED", "PUBLISHING", "PUBLISHED", "REVISION_REQUESTED", "REJECTED", "PLANNING_FAILED", "REVIEW_FAILED",
  "NEEDS_DECISION", "PUBLICATION_PARTIAL", "PUBLICATION_FAILED", "AWAITING_MANUAL_CLAUDE",
];

describe("planning state presentation (§14)", () => {
  it("maps every planning state to a labelled badge", () => {
    expect(Object.keys(PLAN_STATES).sort()).toEqual([...ALL_STATES].sort());
    for (const s of ALL_STATES) expect(planStateSpec(s).label).toBeTruthy();
  });

  it("never renders a waiting or failed state as a pass", () => {
    for (const s of NEEDS_HUMAN_STATES) expect(statusSpec(planStateSpec(s).badge).tone).not.toBe("ok");
    expect(statusSpec(planStateSpec("REJECTED").badge).tone).toBe("bad");
    expect(statusSpec(planStateSpec(undefined).badge).tone).toBe("warn");
  });

  it("run status failures are not shown as success", () => {
    for (const s of ["FAILED", "OUTPUT_INVALID", "MODEL_MISMATCH", "QUOTA_EXHAUSTED", "AWAITING_MANUAL"]) {
      expect(statusSpec(runStatusSpec(s).badge).tone).not.toBe("ok");
    }
    expect(runStatusSpec("SUCCEEDED").label).toMatch(/bukan persetujuan/);
  });

  it("renders text, not only colour", () => {
    render(<PlanStateBadge state="READY_FOR_HUMAN_REVIEW" />);
    expect(screen.getByText("Menunggu keputusan Anda")).toBeInTheDocument();
  });
});

describe("operationsFor mirrors the backend binding", () => {
  const catalog = {
    plan: ["compile_dag", "execute_tasks"],
    publish: { "google-docs": ["create_doc", "update_doc"], "github-repo": [] },
    delivery: { "github-repo": ["push_branch", "create_pr"] },
  };
  it("adds publication ops only for Approve & Publish", () => {
    const t = [{ provider: "google-docs" }, { provider: "github-repo" }];
    expect(operationsFor("APPROVE_AND_PUBLISH", t, catalog)).toEqual(["compile_dag", "execute_tasks", "create_doc", "update_doc", "push_branch", "create_pr"]);
    expect(operationsFor("APPROVE", [{ provider: "github-repo" }], catalog)).toEqual(["compile_dag", "execute_tasks", "push_branch", "create_pr"]);
    expect(operationsFor("APPROVE_AND_PUBLISH", t, catalog)).not.toContain("merge_pr");
  });
});

describe("layoutDag", () => {
  it("places dependencies left of dependants", () => {
    const pos = layoutDag([{ id: "A" }, { id: "B" }, { id: "C" }], [{ source: "A", target: "B" }, { source: "B", target: "C" }, { source: "X", target: "A" }]);
    const x = Object.fromEntries(pos.map((p) => [p.id, p.x]));
    expect(x.A).toBeLessThan(x.B);
    expect(x.B).toBeLessThan(x.C);
  });
});
