import { render, screen } from "@testing-library/react";
import type { RelationView } from "../../components/inventory/types";
import { DataFlowView } from "./DataFlow";

const rel = (over: Partial<RelationView>) => ({
  id: "r", source: "a", target: "b", source_name: "A", target_name: "B", relation: "depends_on", lens: "DESIGN",
  status: "DESIGN", provenance: "", data_description: "", operation: "", boundary: null, identity_ref: null,
  direction: "out", last_observed_at: null, freshness: { at: null, stale: false }, tombstoned_at: null, evidence: "",
  ...over,
}) as RelationView;

describe("Data-flow view (§36: org map, component map, DAG and data flow are distinct views)", () => {
  it("lists only data-carrying edges, grouped by trust boundary", () => {
    render(<DataFlowView edges={[
      rel({ id: "1", source_name: "Publisher", target_name: "Google Drive", relation: "publishes_to", operation: "WRITE",
        data_description: "Product 1 docs", boundary: "PERSONAL cloud" }),
      rel({ id: "2", source_name: "Verifier", target_name: "Evidence store", relation: "writes", data_description: "test reports" }),
      rel({ id: "3", source_name: "UI", target_name: "API", relation: "depends_on" }), // no data semantics
    ]} />);
    expect(screen.getByText("Batas kepercayaan: PERSONAL cloud")).toBeInTheDocument();
    expect(screen.getByText("Batas kepercayaan: dalam batas yang sama")).toBeInTheDocument();
    expect(screen.getByText("Product 1 docs")).toBeInTheDocument();
    expect(screen.getByText("test reports")).toBeInTheDocument();
    expect(screen.queryByText("UI")).not.toBeInTheDocument();
  });

  it("says so when the lens has no data flow", () => {
    render(<DataFlowView edges={[rel({ id: "3" })]} />);
    expect(screen.getByText(/Tidak ada aliran data tercatat/)).toBeInTheDocument();
  });
});
