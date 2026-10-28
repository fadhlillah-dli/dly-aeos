import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Layers, Network, Search, TableIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { DomainRequired } from "../../components/inventory/DomainRequired";
import { Badge, LENS_LABELS, RELATION_LABELS } from "../../components/inventory/labels";
import type { BlastRadius, MapNode, PathResult, RelationView, SnapshotDiff, SystemMap } from "../../components/inventory/types";
import { Button, Card, EmptyState, ErrorBox, inputBase, Loading, Notice, PageHeader, Table } from "../../components/ui";
import { applyDiff, foldGraph, groupId, hideNodes, type GEdge, type GNode } from "./layout";
import { MapGraph } from "./MapGraph";
import { DataFlowView } from "./DataFlow";
import { EdgeCard, NodeCard, SnapshotPanel } from "./Panels";

const LENSES = ["DESIGN", "CONFIG", "OBSERVED"] as const;
const DRIFT_LABEL: Record<string, string> = {
  "planned-but-missing": "Direncanakan tetapi tidak ditemukan",
  "installed-but-disabled": "Terpasang tetapi tidak aktif",
  "enabled-but-unverified": "Aktif tetapi belum terverifikasi",
  "resolved-version-mismatch": "Versi/identitas berbeda dari pin",
  "observed-but-unregistered": "Teramati tetapi tidak terdaftar",
  "obsolete-reference": "Referensi usang",
};

