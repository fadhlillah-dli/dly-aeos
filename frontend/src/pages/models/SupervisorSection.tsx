import { FlaskConical } from "lucide-react";
import { useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, ErrorBox, Freshness, inputBase, KeyValue, Loading, Mono, Notice } from "../../components/ui";
import { CopyCommand } from "../integrations/CopyCommand";
import { useCapabilityTest, useClaudeStatus } from "./api";
import { Badge } from "./badges";

export function SupervisorSection() {
  const { isAdmin } = useAuth();
  const q = useClaudeStatus();
  const test = useCapabilityTest();
  const [model, setModel] = useState<"opus" | "fable">("opus");

  if (q.isLoading) return <Loading label="Memeriksa Claude Code resmi…" />;
  if (q.error) return <ErrorBox error={q.error} />;
  const s = q.data!;
  const t = s.last_capability_test;

  return (
    <Card
      title="Pengawas: Claude Code resmi (login Max native, tanpa Claude API)"
      actions={
        <span className="flex flex-wrap items-center gap-2">
          <Freshness at={s.checked_at} label="Diperiksa" staleAfterSeconds={300} />
          <StatusBadge status={s.bridge_status === "BLOCKED" ? "BLOCKED" : t?.status ?? "NOT_TESTED"} />
        </span>
      }
    >
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-3">
          <KeyValue
            items={[
              ["Binary", <span key="b" className="flex flex-wrap items-center gap-2"><StatusBadge status={s.binary.status} /> <Mono>{s.binary.path_logical}</Mono></span>],
              ["Versi", s.binary.version ?? <StatusBadge status="UNKNOWN" />],
              ["Mode autentikasi", <span key="a" className="flex flex-wrap items-center gap-2"><StatusBadge status={s.auth.status} /> {s.auth.authMethod ?? "UNKNOWN"}</span>],
              ["Langganan", s.auth.subscriptionType ?? "UNKNOWN"],
              ["Penyedia API", s.auth.apiProvider ?? "UNKNOWN"],
              ["Akun", s.auth.email ?? "—"],
              ["Kuota Max", <span key="q" className="flex flex-wrap items-center gap-2"><Badge status={s.quota.state} /> <span className="text-xs text-ink-3">{s.quota.note}</span></span>],
              ["Penggunaan tambahan berbayar", <span key="x" className="text-xs">{s.extra_usage.note}</span>],
            ]}
          />
          {[...s.binary.reasons, ...s.auth.reasons].length > 0 && (
            <Notice tone="warn">
              <ul className="list-disc pl-4">
                {[...s.binary.reasons, ...s.auth.reasons].map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </Notice>
          )}
          {s.env_hygiene.status !== "OK" && (
            <Notice tone="warn">
              Variabel ditemukan di lingkungan server: <Mono>{s.env_hygiene.forbidden_present.join(", ")}</Mono>. {s.env_hygiene.note}
            </Notice>
          )}
          {s.bridge_status === "BLOCKED" && (
            <div className="space-y-1">
              <div className="text-sm font-medium">Langkah perbaikan</div>
              <CopyCommand command="claude" why="Jalankan lalu ketik /login dengan akun Max (alur resmi)" />
              <CopyCommand command="claude auth status" why="Harus menunjukkan authMethod claude.ai" />
            </div>
          )}
        </div>
        <div className="space-y-3">
          <div>
            <div className="mb-1 text-sm font-medium">Model kebijakan pengawas</div>
            <ul className="space-y-1 text-sm">
              {s.models.map((m) => (
                <li key={m.route_key} className="flex flex-wrap items-center gap-2">
                  <Mono>{m.alias ?? m.model_id}</Mono> → <Mono>{m.model_id}</Mono>
                  <span className="text-ink-3">teresolusi:</span>
                  {m.resolved_version === "UNKNOWN" ? <StatusBadge status="UNKNOWN" /> : <Mono>{m.resolved_version}</Mono>}
                  {m.resolved_observed_at && <Freshness at={m.resolved_observed_at} label="Teramati" staleAfterSeconds={7 * 86400} />}
                </li>
              ))}
            </ul>
            <p className="mt-1 text-xs text-ink-3">
              Baseline perencanaan: {Object.entries(s.policy.stages).map(([k, v]) => `${k}=${v}`).join(", ")} (
              <StatusBadge status={s.policy.mode} className="align-middle" />). Fallback: {s.policy.fallback}.
            </p>
          </div>
          <div>
            <div className="mb-1 flex flex-wrap items-center gap-2 text-sm font-medium">
              Uji kemampuan terakhir {t ? <StatusBadge status={t.status} /> : <StatusBadge status="NOT_TESTED" />}
              {t?.checked_at && <Freshness at={t.checked_at} staleAfterSeconds={7 * 86400} />}
            </div>
            {t ? (
              <div className="space-y-1 text-sm">
                {t.stale_reason && <Notice tone="warn">Usang: {t.stale_reason}. Jalankan ulang uji.</Notice>}
                <div>
                  Diminta <Mono>{t.model_requested}</Mono>, teresolusi {t.model_resolved ? <Mono>{t.model_resolved}</Mono> : <StatusBadge status="UNKNOWN" />}
                  {t.evidence_id ? <span className="text-ink-3"> · bukti {t.evidence_level}</span> : <span className="text-warn"> · bukti tidak tersimpan</span>}
                </div>
                <ul className="space-y-0.5">
                  {t.steps.map((st) => (
                    <li key={st.id} className="flex flex-wrap items-center gap-2 text-xs">
                      <Badge status={st.status} /> <span className="font-medium">{st.id}</span>
                      <span className="text-ink-3">{st.detail}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-sm text-ink-3">
                Belum pernah diuji. Jalankan <Mono>aeos bridge test</Mono> atau tombol di bawah (admin).
              </p>
            )}
          </div>
          {isAdmin ? (
            <div className="flex flex-wrap items-center gap-2">
              <select className={inputBase + " w-auto"} value={model} onChange={(e) => setModel(e.target.value as "opus" | "fable")} aria-label="Model uji">
                <option value="opus">opus</option>
                <option value="fable">fable</option>
              </select>
              <Button variant="primary" busy={test.isPending} onClick={() => test.mutate(model)}>
                <FlaskConical className="h-4 w-4" aria-hidden /> Uji kemampuan
              </Button>
              <span className="text-xs text-ink-3">Memakai 1 panggilan kecil dari kuota Max.</span>
            </div>
          ) : (
            <p className="text-xs text-ink-3">Uji kemampuan hanya untuk SUPER_ADMIN.</p>
          )}
          {test.error && <ErrorBox error={test.error} />}
          {test.data && (
            <Notice tone={test.data.status === "VERIFIED" ? "info" : "warn"}>
              Hasil uji: {test.data.status}; model teresolusi {test.data.model_resolved ?? "UNKNOWN"}.
            </Notice>
          )}
        </div>
      </div>
    </Card>
  );
}
