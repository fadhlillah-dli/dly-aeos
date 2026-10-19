import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { post } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, ErrorBox, Field, inputClass, Mono, Notice } from "../../components/ui";
import type { ReviewPackage } from "./types";

const ACTION_LABELS: Record<string, string> = {
  TRIAL_APPROVE: "Setujui TRIAL terisolasi (bukan promosi)",
  PROMOTE: "Setujui PROMOSI (keputusan terpisah dari trial)",
  KEEP: "Pertahankan baseline (KEEP)",
  REJECT: "Tolak (REJECT)",
  DEFER: "Tunda (DEFER)",
  REQUEST_MORE_EVIDENCE: "Minta bukti tambahan",
  REFERENCE_ONLY: "Simpan sebagai referensi saja",
};

const SCOPE_LABELS: Record<string, string> = {
  target: "Target",
  environment: "Environment",
  provider: "Provider",
  data_class: "Kelas data",
  budget_usd: "Budget (USD, tidak menaikkan budget)",
  max_minutes: "Waktu maks (menit)",
  max_attempts: "Attempt maks",
  stop_conditions: "Stop conditions (pisahkan dengan ;)",
  rollback_scope: "Rollback scope",
};

/** §39 item 9 + §40: the human decision, bound to the EXACT baseline/candidate hashes shown here. */
export function DecisionPanel({ proposalId, pkg, allowed, isAdmin }: { proposalId: string; pkg: ReviewPackage; allowed: string[]; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [picked, setAction] = useState(allowed[0] ?? "");
  const action = allowed.includes(picked) ? picked : (allowed[0] ?? "");
  const [rationale, setRationale] = useState("");
  const [scope, setScope] = useState<Record<string, string>>({ environment: "sandbox", budget_usd: "0", max_minutes: "60", max_attempts: "2" });
  const bind = pkg.human_decision.bind;
  const gate = pkg.human_decision.source_identity_gate;
  const needsScope = action === "TRIAL_APPROVE" || action === "PROMOTE";
  const m = useMutation({
    mutationFn: () =>
      post(`/api/proposals/${proposalId}/decision`, {
        action,
        baseline_hash: bind.baseline_hash,
        candidate_hash: bind.candidate_hash,
        rationale,
        scope: needsScope
          ? Object.fromEntries(
              Object.entries(scope)
                .filter(([, v]) => v !== "")
                .map(([k, v]) => [k, ["budget_usd", "max_minutes", "max_attempts"].includes(k) ? Number(v) : v]),
            )
          : {},
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposal", proposalId] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate();
  };
  return (
    <div className="space-y-3">
      <div className="rounded-md border border-line bg-surface-2 p-3 text-sm">
        <div className="mb-1 flex items-center gap-2 font-medium">
          <ShieldCheck className="h-4 w-4" aria-hidden /> Hash yang akan diikat oleh keputusan
        </div>
        <div className="grid gap-1">
          <div>
            baseline_hash: <Mono>{bind.baseline_hash}</Mono>
          </div>
          <div>
            candidate_hash: <Mono>{bind.candidate_hash}</Mono>
          </div>
        </div>
        <p className="mt-2 text-xs text-ink-3">Perubahan baseline, kandidat, provider, kelas data, budget atau target membatalkan binding; keputusan harus diulang.</p>
      </div>
      <ul className="list-disc pl-5 text-sm text-ink-2">
        {pkg.human_decision.rules.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {gate?.status === "BLOCKED" && (
        <Notice tone="warn">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status="BLOCKED" label="Admission TRIAL/PROMOTE diblokir" />
            <span>Identitas sumber belum terverifikasi:</span>
            {gate.blocking.map((b) => (
              <span key={b.identity} className="inline-flex items-center gap-1">
                <Mono>{b.identity}</Mono> <StatusBadge status={b.status === "CHANGED" ? "DRIFTED" : b.status === "NOT_FOUND" ? "MISSING" : "UNVERIFIED"} label={b.status} />
              </span>
            ))}
          </div>
          {gate.remedy && <p className="mt-1 text-xs">{gate.remedy}</p>}
        </Notice>
      )}
      {pkg.human_decision.next_actions.length > 0 && <Notice>{pkg.human_decision.next_actions.join(" · ")}</Notice>}
      {!isAdmin ? (
        <Notice>Keputusan hanya dapat dibuat oleh SUPER_ADMIN (manusia). Model/agent tidak dapat memberi approval.</Notice>
      ) : allowed.length === 0 ? (
        <Notice tone="warn">Tidak ada keputusan yang tersedia pada status ini (buat paket review terlebih dahulu atau hasil sudah tercatat).</Notice>
      ) : (
        <form onSubmit={submit} className="space-y-3" aria-label="Keputusan manusia">
          <Field label="Keputusan">
            <select className={inputClass} value={action} onChange={(e) => setAction(e.target.value)}>
              {allowed.map((a) => (
                <option key={a} value={a}>
                  {ACTION_LABELS[a] ?? a}
                </option>
              ))}
            </select>
          </Field>
          {action && <p className="text-xs text-ink-3">{pkg.human_decision.options[action]}</p>}
          {needsScope && (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {pkg.human_decision.scope_fields.map((f) => (
                <Field key={f} label={SCOPE_LABELS[f] ?? f}>
                  <input className={inputClass} value={scope[f] ?? ""} onChange={(e) => setScope({ ...scope, [f]: e.target.value })} />
                </Field>
              ))}
            </div>
          )}
          <Field label="Alasan (tercatat pada Decision)">
            <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} required minLength={3} />
          </Field>
          <ErrorBox error={m.error} />
          {m.isSuccess && <StatusBadge status="CONFIGURED" label="Keputusan tercatat" />}
          <Button variant="primary" type="submit" busy={m.isPending} disabled={!action}>
            Catat keputusan terikat hash
          </Button>
        </form>
      )}
    </div>
  );
}
