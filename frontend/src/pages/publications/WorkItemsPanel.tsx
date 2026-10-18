import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, ErrorBox, Freshness, Loading, Mono, Notice, Table } from "../../components/ui";
import { label, shortId } from "./labels";
import type { WorkItemsResponse } from "./types";

function externalTone(status: string): string {
  if (status.startsWith("closed")) return "DONE";
  if (status === "open") return "QUEUED";
  return status || "UNKNOWN";
}

export function WorkItemsPanel({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const q = useQuery({
    queryKey: ["work-items", projectId],
    queryFn: () => get<WorkItemsResponse>(`/api/projects/${projectId}/work-items`),
  });
  const sync = useMutation({
    mutationFn: () => post(`/api/projects/${projectId}/work-items/sync`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items", projectId] }),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const data = q.data!;
  return (
    <Card
      title={<span className="flex items-center gap-2">Proyeksi work item <StatusBadge status="PROJECTION_ONLY" label={label("PROJECTION_ONLY")} /></span>}
      actions={
        <div className="flex items-center gap-2">
          <Freshness at={data.checked_at} label="Sinkron" staleAfterSeconds={3600} />
          {isAdmin && (
            <Button busy={sync.isPending} onClick={() => sync.mutate()}>
              <RefreshCw className="h-4 w-4" aria-hidden /> Sinkronkan
            </Button>
          )}
        </div>
      }
    >
      <div className="space-y-3">
        <Notice tone="info">{data.note}</Notice>
        {data.capabilities.github_projects === "NOT_IMPLEMENTED" && (
          <p className="text-xs text-ink-3">GitHub Projects (roadmap) opsional — tidak diimplementasikan pada adapter ini.</p>
        )}
        <ErrorBox error={sync.error} />
        <Table
          rows={data.items}
          rowKey={(w) => w.id}
          empty="Belum ada issue. Issue dibuat saat publikasi yang disetujui berjalan."
          columns={[
            {
              header: "Issue",
              cell: (w) =>
                w.issue_url ? (
                  <a href={w.issue_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-accent">
                    <ExternalLink className="h-3.5 w-3.5" aria-hidden /> #{w.issue_id}
                  </a>
                ) : (
                  `#${w.issue_id}`
                ),
            },
            { header: "Judul", cell: (w) => w.title || "—" },
            { header: "TaskSpec", cell: (w) => (w.task_id ? <Mono>{shortId(w.task_id)}</Mono> : w.feature_key ?? "—") },
            {
              header: "Status di tracker",
              cell: (w) => <StatusBadge status={externalTone(w.external_status)} label={`${w.external_status} (tracker)`} />,
            },
            {
              header: "Sinkron",
              cell: (w) => (
                <div className="space-y-1">
                  <StatusBadge status={w.sync_status} label={label(w.sync_status)} />
                  <div><Freshness at={w.last_synced_at} label="Sinkron" staleAfterSeconds={3600} /></div>
                  {w.sync_error && <div className="text-xs text-bad">{w.sync_error}</div>}
                </div>
              ),
            },
            { header: "Membuka dependency?", cell: () => <span className="text-xs text-ink-3">Tidak — perlu bukti penerimaan AEOS</span> },
          ]}
        />
        <p className="text-xs text-ink-3">Sumber: {data.source}</p>
      </div>
    </Card>
  );
}
