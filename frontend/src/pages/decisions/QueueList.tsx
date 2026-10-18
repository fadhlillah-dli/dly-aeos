import { ArrowRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { QueueItem } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, Mono } from "../../components/ui";
import { shortHash } from "../../lib/format";

export const KIND_LABEL: Record<string, string> = {
  PLAN: "Rencana",
  PUBLICATION: "Publikasi",
  CHANGE_PROPOSAL: "Usulan perubahan",
  TASK_BLOCKED: "Task terblokir",
  RUN_REVIEW: "Run menunggu",
  DECISION_EXPIRING: "Keputusan kedaluwarsa",
  DECISION_REQUEST: "Permintaan keputusan",
};

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} mnt`;
  if (s < 86400) return `${Math.round(s / 3600)} jam`;
  return `${Math.round(s / 86400)} hari`;
}

/** U-12 single Human Decision Queue: what waits, why, which decision, where to act. */
export function QueueList({
  items,
  highlight,
  renderActions,
}: {
  items: QueueItem[];
  highlight?: string | null;
  renderActions?: (item: QueueItem) => ReactNode;
}) {
  if (!items.length)
    return (
      <EmptyState title="Tidak ada keputusan yang menunggu Anda">
        Antrean diisi otomatis dari record kanonik (rencana, publikasi, usulan, task, run, keputusan, permintaan).
      </EmptyState>
    );
  return (
    <ul className="grid gap-2">
      {items.map((i) => (
        <li
          key={i.id}
          className={`rounded-lg border p-3 ${highlight && i.target.id === highlight ? "border-accent ring-2 ring-accent/30" : "border-line"} bg-surface`}
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={i.priority === 1 ? "BLOCKED" : "REVIEW"} label={KIND_LABEL[i.kind] ?? i.kind} />
                <span className="font-medium">{i.title}</span>
                {i.mode && i.mode !== "LIVE" && <StatusBadge status={i.mode} />}
              </div>
              <p className="mt-1 text-sm text-ink-2">{i.why}</p>
              <p className="mt-1 text-sm">
                <span className="text-ink-3">Keputusan diminta:</span> {i.requested_decision}
              </p>
              <div className="mt-1 flex flex-wrap gap-3 text-xs text-ink-3">
                <span>
                  Target <Mono>{i.target.type}:{String(i.target.id).slice(0, 8)}</Mono>
                </span>
                {i.target.version !== undefined && i.target.version !== null && <span>versi v{i.target.version}</span>}
                {i.target.hash && (
                  <span>
                    hash <Mono>{shortHash(i.target.hash)}</Mono>
                  </span>
                )}
                <span>menunggu {formatAge(i.age_seconds)}</span>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {renderActions?.(i)}
              <Link to={i.link} className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-sm hover:bg-surface-3">
                Tindak lanjuti <ArrowRight className="h-4 w-4" aria-hidden />
              </Link>
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}
