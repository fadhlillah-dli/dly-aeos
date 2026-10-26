import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Coins, Gauge, HelpCircle, Wallet } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { BUDGET_STATE, COST_KIND, MappedBadge, QUOTA_STATE } from "../../components/proposals/MappedBadge";
import { decisionHref } from "../../components/proposals/PageLinks";
import { Link } from "react-router-dom";
import { StatusBadge } from "../../components/StatusBadge";
import { SubscriptionStatusBlock, type SubscriptionStatus } from "../../components/SubscriptionBlock";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputBase, inputClass, KeyValue, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { absoluteTime, shortHash, usd } from "../../lib/format";

interface Bucket {
  actual_usd: number | null;
  estimate_usd: number | null;
  unknown_count: number;
  entries: number;
  input_tokens: number;
  output_tokens: number;
  requests: number;
  quantity: Record<string, number>;
}

interface CategoryRow extends Bucket {
  category: string;
  label: string;
  billing: "SUBSCRIPTION" | "METERED";
}

interface DecisionRef {
  id: string;
  kind?: string;
  actor_user_id?: string;
  status: string;
  created_at?: string | null;
  rationale?: string;
}

export interface BudgetView {
  id: string | null;
  scope_type: string;
  scope_id: string;
  period: string;
  limit_usd: number | null;
  limit_tokens: number | null;
  limit_requests: number | null;
  hard_stop: boolean;
  status: string;
  is_default: boolean;
  implicit: boolean;
  state: string;
  trust_reasons: string[];
  decision: DecisionRef | null;
  spent_usd: number | null;
  held_usd: number | null;
  headroom_usd: number | null;
  spend_hidden?: boolean;
  spend_note?: string;
  notes: string;
}

interface Summary {
  source: string;
  checked_at: string;
  window: { period: string; start: string | null };
  totals: { actual_usd: number; estimate_usd: number; unknown_count: number; entries: number; note: string };
  by_category: CategoryRow[];
  by_project: (Bucket & { project_id: string | null; project_name: string })[];
  by_route: (Bucket & { route_key: string; billing?: "SUBSCRIPTION" | "FREE" | "METERED" | "UNKNOWN"; plan?: string | null; allowance_usd?: number })[];
  /** ADR-0080 amendment: flat-fee worker routes as plan + allowance per window (absent on older servers) */
  subscriptions?: SubscriptionStatus[];
  native_subscription: {
    billing: string;
    dollar_cost: null;
    dollar_note: string;
    remaining_quota: string;
    quota_state: string;
    quota_source: string;
    quota_observed_at: string | null;
    requests: number;
    input_tokens: number;
    output_tokens: number;
    entries: number;
    note: string;
  };
  stop_caps: BudgetView[];
  per_accepted_outcome: {
    accepted_outcomes: number;
    cost_per_accepted_usd: number | null;
    status: string;
    basis: string;
    note?: string;
    total_known_usd: number;
    unknown_entries: number;
    per_task: { task_id: string; cost_usd: number | null; known_usd: number; unknown_entries: number; entries: number }[];
  };
  excluded_non_live: { entries: number; modes: string[]; note: string };
  /** §42: latency and repair count monitored separately from money and tokens (absent on older servers). */
  operations?: {
    latency: { p50_s: number | null; p95_s: number | null; samples: number; runs: number; basis: string; status: string };
    repairs: { repair_attempts: number; tasks: number; tasks_repaired: number; basis: string };
    note: string;
  };
}

interface LedgerRow {
  id: string;
  at: string;
  category: string;
  provider: string;
  model: string | null;
  route_key: string | null;
  project_id: string | null;
  run_id: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  requests: number;
  cost_usd: number | null;
  cost_kind: string;
  charged_usd: number | null;
  quantity: number | null;
  unit: string | null;
  source: string;
  mode: string;
}

interface QuotaRow {
  id: string | null;
  provider: string;
  scope: string;
  state: string;
  source: string;
  observed_at: string | null;
  resets_at: string | null;
  official: boolean;
  implicit: boolean;
}

