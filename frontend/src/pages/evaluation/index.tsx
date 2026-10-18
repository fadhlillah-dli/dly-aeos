import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { ELIGIBILITY, MappedBadge } from "../../components/proposals/MappedBadge";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, inputClass, KeyValue, Loading, Mono, Notice, PageHeader, Table, Tabs } from "../../components/ui";
import { absoluteTime, shortHash } from "../../lib/format";

interface Suite {
  id: string;
  suite_key: string;
  title: string;
  role_key: string | null;
  task_class: string;
  version: string;
  content_hash: string;
  held_out: boolean;
  protected: boolean;
  description: string;
  acceptance: string[];
  isolation: string;
  case_count: number;
  case_kinds: string[];
  cases: { case_id: string; kind: string; title: string; expected: string; acceptance: string[] }[];
  integrity: string;
  results: string;
}

interface Experiment {
  key: string;
  name: string;
  compares: string;
  max_parallel_workers: number;
  specialists: string;
  status: string;
  note?: string;
  gates_hash: string;
  promotion_rule: string;
}

interface Dimension {
  key: string;
  label: string;
  explain: string;
  metrics: string[];
}

interface RankEntry {
  configuration_hash: string;
  label: string;
  configuration: Record<string, unknown>;
  topologies: string[];
  runs: number;
  sample_size: number;
  final_acceptance_rate: number | null;
  first_pass_acceptance_rate: number | null;
  acceptance_ci95: [number, number] | null;
  acceptance_variance: number | null;
  failures_reported: number;
  metrics: { performance: { latency_p50_s: number | null; latency_p95_s: number | null }; economics: { worker_cost_per_accepted_usd: number | null; worker_cost_status: string } };
  eligibility: { eligible: boolean; status: string; gates_failed: string[]; gates_unproven: string[] };
  small_sample: boolean;
  rank?: number;
  tie?: boolean;
}

interface Leaderboard {
  role: string | null;
  source: string;
  checked_at: string;
  status: string;
  message: string | null;
  suites: { suite_key: string; title: string; role_key: string | null; ranking: RankEntry[]; excluded: RankEntry[]; status: string }[];
  candidates_not_tested: { label: string; route_key: string | null; status: string; source: string }[];
  combined_note: string | null;
  route_change: string;
  ranking_rule: string;
  dimensions: Dimension[];
}

