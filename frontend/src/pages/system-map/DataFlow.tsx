import { Badge, RELATION_LABELS } from "../../components/inventory/labels";
import type { RelationView } from "../../components/inventory/types";
import { Card, EmptyState, Table } from "../../components/ui";

const DATA_RELATIONS = new Set(["reads", "writes", "publishes_to", "calls", "stores_in", "sends_to", "syncs_with"]);

/** §36 data-flow view: which data moves where (operation, data, boundary), separate from the component map
 * and the org/DAG views; grouped by trust boundary. Edges without data semantics are not data flow. */
export function DataFlowView({ edges }: { edges: RelationView[] }) {
  const flows = edges.filter((e) => e.data_description || (e.operation && e.operation !== "") || DATA_RELATIONS.has(e.relation));
  if (flows.length === 0) {
    return <EmptyState title="Tidak ada aliran data tercatat pada lensa ini">Relasi tanpa deskripsi data atau operasi bukan aliran data.</EmptyState>;
  }
  const groups = new Map<string, RelationView[]>();
  for (const e of flows) {
    const key = e.boundary || "dalam batas yang sama";
    groups.set(key, [...(groups.get(key) ?? []), e]);
  }
  return (
    <div className="space-y-3" data-testid="data-flow">
      {[...groups.entries()].map(([boundary, rows]) => (
        <Card key={boundary} title={`Batas kepercayaan: ${boundary}`}>
          <Table
            rows={rows}
            rowKey={(e) => e.id}
            columns={[
              { header: "Dari", cell: (e) => e.source_name || e.source },
              { header: "Operasi", cell: (e) => <span className="text-xs">{e.operation || RELATION_LABELS[e.relation] || e.relation}</span> },
              { header: "Ke", cell: (e) => e.target_name || e.target },
              { header: "Data", cell: (e) => <span className="text-xs">{e.data_description || "tidak dijelaskan"}</span> },
              { header: "Status", cell: (e) => <Badge status={e.status} /> },
            ]}
          />
        </Card>
      ))}
    </div>
  );
}
