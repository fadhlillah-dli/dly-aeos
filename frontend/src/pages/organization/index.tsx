import { useQuery } from "@tanstack/react-query";
import { Bot, Cog, Search, UserRound } from "lucide-react";
import { useMemo, useState } from "react";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { ExplainButtons } from "../../components/ExplainButtons";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, EmptyState, ErrorBox, inputClass, Loading, Mono, Notice, PageHeader } from "../../components/ui";
import type { AgentDef, RoleCard, RoleGroup } from "../../components/workforce/types";
import { useLiveInvalidation } from "../../components/workforce/useLive";

const KIND_LABEL: Record<string, string> = {
  PLANNER: "Perencana",
  REVIEWER: "Reviewer",
  EXECUTOR: "Pelaksana",
  SERVICE: "Layanan deterministik",
  HUMAN: "Manusia (AUTH)",
};

function Availability({ role }: { role: RoleCard }) {
  if (role.active_runs > 0) return <StatusBadge status="IN_USE" label={`Menjalankan ${role.active_runs} run`} />;
  if (role.availability === "SERVICE") return <StatusBadge status="CONFIGURED" label="Layanan kode, bukan LLM" />;
  if (role.availability === "HUMAN") return <StatusBadge status="CONFIGURED" label="Peran manusia" />;
  if (role.availability === "CANDIDATE") return <StatusBadge status="CANDIDATE" label="Kandidat impor — tidak dijalankan sebelum review & evaluasi" />;
  return <StatusBadge status="IDLE" label="Tersedia, tidak aktif" />;
}