export default function SystemMapPage() {
  const { domain } = useAuth();
  const [params, setParams] = useSearchParams();
  const lens = (params.get("lens") ?? "DESIGN").toUpperCase();
  const blastRoot = params.get("blast") ?? "";
  // §36 "Pengguna memilih domain, proyek, environment dan snapshot"
  const projectId = params.get("project") ?? "";
  const environment = params.get("env") ?? "";
  const snapshotId = params.get("snapshot") ?? "";
  const [snapshotDiff, setSnapshotDiff] = useState<SnapshotDiff | null>(null);
  const [view, setView] = useState<"graph" | "table" | "flow">("graph");
  const [advanced, setAdvanced] = useState(false);
  const [hideAbsent, setHideAbsent] = useState(false);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [pathFrom, setPathFrom] = useState("");
  const [pathTo, setPathTo] = useState("");
  const [direction, setDirection] = useState<"dependents" | "dependencies">("dependents");
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GEdge | null>(null);

  const mapQs = new URLSearchParams({ lens });
  if (projectId) mapQs.set("project_id", projectId);
  if (environment) mapQs.set("environment", environment);
  if (snapshotId) mapQs.set("snapshot", snapshotId);
  const map = useQuery({
    queryKey: ["inventory", "map", domain, mapQs.toString()],
    queryFn: () => get<SystemMap>(`/api/inventory/map?${mapQs.toString()}`),
    enabled: !!domain,
  });
  const projects = useQuery({
    queryKey: ["projects", "system-map", domain],
    queryFn: () => get<{ items: { id: string; name: string }[] }>("/api/projects"),
    enabled: !!domain,
    retry: false,
  });
  const path = useQuery({
    queryKey: ["inventory", "path", domain, lens, pathFrom, pathTo],
    queryFn: () => get<PathResult>(`/api/inventory/path?from=${encodeURIComponent(pathFrom)}&to=${encodeURIComponent(pathTo)}`),
    enabled: !!domain && !!pathFrom && !!pathTo && pathFrom !== pathTo,
  });
  const blast = useQuery({
    queryKey: ["inventory", "blast", domain, blastRoot, direction],
    queryFn: () => get<BlastRadius>(`/api/inventory/blast-radius/${encodeURIComponent(blastRoot)}?direction=${direction}`),
    enabled: !!domain && !!blastRoot,
  });
  // Live refresh: one shared event -> query map in AppShell (lib/eventInvalidation.ts), not a per-page filter.

  const setParam = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };

  const data = map.data;
  // §36 "Zoom-out merangkum": start with every functional domain folded; users unfold what they need.
  const foldInit = useRef(false);
  useEffect(() => {
    if (!foldInit.current && data?.groups.length) {
      foldInit.current = true;
      setFolded(new Set(data.groups.filter((g) => g.node_ids.length > 3).map((g) => g.id)));
    }
  }, [data]);
  const visibleNodes = useMemo(() => {
    let nodes = data?.nodes ?? [];
    if (!advanced) nodes = nodes.filter((n) => !["CANDIDATE", "REFERENCE_ONLY"].includes(n.requirement));
    if (hideAbsent) nodes = nodes.filter((n) => n.present);
    return nodes;
  }, [data, advanced, hideAbsent]);
  const graph = useMemo(() => {
    if (!data) return { nodes: [] as GNode[], edges: [] as GEdge[] };
    const ids = new Set(visibleNodes.map((n) => n.id));
    const folded_ = foldGraph(visibleNodes, data.edges.filter((e) => ids.has(e.source) && ids.has(e.target)), data.groups, folded);
    return applyDiff(hideNodes(folded_, () => true), snapshotDiff);
  }, [data, visibleNodes, folded, snapshotDiff]);

  const ownerOf = useMemo(() => {
    const m = new Map<string, string>();
    (data?.nodes ?? []).forEach((n) => m.set(n.id, folded.has(n.category) ? groupId(n.category) : n.id));
    return (id: string) => m.get(id) ?? id;
  }, [data, folded]);

  const { highlightNodes, highlightEdges } = useMemo(() => {
    const hn = new Set<string>();
    const he = new Set<string>();
    const needle = search.trim().toLowerCase();
    if (needle) visibleNodes.filter((n) => n.label.toLowerCase().includes(needle) || n.id.includes(needle)).forEach((n) => hn.add(ownerOf(n.id)));
    if (path.data?.found) {
      path.data.path.forEach((id) => hn.add(ownerOf(id)));
      path.data.edges.forEach((e) => {
        he.add(`${ownerOf(e.source)}->${ownerOf(e.target)}`);
        he.add(`${ownerOf(e.target)}->${ownerOf(e.source)}`);
      });
    }
    if (blast.data) {
      hn.add(ownerOf(blast.data.root));
      blast.data.items.forEach((i) => hn.add(ownerOf(i.component_id)));
      blast.data.edges.forEach((e) => he.add(`${ownerOf(e.source)}->${ownerOf(e.target)}`));
    }
    return { highlightNodes: hn, highlightEdges: he };
  }, [search, visibleNodes, path.data, blast.data, ownerOf]);

  const nodeById = useMemo(() => new Map((data?.nodes ?? []).map((n) => [n.id, n])), [data]);
  const relById = useMemo(() => new Map((data?.edges ?? []).map((e) => [e.id, e])), [data]);
  const selected: MapNode | undefined = selectedNode ? nodeById.get(selectedNode) : undefined;
  const selectedRels: RelationView[] = selectedEdge ? selectedEdge.relationIds.map((id) => relById.get(id)).filter((x): x is RelationView => !!x) : [];

  const toggleFold = (cat: string) => {
    const next = new Set(folded);
    if (next.has(cat)) next.delete(cat);
    else next.add(cat);
    setFolded(next);
  };

  const sortedNodes = [...(data?.nodes ?? [])].sort((a, b) => a.label.localeCompare(b.label));

  return (
    <div>
      <PageHeader
        title="Peta Sistem"
        question="Sistem ini terdiri dari apa dan siapa terhubung dengan siapa?"
        source={data?.lens_source}
        checkedAt={data?.checked_at ?? null}
        evidenceLimit="Relasi menunjukkan kemungkinan hubungan sesuai scope, bukan izin aktif; yang tidak diinstrumentasi = UNKNOWN"
        relatedDecisionsHref="/decisions?target_type=architecture_snapshot"
        mode={data?.mode}
      />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex gap-1" role="tablist" aria-label="Lensa peta">
          {LENSES.map((l) => (
            <Button key={l} role="tab" aria-selected={lens === l} variant={lens === l ? "primary" : "secondary"} onClick={() => setParam("lens", l)}>
              {LENS_LABELS[l]}
            </Button>
          ))}
        </div>
        <select aria-label="Proyek" className="rounded-md border border-line bg-surface px-2 py-1 text-sm" value={projectId} onChange={(e) => setParam("project", e.target.value)}>
          <option value="">Semua proyek (platform)</option>
          {(projects.data?.items ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select aria-label="Environment" className="rounded-md border border-line bg-surface px-2 py-1 text-sm" value={environment} onChange={(e) => setParam("env", e.target.value)}>
          <option value="">Semua environment</option>
          {(data?.filters?.environments ?? ["local"]).map((env) => (
            <option key={env} value={env}>
              {env}
            </option>
          ))}
        </select>
        {snapshotId && (
          <Button variant="secondary" onClick={() => setParam("snapshot", "")} title="Kembali ke lensa live">
            Snapshot {data?.snapshot?.label ?? ""} — tutup
          </Button>
        )}
        <div className="ml-auto flex gap-1">
          <Button variant={view === "graph" ? "primary" : "secondary"} onClick={() => setView("graph")}>
            <Network className="h-4 w-4" aria-hidden /> Peta
          </Button>
          <Button variant={view === "table" ? "primary" : "secondary"} onClick={() => setView("table")}>
            <TableIcon className="h-4 w-4" aria-hidden /> Tabel
          </Button>
          <Button variant={view === "flow" ? "primary" : "secondary"} onClick={() => setView("flow")}>
            <Layers className="h-4 w-4" aria-hidden /> Aliran data
          </Button>
        </div>
      </div>
      {!domain && <DomainRequired />}
      {data && <p className="mb-3 text-xs text-ink-3">{data.note}</p>}
      {lens === "DESIGN" && <Notice tone="mock">Lensa Rancangan berasal dari InstallManifest/ADR: komponen yang direncanakan, belum bukti instalasi atau penggunaan.</Notice>}

      {!domain ? null : map.isLoading ? (
        <Loading />
      ) : map.error ? (
        <ErrorBox error={map.error} />
      ) : !data || data.nodes.length === 0 ? (
        <EmptyState title="Belum ada komponen terdaftar">Muat katalog desain dengan perintah: aeos seed</EmptyState>
      ) : (
        <>
          <div className="mb-3 mt-3 flex flex-wrap items-center gap-2 text-sm">
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} /> Advanced (tampilkan kandidat/rujukan)
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={hideAbsent} onChange={(e) => setHideAbsent(e.target.checked)} /> Sembunyikan yang tidak ada di lensa ini
            </label>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-2 h-4 w-4 text-ink-3" aria-hidden />
              <input className={`${inputBase} w-full pl-8 sm:w-56`} placeholder="Cari komponen…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Cari komponen di peta" />
            </div>
          </div>
          <div className="mb-3 flex flex-wrap items-center gap-1 text-xs">
            <Layers className="h-4 w-4 text-ink-3" aria-hidden />
            <span className="text-ink-3">Lipat domain fungsi:</span>
            {data.groups.map((g) => (
              <button
                key={g.id}
                onClick={() => toggleFold(g.id)}
                className={clsx("rounded-full border px-2 py-0.5", folded.has(g.id) ? "border-accent bg-accent text-accent-ink" : "border-line hover:bg-surface-3")}
                aria-pressed={folded.has(g.id)}
              >
                {g.label} ({g.counts.present}/{g.counts.total}
                {g.counts.drift ? `, drift ${g.counts.drift}` : ""})
              </button>
            ))}
            <button className="ml-1 text-accent hover:underline" onClick={() => setFolded(new Set(data.groups.map((g) => g.id)))}>Lipat semua</button>
            <button className="text-accent hover:underline" onClick={() => setFolded(new Set())}>Buka semua</button>
          </div>
          {Object.values(data.drift_summary).some((n) => n > 0) && (
            <div className="mb-3 flex flex-wrap gap-2 text-xs">
              <span className="text-ink-3">Drift antar lensa:</span>
              {Object.entries(data.drift_summary)
                .filter(([, n]) => n > 0)
                .map(([k, n]) => (
                  <span key={k} className="rounded bg-warn-bg px-1.5 py-0.5 text-warn">
                    {DRIFT_LABEL[k] ?? k}: {n}
                  </span>
                ))}
            </div>
          )}

          <div className={clsx("grid gap-4", (selected || selectedRels.length > 0) && "xl:grid-cols-[minmax(0,1fr)_380px]")}>
            <div className="min-w-0">
              {view === "flow" ? (
                <DataFlowView edges={data.edges.filter((e) => visibleNodes.some((n) => n.id === e.source) && visibleNodes.some((n) => n.id === e.target))} />
              ) : view === "graph" ? (
                <MapGraph
                  nodes={graph.nodes}
                  edges={graph.edges}
                  lens={lens}
                  highlightNodes={highlightNodes}
                  highlightEdges={highlightEdges}
                  selectedId={selectedNode}
                  onNodeClick={(n) => {
                    if (n.isGroup) {
                      toggleFold(n.category);
                      return;
                    }
                    setSelectedEdge(null);
                    setSelectedNode(n.id);
                  }}
                  onEdgeClick={(e) => {
                    setSelectedNode(null);
                    setSelectedEdge(e);
                  }}
                />
              ) : (
                <div className="space-y-4">
                  <Table
                    rows={visibleNodes}
                    rowKey={(n) => n.id}
                    onRowClick={(n) => setSelectedNode(n.id)}
                    columns={[
                      { header: "Komponen", cell: (n) => <div><div className="font-medium">{n.label}</div><div className="mono text-xs text-ink-3">{n.id}</div></div> },
                      { header: "Domain fungsi", cell: (n) => <span className="text-xs">{data.groups.find((g) => g.id === n.category)?.label ?? n.category}</span> },
                      { header: `Status (${LENS_LABELS[lens]})`, cell: (n) => <Badge status={n.status} /> },
                      { header: "Ada di lensa", cell: (n) => <span className="text-xs">{n.present ? "ya" : "tidak"}</span> },
                      { header: "Dasar", cell: (n) => <span className="text-xs text-ink-3">{n.basis.join("; ") || "—"}</span> },
                      { header: "Drift", cell: (n) => <span className="text-xs">{n.drift.map((d) => d.label).join(", ") || "—"}</span> },
                    ]}
                  />
                  <Table
                    rows={data.edges.filter((e) => visibleNodes.some((n) => n.id === e.source) && visibleNodes.some((n) => n.id === e.target))}
                    rowKey={(e) => e.id}
                    empty="Tidak ada relasi pada lensa ini"
                    onRowClick={(e) => setSelectedEdge({ id: e.id, source: e.source, target: e.target, relation: e.relation, status: e.status, count: 1, relationIds: [e.id] })}
                    columns={[
                      { header: "Dari", cell: (e) => e.source_name },
                      { header: "Relasi", cell: (e) => RELATION_LABELS[e.relation] ?? e.relation },
                      { header: "Ke", cell: (e) => e.target_name },
                      { header: "Status", cell: (e) => <Badge status={e.status} /> },
                      { header: "Data", cell: (e) => <span className="text-xs">{e.data_description}</span> },
                      { header: "Operasi", cell: (e) => <span className="text-xs">{e.operation}</span> },
                    ]}
                  />
                </div>
              )}
            </div>
            {selected && <NodeCard node={selected} lensLabel={LENS_LABELS[lens]} onClose={() => setSelectedNode(null)} />}
            {!selected && selectedRels.length > 0 && <EdgeCard rels={selectedRels} onClose={() => setSelectedEdge(null)} />}
          </div>

          <div className="mt-5 grid gap-4 lg:grid-cols-2">
            <Card title="Pencarian jalur">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                {(
                  [
                    { v: pathFrom, set: setPathFrom, l: "Dari" },
                    { v: pathTo, set: setPathTo, l: "Ke" },
                  ] as { v: string; set: (x: string) => void; l: string }[]
                ).map(({ v, set, l }) => (
                  <select key={l} aria-label={`Jalur ${l}`} className="max-w-56 rounded-md border border-line bg-surface px-2 py-1" value={v} onChange={(e) => set(e.target.value)}>
                    <option value="">— {l} —</option>
                    {sortedNodes.map((n) => (
                      <option key={n.id} value={n.id}>
                        {n.label}
                      </option>
                    ))}
                  </select>
                ))}
                {(pathFrom || pathTo) && (
                  <Button variant="ghost" onClick={() => { setPathFrom(""); setPathTo(""); }}>
                    Hapus
                  </Button>
                )}
              </div>
              {path.isLoading && <Loading />}
              {path.error && <ErrorBox error={path.error} />}
              {path.data && (
                <div className="mt-2 text-sm">
                  {path.data.found ? (
                    <ol className="list-decimal pl-5">
                      {path.data.path.map((id, i) => (
                        <li key={id}>
                          {nodeById.get(id)?.label ?? id}
                          {i < path.data!.edges.length && <span className="text-xs text-ink-3"> — {RELATION_LABELS[path.data!.edges[i].relation] ?? path.data!.edges[i].relation} →</span>}
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="text-ink-3">{path.data.note}</p>
                  )}
                  {path.data.note && path.data.found && <p className="text-xs text-warn">{path.data.note}</p>}
                </div>
              )}
            </Card>
            <Card title="Blast radius">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <select aria-label="Komponen blast radius" className="max-w-56 rounded-md border border-line bg-surface px-2 py-1" value={blastRoot} onChange={(e) => setParam("blast", e.target.value)}>
                  <option value="">— pilih komponen —</option>
                  {sortedNodes.filter((n) => n.kind !== "unregistered").map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.label}
                    </option>
                  ))}
                </select>
                <select aria-label="Arah" className="rounded-md border border-line bg-surface px-2 py-1" value={direction} onChange={(e) => setDirection(e.target.value as "dependents" | "dependencies")}>
                  <option value="dependents">Yang terdampak bila komponen ini berubah</option>
                  <option value="dependencies">Yang dibutuhkan komponen ini</option>
                </select>
              </div>
              {blast.isLoading && <Loading />}
              {blast.error && <ErrorBox error={blast.error} />}
              {blast.data && (
                <div className="mt-2 text-sm">
                  <p>
                    {blast.data.summary.affected} komponen ({Object.entries(blast.data.summary.by_category).map(([k, v]) => `${data.groups.find((g) => g.id === k)?.label ?? k}: ${v}`).join(", ") || "—"})
                  </p>
                  <ul className="mt-1 max-h-48 overflow-y-auto text-xs">
                    {blast.data.items.map((i) => (
                      <li key={i.component_id}>
                        <Link className="text-accent hover:underline" to={`/tools/${i.component_id}`}>{i.name}</Link> <span className="text-ink-3">jarak {i.depth} via {i.via.map((v) => RELATION_LABELS[v] ?? v).join(" → ")}</span>
                      </li>
                    ))}
                  </ul>
                  <p className="mt-1 text-xs text-ink-3">{blast.data.note}</p>
                </div>
              )}
            </Card>
          </div>

          <Card title="Legenda" className="mt-5">
            <ul className="grid gap-1 text-xs sm:grid-cols-2">
              {data.legend.map((l) => (
                <li key={l.key} className="flex items-center gap-2">
                  <Badge status={l.key === "DRIFT" ? "DRIFTED" : l.key === "LENS" ? "DESIGN" : l.key} /> {l.label}
                </li>
              ))}
              <li className="flex items-center gap-2">
                <span className="inline-block h-3 w-6 rounded border border-dashed border-line opacity-60" /> Node pudar bergaris putus: tidak ada di lensa ini
              </li>
            </ul>
          </Card>
        </>
      )}
      {domain && <SnapshotPanel onDiff={setSnapshotDiff} onShow={(id) => setParam("snapshot", id)} />}
    </div>
  );
}
