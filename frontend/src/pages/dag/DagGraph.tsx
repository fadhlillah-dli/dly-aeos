import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { clsx } from "clsx";
import { AlertTriangle, Lock, ShieldAlert } from "lucide-react";
import { useMemo } from "react";
import { layoutDag } from "../../components/planning/dagLayout";
import type { DagNode, DagView } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { acceptanceBadge, runtimeBadge } from "./dagStatus";

type TaskNodeData = { task: DagNode; highlighted: boolean; blocked: boolean };

function TaskNode({ data }: NodeProps<Node<TaskNodeData>>) {
  const t = data.task;
  const rt = runtimeBadge(t.runtime.state);
  const acc = acceptanceBadge(t.acceptance);
  return (
    <div
      className={clsx(
        "w-[240px] rounded-md border bg-surface p-2 text-xs shadow-sm",
        data.highlighted ? "border-accent ring-2 ring-accent/40" : data.blocked ? "border-bad" : "border-line",
      )}
    >
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center justify-between gap-1">
        <span className="mono font-semibold">{t.key}</span>
        <span className="flex items-center gap-1">
          {t.conflicts_with.length > 0 && <AlertTriangle className="h-3.5 w-3.5 text-warn" aria-label={`konflik tulis dengan ${t.conflicts_with.join(", ")}`} />}
          {t.quarantine && <ShieldAlert className="h-3.5 w-3.5 text-bad" aria-label="path terlindungi: review khusus" />}
          {t.lease && <Lock className="h-3.5 w-3.5 text-info" aria-label={`lease ${t.lease.owner}`} />}
        </span>
      </div>
      <div className="mt-0.5 line-clamp-2 font-medium">{t.title}</div>
      <div className="mt-1 flex flex-wrap gap-1">
        <StatusBadge status={rt.badge} label={rt.label} />
        <StatusBadge status={acc.badge} label={acc.label} />
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

export function DagGraph({ view, highlight }: { view: DagView; highlight: string | null }) {
  const { nodes, edges } = useMemo(() => {
    const blocked = new Set(view.critical_blockers.flatMap((b) => [b.task_key, ...b.blocks]));
    const pos = layoutDag(view.nodes.map((n) => ({ id: n.key })), view.edges);
    const byId = new Map(pos.map((p) => [p.id, p]));
    const ns: Node<TaskNodeData>[] = view.nodes.map((t) => ({
      id: t.key,
      type: "task",
      position: { x: byId.get(t.key)?.x ?? 0, y: byId.get(t.key)?.y ?? 0 },
      data: { task: t, highlighted: highlight === t.key, blocked: blocked.has(t.key) },
      draggable: false,
    }));
    const es: Edge[] = view.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      markerEnd: { type: MarkerType.ArrowClosed },
      animated: view.nodes.find((n) => n.key === e.target)?.runtime.state === "RUNNING",
    }));
    return { nodes: ns, edges: es };
  }, [view, highlight]);
  return (
    <div className="h-[520px] rounded-lg border border-line bg-surface-2" aria-label="Graf dependensi task">
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView nodesConnectable={false} elementsSelectable={false} proOptions={{ hideAttribution: true }}>
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
