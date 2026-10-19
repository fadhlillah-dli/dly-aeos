import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { clsx } from "clsx";
import { AlertTriangle, Layers, Play } from "lucide-react";
import { memo, useMemo } from "react";
import { Badge, RELATION_LABELS } from "../../components/inventory/labels";
import { useTheme } from "../../lib/theme";
import { absentLabel, diffVisual, edgeVisual, layoutGraph, NODE_H, NODE_W, type GEdge, type GNode } from "./layout";

type CompData = {
  node: GNode;
  lens: string;
  highlighted: boolean;
  dimmed: boolean;
  selected: boolean;
};
type CompNode = Node<CompData, "component">;

const ComponentNode = memo(function ComponentNode({ data }: NodeProps<CompNode>) {
  const n = data.node;
  return (
    <div
      className={clsx(
        "rounded-md border bg-surface px-2 py-1.5 text-left shadow-sm transition",
        n.isGroup ? "border-accent border-2" : "border-line",
        !n.present && !n.isGroup && "border-dashed opacity-60",
        n.diff === "added" && "ring-2 ring-ok",
        n.diff === "changed" && "ring-2 ring-warn",
        n.diff === "removed" && "ring-2 ring-bad",
        data.highlighted && "ring-2 ring-accent",
        data.selected && "ring-2 ring-ink",
        data.dimmed && "opacity-25",
      )}
      style={{ width: NODE_W, height: NODE_H }}
      title={n.label}
    >
      <Handle type="target" position={Position.Left} className="!h-1.5 !w-1.5 !bg-line" />
      <div className="flex items-start justify-between gap-1">
        <div className="min-w-0 truncate text-xs font-semibold">
          {n.isGroup && <Layers className="mr-1 inline h-3 w-3" aria-hidden />}
          {n.label}
        </div>
        {n.drift > 0 && (
          <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-warn-bg px-1 text-[10px] text-warn" title="Drift antar lensa">
            <AlertTriangle className="h-3 w-3" aria-hidden /> {n.drift}
          </span>
        )}
      </div>
      <div className="mt-1 flex items-center gap-1">
        {n.isGroup ? (
          <span className="text-[11px] text-ink-3">
            {n.count} komponen · {n.presentCount} ada di lensa ini · klik untuk buka
          </span>
        ) : (
          <>
            <Badge status={n.status} className="!px-1.5 !py-0 !text-[10px]" />
            {!n.present && <span className="truncate text-[10px] text-ink-3">{absentLabel(data.lens)}</span>}
            {(n.runs ?? 0) > 0 && (
              <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-ok-bg px-1 text-[10px] text-ok" title="Run aktif yang memakai komponen ini">
                <Play className="h-3 w-3" aria-hidden /> {n.runs} run
              </span>
            )}
            {n.diff && <span className="text-[10px] text-ink-3">{n.diff === "added" ? "baru" : n.diff === "removed" ? "dihapus" : "berubah"}</span>}
          </>
        )}
      </div>
      <Handle type="source" position={Position.Right} className="!h-1.5 !w-1.5 !bg-line" />
    </div>
  );
});

const nodeTypes = { component: ComponentNode };

export function MapGraph({
  nodes,
  edges,
  lens,
  highlightNodes,
  highlightEdges,
  selectedId,
  onNodeClick,
  onEdgeClick,
}: {
  nodes: GNode[];
  edges: GEdge[];
  lens: string;
  highlightNodes: Set<string>;
  highlightEdges: Set<string>;
  selectedId: string | null;
  onNodeClick: (n: GNode) => void;
  onEdgeClick: (e: GEdge) => void;
}) {
  const { theme } = useTheme();
  const positions = useMemo(() => layoutGraph(nodes, edges), [nodes, edges]);
  const anyHighlight = highlightNodes.size > 0;
  const rfNodes: CompNode[] = useMemo(
    () =>
      nodes.map((n) => ({
        id: n.id,
        type: "component",
        position: positions[n.id] ?? { x: 0, y: 0 },
        data: { node: n, lens, highlighted: highlightNodes.has(n.id), dimmed: anyHighlight && !highlightNodes.has(n.id), selected: selectedId === n.id },
        draggable: true,
      })),
    [nodes, positions, lens, highlightNodes, anyHighlight, selectedId],
  );
  const rfEdges: Edge[] = useMemo(
    () =>
      edges.map((e) => {
        const base = edgeVisual(e.status);
        const dv = diffVisual(e.diff);
        const v = dv ? { ...base, stroke: dv.stroke, dash: dv.dash ?? base.dash, label: `${base.label}, ${dv.label}` } : base;
        const hl = highlightEdges.has(`${e.source}->${e.target}`);
        const dim = highlightEdges.size > 0 && !hl;
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          animated: v.animated,
          label: e.count > 1 ? `${e.count} relasi` : hl ? RELATION_LABELS[e.relation] ?? e.relation : dv ? dv.label : undefined,
          style: { stroke: hl ? "var(--color-accent)" : v.stroke, strokeDasharray: v.dash, strokeWidth: hl || dv ? 2.5 : 1.2, opacity: dim ? 0.2 : 1 },
          markerEnd: { type: MarkerType.ArrowClosed, color: hl ? "var(--color-accent)" : v.stroke, width: 14, height: 14 },
          data: { gedge: e },
          ariaLabel: `${e.source} ${RELATION_LABELS[e.relation] ?? e.relation} ${e.target} (${v.label})`,
        } satisfies Edge;
      }),
    [edges, highlightEdges],
  );
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const edgeById = useMemo(() => new Map(edges.map((e) => [e.id, e])), [edges]);

  return (
    <div className="h-[640px] w-full overflow-hidden rounded-lg border border-line bg-surface-2" data-testid="system-map-graph">
      <ReactFlow
        key={`${nodes.length}:${edges.length}`}
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.1}
        colorMode={theme}
        onNodeClick={(_, n) => {
          const g = byId.get(n.id);
          if (g) onNodeClick(g);
        }}
        onEdgeClick={(_, e) => {
          const g = edgeById.get(e.id);
          if (g) onEdgeClick(g);
        }}
      >
        <Background />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  );
}
