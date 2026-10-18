import dagre from "@dagrejs/dagre";
import type { MapGroup, MapNode, RelationView } from "../../components/inventory/types";

export const NODE_W = 200;
export const NODE_H = 64;

export interface GNode {
  id: string;
  label: string;
  category: string;
  kind: string;
  status: string;
  present: boolean;
  drift: number;
  requirement: string;
  isGroup: boolean;
  count?: number;
  presentCount?: number;
}

export interface GEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  status: string;
  count: number;
  relationIds: string[];
}

export const groupId = (category: string) => `group:${category}`;

/** Replace the nodes of folded categories by one summary node and aggregate their edges (§36 fold). */
export function foldGraph(nodes: MapNode[], edges: RelationView[], groups: MapGroup[], folded: Set<string>): { nodes: GNode[]; edges: GEdge[] } {
  const owner = new Map<string, string>();
  const out: GNode[] = [];
  for (const g of groups) {
    if (!folded.has(g.id)) continue;
    const members = nodes.filter((n) => n.category === g.id);
    if (!members.length) continue;
    members.forEach((m) => owner.set(m.id, groupId(g.id)));
    out.push({
      id: groupId(g.id),
      label: g.label,
      category: g.id,
      kind: "group",
      status: "FOLDED",
      present: members.some((m) => m.present),
      drift: members.reduce((s, m) => s + m.drift.length, 0),
      requirement: "",
      isGroup: true,
      count: members.length,
      presentCount: members.filter((m) => m.present).length,
    });
  }
  for (const n of nodes) {
    if (owner.has(n.id)) continue;
    out.push({
      id: n.id,
      label: n.label,
      category: n.category,
      kind: n.kind,
      status: n.status,
      present: n.present,
      drift: n.drift.length,
      requirement: n.requirement,
      isGroup: false,
    });
  }
  const agg = new Map<string, GEdge>();
  for (const e of edges) {
    const s = owner.get(e.source) ?? e.source;
    const t = owner.get(e.target) ?? e.target;
    if (s === t) continue;
    const folding = owner.has(e.source) || owner.has(e.target);
    const key = folding ? `${s}->${t}` : e.id;
    const prev = agg.get(key);
    if (prev) {
      prev.count += 1;
      prev.relationIds.push(e.id);
      if (prev.status !== e.status) prev.status = worstStatus(prev.status, e.status);
    } else {
      agg.set(key, { id: key, source: s, target: t, relation: folding ? "aggregated" : e.relation, status: e.status, count: 1, relationIds: [e.id] });
    }
  }
  return { nodes: out, edges: [...agg.values()] };
}

const SEVERITY: Record<string, number> = { OBSERVED: 0, CONFIGURED: 1, DESIGN: 2, STALE: 3, UNKNOWN: 4 };

/** The less-proven status wins when edges are aggregated (never upgrade evidence by folding). */
export function worstStatus(a: string, b: string): string {
  return (SEVERITY[a] ?? 4) >= (SEVERITY[b] ?? 4) ? a : b;
}

export function hideNodes(graph: { nodes: GNode[]; edges: GEdge[] }, keep: (n: GNode) => boolean): { nodes: GNode[]; edges: GEdge[] } {
  const nodes = graph.nodes.filter(keep);
  const ids = new Set(nodes.map((n) => n.id));
  return { nodes, edges: graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
}

/** Deterministic dagre layout; returns top-left positions for React Flow. */
export function layoutGraph(nodes: GNode[], edges: GEdge[], direction: "LR" | "TB" = "LR"): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: direction, nodesep: 22, ranksep: 80, marginx: 10, marginy: 10 });
  g.setDefaultEdgeLabel(() => ({}));
  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  edges.forEach((e) => {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  });
  dagre.layout(g);
  const pos: Record<string, { x: number; y: number }> = {};
  nodes.forEach((n) => {
    const p = g.node(n.id) as { x: number; y: number } | undefined;
    pos[n.id] = p ? { x: p.x - NODE_W / 2, y: p.y - NODE_H / 2 } : { x: 0, y: 0 };
  });
  return pos;
}

export interface EdgeVisual {
  stroke: string;
  dash?: string;
  animated: boolean;
  label: string;
}

/** Edge style per evidence status: style AND text label, never colour alone (§35). */
export function edgeVisual(status: string): EdgeVisual {
  switch (status) {
    case "OBSERVED":
      return { stroke: "var(--color-ok)", animated: true, label: "teramati" };
    case "CONFIGURED":
      return { stroke: "var(--color-ok)", animated: false, label: "terkonfigurasi" };
    case "DESIGN":
      return { stroke: "var(--color-info)", dash: "6 4", animated: false, label: "rancangan" };
    case "STALE":
      return { stroke: "var(--color-warn)", dash: "4 4", animated: false, label: "usang" };
    default:
      return { stroke: "var(--color-muted)", dash: "2 4", animated: false, label: "UNKNOWN" };
  }
}

export function absentLabel(lens: string): string {
  if (lens === "CONFIG") return "Belum di-enable";
  if (lens === "OBSERVED") return "Belum teramati (UNKNOWN)";
  return "";
}
