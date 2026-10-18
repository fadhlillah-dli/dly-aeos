import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert, ShieldCheck, Sparkles } from "lucide-react";
import { useAuth } from "../../auth/AuthContext";
import { Button, EmptyState, ErrorBox, Loading, Mono, Notice, Table } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { evidenceApi, type SummaryItem } from "./api";
import { Status } from "./labels";

/**
 * §35: worker-reported completion, verifier result, reviewer verdict, accepted outcome and merged
 * status are separate fields — never one check mark.
 */
export function RunStatusPanel({ onSelectTask }: { onSelectTask: (taskId: string) => void }) {
  const { domain, isAdmin } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["verification-summary", domain], queryFn: evidenceApi.summary });
  const refresh = () => qc.invalidateQueries({ predicate: (x) => ["verification-summary", "evidence", "reviews", "acceptance", "gates"].includes(String(x.queryKey[0])) });
  const verify = useMutation({ mutationFn: (runId: string) => evidenceApi.verify(runId), onSettled: refresh });
  const review = useMutation({ mutationFn: (runId: string) => evidenceApi.review(runId), onSettled: refresh });
  const security = useMutation({ mutationFn: (runId: string) => evidenceApi.review(runId, "SECURITY"), onSettled: refresh });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const rows = q.data?.items ?? [];
  return (
    <div className="space-y-3">
      <Notice>
        Status dipisah per sumber: laporan worker hanyalah klaim; hanya verifier independen yang menjalankan suite tepercaya;
        verdict reviewer bukan penerimaan; merge tetap tindakan manusia.
      </Notice>
      <ErrorBox error={verify.error ?? review.error ?? security.error} />
      {rows.length === 0 ? (
        <EmptyState title="Belum ada run worker di domain ini">
          Run muncul setelah scheduler menjalankan task dari rencana yang disetujui (<Mono>aeos work …</Mono>).
        </EmptyState>
      ) : (
        <Table<SummaryItem>
          rows={rows}
          rowKey={(r) => r.run_id}
          columns={[
            {
              header: "Task / run",
              cell: (r) => (
                <div className="min-w-0">
                  <div className="font-medium">{r.task_title ?? "—"}</div>
                  <div className="mono text-xs text-ink-3">
                    {r.task_key ?? "?"} · run {shortHash(r.run_id, 8)} · attempt {r.attempt} · {shortHash(r.head_commit, 8)}
                  </div>
                </div>
              ),
            },
            { header: "Eksekusi", cell: (r) => <Status status={r.execution_status} /> },
            {
              header: "Laporan worker",
              cell: (r) => (
                <div className="space-y-1">
                  <div className="text-xs">{r.worker_reported.status ?? "—"}</div>
                  <Status status="UNTRUSTED_CLAIM" />
                </div>
              ),
            },
            { header: "Verifier", cell: (r) => <Status status={r.verifier.status} label={r.verifier.status ? undefined : "Belum diverifikasi"} /> },
            { header: "Reviewer", cell: (r) => <Status status={r.reviewer.verdict} label={r.reviewer.verdict ? undefined : "Belum direview"} /> },
            {
              header: "Penerimaan",
              cell: (r) =>
                r.accepted.status === "ACCEPTED" && !r.accepted.valid ? (
                  <Status status="STALE" />
                ) : (
                  <Status status={r.accepted.status} label={r.accepted.status ? undefined : "Belum diputuskan"} />
                ),
            },
            { header: "Merge", cell: (r) => <Status status={r.merged.status} label={r.merged.status ? undefined : "Belum di-merge"} /> },
            {
              header: "Aksi",
              cell: (r) => (
                <div className="flex flex-wrap gap-1.5">
                  {r.task_id && (
                    <Button className="text-xs" onClick={() => onSelectTask(r.task_id!)}>
                      Gate
                    </Button>
                  )}
                  {isAdmin && r.execution_status === "REVIEW" && (
                    <>
                      <Button className="text-xs" busy={verify.isPending && verify.variables === r.run_id} onClick={() => verify.mutate(r.run_id)}>
                        <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> Verifikasi
                      </Button>
                      <Button
                        className="text-xs"
                        disabled={r.verifier.status !== "PASS"}
                        title={r.verifier.status !== "PASS" ? "Review hanya setelah verifier PASS" : undefined}
                        busy={review.isPending && review.variables === r.run_id}
                        onClick={() => review.mutate(r.run_id)}
                      >
                        <Sparkles className="h-3.5 w-3.5" aria-hidden /> Minta review
                      </Button>
                      <Button
                        className="text-xs"
                        disabled={r.verifier.status !== "PASS"}
                        title="Sesi Security Reviewer terpisah (§15) — wajib untuk risiko R3/R4 atau perubahan kontrol keamanan"
                        busy={security.isPending && security.variables === r.run_id}
                        onClick={() => security.mutate(r.run_id)}
                      >
                        <ShieldAlert className="h-3.5 w-3.5" aria-hidden /> Review keamanan
                      </Button>
                    </>
                  )}
                </div>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}
