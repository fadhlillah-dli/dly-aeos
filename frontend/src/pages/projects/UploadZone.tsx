import { clsx } from "clsx";
import { FileWarning, Loader2, ShieldAlert, Upload, X } from "lucide-react";
import { useCallback, useRef, useState, type DragEvent } from "react";
import { post } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { ErrorBox } from "../../components/ui";
import { formatBytes, ModuleBadge } from "./badges";
import type { AttachmentView, IntakePolicy } from "./types";

export type QueueStatus = "queued" | "rejected-local" | "uploading" | "done" | "error";

export interface QueuedFile {
  id: string;
  file: File;
  status: QueueStatus;
  localReason?: string;
  result?: AttachmentView;
  error?: unknown;
}

const DEFAULT_ALLOWED = [".md", ".markdown", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".docx", ".json", ".yaml", ".yml", ".csv", ".zip"];

/** Client-side pre-check is a convenience only; the backend enforces type, size and content (§05 step 5). */
export function precheck(file: File, policy?: IntakePolicy): string | null {
  const allowed = policy?.allowed_extensions ?? DEFAULT_ALLOWED;
  const name = file.name.toLowerCase();
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot) : "";
  if (!allowed.includes(ext)) return `Jenis ${ext || "(tanpa ekstensi)"} tidak diterima. Diterima: ${allowed.join(" ")}`;
  if (policy && file.size > policy.max_upload_bytes) return `Ukuran ${formatBytes(file.size)} melebihi batas ${formatBytes(policy.max_upload_bytes)}`;
  if (file.size === 0) return "Berkas kosong";
  return null;
}

let seq = 0;

export function useUploadQueue(policy?: IntakePolicy) {
  const [items, setItems] = useState<QueuedFile[]>([]);
  const update = (id: string, patch: Partial<QueuedFile>) => setItems((prev) => prev.map((q) => (q.id === id ? { ...q, ...patch } : q)));

  const add = useCallback(
    (files: FileList | File[]) => {
      const next = Array.from(files).map<QueuedFile>((file) => {
        const reason = precheck(file, policy);
        seq += 1;
        return { id: `f${seq}`, file, status: reason ? "rejected-local" : "queued", localReason: reason ?? undefined };
      });
      setItems((prev) => [...prev, ...next]);
    },
    [policy],
  );

  const remove = (id: string) => setItems((prev) => prev.filter((q) => q.id !== id));

  const uploadAll = async (projectId: string, source = "upload"): Promise<number> => {
    let ok = 0;
    for (const q of items) {
      if (q.status !== "queued" && q.status !== "error") continue;
      update(q.id, { status: "uploading", error: undefined });
      const fd = new FormData();
      fd.append("file", q.file, q.file.name);
      fd.append("source", source);
      try {
        const res = await post<{ attachment: AttachmentView }>(`/api/projects/${projectId}/attachments`, fd);
        update(q.id, { status: "done", result: res.attachment });
        ok += 1;
      } catch (e) {
        update(q.id, { status: "error", error: e });
      }
    }
    return ok;
  };

  const pending = items.filter((q) => q.status === "queued" || q.status === "error").length;
  const clearDone = () => setItems((prev) => prev.filter((q) => q.status !== "done"));
  return { items, add, remove, uploadAll, pending, clearDone };
}

export function DropZone({ onFiles, disabled, policy }: { onFiles: (f: FileList | File[]) => void; disabled?: boolean; policy?: IntakePolicy }) {
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setOver(false);
    if (!disabled && e.dataTransfer.files.length) onFiles(e.dataTransfer.files);
  };
  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      aria-label="Unggah lampiran"
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      onClick={() => !disabled && input.current?.click()}
      onKeyDown={(e) => {
        if (!disabled && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          input.current?.click();
        }
      }}
      className={clsx(
        "flex flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed p-6 text-center text-sm",
        over ? "border-accent bg-info-bg" : "border-line bg-surface",
        disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer hover:border-accent",
      )}
    >
      <Upload className="h-6 w-6 text-ink-3" aria-hidden />
      <div className="font-medium">Tarik &amp; lepas berkas di sini, atau klik untuk memilih</div>
      <div className="text-xs text-ink-3">
        .md, dokumen, gambar, data, .zip — maks {policy ? formatBytes(policy.max_upload_bytes) : "25 MB"} per berkas. Berkas tidak pernah dieksekusi.
      </div>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        data-testid="file-input"
        accept={(policy?.allowed_extensions ?? DEFAULT_ALLOWED).join(",")}
        onChange={(e) => {
          if (e.target.files?.length) onFiles(e.target.files);
          e.target.value = "";
        }}
      />
    </div>
  );
}

const STATUS_LABEL: Record<QueueStatus, [string, string]> = {
  queued: ["QUEUED", "Siap diunggah"],
  "rejected-local": ["REJECTED", "Ditolak sebelum dikirim"],
  uploading: ["RUNNING", "Mengunggah & memindai…"],
  done: ["SUCCEEDED", "Diterima sebagai DATA"],
  error: ["FAILED", "Ditolak server"],
};

export function QueueList({ items, onRemove }: { items: QueuedFile[]; onRemove?: (id: string) => void }) {
  if (!items.length) return null;
  return (
    <ul className="mt-3 divide-y divide-line rounded-lg border border-line bg-surface" aria-label="Antrean lampiran">
      {items.map((q) => {
        const [st, label] = STATUS_LABEL[q.status];
        const findings = q.result?.scan_findings ?? [];
        return (
          <li key={q.id} className="p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              {q.status === "uploading" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
              <span className="min-w-0 flex-1 truncate font-medium" title={q.file.name}>
                {q.file.name}
              </span>
              <span className="text-xs text-ink-3">{formatBytes(q.file.size)}</span>
              <StatusBadge status={st} label={label} />
              {q.result && (
                <>
                  <ModuleBadge kind="scan" value={q.result.scan_status} />
                  <ModuleBadge kind="parse" value={q.result.parse_status} />
                </>
              )}
              {onRemove && q.status !== "uploading" && q.status !== "done" && (
                <button className="rounded p-1 text-ink-3 hover:bg-surface-3" onClick={() => onRemove(q.id)} aria-label={`Hapus ${q.file.name} dari antrean`}>
                  <X className="h-4 w-4" aria-hidden />
                </button>
              )}
            </div>
            {q.localReason && (
              <div className="mt-1 flex items-center gap-1 text-xs text-bad">
                <FileWarning className="h-3.5 w-3.5" aria-hidden /> {q.localReason}
              </div>
            )}
            {q.status === "error" && (
              <div className="mt-2">
                <ErrorBox error={q.error} />
              </div>
            )}
            {findings.length > 0 && (
              <ul className="mt-2 space-y-1 text-xs">
                {findings.map((f, i) => (
                  <li key={i} className="flex items-start gap-1 text-warn">
                    <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span>
                      <strong>{f.kind}</strong>
                      {f.marker ? ` (${f.marker}, baris ${f.line})` : ""}: {f.message}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}