interface NativeEstimate {
  status: string;
  available: boolean;
  reason?: string;
  remedy?: string;
  authority: string;
  dollar_figures: string;
  totals?: Record<string, number>;
  days?: { date: string; total_tokens?: number; input_tokens?: number; output_tokens?: number; models: string[] }[];
}

const PERIODS = [
  ["DAY", "Hari ini"],
  ["WEEK", "Minggu ini"],
  ["MONTH", "Bulan ini"],
  ["TOTAL", "Sepanjang waktu"],
] as const;

function Stat({ icon, label, value, hint, badge }: { icon: ReactNode; label: string; value: ReactNode; hint?: string; badge?: ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="flex items-center gap-2 text-xs text-ink-3">
        {icon}
        {label}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-xl font-semibold">
        {value}
        {badge}
      </div>
      {hint && <div className="mt-1 text-xs text-ink-3">{hint}</div>}
    </div>
  );
}

function scopeLabel(b: BudgetView) {
  const scope = b.scope_type === "GLOBAL" ? "Global" : `${b.scope_type} ${b.scope_id}`;
  const per = { DAY: "per hari", WEEK: "per minggu", MONTH: "per bulan", TOTAL: "total" }[b.period] ?? b.period;
  return `${scope} · ${per}`;
}

function limitText(b: BudgetView) {
  const parts = [];
  if (b.limit_usd !== null) parts.push(usd(b.limit_usd));
  if (b.limit_tokens !== null) parts.push(`${b.limit_tokens.toLocaleString("id-ID")} token`);
  if (b.limit_requests !== null) parts.push(`${b.limit_requests} request`);
  return parts.join(" · ") || "—";
}

function BudgetForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [scopeType, setScopeType] = useState("GLOBAL");
  const [scopeId, setScopeId] = useState("");
  const [period, setPeriod] = useState("MONTH");
  const [limitUsd, setLimitUsd] = useState("");
  const [limitTokens, setLimitTokens] = useState("");
  const [limitRequests, setLimitRequests] = useState("");
  const [hardStop, setHardStop] = useState(true);
  const [rationale, setRationale] = useState("");
  const m = useMutation({
    mutationFn: () =>
      post<BudgetView>("/api/budgets", {
        scope_type: scopeType,
        scope_id: scopeType === "GLOBAL" ? "*" : scopeId,
        period,
        limit_usd: limitUsd === "" ? null : Number(limitUsd),
        limit_tokens: limitTokens === "" ? null : Number(limitTokens),
        limit_requests: limitRequests === "" ? null : Number(limitRequests),
        hard_stop: hardStop,
        rationale,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["costs"] });
      qc.invalidateQueries({ queryKey: ["budgets"] });
      onDone();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate();
  };
  return (
    <form onSubmit={submit} className="space-y-3">
      <Notice tone="warn">
        Menyimpan anggaran mencatat <b>keputusan BUDGET</b> pemilik yang terikat hash ketentuan anggaran. Tidak ada auto-topup, paid
        fallback atau rotasi akun/key; anggaran lama ditandai SUPERSEDED, tidak dihapus.
      </Notice>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Cakupan">
          <select className={inputClass} value={scopeType} onChange={(e) => setScopeType(e.target.value)}>
            <option value="GLOBAL">GLOBAL</option>
            <option value="PROJECT">PROJECT</option>
            <option value="ROUTE">ROUTE</option>
            <option value="RUN">RUN</option>
          </select>
        </Field>
        {scopeType !== "GLOBAL" && (
          <Field label="ID cakupan" hint="ID proyek / route_key / run ID">
            <input className={inputClass} value={scopeId} onChange={(e) => setScopeId(e.target.value)} required />
          </Field>
        )}
        <Field label="Periode">
          <select className={inputClass} value={period} onChange={(e) => setPeriod(e.target.value)}>
            {PERIODS.map(([k, l]) => (
              <option key={k} value={k}>
                {l}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Batas USD" hint="Wajib untuk GLOBAL; 0 = hanya rute gratis">
          <input className={inputClass} type="number" min={0} step="0.01" value={limitUsd} onChange={(e) => setLimitUsd(e.target.value)} />
        </Field>
        <Field label="Batas token">
          <input className={inputClass} type="number" min={0} value={limitTokens} onChange={(e) => setLimitTokens(e.target.value)} />
        </Field>
        <Field label="Batas request">
          <input className={inputClass} type="number" min={0} value={limitRequests} onChange={(e) => setLimitRequests(e.target.value)} />
        </Field>
        <label className="flex items-center gap-2 pt-6 text-sm">
          <input type="checkbox" checked={hardStop} onChange={(e) => setHardStop(e.target.checked)} /> Hard stop (tolak admission saat terlampaui)
        </label>
      </div>
      <Field label="Alasan keputusan" hint="Tercatat pada Decision dan audit">
        <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} required minLength={3} />
      </Field>
      <ErrorBox error={m.error} />
      <div className="flex gap-2">
        <Button variant="primary" type="submit" busy={m.isPending}>
          Catat keputusan BUDGET
        </Button>
        <Button type="button" onClick={onDone}>
          Batal
        </Button>
      </div>
    </form>
  );
}

function NativeEstimator() {
  const m = useMutation({ mutationFn: () => post<NativeEstimate>("/api/costs/native-estimate") });
  const d = m.data;
  return (
    <div className="space-y-2">
      <Button onClick={() => m.mutate()} busy={m.isPending}>
        Jalankan estimator ccusage (ESTIMATE)
      </Button>
      <ErrorBox error={m.error} />
      {d && (
        <div className="space-y-1 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={d.status === "ESTIMATE" ? "ESTIMATE" : d.status === "NOT_AVAILABLE" ? "MISSING" : "FAILED"} label={d.status} />
            <span className="text-ink-3">{d.authority}</span>
          </div>
          {d.reason && <div className="text-ink-2">{d.reason}</div>}
          {d.remedy && <Mono>{d.remedy}</Mono>}
          {d.totals && (
            <KeyValue
              items={Object.entries(d.totals).map(([k, v]) => [k.replaceAll("_", " "), v.toLocaleString("id-ID")] as [ReactNode, ReactNode])}
            />
          )}
          <div className="text-xs text-ink-3">{d.dollar_figures}</div>
        </div>
      )}
    </div>
  );
}

export default function CostsPage() {
  const { isAdmin } = useAuth();
  const [period, setPeriod] = useState("MONTH");
  const [editing, setEditing] = useState(false);
  const summary = useQuery({ queryKey: ["costs", "summary", period], queryFn: () => get<Summary>(`/api/costs/summary?period=${period}`) });
  const budgets = useQuery({ queryKey: ["budgets", "all"], queryFn: () => get<{ items: BudgetView[] }>("/api/budgets?include_superseded=true") });
  const ledger = useQuery({ queryKey: ["costs", "ledger"], queryFn: () => get<{ items: LedgerRow[]; checked_at: string }>("/api/costs/ledger?limit=100") });
  const quotas = useQuery({ queryKey: ["costs", "quotas"], queryFn: () => get<{ items: QuotaRow[]; note: string }>("/api/quotas") });
  const s = summary.data;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Biaya & Kuota"
        question="Biaya apa yang terpakai, diperkirakan dan belum diketahui?"
        source={s?.source ?? "usage_ledger, budgets, quota_states"}
        checkedAt={s?.checked_at ?? null}
        evidenceLimit="Angka dari ledger/provider; aktual, estimasi dan UNKNOWN tidak dijumlahkan; langganan Max tidak dikonversi ke dolar"
        relatedDecisionsHref="/decisions?target_type=budget"
        actions={
          <>
            <select aria-label="Periode" className={inputBase + " w-full sm:w-44"} value={period} onChange={(e) => setPeriod(e.target.value)}>
              {PERIODS.map(([k, l]) => (
                <option key={k} value={k}>
                  {l}
                </option>
              ))}
            </select>
          </>
        }
      />
      {summary.isLoading && <Loading />}
      <ErrorBox error={summary.error} />
      {s && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Stat icon={<Coins className="h-4 w-4" aria-hidden />} label="Aktual (tagihan provider)" value={usd(s.totals.actual_usd)} badge={<StatusBadge status="LIVE" label="Aktual" />} hint={`${s.totals.entries} entri ledger LIVE`} />
            <Stat icon={<Gauge className="h-4 w-4" aria-hidden />} label="Estimasi" value={usd(s.totals.estimate_usd)} badge={<StatusBadge status="ESTIMATE" />} hint="Dihitung kode dari pricing; bukan tagihan" />
            <Stat icon={<HelpCircle className="h-4 w-4" aria-hidden />} label="Biaya UNKNOWN" value={`${s.totals.unknown_count} entri`} badge={<StatusBadge status="UNKNOWN" />} hint="Tidak ditebak; dibebankan ke budget sebesar reservasi" />
            <Stat
              icon={<Wallet className="h-4 w-4" aria-hidden />}
              label="Biaya per accepted outcome"
              value={s.per_accepted_outcome.cost_per_accepted_usd === null ? "—" : usd(s.per_accepted_outcome.cost_per_accepted_usd)}
              badge={<StatusBadge status={s.per_accepted_outcome.status === "COMPUTED" ? "LIVE" : "UNKNOWN"} label={s.per_accepted_outcome.status === "COMPUTED" ? "Terhitung" : s.per_accepted_outcome.status === "NO_ACCEPTED_OUTCOMES" ? "Belum ada" : "UNKNOWN"} />}
              hint={`${s.per_accepted_outcome.accepted_outcomes} accepted outcome · ${s.per_accepted_outcome.basis}`}
            />
          </div>
          <Notice>{s.totals.note}</Notice>
          {s.excluded_non_live.entries > 0 && (
            <Notice tone="mock">
              {s.excluded_non_live.entries} entri {s.excluded_non_live.modes.join("/")} dikecualikan. {s.excluded_non_live.note}
            </Notice>
          )}
          {s.operations && (
            <Card title="Latency dan repair pekerja">
              <KeyValue
                items={[
                  [
                    "Latency run (p50 / p95)",
                    s.operations.latency.samples === 0 ? (
                      <StatusBadge key="l" status="UNKNOWN" label="Belum ada run selesai" />
                    ) : (
                      `${s.operations.latency.p50_s ?? "—"} s / ${s.operations.latency.p95_s ?? "—"} s (n=${s.operations.latency.samples} dari ${s.operations.latency.runs} run)`
                    ),
                  ],
                  ["Attempt repair", `${s.operations.repairs.repair_attempts} (${s.operations.repairs.tasks_repaired} dari ${s.operations.repairs.tasks} task)`],
                  ["Dasar", `${s.operations.latency.basis}; ${s.operations.repairs.basis}`],
                ]}
              />
              <p className="mt-2 text-xs text-ink-3">{s.operations.note}</p>
            </Card>
          )}

          {s.subscriptions && s.subscriptions.length > 0 && (
            <Card title="Langganan worker (paket + allowance)">
              <div className="space-y-4">
                <Notice>
                  Biaya paket tercatat sebagai keputusan BUDGET; pemakaian dihitung sebagai allowance per jendela provider, bukan biaya per token.
                  Hanya panggilan lewat instance AEOS ini yang terhitung.
                </Notice>
                {s.subscriptions.map((sub) => (
                  <div key={sub.route_key} className="border-t border-line pt-3 first:border-t-0 first:pt-0">
                    <SubscriptionStatusBlock s={sub} showRoute />
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card title="Claude native (langganan Max)">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge status="NOT_APPLICABLE" label="Langganan — bukan dolar" />
                  <MappedBadge value={s.native_subscription.remaining_quota} map={QUOTA_STATE} />
                </div>
                <p className="font-medium">{s.native_subscription.note}</p>
                <p className="text-sm text-ink-2">{s.native_subscription.dollar_note}</p>
              </div>
              <KeyValue
                items={[
                  ["Sumber kuota", s.native_subscription.quota_source],
                  ["Diamati", s.native_subscription.quota_observed_at ? <Freshness at={s.native_subscription.quota_observed_at} /> : "belum pernah"],
                  ["Request tercatat", s.native_subscription.requests],
                  ["Token (dilaporkan bridge)", `${s.native_subscription.input_tokens.toLocaleString("id-ID")} in / ${s.native_subscription.output_tokens.toLocaleString("id-ID")} out`],
                ]}
              />
            </div>
            {isAdmin && (
              <div className="mt-4 border-t border-line pt-3">
                <NativeEstimator />
              </div>
            )}
          </Card>

          <Card title="Rincian per kategori">
            <Table
              rows={s.by_category}
              rowKey={(r) => r.category}
              columns={[
                { header: "Kategori", cell: (r) => <span className="font-medium">{r.label}</span> },
                { header: "Aktual", cell: (r) => (r.billing === "SUBSCRIPTION" ? <StatusBadge status="NOT_APPLICABLE" label="Langganan" /> : usd(r.actual_usd)) },
                { header: "Estimasi", cell: (r) => (r.billing === "SUBSCRIPTION" ? "—" : usd(r.estimate_usd)) },
                { header: "UNKNOWN", cell: (r) => (r.unknown_count ? <StatusBadge status="UNKNOWN" label={`${r.unknown_count} entri`} /> : "0") },
                { header: "Request", cell: (r) => r.requests },
                { header: "Token in/out", cell: (r) => `${r.input_tokens.toLocaleString("id-ID")} / ${r.output_tokens.toLocaleString("id-ID")}` },
                { header: "Kuantitas", cell: (r) => Object.entries(r.quantity).map(([u, v]) => `${v} ${u}`).join(", ") || "—" },
              ]}
            />
          </Card>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card title="Per proyek">
              <Table
                rows={s.by_project}
                rowKey={(r) => r.project_id ?? "platform"}
                empty="Belum ada penggunaan berbayar/terukur pada domain ini"
                columns={[
                  { header: "Proyek", cell: (r) => r.project_name },
                  { header: "Aktual", cell: (r) => usd(r.actual_usd) },
                  { header: "Estimasi", cell: (r) => usd(r.estimate_usd) },
                  { header: "UNKNOWN", cell: (r) => r.unknown_count },
                ]}
              />
            </Card>
            <Card title="Per rute model">
              <Table
                rows={s.by_route}
                rowKey={(r) => r.route_key}
                empty="Belum ada penggunaan rute"
                columns={[
                  { header: "Rute", cell: (r) => <Mono>{r.route_key}</Mono> },
                  { header: "Aktual", cell: (r) => usd(r.actual_usd) },
                  { header: "Estimasi", cell: (r) => usd(r.estimate_usd) },
                  { header: "UNKNOWN", cell: (r) => r.unknown_count },
                ]}
              />
            </Card>
          </div>

          <Card title="Biaya per accepted outcome">
            {s.per_accepted_outcome.per_task.length === 0 ? (
              <EmptyState title={s.per_accepted_outcome.note ?? "Belum ada accepted outcome"}>
                Accepted outcome berasal dari AcceptanceRecord verifier; jalankan pekerjaan lewat workforce dan verifikasi independen.
              </EmptyState>
            ) : (
              <Table
                rows={s.per_accepted_outcome.per_task}
                rowKey={(r) => r.task_id}
                columns={[
                  { header: "Task", cell: (r) => <Mono>{r.task_id}</Mono> },
                  { header: "Biaya (semua attempt)", cell: (r) => (r.cost_usd === null ? <StatusBadge status="UNKNOWN" label={`≥ ${usd(r.known_usd)} + ${r.unknown_entries} UNKNOWN`} /> : usd(r.cost_usd)) },
                  { header: "Entri", cell: (r) => r.entries },
                ]}
              />
            )}
          </Card>
        </>
      )}

      <Card
        title="Anggaran & stop caps"
        actions={
          isAdmin && !editing ? (
            <Button variant="primary" onClick={() => setEditing(true)}>
              Ubah anggaran
            </Button>
          ) : undefined
        }
      >
        {editing && isAdmin && (
          <div className="mb-4 rounded-md border border-line p-3">
            <BudgetForm onDone={() => setEditing(false)} />
          </div>
        )}
        <ErrorBox error={budgets.error} />
        {budgets.data && (
          <Table
            rows={budgets.data.items}
            rowKey={(b) => b.id ?? "implicit-default"}
            columns={[
              {
                header: "Cakupan",
                cell: (b) => (
                  <div>
                    <div className="font-medium">{scopeLabel(b)}</div>
                    {b.is_default && <div className="text-xs text-ink-3">Default {b.implicit ? "(belum dimaterialisasi)" : ""}</div>}
                    {b.status === "SUPERSEDED" && <StatusBadge status="STALE" label="Digantikan" />}
                  </div>
                ),
              },
              { header: "Batas", cell: (b) => <span>{limitText(b)} {b.hard_stop ? "· hard stop" : "· soft (peringatan)"}</span> },
              {
                header: "Terpakai / ditahan",
                cell: (b) => (b.spend_hidden ? <span className="text-xs text-ink-3">{b.spend_note}</span> : `${usd(b.spent_usd)} / ${usd(b.held_usd)}`),
              },
              { header: "Sisa", cell: (b) => (b.spend_hidden ? "—" : usd(b.headroom_usd)) },
              {
                header: "Status",
                cell: (b) => (
                  <div className="space-y-1">
                    <MappedBadge value={b.state} map={BUDGET_STATE} />
                    {b.trust_reasons.map((r) => (
                      <div key={r} className="text-xs text-bad">
                        {r}
                      </div>
                    ))}
                  </div>
                ),
              },
              {
                header: "Keputusan yang menetapkan",
                cell: (b) =>
                  b.decision ? (
                    <div className="text-xs">
                      <Link to={decisionHref(b.decision.id)}>
                        <Mono>{shortHash(b.decision.id, 8)}</Mono>
                      </Link>{" "}
                      {b.decision.kind} oleh {b.decision.actor_user_id}
                      <div className="text-ink-3">{absoluteTime(b.decision.created_at)} — {b.decision.rationale}</div>
                    </div>
                  ) : b.is_default ? (
                    <span className="text-xs text-ink-3">Default $0 (tidak perlu keputusan)</span>
                  ) : (
                    <StatusBadge status="MISSING" label="Tanpa keputusan" />
                  ),
              },
            ]}
          />
        )}
      </Card>

      <Card title="Kuota provider">
        <ErrorBox error={quotas.error} />
        {quotas.data && (
          <>
            <p className="mb-2 text-xs text-ink-3">{quotas.data.note}</p>
            <Table
              rows={quotas.data.items}
              rowKey={(q) => `${q.provider}:${q.scope}`}
              columns={[
                { header: "Provider", cell: (q) => <Mono>{q.provider}</Mono> },
                { header: "Cakupan", cell: (q) => q.scope },
                { header: "Status", cell: (q) => <MappedBadge value={q.state} map={QUOTA_STATE} /> },
                { header: "Sumber", cell: (q) => <span>{q.source}{q.official ? " (resmi)" : ""}</span> },
                { header: "Kesegaran", cell: (q) => (q.observed_at ? <Freshness at={q.observed_at} label="Diamati" /> : <StatusBadge status="NOT_CHECKED" />) },
                { header: "Reset", cell: (q) => absoluteTime(q.resets_at) },
              ]}
            />
          </>
        )}
      </Card>

      <Card title="Ledger penggunaan (100 terbaru)">
        <ErrorBox error={ledger.error} />
        {ledger.data && (
          <Table
            rows={ledger.data.items}
            rowKey={(r) => r.id}
            empty="Ledger kosong — penggunaan tercatat otomatis oleh broker/bridge/collector saat pekerjaan berjalan"
            columns={[
              { header: "Waktu", cell: (r) => absoluteTime(r.at) },
              { header: "Kategori", cell: (r) => r.category },
              { header: "Provider / model", cell: (r) => <span>{r.provider}{r.model ? ` · ${r.model}` : ""}</span> },
              { header: "Token in/out", cell: (r) => `${r.input_tokens ?? "?"} / ${r.output_tokens ?? "?"}` },
              { header: "Biaya", cell: (r) => (r.cost_kind === "SUBSCRIPTION" ? "—" : usd(r.cost_usd)) },
              { header: "Jenis", cell: (r) => <MappedBadge value={r.cost_kind} map={COST_KIND} /> },
              { header: "Sumber", cell: (r) => <span>{r.source}{r.mode !== "LIVE" ? ` · ${r.mode}` : ""}</span> },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
