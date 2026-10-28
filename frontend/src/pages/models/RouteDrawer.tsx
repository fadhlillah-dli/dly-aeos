import { X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useAuth } from "../../auth/AuthContext";
import { ExplainButtons } from "../../components/ExplainButtons";
import { StatusBadge } from "../../components/StatusBadge";
import { SubscriptionStatusBlock } from "../../components/SubscriptionBlock";
import { Button, ErrorBox, Field, Freshness, KeyValue, Loading, Mono, Notice, inputClass } from "../../components/ui";
import { absoluteTime, shortHash } from "../../lib/format";
import { ActivationDialog } from "./ActivationDialog";
import { useDisableRoute, useRouteDetail, useSetRouteEndpoint } from "./api";
import { CopyCommand } from "../integrations/CopyCommand";
import { RouteStatusBadge, credentialProblem, credentialRemedy, isActiveRoute, isCredentialBlockedRoute } from "./badges";
import { POOL_LABELS, type FreeTierView, type RouteItem } from "./types";

function freeLabel(r: RouteItem) {
  if (r.is_free === "YES" && r.effectively_free) return <StatusBadge status="CANDIDATE" label="Gratis (katalog E1)" />;
  if (r.is_free === "YES") return <StatusBadge status="EXPIRED" label="Masa gratis berakhir" />;
  if (r.is_free === "NO") return <StatusBadge status="DISABLED" label="Berbayar" />;
  return <StatusBadge status="UNKNOWN" />;
}

export { freeLabel };

/** Decision C (ADR-0041 amendment): today's use of the account-wide free-model quota and the WORKER eligibility.
 * Only for a `:free` route (the API sends `free_tier: null` for every other one). The count covers requests this AEOS
 * instance sent; the provider's own 429 stays authoritative. */
