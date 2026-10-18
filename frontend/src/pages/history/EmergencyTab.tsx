import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";
import { useState } from "react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Button, Card, ErrorBox, Field, inputClass, KeyValue, Loading, Mono, Notice } from "../../components/ui";
import { relativeTime } from "../../lib/format";
import type { EmergencyReport, StepResult } from "./types";

export const CONFIRM_PHRASE = "HENTIKAN";

interface LastInfo {
  last: { incident_id: string; at: string; status: string; reason: string; by: string; steps?: StepResult[] } | null;
  scheduling: { status: string; reason?: string };
  steps: { step: string; description: string }[];
}

const COUNT_LABELS: [string, string][] = [
  ["active_before", "aktif sebelum"],
  ["remaining_active", "masih aktif"],
  ["active_runs", "run aktif"],
  ["cancelled", "dibatalkan"],
  ["failed", "gagal"],
];

function counts(s: StepResult): string {
  return COUNT_LABELS.filter(([k]) => typeof s[k] === "number")
    .map(([k, label]) => `${label}: ${String(s[k])}`)
    .join(", ");
}

export function StepList({ steps, scope }: { steps: StepResult[]; scope?: string }) {
  const otherNote = steps.map((s) => s.other_domains).find((n): n is string => typeof n === "string");
  return (
    <div>
      {scope && (
        <p className="mb-1 text-xs text-ink-3">
          {scope === "NO_DOMAIN_SELECTED" ? "Tanpa domain terpilih: hanya status per langkah yang ditampilkan." : `Rincian untuk domain ${scope}.`}
        </p>
      )}
      <ol className="space-y-1 text-sm">
        {steps.map((s) => (
          <li key={s.step} className="flex flex-wrap items-center gap-2">
            <OpsBadge status={s.status} />
            <Mono>{s.step}</Mono>
            <span className="text-ink-3">{s.reason ?? s.detail ?? ""}</span>
            {counts(s) && <span className="text-xs text-ink-3">({counts(s)})</span>}
          </li>
        ))}
      </ol>
      {otherNote && <p className="mt-1 text-xs text-ink-3">{otherNote}</p>}
    </div>
  );
}

export function EmergencyTab() {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["ops-emergency"], queryFn: () => get<LastInfo>("/api/ops/emergency-stop/last") });
  const [reason, setReason] = useState("");
  const [owner, setOwner] = useState("");
  const [blast, setBlast] = useState("");
  const [phrase, setPhrase] = useState("");
  const [report, setReport] = useState<EmergencyReport | null>(null);
  const run = useMutation({
    mutationFn: async () => {
      const prep = await post<{ token: string }>("/api/ops/emergency-stop/prepare");
      return post<EmergencyReport>("/api/ops/emergency-stop", { token: prep.token, reason, incident_owner: owner, blast_radius: blast });
    },
    onSuccess: (r) => {
      setReport(r);
      setPhrase("");
      qc.invalidateQueries();
    },
  });
  const ready = isAdmin && reason.trim().length >= 5 && phrase === CONFIRM_PHRASE;

  if (info.isLoading) return <Loading />;
  return (
    <div className="space-y-3">
      <Notice tone="warn">
        Emergency stop menjeda penjadwalan, mencabut semua capability aktif, membatalkan run RUNNING/LEASED, menyimpan bukti dan mencatat
        audit. Setiap langkah dilaporkan terpisah. Efek eksternal yang sudah terjadi tidak dibatalkan otomatis.
      </Notice>
      {info.error ? <ErrorBox error={info.error} /> : null}
      {info.data && (
        <Card title="Status saat ini">
          <KeyValue
            items={[
              ["Penjadwalan", <OpsBadge status={info.data.scheduling.status} />],
              ["Emergency stop terakhir", info.data.last ? `${info.data.last.status} · ${relativeTime(info.data.last.at)} · ${info.data.last.reason}` : "belum pernah"],
            ]}
          />
          {info.data.last?.steps?.length ? (
            <div className="mt-2">
              <StepList steps={info.data.last.steps} />
            </div>
          ) : null}
        </Card>
      )}
      <Card title={<span className="inline-flex items-center gap-1"><ShieldAlert className="h-4 w-4" aria-hidden /> Jalankan emergency stop</span>}>
        {!isAdmin ? (
          <p className="text-sm text-ink-3">Hanya SUPER_ADMIN yang dapat menjalankan emergency stop (backend juga menolak VIEWER).</p>
        ) : (
          <div className="space-y-3">
            <Field label="Alasan (wajib)">
              <textarea className={inputClass} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Pemilik insiden">
                <input className={inputClass} value={owner} onChange={(e) => setOwner(e.target.value)} />
              </Field>
              <Field label="Blast radius yang diketahui">
                <input className={inputClass} value={blast} onChange={(e) => setBlast(e.target.value)} />
              </Field>
            </div>
            <Field label={`Ketik ${CONFIRM_PHRASE} untuk konfirmasi`}>
              <input className={inputClass} value={phrase} onChange={(e) => setPhrase(e.target.value)} aria-label="Konfirmasi emergency stop" />
            </Field>
            <Button variant="danger" disabled={!ready} busy={run.isPending} onClick={() => run.mutate()}>
              Hentikan semua sekarang
            </Button>
            {run.error ? <ErrorBox error={run.error} /> : null}
          </div>
        )}
      </Card>
      {report && (
        <Card title={<span className="flex items-center gap-2">Hasil insiden <Mono>{report.incident_id.slice(0, 8)}</Mono> <OpsBadge status={report.status} /></span>}>
          <StepList steps={report.steps} scope={report.scope} />
          <p className="mt-2 text-xs text-ink-3">{report.notice}</p>
        </Card>
      )}
    </div>
  );
}
