import type { ReactNode } from "react";
import { MermaidDiagram } from "../../components/planning/MermaidDiagram";
import type { MaterialQuestion, PlanContent, Statement } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, Mono, Notice, Table, Tabs } from "../../components/ui";

// Indonesian labels for common plan-content keys; unknown keys are humanised (technical IDs stay English).
const LABELS: Record<string, string> = {
  name: "Nama",
  summary: "Ringkasan",
  product_owner: "Product Owner",
  engineering_lead: "Engineering Lead",
  tech_lead: "Tech Lead",
  repository: "Repository",
  issue_tracker: "Pelacak kerja",
  criticality: "Kritikalitas",
  data_classification: "Klasifikasi data",
  stack: "Stack",
  quick_links: "Tautan cepat",
  current_development: "Pengembangan saat ini",
  active_features: "Fitur aktif",
  open_risks: "Risiko terbuka",
  sdlc_lifecycle: "Siklus SDLC",
  governance_links: "Tautan governance",
  overview: "Ikhtisar",
  vision: "Visi",
  objectives: "Tujuan",
  scope_in: "Dalam scope",
  scope_out: "Di luar scope",
  team_ownership: "Tim & kepemilikan",
  stakeholders: "Stakeholder",
  roadmap: "Roadmap",
  system_inventory: "Inventaris sistem",
  data_overview: "Ikhtisar data",
  problem: "Masalah",
  background: "Latar belakang",
  goals: "Tujuan",
  non_goals: "Bukan tujuan",
  business_requirements: "Kebutuhan bisnis",
  functional_requirements: "Kebutuhan fungsional",
  non_functional_requirements: "Kebutuhan non-fungsional",
  user_stories: "User stories",
  acceptance_criteria: "Acceptance criteria",
  user_impact: "Dampak pengguna",
  dependencies: "Dependensi",
  assumptions: "Asumsi",
  risks: "Risiko",
  success_metrics: "Metrik keberhasilan",
  rollout_considerations: "Pertimbangan rollout",
  related_work: "Pekerjaan terkait",
  test_strategy: "Test Strategy",
  scenarios: "Skenario",
  exit_criteria: "Kriteria selesai",
  release_notes: "Release Notes",
  rollback: "Rollback",
  migration: "Migrasi",
  deployment: "Deployment",
};

