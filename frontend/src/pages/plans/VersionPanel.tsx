import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import type { DiffView, VersionSummary } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { ErrorBox, inputClass, Loading, Mono, Table } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";

export function VersionSelector({
  versions,
  selected,
  onSelect,
  currentId,
  approvedId,
}: {
  versions: VersionSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
  currentId: string | null;
  approvedId: string | null;
}) {
  return (
    <select aria-label="Pilih versi" className={inputClass} value={selected ?? ""} onChange={(e) => onSelect(e.target.value)}>
      {[...versions].reverse().map((v) => (
        <option key={v.id} value={v.id}>
          v{v.version} · {v.source} · {shortHash(v.manifest_hash)}
          {v.id === currentId ? " · terbaru" : ""}
          {v.id === approvedId ? " · disetujui" : ""}
          {v.mode !== "LIVE" ? ` · ${v.mode}` : ""}
        </option>
      ))}
    </select>
  );
}

export function VersionHistory({ versions, currentId, approvedId }: { versions: VersionSummary[]; currentId: string | null; approvedId: string | null }) {
  return (
    <Table
      rows={[...versions].reverse()}
      rowKey={(v) => v.id}
      empty="Belum ada versi"
      columns={[
        { header: "Versi", cell: (v) => <span>v{v.version}</span> },
        { header: "Sumber", cell: (v) => <Mono>{v.source}</Mono> },
        { header: "Hash", cell: (v) => <Mono>{shortHash(v.manifest_hash, 16)}</Mono> },
        {
          header: "Status",
          cell: (v) => (
            <div className="flex flex-wrap gap-1">
              {v.id === approvedId && <StatusBadge status="APPROVED" />}
              {v.id === currentId && <StatusBadge status="PENDING" label="Terbaru" />}
              {v.mode !== "LIVE" && <StatusBadge status={v.mode} />}
            </div>
          ),
        },
        { header: "Dibuat", cell: (v) => <span title={v.created_at ?? ""}>{relativeTime(v.created_at)} · {v.created_by ?? "—"}</span> },
      ]}
    />
  );
}

const STATUS_LABEL: Record<string, string> = { changed: "Berubah", added: "Ditambah", removed: "Dihapus", unchanged: "Tidak berubah" };

export function VersionDiff({ planId, versionId, against }: { planId: string; versionId: string; against: string | null }) {
  const q = useQuery({
    queryKey: ["plan", planId, "diff", versionId, against],
    queryFn: () => get<DiffView>(`/api/plans/${planId}/versions/${versionId}/diff${against ? `?against=${against}` : ""}`),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const changed = d.sections.filter((s) => s.status !== "unchanged");
  return (
    <div className="grid gap-2">
      <div className="text-sm text-ink-2">
        {d.from ? (
          <>
            v{d.from.version} <Mono>{shortHash(d.from.manifest_hash)}</Mono> → v{d.to.version} <Mono>{shortHash(d.to.manifest_hash)}</Mono>
          </>
        ) : (
          <>Versi pertama (tidak ada versi induk) — semua bagian baru.</>
        )}
      </div>
      <Table
        rows={changed}
        rowKey={(s) => s.section}
        empty="Tidak ada perbedaan antar versi"
        columns={[
          { header: "Bagian", cell: (s) => <Mono>{s.section}</Mono> },
          { header: "Perubahan", cell: (s) => STATUS_LABEL[s.status] ?? s.status },
          {
            header: "Detail",
            cell: (s) => (
              <div className="text-xs">
                {s.added?.length ? <div>+ {s.added.join(", ")}</div> : null}
                {s.removed?.length ? <div>− {s.removed.join(", ")}</div> : null}
                {s.changed?.length ? <div>~ {s.changed.join(", ")}</div> : null}
                {s.fields_changed?.length ? <div>field: {s.fields_changed.join(", ")}</div> : null}
                {s.count ? <div>jumlah {s.count.from} → {s.count.to}</div> : null}
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