function Bullets({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div>
      <div className="text-xs font-semibold uppercase tracking-wide text-ink-3">{title}</div>
      <ul className="list-disc pl-5 text-sm">
        {items.map((x) => (
          <li key={x}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

function RoleCardView({ role }: { role: RoleCard }) {
  return (
    <article className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-3">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold">{role.name}</h3>
          <div className="text-xs text-ink-3">
            {KIND_LABEL[role.kind] ?? role.kind} · <Mono>{role.role_key}</Mono> · {role.source_section} · v{role.version}
          </div>
        </div>
        <Availability role={role} />
      </header>
      <p className="text-xs text-ink-2" data-testid={`assigned-${role.role_key}`}>
        Penugasan: {role.assigned_tasks ?? 0} task aktif memakai peran ini (proyeksi; bukan izin atau wewenang).
      </p>
      {role.engine_hint && <p className="text-xs text-ink-2">Engine/model (panduan desain, bukan route aktif): {role.engine_hint}</p>}
      <Bullets title="Masukan" items={role.inputs ?? []} />
      <Bullets title="Keluaran" items={role.outputs} />
      <Bullets title="Batas" items={role.boundaries} />
      <Bullets title="Verifikasi" items={role.verification} />
      <Bullets title="Mode kegagalan yang diperiksa" items={role.failure_modes ?? []} />
      {(role.example_outputs ?? []).length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-xs text-ink-3">Contoh keluaran ({(role.example_outputs ?? []).length})</summary>
          <ul className="list-disc pl-5">
            {(role.example_outputs ?? []).map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="text-xs text-ink-3">
        Kasus evaluasi:{" "}
        {(role.evaluation_cases ?? []).length
          ? (role.evaluation_cases ?? []).map((c) => `${c.suite_key} (${c.case_ids.length} kasus)`).join(", ")
          : "belum ada suite evaluasi terproteksi untuk peran ini"}
      </div>
      <div className="flex flex-wrap gap-1.5">
        <StatusBadge status={role.mode} />
        <StatusBadge status={role.eval_status === "NOT_EVALUATED" ? "NOT_TESTED" : role.eval_status} label={role.eval_status === "NOT_EVALUATED" ? "Belum dievaluasi" : role.eval_status} />
        {role.demo_runs > 0 && <StatusBadge status="DEMO" label={`${role.demo_runs} run DEMO (tidak dihitung)`} />}
      </div>
      {role.forbidden_actions.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-xs text-ink-3">Tindakan terlarang ({role.forbidden_actions.length})</summary>
          <ul className="list-disc pl-5">
            {role.forbidden_actions.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        </details>
      )}
    </article>
  );
}

function AgentCard({ a }: { a: AgentDef }) {
  return (
    <article className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-3">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <Bot className="h-4 w-4 text-ink-3" aria-hidden />
          <div>
            <h3 className="font-semibold">
              <Mono>{a.agent_key}</Mono>
            </h3>
            <div className="text-xs text-ink-3">
              harness {a.harness} · peran {a.role_key} · rev {a.revision}
            </div>
            <div className="text-xs text-ink-2">Pemilik (manusia yang bertanggung jawab): {a.owner || "belum ditetapkan"}</div>
          </div>
        </div>
        {a.active_runs > 0 ? (
          <StatusBadge status="IN_USE" label={`Menjalankan ${a.active_runs} run`} />
        ) : a.enabled ? (
          <StatusBadge status="ENABLED" label="Diaktifkan, sedang idle" />
        ) : (
          <StatusBadge status="DISABLED" />
        )}
      </header>
      <p className="text-sm text-ink-2">{a.description}</p>
      {!a.enabled && a.enabled_reason && <p className="text-xs text-warn">{a.enabled_reason}</p>}
      <div className="text-xs text-ink-3">
        Tools: {a.tools.join(", ") || "—"} · {a.deterministic ? "deterministik (tanpa LLM)" : "LLM via route yang diizinkan"}
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-ink-3">Batas & scope</summary>
        <pre className="mono mt-1 overflow-x-auto rounded bg-surface-2 p-2">{JSON.stringify({ limits: a.limits, scope: a.allowed_scope, route: a.route_policy }, null, 2)}</pre>
      </details>
    </article>
  );
}

export default function OrganizationPage() {
  const { domain } = useAuth();
  const roles = useQuery({
    queryKey: ["workforce-roles", domain],
    queryFn: () => get<{ items: RoleGroup[]; note: string; checked_at: string; mode: string }>("/api/workforce/roles"),
    refetchInterval: 30_000,
  });
  const agents = useQuery({
    queryKey: ["workforce-agents", domain],
    queryFn: () => get<{ items: AgentDef[]; checked_at: string }>("/api/workforce/agents"),
    refetchInterval: 30_000,
  });
  useLiveInvalidation([["workforce-roles", domain], ["workforce-agents", domain]], (e) => e.kind === "run.state_changed");
  const [query, setQuery] = useState("");
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (roles.data?.items ?? []).map((g) => ({
      ...g,
      roles: g.roles.filter((r) => !r.deterministic_service && (!q || `${r.name} ${r.role_key} ${r.outputs.join(" ")}`.toLowerCase().includes(q))),
    }));
  }, [roles.data, query]);
  const services = (roles.data?.items ?? []).flatMap((g) => g.roles.filter((r) => r.deterministic_service));
  const total = (roles.data?.items ?? []).reduce((n, g) => n + g.roles.length, 0);
  const active = (roles.data?.items ?? []).reduce((n, g) => n + g.roles.reduce((m, r) => m + r.active_runs, 0), 0);
  const assigned = (roles.data?.items ?? []).reduce((n, g) => n + g.roles.reduce((m, r) => m + (r.assigned_tasks ?? 0), 0), 0);
  // "Lihat bukti" is derived from the records (never a fixed claim): evaluation status counts of the roles
  const evalCounts = (roles.data?.items ?? []).flatMap((g) => g.roles).reduce<Record<string, number>>((acc, r) => {
    acc[r.eval_status] = (acc[r.eval_status] ?? 0) + 1;
    return acc;
  }, {});
  const evalText = Object.entries(evalCounts).map(([k, v]) => `${k}: ${v}`).join(", ") || "belum ada data peran";

  return (
    <div className="space-y-5">
      <PageHeader
        title="Organisasi & Peran"
        question="Fungsi engineering apa yang tersedia dan siapa sedang menjalankannya?"
        source="Katalog RoleSkillPack + AgentDefinition (seed DESIGN) dan run aktif (WorkerRun)"
        checkedAt={roles.data?.checked_at ?? null}
        evidenceLimit="Katalog desain, belum dievaluasi. Jumlah run aktif dihitung dari run nyata, bukan dari org chart."
        mode="DESIGN"
      />
      <Notice>
        Peran adalah definisi kompetensi dan kontrak, <strong>bukan karyawan AI yang selalu hidup</strong>. Slot tanpa run ditampilkan
        “Tersedia, tidak aktif”. {roles.data?.note ?? "Garis organisasi tidak memberi permission atau authority."} Pemilik manusia tetap
        satu-satunya AUTH.
      </Notice>
      <ExplainButtons
        content={{
          what: "Proyeksi katalog peran (§16), kelompok organisasi (§17) dan konfigurasi agent. Tidak menjalankan LLM apa pun.",
          why: "Pola terpilih: controller deterministik + spesialis on-demand + review frontier (§18). Satu implementer cukup untuk tugas kecil.",
          usedBy: `Run aktif saat ini: ${active}; task aktif yang ditugaskan ke peran: ${assigned}. Scheduler memilih agent per TaskSpec; peran lain tidak otomatis dipanggil.`,
          evidence: `Setiap kartu memuat versi + revision hash. Status evaluasi peran (dari katalog): ${evalText}.`,
          impact: "Mengubah peran/agent membuat revisi baru; riwayat run lama tidak ditulis ulang.",
          propose: "Gunakan Usulan Upgrade (ChangeProposal) untuk menambah/mengganti peran.",
        }}
      />

      <Card title="Definisi agent">
        {agents.isLoading ? (
          <Loading />
        ) : agents.error ? (
          <ErrorBox error={agents.error} />
        ) : (agents.data?.items ?? []).length === 0 ? (
          <EmptyState title="Belum ada AgentDefinition">
            Muat katalog: <Mono>aeos seed --only aeos.workforce.seed</Mono>
          </EmptyState>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {agents.data!.items.map((a) => (
              <AgentCard key={a.agent_key} a={a} />
            ))}
          </div>
        )}
      </Card>

      {roles.isLoading ? (
        <Loading />
      ) : roles.error ? (
        <ErrorBox error={roles.error} />
      ) : total === 0 ? (
        <EmptyState title="Katalog peran belum dimuat">
          Jalankan <Mono>aeos seed --only aeos.workforce.seed</Mono> (tidak membuat run atau biaya).
        </EmptyState>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <label className="relative w-full max-w-sm">
              <Search className="absolute left-2 top-2 h-4 w-4 text-ink-3" aria-hidden />
              <input className={`${inputClass} pl-8`} placeholder="Cari peran atau keluaran…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Cari peran" />
            </label>
            <span className="text-xs text-ink-3">
              {total} definisi · {active} run aktif
            </span>
          </div>
          {groups.map((g) => (
            <Card key={g.key} title={<span className="inline-flex items-center gap-2"><UserRound className="h-4 w-4" aria-hidden />{g.label} ({g.roles.length})</span>}>
              {g.roles.length === 0 ? (
                <p className="text-sm text-ink-3">Tidak ada peran yang cocok.</p>
              ) : (
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {g.roles.map((r) => (
                    <RoleCardView key={r.role_key} role={r} />
                  ))}
                </div>
              )}
            </Card>
          ))}
          <Card title={<span className="inline-flex items-center gap-2"><Cog className="h-4 w-4" aria-hidden />Layanan deterministik ({services.length})</span>}>
            <p className="mb-3 text-sm text-ink-2">
              Scheduler, Policy Enforcer, Credential Broker, Publication Worker, Evidence Collector, Budget Counter dan Stop/Kill Controller
              adalah kode. Model dapat menjelaskan keluarannya, tetapi tidak mendefinisikan state, angka, atau otorisasinya.
            </p>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {services.map((r) => (
                <RoleCardView key={r.role_key} role={r} />
              ))}
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
