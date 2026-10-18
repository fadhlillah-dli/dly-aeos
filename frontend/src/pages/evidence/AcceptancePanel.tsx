import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../../auth/AuthContext";
import { Button, EmptyState, ErrorBox, Loading, Mono, Notice, Table } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";
import { evidenceApi, type AcceptanceItem } from "./api";
import { Status } from "./labels";

export function AcceptancePanel() {
  const { domain, isAdmin } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["acceptance", domain], queryFn: () => evidenceApi.acceptance() });
  const recheck = useMutation({
    mutationFn: (id: string) => evidenceApi.recheck(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ["acceptance"] }),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const items = q.data?.items ?? [];
  return (
    <div className="space-y-3">
      <Notice>
        Penerimaan adalah keputusan kode (verifier PASS + review PASS-CONTROLLED atau lebih) yang terikat hash artefak,
        base/head, config dan versi rencana. Bukan persetujuan manusia; merge tetap keputusan pemilik.
      </Notice>
      <ErrorBox error={recheck.error} />
      {items.length === 0 ? (
        <EmptyState title="Belum ada keputusan penerimaan">
          Penerimaan muncul setelah verifier PASS dan review lulus: <Mono>aeos verify accept &lt;run_id&gt;</Mono>.
        </EmptyState>
      ) : (
        <Table<AcceptanceItem>
          rows={items}
          rowKey={(r) => r.id}
          columns={[
            { header: "Task / run", cell: (r) => <span className="mono text-xs">{shortHash(r.task_id, 8)} / {shortHash(r.run_id, 8)}</span> },
            {
              header: "Status",
              cell: (r) => (
                <div className="space-y-1">
                  <Status status={r.acceptance_status} />
                  {r.acceptance_status === "ACCEPTED" && !r.valid && <Status status="STALE" label="Tidak berlaku lagi" />}
                  {r.mode !== "LIVE" && <Status status={r.mode} />}
                </div>
              ),
            },
            { header: "Verdict review", cell: (r) => <Status status={r.review_verdict} /> },
            {
              header: "Ikatan (artefak · head · config · plan)",
              cell: (r) => (
                <span className="mono text-xs">
                  {shortHash(r.artifact_hash, 8)} · {shortHash(r.head_commit, 8)} · {shortHash(r.config_hash, 8)} · {shortHash(r.plan_version_hash, 8)}
                </span>
              ),
            },
            {
              header: "Alasan / kedaluwarsa",
              cell: (r) => {
                const lines = [...r.reasons, ...r.staleness, ...(r.invalidated_reason ? [r.invalidated_reason] : [])];
                return lines.length ? (
                  <ul className="list-disc pl-4 text-xs">
                    {lines.map((l, i) => (
                      <li key={i}>{l}</li>
                    ))}
                  </ul>
                ) : (
                  <span className="text-xs text-ink-3">—</span>
                );
              },
            },
            { header: "Waktu", cell: (r) => <span className="text-xs">{relativeTime(r.updated_at)}</span> },
            {
              header: "",
              cell: (r) =>
                isAdmin && r.acceptance_status === "ACCEPTED" ? (
                  <Button className="text-xs" busy={recheck.isPending && recheck.variables === r.id} onClick={() => recheck.mutate(r.id)}>
                    Periksa ulang
                  </Button>
                ) : null,
            },
          ]}
        />
      )}
    </div>
  );
}