export function FreeTierBlock({ tier }: { tier: FreeTierView }) {
  const c = tier.decision_c;
  return (
    <div data-testid="free-tier-block" className="space-y-2 rounded-md border border-line p-3 text-sm">
      <div className="font-medium">Kuota gratis (free tier)</div>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge
          status={tier.state}
          label={tier.state === "LIMIT_REACHED" ? "Batas harian tercapai" : "Di bawah batas"}
          title={tier.reason ?? tier.state}
        />
        <span data-testid="free-tier-usage">
          Hari ini (UTC): <strong>{tier.requests_today_utc}</strong> dari {tier.daily_limit} permintaan · menit terakhir{" "}
          {tier.requests_last_minute} dari {tier.rpm_limit}
        </span>
      </div>
      <div className="text-xs text-ink-3">
        Batas harian dari {tier.daily_basis === "KEY_ENDPOINT" ? "endpoint kunci penyedia" : "pengaturan pemilik"}; {tier.scope}. {tier.counted}.
      </div>
      {tier.reason && <div className="text-xs text-warn">{tier.reason}</div>}
      {c && (
        <div className="space-y-1 border-t border-line pt-2" data-testid="free-tier-decision-c">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">Keputusan C:</span>
            <StatusBadge status={c.status} />
          </div>
          <div className="text-xs text-ink-2">{c.note}</div>
          {c.problems.length > 0 && (
            <ul className="list-disc pl-5 text-xs text-warn">
              {c.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** §37: an identity field that is not recorded shows UNKNOWN, never a guess. */
function cfgText(config: Record<string, unknown> | undefined, key: string): string {
  const v = config?.[key];
  if (v === null || v === undefined || v === "") return "UNKNOWN";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "UNKNOWN";
  if (typeof v === "object") return Object.keys(v as object).length ? JSON.stringify(v) : "UNKNOWN";
  return String(v);
}

/** "NOT_SENT" = the broker sends no level (the provider default applies); a level comes from the route configuration. */
export function reasoningSentText(v: string | null | undefined): string {
  if (!v) return "UNKNOWN";
  if (v === "NOT_SENT") return "tidak dikirim (default provider)";
  return `${v} (dari konfigurasi rute)`;
}

/** ADR-0041 (2026-10-25): the exact upstream request the broker would send - an E1 documentation shape, never a pass. */
function upstreamRequest(r: RouteItem) {
  const u = r.upstream_request;
  if (!u) return r.pool === "SUPERVISOR" ? "Bridge native (bukan broker)" : "UNKNOWN";
  if (u.status === "REFUSED")
    return (
      <span key="ur" className="space-y-1">
        <StatusBadge status="BLOCKED" label="Ditolak broker" />
        {u.code && (
          <span className="block text-xs">
            Kode: <Mono>{u.code}</Mono>
          </span>
        )}
        <span className="block text-xs text-warn">{u.note}</span>
      </span>
    );
  if (u.status !== "READY_SHAPE")
    return (
      <span key="ur" className="space-y-1">
        <StatusBadge status="UNKNOWN" label="Bentuk permintaan tidak diketahui" />
        <span className="block text-xs text-ink-3">{u.note}</span>
      </span>
    );
  return (
    <span key="ur" className="space-y-1">
      <StatusBadge status="NOT-PROVEN" label="Sesuai dokumen provider (E1), belum dibuktikan" />
      <span className="block text-xs">
        POST <Mono>{u.url ?? "UNKNOWN"}</Mono> · model <Mono>{u.model ?? "UNKNOWN"}</Mono>
      </span>
      <span className="block text-xs">
        API <Mono>{u.api ?? "UNKNOWN"}</Mono>
        {u.catalog_model_id && u.catalog_model_id !== u.model && (
          <>
            {" "}
            · ID katalog <Mono>{u.catalog_model_id}</Mono>
          </>
        )}{" "}
        · reasoning: {reasoningSentText(u.reasoning_effort)}
      </span>
      <span className="block text-xs text-ink-3">{u.note}</span>
    </span>
  );
}

function clientRestriction(r: RouteItem) {
  const c = r.client_restriction;
  if (!c) return "Tidak ada batasan klien tercatat";
  return (
    <span key="cr" className="space-y-1">
      {c.status === "OWNER_ACCEPTED" ? (
        <StatusBadge status="REVIEWED" label="Diputuskan pemilik; penerimaan provider NOT-PROVEN" />
      ) : (
        <StatusBadge status="NEEDS_DECISION" label="Perlu keputusan pemilik - tidak dapat diaktifkan" />
      )}
      <span className="block text-xs">“{c.statement ?? "UNKNOWN"}” ({c.source ?? "sumber UNKNOWN"})</span>
      {c.applies_to && (
        <span className="block text-xs">
          Berlaku untuk: <Mono>{c.applies_to}</Mono>
        </span>
      )}
      {c.owner_note && <span className="block text-xs">Catatan pemilik: {c.owner_note}</span>}
      <span className="block text-xs text-ink-3">{c.note}</span>
    </span>
  );
}

/** §48 arena / M6: which evaluation can produce results for this route. A wired path is never a result. */
const EVALUATION_PATH: Record<string, { status: string; label: string }> = {
  EVAL_RUN: { status: "CONFIGURED", label: "EvalRun lewat task pekerja (jalur tersambung, bukan hasil)" },
  EVAL_ONLY: { status: "NOT-PROVEN", label: "Khusus evaluasi: hanya task terikat EvalRun, hasil belum terbukti" },
  PLANNING_BASELINE: { status: "CONFIGURED", label: "Baseline perencanaan (bukan EvalRun pekerja)" },
  NOT_WIRED: { status: "NOT-PROVEN", label: "Evaluasi/scorecard belum tersambung" },
};

export function evaluationPath(r: RouteItem) {
  const e = r.evaluation_path;
  if (!e) return <StatusBadge key="ev" status="UNKNOWN" label="Jalur evaluasi tidak diketahui" />;
  const spec = EVALUATION_PATH[e.status] ?? { status: "UNKNOWN", label: `Jalur evaluasi: ${e.status}` };
  return (
    <span key="ev" className="space-y-1 text-sm" data-testid="evaluation-path">
      <StatusBadge status={spec.status} label={spec.label} title={e.status} />
      <span className="block text-xs text-ink-3">{e.note}</span>
    </span>
  );
}

/** §22/§42: a paid route needs its own BUDGET decision at activation; a free or loopback LOCAL route does not. */
export function budgetRequired(r: RouteItem) {
  if (r.budget_required === undefined || r.budget_required === null)
    return <StatusBadge key="br" status="UNKNOWN" label="Tidak diketahui (server lama): diperlakukan wajib bila tidak gratis" />;
  if (r.pool === "SUPERVISOR" && !r.budget_required)
    return <StatusBadge key="br" status="NOT_APPLICABLE" label="Tidak berlaku (bridge native, bukan broker)" />;
  return r.budget_required ? (
    <StatusBadge key="br" status="NEEDS_DECISION" label="Wajib: keputusan BUDGET terpisah saat aktivasi" />
  ) : (
    <StatusBadge key="br" status="NOT_APPLICABLE" label="Tidak perlu (gratis atau LOKAL loopback)" />
  );
}

/** ADR-0080 amendment (2026-10-26): a subscription route is paid by a flat plan fee (the BUDGET decision) plus allowance per window. */
function billingCell(r: RouteItem) {
  return (
    <div key="bl" className="space-y-2">
      <StatusBadge status="NOT_APPLICABLE" label="Langganan — biaya paket + allowance, bukan per token" />
      {r.budget_note && <p className="text-xs text-ink-3">{r.budget_note}</p>}
      {r.subscription && <SubscriptionStatusBlock s={r.subscription} />}
    </div>
  );
}

/** The value the configuration records now (``mode_params``), shown next to an "empty = unchanged" field. */
function recorded(config: Record<string, unknown> | undefined, key: string): string {
  const mp = config?.mode_params;
  const v = mp && typeof mp === "object" ? (mp as Record<string, unknown>)[key] : undefined;
  return typeof v === "string" && v.trim() ? v : "belum dicatat";
}

/** LOCAL route: the owner points it at a loopback OpenAI-compatible server (a new configuration, new TRIAL). */
function LocalEndpointForm({ route, config }: { route: RouteItem; config?: Record<string, unknown> }) {
  const save = useSetRouteEndpoint(route.route_key);
  const [endpoint, setEndpoint] = useState(route.endpoint ?? "");
  const [model, setModel] = useState("");
  const [why, setWhy] = useState("");
  // §23 LOCAL: the build (quantization, inference backend, hardware) is part of the route's identity; empty = unchanged
  const [quantization, setQuantization] = useState("");
  const [backend, setBackend] = useState("");
  const [hardware, setHardware] = useState("");
  const loopback = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+(\/.*)?$/.test(endpoint.trim());
  return (
    <fieldset className="space-y-2 rounded border border-line p-3">
      <legend className="px-1 text-sm font-medium">Endpoint server lokal (loopback)</legend>
      <p className="text-xs text-ink-3">
        Base OpenAI-compatible server di komputer ini, mis. <Mono>http://127.0.0.1:8080/v1</Mono>; broker menambahkan{" "}
        <Mono>/chat/completions</Mono>. Mengubahnya = konfigurasi baru: keputusan lama tidak berlaku, perlu TRIAL baru.
      </p>
      <Field label="Endpoint loopback">
        <input aria-label="Endpoint loopback" className={inputClass} value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="http://127.0.0.1:8080/v1" />
      </Field>
      <Field label="Nama model di server lokal (opsional)" hint={`Kosong = tidak diubah. Tercatat: ${recorded(config, "upstream_model")}.`}>
        <input aria-label="Nama model di server lokal" className={inputClass} value={model} onChange={(e) => setModel(e.target.value)} />
      </Field>
      <Field label="Kuantisasi (opsional)" hint={`Mis. Q4_K_M; kosong = tidak diubah. Tercatat: ${recorded(config, "quantization")}.`}>
        <input aria-label="Kuantisasi" className={inputClass} value={quantization} onChange={(e) => setQuantization(e.target.value)} />
      </Field>
      <Field label="Backend inferensi (opsional)" hint={`Mis. llama.cpp beserta versinya. Tercatat: ${recorded(config, "inference_backend")}.`}>
        <input aria-label="Backend inferensi" className={inputClass} value={backend} onChange={(e) => setBackend(e.target.value)} />
      </Field>
      <Field label="Perangkat keras (opsional)" hint={`Tercatat: ${recorded(config, "hardware")}.`}>
        <input aria-label="Perangkat keras" className={inputClass} value={hardware} onChange={(e) => setHardware(e.target.value)} />
      </Field>
      <Field label="Alasan perubahan" hint="Minimal 10 karakter; tersimpan di audit.">
        <input aria-label="Alasan perubahan endpoint" className={inputClass} value={why} onChange={(e) => setWhy(e.target.value)} />
      </Field>
      {endpoint.trim() && !loopback && <p className="text-xs text-warn">Hanya http pada 127.0.0.1, localhost atau [::1] dengan port.</p>}
      <Button
        disabled={!loopback || why.trim().length < 10}
        busy={save.isPending}
        onClick={() =>
          save.mutate({
            endpoint: endpoint.trim(),
            rationale: why.trim(),
            upstream_model: model.trim() || null,
            quantization: quantization.trim() || null,
            inference_backend: backend.trim() || null,
            hardware: hardware.trim() || null,
          })
        }
      >
        Tetapkan endpoint
      </Button>
      <div aria-live="polite" className="space-y-2">
        {save.data &&
          (save.data.needs_new_trial ? (
            <Notice tone="warn">Konfigurasi berubah: aktifkan TRIAL baru sebelum probe.</Notice>
          ) : save.data.config_changed ? (
            <Notice tone="info">Endpoint tersimpan sebagai konfigurasi baru (hash baru); aktifkan TRIAL untuk konfigurasi ini sebelum probe.</Notice>
          ) : (
            <Notice tone="info">Tidak ada perubahan: endpoint dan build sama dengan yang tercatat.</Notice>
          ))}
      </div>
      {save.error && <ErrorBox error={save.error} />}
    </fieldset>
  );
}

/** The credential block as stored-or-not metadata (never a value): which reference, for which domain. */
function credentialCell(r: RouteItem): ReactNode {
  const c = r.credential;
  if (!c) return <StatusBadge status="UNKNOWN" label="Tidak dilaporkan server" />;
  const label: Record<string, [string, string]> = {
    PRESENT: ["CONFIGURED", "Tersimpan"],
    NOT_NEEDED: ["NOT_APPLICABLE", "Tidak perlu kunci"],
    MISSING: ["MISSING", "Belum tersedia"],
    UNAVAILABLE: ["UNKNOWN", "Tidak dapat diperiksa (vault terkunci/tidak terjangkau)"],
    NOT_ELIGIBLE_IN_DOMAIN: ["NOT_APPLICABLE", "Tidak diperiksa: rute tidak berlaku di domain sesi ini"],
    WITHHELD: ["UNKNOWN", "Disembunyikan: referensi proyek di luar cakupan Anda"],
  };
  const [status, text] = label[c.state] ?? ["UNKNOWN", c.state];
  return (
    <span key="cred" className="inline-flex flex-wrap items-center gap-1">
      <StatusBadge status={status} label={text} />
      {c.ref && <Mono>{c.ref}</Mono>}
      <span className="text-xs text-ink-3">domain {c.domain}</span>
    </span>
  );
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
              <RouteStatusBadge route={r} />
              {isActiveRoute(r) && r.active_decision?.expires_at && (
                <span className="self-center text-xs text-ink-2" data-testid="route-valid-until">
                  s/d {absoluteTime(r.active_decision.expires_at)}
                </span>
              )}
              <StatusBadge status={r.mode} />
              {freeLabel(r)}
              <StatusBadge status={r.identity_status === "UNVERIFIED" ? "UNVERIFIED" : "CANDIDATE"} label={`Identitas: ${r.identity_status}`} />
              <StatusBadge status={r.availability === "MISSING" ? "MISSING" : r.availability === "AVAILABLE" ? "CONNECTED" : "UNKNOWN"} label={`Katalog: ${r.availability}`} />
            </div>
            {isCredentialBlockedRoute(r) && (
              <div data-testid="route-credential-remedy">
                <Notice tone="warn">
                  Aktif, tidak dapat dipakai: {credentialProblem(r)}. Router menolak rute ini sampai kuncinya tersedia
                  {r.credential?.reason ? ` (${r.credential.reason})` : ""}.
                  {credentialRemedy(r) && (
                    <div className="mt-2">
                      <CopyCommand
                        command={credentialRemedy(r) as string}
                        why={r.credential?.state === "UNAVAILABLE" ? "Buka vault, lalu muat ulang" : "Simpan kunci (nilai diminta tersembunyi)"}
                      />
                    </div>
                  )}
                </Notice>
              </div>
            )}
            {r.mode === "DESIGN" && (
              <Notice tone="mock">Entri desain dari master §23 (E1) — bukan bukti rute terpasang, terhubung, teruji atau dipakai.</Notice>
            )}
            <KeyValue
              items={[
                ["Pool", POOL_LABELS[r.pool] ?? r.pool],
                ["Penyedia / endpoint", <span key="p">{r.provider} {r.endpoint ? <Mono>{r.endpoint}</Mono> : null}</span>],
                ["Model ID persis", <Mono key="m">{r.model_id}</Mono>],
                ["Permintaan upstream", upstreamRequest(r)],
                ["Syarat klien penyedia", clientRestriction(r)],
                ["Kunci (kredensial)", credentialCell(r)],
                ["Keputusan BUDGET", budgetRequired(r)],
                ...(r.billing === "SUBSCRIPTION" ? ([["Penagihan", billingCell(r)]] as [ReactNode, ReactNode][]) : []),
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
                [
                  "Evaluasi",
                  r.eval_summary.state === "DIUJI" ? (
                    Object.keys(r.eval_summary.roles).join(", ")
                  ) : r.eval_summary.state === "NEEDS_EVALUATION" ? (
                    <span key="ne" className="space-y-1">
                      <StatusBadge status="STALE" label="Perlu evaluasi ulang" />
                      <span className="block text-xs text-ink-3">{r.eval_summary.note}</span>
                    </span>
                  ) : (
                    <StatusBadge status="NOT_TESTED" label="BELUM DIUJI" />
                  ),
                ],
                [
                  "Binding runtime",
                  !r.runtime_binding ? (
                    "UNKNOWN"
                  ) : r.runtime_binding.status === "DRIFT" ? (
                    <span key="rb" className="space-y-1">
                      <StatusBadge status="STALE" label="Berubah sejak aktivasi" />
                      <span className="block text-xs text-warn">{r.runtime_binding.drift.join("; ")}</span>
                    </span>
                  ) : r.runtime_binding.status === "BOUND" ? (
                    <StatusBadge key="rb" status="CONFIGURED" label="Terikat ke policy/prompt/strategi konteks saat aktivasi" />
                  ) : (
                    <StatusBadge key="rb" status="NOT_TESTED" label="Belum diaktifkan" />
                  ),
                ],
                ["Jalur evaluasi", evaluationPath(r)],
                ["Jalur eksekusi", r.execution_path ? <span key="ep" className="text-sm">{r.execution_path.status === "NOT_WIRED" ? <StatusBadge status="NOT-PROVEN" label="Belum tersambung" /> : r.execution_path.status === "FEASIBILITY_PROBE_ONLY" ? <StatusBadge status="NOT-PROVEN" label="Hanya probe kelayakan (bukan task worker)" /> : r.execution_path.status} <span className="block text-xs text-ink-3">{r.execution_path.note}</span></span> : "UNKNOWN"],
                ["Rollback", r.active_decision?.rollback_target ? (r.active_decision.rollback_target.action === "DISABLE_ROUTE" ? "Nonaktifkan rute (belum ada konfigurasi lain yang pernah disetujui)" : <Mono key="rt">{r.active_decision.rollback_target.config_hash}</Mono>) : "—"],
                ["Terakhir dipakai", r.last_used?.at ? absoluteTime(r.last_used.at) : <StatusBadge status="NEVER_OBSERVED" />],
                ["Sumber", r.source_refs.join(", ")],
              ]}
            />
            {r.free_tier && <FreeTierBlock tier={r.free_tier} />}
            <ExplainButtons
              content={{
                what: (
                  <p>
                    {`Model ${r.model_id} lewat ${r.provider} (pool ${POOL_LABELS[r.pool] ?? r.pool}; harness ${r.harness ?? "UNKNOWN"}).`}
                    {r.notes ? ` ${r.notes}` : ""}
                  </p>
                ),
                why: (
                  <p>
                    {`Asal record: ${r.origin} (bukti ${r.evidence_level}; sumber ${r.source_refs.join(", ") || "tidak tercatat"}). ` +
                      `Status ${r.status}; domain yang memenuhi syarat: ${r.eligible_domains.join(", ") || "tidak ada"}; evaluasi: ${r.eval_summary.state}. `}
                    {r.decisions[0]?.rationale
                      ? `Alasan keputusan terakhir (${r.decisions[0].kind} ${r.decisions[0].status}): ${r.decisions[0].rationale}`
                      : "Belum ada keputusan pemilik: pemilihan produksi hanya setelah evaluasi dan keputusan manusia."}
                  </p>
                ),
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
                impact: (
                  <p>
                    {`Mengubah model/alias/harga/terms membuat hash konfigurasi baru (sekarang ${shortHash(r.config_hash)}): ` +
                      (r.active_decision ? `aktivasi ${r.active_decision.kind} ${r.active_decision.id} tidak berlaku lagi` : "belum ada aktivasi yang terdampak") +
                      `; ${Object.keys(r.eval_summary.roles).length} hasil evaluasi peran tidak diwariskan` +
                      (r.fallback_chain.length ? `; rantai fallback terdampak: ${r.fallback_chain.join(" → ")}` : "; tidak ada rantai fallback") +
                      "."}
                  </p>
                ),
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
            {isAdmin && r.pool === "LOCAL" && r.provider === "local" && <LocalEndpointForm route={r} config={r.config} />}
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
