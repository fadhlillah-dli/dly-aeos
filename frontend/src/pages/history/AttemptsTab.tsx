import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../../api/client";
import { Provenance } from "../../components/control_center/Provenance";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, ErrorBox, Field, inputClass, Loading, Mono, Table } from "../../components/ui";
import { absoluteTime, relativeTime } from "../../lib/format";

export interface AttemptRow {
  run_id: string;
  task_id: string | null;
  project_id: string;
  attempt: number;
  run_type: string;
  execution_status: string;
  worker_reported_status: string | null;
  verifier_status: string | null;
  acceptance_status: string | null;
  merged_status: string | null;
  started_at: string | null;
  finished_at: string | null;
  mode: string;
  link: string;
}

interface AttemptPage {
  items: AttemptRow[];
  page: number;
  page_size: number;
  total: number;
  source: string;
  checked_at: string;
}

/** §35 History: accepted / failed / cancelled attempts across runs; the four statuses stay separate. */
export function AttemptsTab() {
  const [outcome, setOutcome] = useState("");
  const [page, setPage] = useState(1);
  const params = new URLSearchParams({ page: String(page), page_size: "50" });
  if (outcome) params.set("outcome", outcome);
  const q = useQuery({
    queryKey: ["history-attempts", params.toString()],
    queryFn: () => get<AttemptPage>(`/api/control-center/history/attempts?${params.toString()}`),
  });
  const pages = q.data ? Math.max(1, Math.ceil(q.data.total / q.data.page_size)) : 1;
  return (
    <div className="space-y-3">
      <Field label="Hasil percobaan">
        <select aria-label="Filter hasil percobaan" className={`${inputClass} w-auto`} value={outcome}
          onChange={(e) => { setOutcome(e.target.value); setPage(1); }}>
          <option value="">semua</option>
          <option value="ACCEPTED">diterima</option>
          <option value="FAILED">gagal</option>
          <option value="CANCELLED">dibatalkan</option>
        </select>
      </Field>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : (
        <Table
          rows={q.data?.items ?? []}
          rowKey={(r) => r.run_id}
          empty="Belum ada percobaan untuk filter ini"
          columns={[
            { header: "Selesai", cell: (r) => <span title={absoluteTime(r.finished_at)}>{relativeTime(r.finished_at)}</span> },
            {
              header: "Run / attempt",
              cell: (r) => (
                <Link to={r.link} className="text-accent underline">
                  <Mono>{r.run_id.slice(0, 8)}</Mono> · {r.run_type} · attempt {r.attempt}
                </Link>
              ),
            },
            { header: "Eksekusi", cell: (r) => <StatusBadge status={r.execution_status} /> },
            { header: "Laporan worker", cell: (r) => <span className="text-xs">{r.worker_reported_status ?? "—"} (klaim)</span> },
            { header: "Verifier", cell: (r) => <StatusBadge status={r.verifier_status ?? "NOT_RUN"} /> },
            { header: "Penerimaan", cell: (r) => <StatusBadge status={r.acceptance_status ?? "PENDING"} /> },
            { header: "Merge", cell: (r) => <span className="text-xs">{r.merged_status ?? "NOT_MERGED"} (manusia)</span> },
            { header: "Mode", cell: (r) => (r.mode === "LIVE" ? "LIVE" : <StatusBadge status={r.mode} />) },
          ]}
        />
      )}
      <div className="flex items-center gap-2 text-xs">
        <Button disabled={page <= 1} onClick={() => setPage(page - 1)}>Sebelumnya</Button>
        <span>halaman {page} / {pages}</span>
        <Button disabled={page >= pages} onClick={() => setPage(page + 1)}>Berikutnya</Button>
      </div>
      <Provenance source={q.data?.source} checkedAt={q.data?.checked_at}
        evidenceLimit="Laporan worker adalah klaim; hanya verifier + penerimaan yang membuktikan; merge tetap tindakan manusia." />
    </div>
  );
}