interface Run {
  id: string;
  suite_key: string;
  role_key: string | null;
  topology: string | null;
  status: string;
  configuration: Record<string, unknown>;
  configuration_hash: string;
  sample_size: number;
  failures: unknown[];
  created_at: string | null;
}

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(0)}%`);

function LeaderboardView({ role }: { role: string }) {
  const { isAdmin } = useAuth();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["eval", "leaderboard", role], queryFn: () => get<Leaderboard>(`/api/eval/leaderboard${role ? `?role=${encodeURIComponent(role)}` : ""}`) });
  const scorecard = useMutation({
    mutationFn: () => post<{ proposal_id: string }>("/api/eval/scorecard", { role }),
    onSuccess: (d) => nav(`/proposals/${d.proposal_id}`),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const b = q.data;
  if (!b) return null;
  return (
    <div className="space-y-4">
      <Notice>{b.ranking_rule}</Notice>
      <Notice tone="warn">{b.route_change}</Notice>
      {b.status === "BELUM_DIUJI" && (
        <EmptyState title={b.message ?? "Semua kandidat BELUM DIUJI dalam platform ini"}>
          Hasil hanya berasal dari EvalRun yang dijalankan workforce dan dicatat verifier independen. Rencanakan run pada tab “Run”.
        </EmptyState>
      )}
      {b.suites
        .filter((s) => s.ranking.length || s.excluded.length)
        .map((s) => (
          <Card key={s.suite_key} title={`${s.title} (${s.suite_key})`} actions={<MappedBadge value={s.status} map={ELIGIBILITY} />}>
            <Table
              rows={s.ranking}
              rowKey={(e) => e.configuration_hash}
              empty="Tidak ada konfigurasi yang lulus gate"
              columns={[
                { header: "Peringkat", cell: (e) => <span className="font-semibold">#{e.rank}{e.tie ? " (seri)" : ""}</span> },
                { header: "Konfigurasi", cell: (e) => <div><div>{e.label}</div><Mono>{shortHash(e.configuration_hash)}</Mono></div> },
                { header: "n", cell: (e) => <span>{e.sample_size}{e.small_sample ? " (kecil)" : ""}</span> },
                { header: "Diterima", cell: (e) => pct(e.final_acceptance_rate) },
                { header: "First-pass", cell: (e) => pct(e.first_pass_acceptance_rate) },
                { header: "CI 95%", cell: (e) => (e.acceptance_ci95 ? `${pct(e.acceptance_ci95[0])}–${pct(e.acceptance_ci95[1])}` : "—") },
                { header: "Varians", cell: (e) => (e.acceptance_variance === null ? "—" : e.acceptance_variance.toFixed(3)) },
                { header: "Gagal/timeout/kuota", cell: (e) => e.failures_reported },
                { header: "p50 / p95", cell: (e) => `${e.metrics.performance.latency_p50_s ?? "—"}s / ${e.metrics.performance.latency_p95_s ?? "—"}s` },
                { header: "Biaya/accepted", cell: (e) => (e.metrics.economics.worker_cost_per_accepted_usd === null ? <StatusBadge status="UNKNOWN" label={e.metrics.economics.worker_cost_status} /> : `$${e.metrics.economics.worker_cost_per_accepted_usd}`) },
              ]}
            />
            {s.excluded.length > 0 && (
              <div className="mt-3">
                <div className="mb-1 text-sm font-medium">Dikecualikan (gate keamanan/authority/bukti — tidak ditebus skor)</div>
                <Table
                  rows={s.excluded}
                  rowKey={(e) => e.configuration_hash}
                  columns={[
                    { header: "Konfigurasi", cell: (e) => e.label },
                    { header: "Alasan", cell: (e) => <MappedBadge value={e.eligibility.status} map={ELIGIBILITY} /> },
                    { header: "Gate", cell: (e) => [...e.eligibility.gates_failed.map((g) => `${g}: FAIL`), ...e.eligibility.gates_unproven.map((g) => `${g}: belum terbukti`)].join(", ") },
                    { header: "n", cell: (e) => e.sample_size },
                  ]}
                />
              </div>
            )}
          </Card>
        ))}
      <Card title="Kandidat yang BELUM DIUJI">
        <Table
          rows={b.candidates_not_tested}
          rowKey={(c) => `${c.source}:${c.route_key ?? c.label}`}
          empty="Belum ada kandidat terdaftar — muat registry model (aeos seed) atau rencanakan run"
          columns={[
            { header: "Kandidat", cell: (c) => c.label },
            { header: "Rute", cell: (c) => <Mono>{c.route_key ?? "—"}</Mono> },
            { header: "Status", cell: () => <MappedBadge value="BELUM_DIUJI" map={ELIGIBILITY} /> },
            { header: "Sumber", cell: (c) => c.source },
          ]}
        />
      </Card>
      {b.combined_note && <p className="text-xs text-ink-3">{b.combined_note}</p>}
      {isAdmin && role && (
        <div>
          <Button onClick={() => scorecard.mutate()} busy={scorecard.isPending}>
            Buka scorecard sebagai usulan keputusan
          </Button>
          <ErrorBox error={scorecard.error} />
        </div>
      )}
    </div>
  );
}

function PlanRunForm({ suites }: { suites: Suite[] }) {
  const qc = useQueryClient();
  const [suite, setSuite] = useState(suites[0]?.suite_key ?? "");
  const [cfg, setCfg] = useState({
    model_id: "",
    model_version: "UNKNOWN",
    provider: "",
    provider_route: "",
    harness: "opencode",
    harness_version: "UNKNOWN",
    prompt_revision: "UNKNOWN",
    tools: "",
    policy_version: "UNKNOWN",
    context_params: "{}",
  });
  const [topology, setTopology] = useState("");
  const [reps, setReps] = useState("3");
  const [limits, setLimits] = useState({ max_attempts: "2", max_minutes: "30", max_cost_usd: "0" });
  const [err, setErr] = useState<string | null>(null);
  const m = useMutation({
    mutationFn: (body: Record<string, unknown>) => post<Run>("/api/eval/runs", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["eval"] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    let ctx: unknown;
    try {
      ctx = JSON.parse(cfg.context_params || "{}");
    } catch {
      setErr("context_params harus JSON");
      return;
    }
    m.mutate({
      suite_key: suite,
      topology: topology || null,
      repetitions: Number(reps),
      configuration: { ...cfg, tools: cfg.tools.split(",").map((t) => t.trim()).filter(Boolean), context_params: ctx },
      limits: { max_attempts: Number(limits.max_attempts), max_minutes: Number(limits.max_minutes), max_cost_usd: Number(limits.max_cost_usd) },
    });
  };
  const fields: [keyof typeof cfg, string][] = [
    ["model_id", "Model ID"],
    ["model_version", "Versi model (UNKNOWN bila alias)"],
    ["provider", "Provider"],
    ["provider_route", "Rute provider"],
    ["harness", "Harness"],
    ["harness_version", "Versi harness"],
    ["prompt_revision", "Revisi prompt"],
    ["tools", "Tools (pisahkan koma)"],
    ["policy_version", "Versi policy"],
    ["context_params", "Parameter konteks (JSON)"],
  ];
  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Rencanakan run evaluasi">
      <Notice>Merencanakan run tidak menjalankan model. Eksekusi lewat workforce (admission, budget, sandbox, verifier); biaya &gt; $0 tetap memerlukan keputusan BUDGET.</Notice>
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="Suite">
          <select className={inputClass} value={suite} onChange={(e) => setSuite(e.target.value)}>
            {suites.map((s) => (
              <option key={s.suite_key} value={s.suite_key}>
                {s.title}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Topologi (§44)">
          <select className={inputClass} value={topology} onChange={(e) => setTopology(e.target.value)}>
            <option value="">—</option>
            <option value="A">A — Sequential</option>
            <option value="B">B — Specialist on-demand</option>
            <option value="C">C — Bounded parallel</option>
            <option value="D">D — Company-role workflow</option>
          </select>
        </Field>
        <Field label="Pengulangan">
          <input className={inputClass} type="number" min={1} max={50} value={reps} onChange={(e) => setReps(e.target.value)} />
        </Field>
        {fields.map(([k, label]) => (
          <Field key={k} label={label}>
            <input className={inputClass} value={cfg[k]} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })} required />
          </Field>
        ))}
        <Field label="Attempt maks">
          <input className={inputClass} type="number" min={0} value={limits.max_attempts} onChange={(e) => setLimits({ ...limits, max_attempts: e.target.value })} />
        </Field>
        <Field label="Waktu maks (menit)">
          <input className={inputClass} type="number" min={0} value={limits.max_minutes} onChange={(e) => setLimits({ ...limits, max_minutes: e.target.value })} />
        </Field>
        <Field label="Biaya maks (USD)">
          <input className={inputClass} type="number" min={0} step="0.01" value={limits.max_cost_usd} onChange={(e) => setLimits({ ...limits, max_cost_usd: e.target.value })} />
        </Field>
      </div>
      {err && <ErrorBox error={new Error(err)} />}
      <ErrorBox error={m.error} />
      {m.data && <Notice>Run {m.data.id.slice(0, 8)} tercatat PLANNED (hash {shortHash(m.data.configuration_hash)}).</Notice>}
      <Button type="submit" variant="primary" busy={m.isPending}>
        Rencanakan run
      </Button>
    </form>
  );
}

export default function EvaluationPage() {
  const { isAdmin } = useAuth();
  const suites = useQuery({ queryKey: ["eval", "suites"], queryFn: () => get<{ items: Suite[]; checked_at: string; source: string }>("/api/eval/suites") });
  const experiments = useQuery({
    queryKey: ["eval", "experiments"],
    queryFn: () => get<{ items: Experiment[]; common_gates: Record<string, string>; measures: string[]; not_measured: string[]; dimensions: Dimension[]; note: string }>("/api/eval/experiments"),
  });
  const runs = useQuery({ queryKey: ["eval", "runs"], queryFn: () => get<{ items: Run[] }>("/api/eval/runs") });
  const roles = Array.from(new Set((suites.data?.items ?? []).map((s) => s.role_key).filter((r): r is string => Boolean(r))));
  const [role, setRole] = useState("");

  return (
    <div className="space-y-5">
      <PageHeader
        title="Evaluasi Model"
        question="Konfigurasi mana yang terbukti terbaik per peran?"
        source={suites.data?.source ?? "eval_suites / eval_runs"}
        checkedAt={suites.data?.checked_at ?? null}
        evidenceLimit="Peringkat hanya dari EvalRun tercatat; unit evaluasi = AIConfiguration lengkap; tidak ada juara yang dinyatakan tanpa hasil"
      />
      <Tabs
        tabs={[
          {
            id: "leaderboard",
            label: "Leaderboard per peran",
            content: (
              <div className="space-y-3">
                <Field label="Peran">
                  <select className={inputClass + " max-w-sm"} value={role} onChange={(e) => setRole(e.target.value)}>
                    <option value="">Semua peran</option>
                    {roles.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </Field>
                <LeaderboardView role={role} />
              </div>
            ),
          },
          {
            id: "suites",
            label: "Suite",
            content: (
              <Card title="Suite held-out terlindungi (definisi, tanpa hasil)">
                {suites.isLoading && <Loading />}
                <ErrorBox error={suites.error} />
                {suites.data && suites.data.items.length === 0 ? (
                  <EmptyState title="Suite belum dimuat">
                    Jalankan <Mono>aeos seed --only aeos.evaluation.seed</Mono>
                  </EmptyState>
                ) : (
                  <div className="space-y-3">
                    {(suites.data?.items ?? []).map((s) => (
                      <details key={s.suite_key} className="rounded-md border border-line p-3">
                        <summary className="flex cursor-pointer flex-wrap items-center gap-2">
                          <span className="font-medium">{s.title}</span>
                          <Mono>{s.suite_key}</Mono>
                          <span className="text-xs text-ink-3">peran {s.role_key} · {s.case_count} kasus ({s.case_kinds.join(", ")})</span>
                          <StatusBadge status={s.integrity === "OK" ? "CONFIGURED" : "DRIFTED"} label={s.integrity === "OK" ? "Terlindungi (hash cocok)" : "Hash berubah — perlu review"} />
                          <StatusBadge status="DESIGN" />
                        </summary>
                        <p className="mt-2 text-sm">{s.description}</p>
                        <KeyValue items={[["Isolasi", s.isolation], ["Acceptance suite", <ul key="a" className="list-disc pl-5">{s.acceptance.map((a) => <li key={a}>{a}</li>)}</ul>], ["Hash", <Mono key="h">{shortHash(s.content_hash, 16)}</Mono>], ["Hasil", s.results]]} />
                        <Table
                          rows={s.cases}
                          rowKey={(c) => c.case_id}
                          columns={[
                            { header: "Kasus", cell: (c) => <Mono>{c.case_id}</Mono> },
                            { header: "Jenis", cell: (c) => c.kind },
                            { header: "Judul", cell: (c) => c.title },
                            { header: "Diharapkan", cell: (c) => c.expected },
                            { header: "Acceptance", cell: (c) => c.acceptance.join("; ") },
                          ]}
                        />
                      </details>
                    ))}
                  </div>
                )}
              </Card>
            ),
          },
          {
            id: "experiments",
            label: "Eksperimen A–D",
            content: (
              <div className="space-y-3">
                <ErrorBox error={experiments.error} />
                {experiments.data && (
                  <>
                    <Notice>{experiments.data.note}</Notice>
                    <div className="grid gap-3 md:grid-cols-2">
                      {experiments.data.items.map((e) => (
                        <Card key={e.key} title={`${e.key} — ${e.name}`} actions={<StatusBadge status="DESIGN" label={e.status === "EXPERIMENT_ONLY" ? "Eksperimen saja" : "Definisi"} />}>
                          <p className="text-sm">{e.compares}</p>
                          <KeyValue items={[["Worker paralel maks", e.max_parallel_workers], ["Spesialis", e.specialists], ["Hash gate", <Mono key="g">{shortHash(e.gates_hash)}</Mono>], ["Promosi", e.promotion_rule]]} />
                          {e.note && <p className="mt-2 text-xs text-ink-3">{e.note}</p>}
                        </Card>
                      ))}
                    </div>
                    <Card title="Gate yang identik untuk keempat eksperimen">
                      <KeyValue items={Object.entries(experiments.data.common_gates).map(([k, v]) => [k, v] as [string, string])} />
                      <p className="mt-3 text-sm">Diukur: {experiments.data.measures.join(", ")}.</p>
                      <p className="text-sm text-ink-3">Tidak dinilai dari: {experiments.data.not_measured.join(", ")}.</p>
                    </Card>
                  </>
                )}
              </div>
            ),
          },
          {
            id: "dimensions",
            label: "Dimensi metrik",
            content: (
              <Card title="Dimensi evaluasi (§43)">
                <Table
                  rows={experiments.data?.dimensions ?? []}
                  rowKey={(d) => d.key}
                  columns={[
                    { header: "Dimensi", cell: (d) => <span className="font-medium">{d.label}</span> },
                    { header: "Metrik", cell: (d) => d.explain },
                    { header: "Field", cell: (d) => d.metrics.map((m) => <Mono key={m}>{m}</Mono>) },
                  ]}
                />
                <p className="mt-2 text-xs text-ink-3">
                  Gate keamanan/authority/bukti adalah syarat kelayakan, bukan komponen skor. Run gagal, timeout dan terputus kuota tetap dihitung; ukuran sampel dan variasi selalu ditampilkan.
                </p>
              </Card>
            ),
          },
          {
            id: "runs",
            label: "Run",
            content: (
              <div className="space-y-3">
                {isAdmin && suites.data && suites.data.items.length > 0 && (
                  <Card title="Rencanakan run evaluasi">
                    <PlanRunForm suites={suites.data.items} />
                  </Card>
                )}
                <Card title="Run tercatat">
                  <ErrorBox error={runs.error} />
                  <Table
                    rows={runs.data?.items ?? []}
                    rowKey={(r) => r.id}
                    empty="Belum ada run — semua kandidat BELUM DIUJI"
                    columns={[
                      { header: "Waktu", cell: (r) => absoluteTime(r.created_at) },
                      { header: "Suite", cell: (r) => <Mono>{r.suite_key}</Mono> },
                      { header: "Konfigurasi", cell: (r) => <span>{String(r.configuration.model_id)} @ {String(r.configuration.provider_route)}</span> },
                      { header: "Topologi", cell: (r) => r.topology ?? "—" },
                      { header: "Status", cell: (r) => <StatusBadge status={r.status === "PLANNED" ? "PENDING" : r.status === "COMPLETED" ? "CONFIGURED" : r.status} label={r.status} /> },
                      { header: "n / gagal", cell: (r) => `${r.sample_size} / ${r.failures.length}` },
                    ]}
                  />
                </Card>
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
