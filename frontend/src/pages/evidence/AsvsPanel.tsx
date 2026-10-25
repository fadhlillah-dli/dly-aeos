import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { useMemo, useState } from "react";
import { Card, EmptyState, ErrorBox, KeyValue, Loading, Mono, Table } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { evidenceApi, type AsvsItem, type AsvsStatus } from "./api";
import { Status } from "./labels";

// §46: a TESTED control names existing tests (a trace), it is not a fresh pass; NOT_PROVEN is never shown as met.
const STATUS_VIEW: Record<AsvsStatus, { status: string; label: string }> = {
  TESTED: { status: "CONFIGURED", label: "Ada test (terlacak)" },
  NOT_PROVEN: { status: "NOT-PROVEN", label: "Belum terbukti" },
  NOT_SELECTED: { status: "NOT_APPLICABLE", label: "Tidak dipilih (alasan tercatat)" },
  DELETED: { status: "NOT_APPLICABLE", label: "Dihapus di ASVS 4.0.3" },
};

const FILTERS: { id: AsvsStatus | "ALL"; label: string }[] = [
  { id: "ALL", label: "Semua" },
  { id: "NOT_PROVEN", label: "Belum terbukti" },
  { id: "TESTED", label: "Ada test" },
  { id: "NOT_SELECTED", label: "Tidak dipilih" },
];

function Explanation({ item }: { item: AsvsItem }) {
  if (item.status === "TESTED") {
    return (
      <ul className="space-y-0.5">
        {(item.tests ?? []).map((t) => (
          <li key={t}>
            <Mono>{t}</Mono>
          </li>
        ))}
        {item.note && <li className="text-ink-3">{item.note}</li>}
      </ul>
    );
  }
  return <span className="text-ink-3">{item.reason ?? item.note ?? "—"}</span>;
}

/** OWASP ASVS trace of the control plane (§46): every 4.0.3 requirement, per chapter, read-only. */
export function AsvsPanel() {
  const q = useQuery({ queryKey: ["asvs"], queryFn: evidenceApi.asvs, staleTime: 300_000 });
  const [filter, setFilter] = useState<AsvsStatus | "ALL">("NOT_PROVEN");
  const [chapter, setChapter] = useState<string>("ALL");
  const rows = useMemo(
    () => (q.data?.items ?? []).filter((i) => (filter === "ALL" || i.status === filter) && (chapter === "ALL" || i.chapter === chapter)),
    [q.data, filter, chapter],
  );
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data;
  if (!d) return <EmptyState title="Trace ASVS tidak tersedia" />;
  const names = Object.fromEntries(d.chapters.map((c) => [c.chapter, c.name]));
  return (
    <div className="space-y-4">
      <Card title={`OWASP ASVS ${d.asvs_version} — control plane`}>
        <KeyValue
          items={[
            ["Aplikasi", <Mono key="a">{d.application}</Mono>],
            ["Versi dipin", `ASVS ${d.asvs_version} (katalog ${d.catalog_version}, ${d.items.length} requirement)`],
            ["Ringkasan", `${d.totals.tested} ada test · ${d.totals.not_proven} belum terbukti · ${d.totals.not_selected} tidak dipilih · ${d.totals.deleted} dihapus`],
            ["Sumber", <span key="s">{d.source} <Mono>{shortHash(d.selection_sha256 ?? "")}</Mono></span>],
          ]}
        />
        <div role="note" className="mt-3 flex items-start gap-2 rounded-md border border-warn/40 bg-warn-bg p-3 text-sm text-warn">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div>
            <div>
              &quot;Ada test&quot; berarti trace menyebut test yang menguji kontrol tersebut — bukan bukti lulus pada commit ini (itu bukti
              gate CI G1/G2). &quot;Belum terbukti&quot; tidak pernah dihitung lulus.
            </div>
            <div className="mt-1">
              Aplikasi yang dibangun AEOS: level ASVS per klasifikasi data/risiko <strong>BELUM DIPUTUSKAN</strong> pemilik — tidak ada
              seleksi per proyek yang diklaim.
            </div>
          </div>
        </div>
      </Card>
      <Card title="Per bab">
        <Table
          rows={d.chapters}
          rowKey={(c) => c.chapter}
          onRowClick={(c) => setChapter(chapter === c.chapter ? "ALL" : c.chapter)}
          columns={[
            { header: "Bab", cell: (c) => <span className={chapter === c.chapter ? "font-semibold" : undefined}>{c.chapter} {c.name}</span> },
            { header: "Ada test", cell: (c) => c.tested },
            { header: "Belum terbukti", cell: (c) => (c.not_proven ? <Status status="NOT-PROVEN" label={String(c.not_proven)} /> : "0") },
            { header: "Tidak dipilih", cell: (c) => c.not_selected },
            { header: "Dihapus", cell: (c) => c.deleted },
          ]}
        />
      </Card>
      <Card
        title={chapter === "ALL" ? "Kontrol" : `Kontrol ${chapter} ${names[chapter] ?? ""}`}
        actions={
          <div className="flex flex-wrap gap-1" role="group" aria-label="Saring status">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                aria-pressed={filter === f.id}
                onClick={() => setFilter(f.id)}
                className={"rounded-md px-2 py-1 text-xs " + (filter === f.id ? "bg-accent text-accent-ink" : "border border-line bg-surface hover:bg-surface-3")}
              >
                {f.label}
              </button>
            ))}
            {chapter !== "ALL" && (
              <button type="button" onClick={() => setChapter("ALL")} className="rounded-md border border-line bg-surface px-2 py-1 text-xs hover:bg-surface-3">
                Semua bab
              </button>
            )}
          </div>
        }
      >
        <Table
          rows={rows}
          rowKey={(i) => i.id}
          empty="Tidak ada kontrol dengan saringan ini"
          columns={[
            { header: "ID", cell: (i) => <Mono>{i.id}</Mono> },
            { header: "Requirement", cell: (i) => i.selected_title ?? i.title, className: "min-w-[16rem]" },
            { header: "Status", cell: (i) => <Status status={STATUS_VIEW[i.status].status} label={STATUS_VIEW[i.status].label} /> },
            { header: "Test / alasan", cell: (i) => <Explanation item={i} />, className: "min-w-[16rem]" },
          ]}
        />
      </Card>
    </div>
  );
}
