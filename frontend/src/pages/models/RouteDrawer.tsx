import { X } from "lucide-react";
import { useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import { ExplainButtons } from "../../components/ExplainButtons";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, inputClass } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { ActivationDialog } from "./ActivationDialog";
import { useDisableRoute, useRouteDetail } from "./api";
import { POOL_LABELS, type RouteItem } from "./types";

function freeLabel(r: RouteItem) {
  if (r.is_free === "YES" && r.effectively_free) return <StatusBadge status="CANDIDATE" label="Gratis (katalog E1)" />;
  if (r.is_free === "YES") return <StatusBadge status="EXPIRED" label="Masa gratis berakhir" />;
  if (r.is_free === "NO") return <StatusBadge status="DISABLED" label="Berbayar" />;
  return <StatusBadge status="UNKNOWN" />;
}

export { freeLabel };

/** §37: an identity field that is not recorded shows UNKNOWN, never a guess. */
function cfgText(config: Record<string, unknown> | undefined, key: string): string {
  const v = config?.[key];
  if (v === null || v === undefined || v === "") return "UNKNOWN";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "UNKNOWN";
  if (typeof v === "object") return Object.keys(v as object).length ? JSON.stringify(v) : "UNKNOWN";
  return String(v);
}

export function RouteDrawer({ routeKey, onClose }: { routeKey: string; onClose: () => void }) {
  const { isAdmin } = useAuth();
  const q = useRouteDetail(routeKey);
  const disable = useDisableRoute(routeKey);
  const [activating, setActivating] = useState(false);
  const [reason, setReason] = useState("");
  const r = q.data;

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <aside
        role="dialog"
        aria-label={`Detail rute ${routeKey}`}
        className="h-full w-full max-w-xl overflow-y-auto border-l border-line bg-surface p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="text-base font-semibold">{r?.display_name || routeKey}</h2>
            <Mono>{routeKey}</Mono>
          </div>
          <button type="button" onClick={onClose} aria-label="Tutup" className="rounded p-1 hover:bg-surface-3">
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        {q.isLoading && <Loading />}
        {q.error && <ErrorBox error={q.error} />}
        {r && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <StatusBadge status={r.status} />
              <StatusBadge status={r.mode} />
              {freeLabel(r)}
              <StatusBadge status={r.identity_status === "UNVERIFIED" ? "UNVERIFIED" : "CANDIDATE"} label={`Identitas: ${r.identity_status}`} />
              <StatusBadge status={r.availability === "MISSING" ? "MISSING" : r.availability === "AVAILABLE" ? "CONNECTED" : "UNKNOWN"} label={`Katalog: ${r.availability}`} />
            </div>
            {r.mode === "DESIGN" && (
              <Notice tone="mock">Entri desain dari master §23 (E1) — bukan bukti rute terpasang, terhubung, teruji atau dipakai.</Notice>
            )}
            <KeyValue
              items={[
                ["Pool", POOL_LABELS[r.pool] ?? r.pool],
                ["Penyedia / endpoint", <span key="p">{r.provider} {r.endpoint ? <Mono>{r.endpoint}</Mono> : null}</span>],
                ["Model ID persis", <Mono key="m">{r.model_id}</Mono>],
                ["Versi teresolusi", r.resolved_version === "UNKNOWN" ? <StatusBadge status="UNKNOWN" /> : <Mono>{r.resolved_version}</Mono>],
                ["Hash konfigurasi", <Mono key="h">{r.config_hash}</Mono>],
                ["Konfigurasi berubah", r.config_changed_at ? absoluteTime(r.config_changed_at) : "—"],
                ["Rantai fallback", r.fallback_chain.length ? r.fallback_chain.map((k) => <Mono key={k}>{k}</Mono>) : "Tidak ada (antre/tunggu)"],
                ["Domain", r.eligible_domains.join(", ") || "—"],
                ["Kelas data", r.allowed_data_classes.join(", ") || "—"],
                ["Konteks / alat", `${r.context_window ?? "UNKNOWN"} token · tools ${r.supports_tools}`],
                ["Harness", `${cfgText(r.config, "harness")} · versi ${cfgText(r.config, "harness_version")}`],
                ["Revisi prompt / policy", `${cfgText(r.config, "prompt_revision")} · ${cfgText(r.config, "policy_version")}`],
                ["Strategi konteks", cfgText(r.config, "context_strategy")],
                ["Parameter mode", cfgText(r.config, "mode_params")],
                ["Set alat", cfgText(r.config, "tool_set")],
                ["Region / residensi", cfgText(r.config, "region")],
                ["Harga", <Freshness key="pc" at={r.pricing_checked_at} label="Harga diperiksa" staleAfterSeconds={30 * 86400} />],
                ["Data terms", <Freshness key="tc" at={r.terms_checked_at} label="Terms diperiksa" staleAfterSeconds={30 * 86400} />],
                ["Gratis sampai", r.expires_note ?? (r.free_until ? absoluteTime(r.free_until) : "—")],
                ["Kuota", <StatusBadge key="q" status={r.quota.state === "EXHAUSTED" ? "BLOCKED" : "UNKNOWN"} label={r.quota.state === "EXHAUSTED" ? "Kuota habis - antre" : "Sisa kuota tidak diketahui"} />],
                ["Evaluasi", r.eval_summary.state === "DIUJI" ? Object.keys(r.eval_summary.roles).join(", ") : <StatusBadge status="NOT_TESTED" label="BELUM DIUJI" />],
                ["Terakhir dipakai", r.last_used?.at ? absoluteTime(r.last_used.at) : <StatusBadge status="NEVER_OBSERVED" />],
                ["Sumber", r.source_refs.join(", ")],
              ]}
            />
            <ExplainButtons
              content={{
                what: <p>{r.notes || "Rute model pekerja yang tercatat di registry."}</p>,
                why: <p>Kandidat dari master §23 / katalog ({r.evidence_level}); pemilihan produksi hanya setelah evaluasi dan keputusan manusia.{r.decisions[0]?.rationale ? ` Alasan keputusan terakhir: ${r.decisions[0].rationale}` : ""}</p>,
                usedBy: (
                  <p>
                    {r.last_used?.at
                      ? `Terakhir dipakai ${absoluteTime(r.last_used.at)} oleh run ${r.last_used.run_id ?? "UNKNOWN"} (${r.last_used.status}).`
                      : "Belum pernah dipakai oleh run mana pun (tercatat di usage ledger)."}
                  </p>
                ),
                propose: (
                  <p>
                    <a className="text-accent underline" href={`/proposals?kind=REPLACE&source=${encodeURIComponent(r.route_key)}`}>
                      Usulkan perubahan untuk rute ini
                    </a>{" "}
                    (usulan tidak mengaktifkan apa pun).
                  </p>
                ),
                evidence: (
                  <pre className="mono whitespace-pre-wrap text-xs">{JSON.stringify({ pricing: r.pricing, data_terms: r.data_terms }, null, 2)}</pre>
                ),
                impact: <p>Mengubah model/alias/harga/terms membuat hash konfigurasi baru: aktivasi lama tidak berlaku dan hasil evaluasi lama tidak diwariskan.</p>,
              }}
            />
            <div>
              <div className="mb-1 text-sm font-medium">Keputusan terkait</div>
              {r.decisions.length === 0 ? (
                <p className="text-sm text-ink-3">Belum ada keputusan. Rute tidak dapat dipakai sampai pemilik mengaktifkannya.</p>
              ) : (
                <ul className="space-y-1 text-sm">
                  {r.decisions.map((d) => (
                    <li key={d.id} className="flex flex-wrap items-center gap-2">
                      <StatusBadge status={d.effective ? "APPROVED" : d.status === "ACTIVE" ? "STALE" : "DISABLED"} label={`${d.kind} ${d.status}${d.effective ? "" : " (tidak berlaku)"}`} />
                      {!d.matches_current_config && <span className="text-xs text-warn">hash lama</span>}
                      {d.matches_current_config && !d.effective && d.status === "ACTIVE" && (
                        <span className="text-xs text-warn">{d.actor_kind !== "HUMAN" ? "bukan keputusan manusia" : "kedaluwarsa"}</span>
                      )}
                      <span className="text-xs text-ink-3">{absoluteTime(d.created_at)}</span>
                      {d.expires_at && <span className="text-xs text-ink-3">s.d. {absoluteTime(d.expires_at)}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
            {isAdmin && r.pool !== "SUPERVISOR" && (
              <div className="space-y-2 border-t border-line pt-3">
                <Button variant="primary" onClick={() => setActivating(true)} disabled={r.mode === "MOCK" || r.mode === "DEMO"}>
                  Aktifkan rute…
                </Button>
                {r.status !== "DISABLED" && (
                  <div className="flex flex-wrap items-center gap-2">
                    <input className={inputClass + " max-w-xs"} placeholder="Alasan menonaktifkan" value={reason} onChange={(e) => setReason(e.target.value)} />
                    <Button variant="danger" disabled={reason.trim().length < 3} busy={disable.isPending} onClick={() => disable.mutate(reason.trim())}>
                      Nonaktifkan
                    </Button>
                  </div>
                )}
                {disable.error && <ErrorBox error={disable.error} />}
              </div>
            )}
            {r.pool === "SUPERVISOR" && (
              <Notice tone="info">Rute pengawas diatur oleh kebijakan perencanaan dan bridge native; tidak diaktifkan sebagai rute pekerja.</Notice>
            )}
          </div>
        )}
        {activating && r && <ActivationDialog route={r} onClose={() => setActivating(false)} />}
      </aside>
    </div>
  );
}
