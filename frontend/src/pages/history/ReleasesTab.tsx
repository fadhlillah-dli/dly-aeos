import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Card, EmptyState, ErrorBox, KeyValue, Loading, Mono, Notice, Table } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";
import type { ReleaseList, ReleasePointer } from "./types";

function PointerCard({ title, p }: { title: string; p: ReleasePointer | null }) {
  return (
    <Card title={title}>
      {p ? (
        <KeyValue
          items={[
            ["Rilis", <Mono>{p.release_id}</Mono>],
            ["Aktif sejak", <span title={absoluteTime(p.activated_at)}>{relativeTime(p.activated_at)}</span>],
            ["Oleh", p.activated_by],
            ["Keputusan", p.decision_id ? <Mono>{p.decision_id}</Mono> : "—"],
            ["Backup pra-promosi", p.pre_backup_id ? <Mono>{p.pre_backup_id}</Mono> : "—"],
          ]}
        />
      ) : (
        <p className="text-sm text-ink-3">Belum ada.</p>
      )}
    </Card>
  );
}

export function ReleasesTab() {
  const q = useQuery({ queryKey: ["ops-releases"], queryFn: () => get<ReleaseList>("/api/ops/releases") });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const data = q.data!;
  return (
    <div className="space-y-3">
      <Notice tone="info">
        {data.notice} Promosi menjalankan backup + restore test, smoke test di proses terpisah, dan mempertahankan rilis sebelumnya bila gagal.
      </Notice>
      <div className="grid gap-3 md:grid-cols-2">
        <PointerCard title="Rilis aktif" p={data.active} />
        <PointerCard title="Rilis sebelumnya (rollback)" p={data.previous} />
      </div>
      {data.items.length === 0 ? (
        <EmptyState title="Belum ada rilis controller">
          Setelah merge manusia ke main: <Mono>aeos ops release build</Mono>, lalu <Mono>aeos ops release promote &lt;id&gt; --operator &lt;admin&gt;</Mono>.
          Saat ini controller berjalan dari checkout repo.
        </EmptyState>
      ) : (
        <Table
          rows={data.items}
          rowKey={(r) => r.release_id}
          columns={[
            { header: "Rilis", cell: (r) => <Mono>{r.release_id}</Mono> },
            { header: "Peran", cell: (r) => <OpsBadge status={r.role} /> },
            { header: "Integritas", cell: (r) => <OpsBadge status={r.integrity} /> },
            { header: "Commit", cell: (r) => <Mono>{r.commit.slice(0, 12)}</Mono> },
            { header: "Artifact", cell: (r) => <Mono>{shortHash(r.artifact_sha256)}</Mono> },
            { header: "Schema head", cell: (r) => r.schema_head ?? "create_all" },
            { header: "Dibangun", cell: (r) => <span title={absoluteTime(r.built_at)}>{relativeTime(r.built_at)}</span> },
          ]}
        />
      )}
      {data.history.length > 0 && (
        <Card title="Riwayat promosi/rollback">
          <ol className="space-y-1 text-sm">
            {[...data.history].reverse().map((h) => (
              <li key={`${h.at}-${h.release_id}`}>
                <span className="text-ink-3">{relativeTime(h.at)}</span> · <Mono>{h.action}</Mono> {h.from_release_id ?? "—"} → <Mono>{h.release_id}</Mono> oleh {h.activated_by}
              </li>
            ))}
          </ol>
        </Card>
      )}
      <Provenance source={data.source} checkedAt={data.checked_at} evidenceLimit="Rollback kode tidak memulihkan data; gunakan backup pra-promosi bila schema berubah." />
    </div>
  );
}
