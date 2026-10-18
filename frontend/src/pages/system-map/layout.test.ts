import type { MapGroup, MapNode, RelationView } from "../../components/inventory/types";
import { edgeVisual, foldGraph, groupId, hideNodes, layoutGraph, worstStatus } from "./layout";

function node(id: string, category: string, present = true): MapNode {
  return {
    id,
    label: id,
    name: id,
    kind: "service",
    category,
    requirement: "REQUIRED",
    design_label: "SELECTED_DESIGN",
    domains: ["PLATFORM"],
    identity_verified: "UNVERIFIED",
    present,
    status: present ? "INSTALLED" : "UNKNOWN",
    at: null,
    stale: false,
    basis: [],
    drift: [],
    function: "",
  };
}

function edge(id: string, source: string, target: string, status = "DESIGN"): RelationView {
  return {
    id,
    source,
    target,
    source_name: source,
    target_name: target,
    relation: "calls",
    lens: "DESIGN",
    status,
    provenance: "manifest",
    data_description: "",
    operation: "CALL",
    boundary: null,
    identity_ref: null,
    direction: `${source} -> ${target}`,
    last_observed_at: null,
    freshness: { at: null, stale: false },
    tombstoned_at: null,
    evidence: "",
  };
}

const nodes = [node("ui", "experience/intake"), node("cp", "planning/governance"), node("db", "data/secrets"), node("vault", "data/secrets", false)];
const edges = [edge("e1", "ui", "cp"), edge("e2", "cp", "db", "OBSERVED"), edge("e3", "cp", "vault", "UNKNOWN"), edge("e4", "db", "vault")];
const groups: MapGroup[] = [
  { id: "experience/intake", label: "Pengalaman", node_ids: ["ui"], counts: { total: 1, present: 1, drift: 0 } },
  { id: "planning/governance", label: "Perencanaan", node_ids: ["cp"], counts: { total: 1, present: 1, drift: 0 } },
  { id: "data/secrets", label: "Data & secret", node_ids: ["db", "vault"], counts: { total: 2, present: 1, drift: 0 } },
];

describe("system map layout helpers", () => {
  it("keeps every node when nothing is folded", () => {
    const g = foldGraph(nodes, edges, groups, new Set());
    expect(g.nodes.map((n) => n.id).sort()).toEqual(["cp", "db", "ui", "vault"]);
    expect(g.edges).toHaveLength(4);
  });

  it("folds a category into one summary node and aggregates edges without upgrading evidence", () => {
    const g = foldGraph(nodes, edges, groups, new Set(["data/secrets"]));
    const folded = g.nodes.find((n) => n.id === groupId("data/secrets"))!;
    expect(folded.isGroup).toBe(true);
    expect(folded.count).toBe(2);
    expect(folded.presentCount).toBe(1);
    const agg = g.edges.find((e) => e.source === "cp" && e.target === groupId("data/secrets"))!;
    expect(agg.count).toBe(2);
    expect(agg.status).toBe("UNKNOWN"); // OBSERVED + UNKNOWN -> UNKNOWN, never "observed"
    expect(g.edges.some((e) => e.source === e.target)).toBe(false); // internal edge dropped
  });

  it("orders statuses from most to least proven", () => {
    expect(worstStatus("OBSERVED", "DESIGN")).toBe("DESIGN");
    expect(worstStatus("CONFIGURED", "STALE")).toBe("STALE");
  });

  it("hides nodes and their dangling edges", () => {
    const g = hideNodes(foldGraph(nodes, edges, groups, new Set()), (n) => n.present);
    expect(g.nodes.map((n) => n.id)).not.toContain("vault");
    expect(g.edges.every((e) => e.target !== "vault")).toBe(true);
  });

  it("lays out nodes left to right deterministically", () => {
    const g = foldGraph(nodes, edges, groups, new Set());
    const a = layoutGraph(g.nodes, g.edges);
    const b = layoutGraph(g.nodes, g.edges);
    expect(a).toEqual(b);
    expect(a.ui.x).toBeLessThan(a.cp.x);
    expect(a.cp.x).toBeLessThan(a.db.x);
  });

  it("labels every edge status with text, unknown is never green", () => {
    expect(edgeVisual("UNKNOWN").label).toBe("UNKNOWN");
    expect(edgeVisual("UNKNOWN").stroke).not.toContain("ok");
    expect(edgeVisual("DESIGN").dash).toBeTruthy();
    expect(edgeVisual("OBSERVED").animated).toBe(true);
  });
});
