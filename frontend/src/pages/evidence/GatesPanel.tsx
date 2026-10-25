import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitPullRequest, Send } from "lucide-react";
import { useAuth } from "../../auth/AuthContext";
import { Button, Card, EmptyState, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, Table } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { evidenceApi, type GateItem, type SeparateStatus } from "./api";
import { GATE_HELP, Status } from "./labels";

const SPECIALIST_LABELS: Record<string, string> = {
  A11Y: "Aksesibilitas/UX",
  PRIVACY: "Privasi/data",
  SRE: "SRE/operasi",
};

function SpecialistReviews({ s }: { s: SeparateStatus }) {
  const rows = Object.entries(s.specialist_reviews ?? {}).filter(([k, v]) => k !== "SECURITY" && (v.required || v.verdict));
  if (rows.length === 0) return <span className="text-xs text-ink-3">Tidak diwajibkan untuk task ini</span>;
  return (
    <span className="flex flex-col gap-1">
      {rows.map(([k, v]) => (
        <span key={k} className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium">{SPECIALIST_LABELS[k] ?? k}</span>
          <Status status={v.verdict} label={v.verdict ? undefined : "Wajib — belum direview"} />
          {v.required && v.reason && <span className="text-xs text-ink-3">{v.reason}</span>}
        </span>
      ))}
    </span>
  );
}

export function SeparateFields({ s }: { s: SeparateStatus }) {
  return (
    <KeyValue
      items={[
        ["Laporan worker", <span key="w" className="flex flex-wrap items-center gap-2">{s.worker_reported.status ?? "—"} <Status status="UNTRUSTED_CLAIM" /></span>],
        [
          "Verifier",
          <span key="v" className="flex flex-wrap items-center gap-2">
            <Status status={s.verifier.status} label={s.verifier.status ? undefined : "Belum diverifikasi"} />
            {/* §20: WEAK (process runner) is never presented as an isolated sandbox; CONTAINER only when proven */}
            {s.verifier.isolation && <Status status={s.verifier.isolation} />}
          </span>,
        ],
        ["Reviewer", <span key="r" className="flex flex-wrap gap-2"><Status status={s.reviewer.verdict} label={s.reviewer.verdict ? undefined : "Belum direview"} />{s.reviewer.status && <Status status={s.reviewer.status} />}</span>],
        [
          "Review keamanan",
          s.security_reviewer?.required || s.security_reviewer?.verdict ? (
            <span key="s" className="flex flex-wrap items-center gap-2">
              <Status status={s.security_reviewer?.verdict} label={s.security_reviewer?.verdict ? undefined : "Wajib — belum direview"} />
              {s.security_reviewer?.required && <span className="text-xs text-ink-3">wajib (risiko/kontrol keamanan)</span>}
            </span>
          ) : (
            <span key="s" className="text-xs text-ink-3">Tidak diwajibkan untuk task ini</span>
          ),
        ],
        ["Review spesialis lain", <SpecialistReviews key="sp" s={s} />],
        [
          "Governance (§32)",
          s.governance ? (
            <span key="g" className="flex flex-wrap items-center gap-2">
              <Status status={s.governance.status} />
              <span className="mono text-xs">{s.governance.adapter}</span>
              {!s.governance.allows_promotion && <span className="text-xs text-warn">menahan promosi</span>}
              {s.governance.note && <span className="text-xs text-ink-3">{s.governance.note}</span>}
            </span>
          ) : (
            <span key="g" className="text-xs text-ink-3">—</span>
          ),
        ],
        [
          "Provider delivery",
          s.delivery_provider ? (
            <span key="dp" className="flex flex-wrap items-center gap-2">
              <Status status={s.delivery_provider.status} />
              <span className="mono text-xs">
                {s.delivery_provider.provider} / {s.delivery_provider.ci}
              </span>
              {s.delivery_provider.reason && <span className="text-xs text-ink-3">{s.delivery_provider.reason}</span>}
            </span>
          ) : (
            <span key="dp" className="text-xs text-ink-3">—</span>
          ),
        ],
        [
          "Diterima",
          s.accepted.status === "ACCEPTED" && !s.accepted.valid ? (
            <Status key="a" status="STALE" />
          ) : (
            <Status key="a" status={s.accepted.status} label={s.accepted.status ? undefined : "Belum diputuskan"} />
          ),
        ],
        ["Merge", <span key="m" className="flex flex-wrap items-center gap-2"><Status status={s.merged.status} label={s.merged.status ? undefined : "Belum di-merge"} />{s.merged.merged_by && <span className="text-xs">oleh {s.merged.merged_by} ({s.merged.merged_by_type})</span>}</span>],
      ]}
    />
  );
}

