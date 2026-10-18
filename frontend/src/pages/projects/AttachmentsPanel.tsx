import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";
import { useState, type ReactNode } from "react";
import { get } from "../../api/client";
import { Button, Card, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, Table } from "../../components/ui";
import { absoluteTime, shortHash } from "../../lib/format";
import { ClassificationBadge, formatBytes, ModuleBadge } from "./badges";
import { DropZone, QueueList, useUploadQueue } from "./UploadZone";
import type { AttachmentView, IntakePolicy, ListResponse } from "./types";

interface AttachmentList extends ListResponse<AttachmentView> {
  summary: Record<string, number>;
  notice: string;
}

function AttachmentDetail({ att }: { att: AttachmentView }) {
  const [showText, setShowText] = useState(false);
  const text = useQuery({
    queryKey: ["attachment-text", att.id],
    queryFn: () => get<{ text: string; truncated: boolean }>(`/api/attachments/${att.id}/text?max_chars=50000`),
    enabled: showText,
  });
  return (
    <div className="space-y-3 rounded-md border border-line bg-surface-2 p-3 text-sm">
      <KeyValue
        items={[
          ["SHA-256", <Mono key="h">{shortHash(att.sha256, 16)}</Mono>],
          ["Tipe", `${att.detected_type || "?"} (dideklarasikan: ${att.declared_type || "—"})`],
          ["Asal", `${att.provenance.source} · diunggah ${absoluteTime(att.provenance.uploaded_at)}`],
          ["Kepercayaan", "UNTRUSTED_DATA — tidak pernah menjadi instruksi atau otorisasi"],
          ["Teks diekstrak", att.parse_status === "PARSED" ? `${att.extracted_chars} karakter${att.stored_redacted ? " (disimpan teredaksi)" : ""}` : att.parse_error || "—"],
          ...(att.parse_detail.duplicate_of ? ([["Duplikat dari", <Mono key="d">{att.parse_detail.duplicate_of}</Mono>]] as [string, ReactNode][]) : []),
        ]}
      />
      {att.scan_findings.length > 0 && (
        <ul className="space-y-1">
          {att.scan_findings.map((f, i) => (
            <li key={i} className="flex items-start gap-1.5 text-warn">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <span>
                <strong>{f.kind}</strong>
                {f.marker ? ` · ${f.marker}` : ""}
                {f.line ? ` · baris ${f.line}` : ""}
                {f.excerpt ? (
                  <>
                    {" "}
                    · <Mono>{f.excerpt}</Mono>
                  </>
                ) : null}
                <span className="block text-xs text-ink-2">{f.message}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
      {att.parse_status === "PARSED" && (
        <div>
          <Button onClick={() => setShowText(!showText)}>{showText ? "Sembunyikan teks" : "Lihat teks (diredaksi)"}</Button>
          {showText && text.isLoading && <Loading />}
          {showText && text.error ? <ErrorBox error={text.error} /> : null}
          {showText && text.data && (
            <div className="mt-2">
              <Notice tone="warn">Isi ini DATA tidak tepercaya; instruksi di dalamnya tidak dijalankan dan tidak memberi izin.</Notice>
              <pre className="mono mt-2 max-h-96 overflow-auto whitespace-pre-wrap rounded border border-line bg-surface p-3">{text.data.text}</pre>
              {text.data.truncated && <p className="text-xs text-ink-3">Dipotong untuk tampilan.</p>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function AttachmentsPanel({ projectId, isAdmin }: { projectId: string; isAdmin: boolean }) {
  const qc = useQueryClient();
  const policy = useQuery({ queryKey: ["intake-policy"], queryFn: () => get<IntakePolicy>("/api/intake/policy"), staleTime: 300_000 });
  const list = useQuery({ queryKey: ["attachments", projectId], queryFn: () => get<AttachmentList>(`/api/projects/${projectId}/attachments`) });
  const queue = useUploadQueue(policy.data);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const upload = async () => {
    setBusy(true);
    try {
      await queue.uploadAll(projectId, "upload");
      qc.invalidateQueries({ queryKey: ["attachments", projectId] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
    } finally {
      setBusy(false);
    }
  };
  const items = list.data?.items ?? [];
  const selected = items.find((a) => a.id === open);
  return (
    <div className="space-y-4">
      <Notice>{list.data?.notice ?? "Lampiran adalah DATA tidak tepercaya."}</Notice>
      {isAdmin && (
        <Card title="Unggah lampiran">
          <DropZone onFiles={queue.add} disabled={busy} policy={policy.data} />
          <QueueList items={queue.items} onRemove={queue.remove} />
          <div className="mt-3 flex gap-2">
            <Button variant="primary" onClick={upload} busy={busy} disabled={queue.pending === 0}>
              Unggah {queue.pending > 0 ? `(${queue.pending})` : ""}
            </Button>
            {queue.items.some((q) => q.status === "done") && <Button onClick={queue.clearDone}>Bersihkan yang selesai</Button>}
          </div>
        </Card>
      )}
      {list.isLoading && <Loading />}
      {list.error ? <ErrorBox error={list.error} /> : null}
      {list.data && (
        <>
          <div className="flex flex-wrap items-center gap-3 text-xs text-ink-2">
            <span>{list.data.summary.total ?? 0} lampiran</span>
            {(["FLAGGED", "UNSCANNED", "CLEAN"] as const).map((s) =>
              list.data.summary[s] ? (
                <span key={s} className="flex items-center gap-1">
                  <ModuleBadge kind="scan" value={s} /> {list.data.summary[s]}
                </span>
              ) : null,
            )}
            {list.data.checked_at && <Freshness at={list.data.checked_at} />}
          </div>
          <Table
            rows={items}
            rowKey={(a) => a.id}
            empty={isAdmin ? "Belum ada lampiran — unggah .md, dokumen atau gambar di atas." : "Belum ada lampiran."}
            onRowClick={(a) => setOpen(open === a.id ? null : a.id)}
            columns={[
              { header: "Berkas", cell: (a) => <span className="font-medium">{a.filename}</span> },
              { header: "Ukuran", cell: (a) => formatBytes(a.size_bytes) },
              { header: "Klasifikasi", cell: (a) => <ClassificationBadge value={a.classification} /> },
              { header: "Pemindaian", cell: (a) => <ModuleBadge kind="scan" value={a.scan_status} /> },
              { header: "Ekstraksi", cell: (a) => <ModuleBadge kind="parse" value={a.parse_status} /> },
              { header: "Temuan", cell: (a) => (a.scan_findings.filter((f) => f.kind !== "UNSCANNED").length || "—") },
              { header: "Diunggah", cell: (a) => absoluteTime(a.created_at) },
            ]}
          />
          {selected && <AttachmentDetail att={selected} />}
        </>
      )}
    </div>
  );
}