function label(key: string): string {
  return LABELS[key] ?? key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function isEmpty(v: unknown): boolean {
  return v === "" || v === null || v === undefined || (Array.isArray(v) && v.length === 0);
}

export function ValueView({ value }: { value: unknown }): ReactNode {
  if (isEmpty(value)) return <span className="text-ink-3">—</span>;
  if (typeof value === "string") return <span className="whitespace-pre-wrap">{value}</span>;
  if (typeof value === "number" || typeof value === "boolean") return <Mono>{String(value)}</Mono>;
  if (Array.isArray(value)) {
    if (value.every((v) => typeof v !== "object" || v === null)) {
      return (
        <ul className="list-disc pl-5">
          {value.map((v, i) => (
            <li key={i}>{String(v)}</li>
          ))}
        </ul>
      );
    }
    return (
      <div className="grid gap-2">
        {value.map((v, i) => (
          <div key={i} className="rounded border border-line p-2">
            <ValueView value={v} />
          </div>
        ))}
      </div>
    );
  }
  if (typeof value === "object") return <ObjectView value={value as Record<string, unknown>} />;
  return String(value);
}

export function ObjectView({ value, skip = [] }: { value: Record<string, unknown> | undefined; skip?: string[] }) {
  if (!value) return <EmptyState title="Bagian ini kosong pada versi ini" />;
  const entries = Object.entries(value).filter(([k]) => !skip.includes(k));
  return (
    <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[minmax(160px,max-content)_1fr]">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ink-3">{label(k)}</dt>
          <dd className="min-w-0 break-words">
            <ValueView value={v} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ListOf({ items, titleKey, empty }: { items: Record<string, unknown>[] | undefined; titleKey: string; empty: string }) {
  if (!items?.length) return <EmptyState title={empty} />;
  return (
    <div className="grid gap-3">
      {items.map((it, i) => (
        <details key={i} className="rounded-md border border-line p-3" open={items.length === 1}>
          <summary className="cursor-pointer text-sm font-medium">
            {String(it.key ?? it.feature_key ?? i + 1)} — {String(it[titleKey] ?? "")}
          </summary>
          <div className="mt-3">
            <ObjectView value={it} skip={["key", titleKey]} />
          </div>
        </details>
      ))}
    </div>
  );
}

function StatementList({ title, items, empty }: { title: string; items: Statement[] | undefined; empty: string }) {
  return (
    <div>
      <h3 className="mb-1 text-sm font-semibold">{title}</h3>
      {items?.length ? (
        <ul className="grid gap-1 text-sm">
          {items.map((s, i) => (
            <li key={i} className="rounded border border-line px-2 py-1">
              {s.statement}
              {(s.source || s.impact) && <div className="text-xs text-ink-3">{s.source ? `Sumber: ${s.source}` : `Dampak: ${s.impact}`}</div>}
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-sm text-ink-3">{empty}</div>
      )}
    </div>
  );
}

/** §05 step 7: the planner's own facts/assumptions/unknowns/material questions for this version (DATA). */
export function PlanAssumptions({ content }: { content: PlanContent }) {
  const questions: MaterialQuestion[] = content.material_questions ?? [];
  return (
    <div className="grid gap-4">
      <Notice tone="info">
        Bagian ini ditulis oleh perencana (model) untuk versi ini dan berstatus DATA/proposal — bukan fakta yang terverifikasi. Fakta dari control plane
        ada di tab <strong>Konteks &amp; pertanyaan</strong>.
      </Notice>
      <div>
        <h3 className="mb-1 text-sm font-semibold">Pertanyaan material</h3>
        {questions.length ? (
          <ul className="grid gap-1 text-sm">
            {questions.map((q, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2 rounded border border-line px-2 py-1">
                {q.blocking ? <StatusBadge status="BLOCKED" label="Memblokir" /> : <StatusBadge status="PENDING" label="Tidak memblokir" />}
                <Mono>{q.category}</Mono> {q.question}
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-ink-3">Perencana tidak mencatat pertanyaan material.</div>
        )}
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <StatementList title="Asumsi" items={content.assumptions} empty="Tidak ada asumsi tercatat" />
        <StatementList title="Unknown" items={content.unknowns} empty="Tidak ada unknown tercatat" />
        <StatementList title="Fakta (menurut perencana)" items={content.facts} empty="Tidak ada fakta tercatat" />
      </div>
    </div>
  );
}

export function ContentViewer({ content }: { content: PlanContent }) {
  const arch = content.architecture;
  const tabs = [
    { id: "home", label: "00 Product Home", content: <ObjectView value={content.product_home} /> },
    { id: "overview", label: "01 Overview", content: <ObjectView value={content.overview} /> },
    { id: "prd", label: "02 PRD", content: <ListOf items={content.requirements} titleKey="title" empty="Belum ada PRD per fitur" /> },
    {
      id: "arch",
      label: "03 Arsitektur",
      content: (
        <div className="grid gap-4">
          <ObjectView value={arch?.overview} />
          {(arch?.diagrams ?? []).length === 0 && <EmptyState title="Tidak ada diagram Mermaid pada versi ini" />}
          {(arch?.diagrams ?? []).map((d) => (
            <div key={d.key}>
              <MermaidDiagram source={d.mermaid} title={`${d.key} — ${d.title} (${d.kind})`} />
              <div className="mt-1 text-xs text-ink-3">Verifikasi terhadap: {d.verification_source || "—"}</div>
            </div>
          ))}
        </div>
      ),
    },
    { id: "sec", label: "04 Security & Privacy", content: <ObjectView value={content.security_privacy as Record<string, unknown>} /> },
    { id: "dev", label: "05 Development", content: <ObjectView value={content.development_baseline as Record<string, unknown>} /> },
    {
      id: "test",
      label: "06 Testing",
      content: (
        <div className="grid gap-3">
          <Notice tone="info">
            Perencanaan hanya menyusun Test Plan (termasuk Test Strategy dan AC). Eksekusi/hasil, defect, final assessment dan sign-off berstatus{" "}
            <Mono>NOT STARTED</Mono> sampai ada bukti verifier independen/UAT — tidak pernah diisi oleh model.
          </Notice>
          <ListOf items={(content.testing as { test_plans?: Record<string, unknown>[] } | undefined)?.test_plans} titleKey="title" empty="Belum ada Test Plan" />
        </div>
      ),
    },
    {
      id: "release",
      label: "07 Release",
      content: (
        <div className="grid gap-3">
          <Notice tone="info">
            Release Plan memuat Release Notes sebagai section. Status testing/security/UAT, sign-off, validasi pasca-rilis dan evidence berstatus{" "}
            <Mono>NOT STARTED</Mono> sampai release candidate dan bukti runtime tersedia.
          </Notice>
          <ListOf items={(content.release as { release_plans?: Record<string, unknown>[] } | undefined)?.release_plans} titleKey="title" empty="Belum ada Release Plan" />
        </div>
      ),
    },
    { id: "ops", label: "08 Operations", content: <ObjectView value={content.operations as Record<string, unknown>} /> },
    {
      id: "milestones",
      label: "Milestone",
      content: (
        <Table
          rows={[...(content.milestones ?? [])].sort((a, b) => a.order - b.order)}
          rowKey={(m) => m.key}
          empty="Belum ada milestone"
          columns={[
            { header: "Kunci", cell: (m) => <Mono>{m.key}</Mono> },
            { header: "Judul", cell: (m) => m.title },
            { header: "Bukti keluar", cell: (m) => m.exit_evidence || "—" },
          ]}
        />
      ),
    },
    {
      id: "features",
      label: "Fitur",
      content: (
        <Table
          rows={content.features ?? []}
          rowKey={(f) => f.key}
          empty="Belum ada fitur"
          columns={[
            { header: "Kunci", cell: (f) => <Mono>{f.key}</Mono> },
            { header: "Fitur", cell: (f) => <div><div className="font-medium">{f.title}</div><div className="text-xs text-ink-3">{f.requirement}</div></div> },
            { header: "Prioritas", cell: (f) => f.priority },
            { header: "Milestone", cell: (f) => <Mono>{f.milestone || "—"}</Mono> },
            { header: "Bergantung pada", cell: (f) => (f.depends_on.length ? f.depends_on.join(", ") : "—") },
            { header: "Acceptance", cell: (f) => <ValueView value={f.acceptance} /> },
          ]}
        />
      ),
    },
    {
      id: "tasks",
      label: "Task",
      content: (
        <Table
          rows={(content.tasks ?? []) as Record<string, unknown>[]}
          rowKey={(t) => String(t.key)}
          empty="Belum ada TaskSpec"
          columns={[
            { header: "Kunci", cell: (t) => <Mono>{String(t.key)}</Mono> },
            { header: "Task", cell: (t) => <div><div className="font-medium">{String(t.title)}</div><div className="text-xs text-ink-3">{String(t.goal ?? "")}</div></div> },
            { header: "Peran", cell: (t) => <Mono>{String(t.role_key ?? "")}</Mono> },
            { header: "Bergantung pada", cell: (t) => ((t.depends_on as string[]) ?? []).join(", ") || "—" },
            { header: "Path diizinkan", cell: (t) => <ValueView value={t.allowed_paths} /> },
            { header: "Risiko", cell: (t) => <Mono>{String(t.risk_class ?? "")}</Mono> },
          ]}
        />
      ),
    },
    { id: "assumptions", label: "Asumsi & pertanyaan", content: <PlanAssumptions content={content} /> },
  ];
  return <Tabs tabs={tabs} />;
}