export function GatesPanel({ taskId }: { taskId: string | null }) {
  const { domain, isAdmin } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["gates", domain, taskId],
    queryFn: () => evidenceApi.gates(taskId!),
    enabled: !!taskId,
  });
  const refresh = () => qc.invalidateQueries({ predicate: (x) => ["gates", "prs", "evidence", "acceptance"].includes(String(x.queryKey[0])) });
  const prepare = useMutation({ mutationFn: (id: string) => evidenceApi.preparePr(id), onSettled: refresh });
  const handoff = useMutation({ mutationFn: (id: string) => evidenceApi.handoff(id), onSettled: refresh });

  if (!taskId)
    return (
      <EmptyState title="Pilih task">
        Buka tab “Status run” lalu klik <strong>Gate</strong> pada task, atau jalankan <Mono>aeos verify gates &lt;task_id&gt;</Mono>.
      </EmptyState>
    );
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const accValid = !!d.acceptance?.valid;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="font-semibold">{d.title}</div>
          <div className="mono text-xs text-ink-3">
            {d.task_key} · head {shortHash(d.head_commit, 10)}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {d.mode !== "LIVE" && <Status status={d.mode} />}
          <Freshness at={d.checked_at} label="Dihitung" />
        </div>
      </div>
      <Notice tone="warn">
        Merge ke main dan rilis adalah tindakan manusia. DLY AEOS tidak memiliki operasi merge atau deploy.
        {d.ready_for_human_merge ? " Semua gate pra-merge terpenuhi untuk keputusan manusia." : " Gate pra-merge belum seluruhnya terpenuhi."}
      </Notice>
      <Table<GateItem>
        rows={d.gates}
        rowKey={(g) => g.gate}
        columns={[
          { header: "Gate", cell: (g) => <div><div className="font-medium">{g.gate} {g.name}</div><div className="text-xs text-ink-3">{GATE_HELP[g.gate]}</div></div> },
          { header: "Status", cell: (g) => <Status status={g.status} /> },
          { header: "Ringkasan", cell: (g) => <span className="text-sm">{g.summary}</span> },
          { header: "Sumber", cell: (g) => <span className="text-xs text-ink-3">{g.source}</span> },
          { header: "Bukti", cell: (g) => <span className="mono text-xs">{g.evidence_refs.map((r) => shortHash(r, 8)).join(", ") || "—"}</span> },
        ]}
      />
      <Card title="Status terpisah (§35)">
        <SeparateFields s={d.separate_status} />
      </Card>
      {isAdmin && (
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            disabled={!accValid || !!d.pull_request}
            title={!accValid ? "Butuh penerimaan yang masih berlaku" : d.pull_request ? "PR sudah disiapkan" : undefined}
            busy={prepare.isPending}
            onClick={() => prepare.mutate(d.task_id)}
          >
            <GitPullRequest className="h-4 w-4" aria-hidden /> Siapkan PR (broker)
          </Button>
          <Button disabled={!accValid} busy={handoff.isPending} onClick={() => handoff.mutate(d.task_id)}>
            <Send className="h-4 w-4" aria-hidden /> Handoff governance
          </Button>
        </div>
      )}
      <ErrorBox error={prepare.error ?? handoff.error} />
      {prepare.data && (
        <Notice tone={prepare.data.notes?.length ? "warn" : "info"}>
          PR disiapkan melalui broker{prepare.data.pull_request.number ? ` (#${prepare.data.pull_request.number})` : ""}. Merge
          tetap keputusan manusia.
          {prepare.data.notes?.map((n) => (
            <span key={n} className="block">
              {n}
            </span>
          ))}
        </Notice>
      )}
      {handoff.data && (
        <Notice tone="warn">
          Hasil governance handoff: <strong>{handoff.data.output.result}</strong> — belum ada governance harness downstream
          yang dikonfigurasi (Harness.io/pipeline kantor belum diperiksa).
        </Notice>
      )}
    </div>
  );
}
