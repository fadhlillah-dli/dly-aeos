import dagre from "@dagrejs/dagre";

export interface LayoutInput {
  id: string;
  width?: number;
  height?: number;
}

export interface Positioned {
  id: string;
  x: number;
  y: number;
}

/** Left-to-right layered layout of the Task DAG (dependency -> dependant). Pure; used by the DAG page. */
export function layoutDag(
  nodes: LayoutInput[],
  edges: { source: string; target: string }[],
  opts: { width?: number; height?: number; rankdir?: "LR" | "TB" } = {},
): Positioned[] {
  const w = opts.width ?? 240;
  const h = opts.height ?? 96;
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: opts.rankdir ?? "LR", nodesep: 28, ranksep: 64, marginx: 8, marginy: 8 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: n.width ?? w, height: n.height ?? h });
  const ids = new Set(nodes.map((n) => n.id));
  for (const e of edges) if (ids.has(e.source) && ids.has(e.target)) g.setEdge(e.source, e.target);
  dagre.layout(g);
  return nodes.map((n) => {
    const p = g.node(n.id) as { x: number; y: number } | undefined;
    // dagre returns centre coordinates; React Flow expects the top-left corner.
    return { id: n.id, x: (p?.x ?? 0) - (n.width ?? w) / 2, y: (p?.y ?? 0) - (n.height ?? h) / 2 };
  });
}
