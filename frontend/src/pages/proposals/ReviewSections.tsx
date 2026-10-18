import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { IDENTITY_STATUS, IMPACT_STATUS, MappedBadge, RESEARCH_LIFECYCLE } from "../../components/proposals/MappedBadge";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, KeyValue, Mono, Notice, Table } from "../../components/ui";
import { absoluteTime, shortHash, usd } from "../../lib/format";
import type { CostItem, Dimension, ReviewPackage } from "./types";

const IMPACT_LABELS: Record<string, string> = {
  contracts: "Kontrak / consumer",
  state_data_migration: "State, data & migration",
  own_auth_cap_enf: "Policy / OWN-AUTH-CAP-ENF",
  secrets: "Secrets (referensi saja)",
  egress: "Egress",
  ci: "CI",
  docs: "Dokumentasi",
  operations: "Operasi",
  rollback: "Rollback",
};

const COST_LABELS: Record<string, string> = {
  cash: "Biaya tunai",
  compute: "Compute",
  ci: "CI",
  storage: "Storage",
  tokens: "Token / konteks",
  oversight: "Oversight (review Claude)",
  operations: "Usaha operasi",
  migration: "Migrasi",
};

function Section({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <Card
      title={
        <span>
          <span className="mr-2 inline-flex h-5 w-5 items-center justify-center rounded-full bg-surface-3 text-xs">{n}</span>
          {title}
        </span>
      }
    >
      {children}
    </Card>
  );
}

function isCost(v: unknown): v is CostItem {
  return Boolean(v && typeof v === "object" && "basis" in (v as Record<string, unknown>));
}

function isDimension(v: unknown): v is Dimension {
  return Boolean(v && typeof v === "object" && "status" in (v as Record<string, unknown>));
}

function Unknown({ v }: { v: string | null | undefined }) {
  if (!v || v === "UNKNOWN") return <StatusBadge status="UNKNOWN" />;
  return <span>{v}</span>;
}

export function SummarySection({ pkg }: { pkg: ReviewPackage }) {
  const s = pkg.summary;
  return (
    <Section n={1} title="Ringkasan fungsi, masalah dan relevansi">
      <p className="mb-2 text-sm text-ink-2">{s.decision_question}</p>
      <KeyValue
        items={[
          ["Masalah / tujuan", <Unknown key="p" v={s.problem} />],
          ["Manfaat diharapkan", <Unknown key="b" v={s.expected_benefit} />],
          [
            "Relevansi (registry)",
            s.relevance === "UNKNOWN" ? (
              <StatusBadge status="UNKNOWN" label="Belum ada entri riset" />
            ) : (
              <ul className="list-disc pl-5">
                {s.relevance.map((r) => (
                  <li key={r.name}>
                    <b>{r.name}</b> — {r.placement} <MappedBadge value={r.lifecycle} map={RESEARCH_LIFECYCLE} />
                  </li>
                ))}
              </ul>
            ),
          ],
          ["Lampiran", s.attachments.length ? s.attachments.map((a) => <div key={a.attachment_id}><Mono>{a.attachment_id.slice(0, 8)}</Mono> — {a.note}</div>) : "—"],
        ]}
      />
      <ul className="mt-3 list-disc space-y-1 pl-5 text-sm">
        {s.plain_language.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
    </Section>
  );
}

export function SourceSection({ pkg }: { pkg: ReviewPackage }) {
  return (
    <Section n={2} title="Identitas sumber (terverifikasi / belum) dengan tanggal">
      <Table
        rows={pkg.source_identity}
        rowKey={(r) => r.identity}
        empty="Tidak ada sumber eksternal (hanya komponen lama)"
        columns={[
          {
            header: "Identitas kanonik",
            cell: (r) => (
              <div>
                <Mono>{r.identity}</Mono>
                {r.research_entry_id && (
                  <div className="text-xs">
                    <Link to={`/research?entry=${r.research_entry_id}`}>entri riset</Link>
                  </div>
                )}
              </div>
            ),
          },
          { header: "Status", cell: (r) => <MappedBadge value={r.status} map={IDENTITY_STATUS} /> },
          { header: "Rilis / commit", cell: (r) => <Unknown v={r.release_or_commit} /> },
          { header: "Maintainer", cell: (r) => <Unknown v={r.maintainer} /> },
          { header: "Lisensi", cell: (r) => <Unknown v={r.license} /> },
          { header: "Distribusi / terms", cell: (r) => <span><Unknown v={r.distribution} /> <Unknown v={r.terms} /></span> },
          { header: "Diperiksa", cell: (r) => (r.checked_at ? absoluteTime(r.checked_at) : `belum live (riset ${r.research_date ?? "?"})`) },
          { header: "Catatan", cell: (r) => <span className="text-xs">{[...r.flags, r.note].filter(Boolean).join(" · ")}</span> },
        ]}
      />
    </Section>
  );
}

export function BaselineSection({ pkg }: { pkg: ReviewPackage }) {
  const b = pkg.baseline;
  return (
    <Section n={3} title="Kondisi baseline saat ini">
      <div className="mb-2 text-xs text-ink-3">
        baseline_hash <Mono>{shortHash(b.baseline_hash, 16)}</Mono> {b.note && `— ${b.note}`}
      </div>
      <Table
        rows={b.components}
        rowKey={(c) => c.component_id}
        empty="Tidak ada komponen lama (ADD/COMPARE)"
        columns={[
          {
            header: "Komponen",
            cell: (c) => (
              <div>
                <Link to={`/tools/${c.component_id}`} className="font-medium">
                  {c.name ?? c.component_id}
                </Link>
                <div className="text-xs text-ink-3">{c.function}</div>
              </div>
            ),
          },
          { header: "Status inventaris", cell: (c) => (c.status === "KNOWN" ? <span>{c.admission_status} · {c.enablement}</span> : <StatusBadge status="UNKNOWN" label={c.reason ?? "UNKNOWN"} />) },
          { header: "Pin / config", cell: (c) => <span><Unknown v={c.approved_pin} /> <span className="text-xs text-ink-3">{c.manifest_hash && c.manifest_hash !== "UNKNOWN" ? shortHash(c.manifest_hash) : ""}</span></span> },
          { header: "Consumer", cell: (c) => (c.consumers?.length ? c.consumers.map((r) => `${r.source_id} ${r.relation}`).join(", ") : "—") },
          {
            header: "Bukti",
            cell: (c) => (
              <span className="text-xs">
                {c.observations && Object.keys(c.observations).length
                  ? Object.entries(c.observations).map(([d, o]) => `${d}: ${o.status}${o.observed_version ? ` (${o.observed_version})` : ""}`).join("; ")
                  : c.evidence_note ?? "—"}
              </span>
            ),
          },
        ]}
      />
    </Section>
  );
}

export function AlternativesSection({ pkg }: { pkg: ReviewPackage }) {
  return (
    <Section n={4} title="Alternatif">
      <Table
        rows={pkg.alternatives}
        rowKey={(a) => a.option}
        columns={[
          { header: "Opsi", cell: (a) => <Mono>{a.option}</Mono> },
          { header: "Berlaku", cell: (a) => (a.applicable ? <StatusBadge status="CONFIGURED" label="Ya" /> : <StatusBadge status="NOT_APPLICABLE" label="Tidak" />) },
          { header: "Ringkasan", cell: (a) => a.summary },
          { header: "Trade-off", cell: (a) => a.tradeoffs },
          { header: "Unknowns", cell: (a) => (a.unknowns.length ? a.unknowns.join("; ") : "—") },
        ]}
      />
    </Section>
  );
}

export function ImpactSection({ pkg }: { pkg: ReviewPackage }) {
  const entries = Object.entries(pkg.impact).filter(([, v]) => isDimension(v)) as [string, Dimension][];
  const unmapped = (pkg.impact.unmapped_components as string[] | undefined) ?? [];
  return (
    <Section n={5} title="Dampak up/downstream">
      {unmapped.length > 0 && <Notice tone="warn">Komponen belum terpetakan di inventaris: {unmapped.join(", ")} — dampaknya UNKNOWN.</Notice>}
      <div className="mt-2 grid gap-3 md:grid-cols-2">
        {entries.map(([k, d]) => (
          <div key={k} className="rounded-md border border-line p-3">
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="font-medium">{IMPACT_LABELS[k] ?? k}</span>
              <MappedBadge value={d.status} map={IMPACT_STATUS} />
            </div>
            {d.items.length ? (
              <ul className="list-disc pl-5 text-sm">
                {d.items.map((i) => (
                  <li key={i}>{i}</li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-ink-3">{d.reason}</p>
            )}
          </div>
        ))}
      </div>
    </Section>
  );
}

function renderValue(v: unknown): ReactNode {
  if (v === null || v === undefined || v === "UNKNOWN") return <StatusBadge status="UNKNOWN" />;
  if (Array.isArray(v))
    return (
      <ul className="list-disc pl-5">
        {v.map((x, i) => (
          <li key={i}>{typeof x === "object" ? Object.entries(x as Record<string, unknown>).map(([k, y]) => `${k}: ${String(y)}`).join(", ") : String(x)}</li>
        ))}
      </ul>
    );
  if (typeof v === "object")
    return (
      <KeyValue items={Object.entries(v as Record<string, unknown>).map(([k, y]) => [k, y === "UNKNOWN" || y === null ? <StatusBadge status="UNKNOWN" /> : String(y)] as [ReactNode, ReactNode])} />
    );
  return String(v);
}

export function ExperimentSection({ pkg }: { pkg: ReviewPackage }) {
  const plan = pkg.experiment_plan;
  const labels: Record<string, string> = {
    type: "Jenis",
    install: "Instalasi",
    pins: "Pin / hash",
    suites: "Suite evaluasi",
    comparison: "Perbandingan",
    isolation: "Isolasi",
    limits: "Batas",
    acceptance: "Acceptance (ditetapkan sebelum eksekusi)",
    stop_conditions: "Stop conditions",
    collector_verifier: "Collector / verifier",
    canary: "Canary",
    note: "Catatan",
  };
  const rp = pkg.replacement_plan;
  const rpLabels: Record<string, string> = {
    previous_accepted_configuration: "Konfigurasi diterima sebelumnya",
    version_schema_data_compatibility: "Kompatibilitas versi/schema/data",
    backup_and_restore_evidence: "Backup & bukti restore",
    adapter_consumer_changes: "Perubahan adapter/consumer",
    credential_egress_changes: "Perubahan credential/egress",
    cutover_order: "Urutan cutover",
    coexistence: "Masa coexistence",
    success_stop_criteria: "Kriteria sukses/stop",
    operations_owner: "Pemilik operasi",
    rollback_or_forward_recovery: "Rollback / forward recovery",
    notes: "Catatan",
  };
  return (
    <Section n={6} title="Rencana eksperimen">
      <KeyValue items={Object.entries(plan).map(([k, v]) => [labels[k] ?? k, renderValue(v)] as [ReactNode, ReactNode])} />
      {rp && (
        <div className="mt-4 border-t border-line pt-3">
          <div className="mb-2 text-sm font-medium">Rencana penggantian dan rollback (§40)</div>
          <KeyValue items={Object.entries(rp).map(([k, v]) => [rpLabels[k] ?? k, renderValue(v)] as [ReactNode, ReactNode])} />
        </div>
      )}
    </Section>
  );
}

export function ContributorsNote({ pkg }: { pkg: ReviewPackage }) {
  if (!pkg.review_contributors) return null;
  return (
    <Card title="Pembagian pekerjaan review">
      <Table
        rows={pkg.review_contributors}
        rowKey={(r) => r.role}
        columns={[
          { header: "Peran", cell: (r) => r.role },
          { header: "Dikerjakan oleh", cell: (r) => r.performed_by },
        ]}
      />
      <p className="mt-2 text-xs text-ink-3">{pkg.generated_by} · {absoluteTime(pkg.generated_at)}</p>
    </Card>
  );
}

export function CostSection({ pkg }: { pkg: ReviewPackage }) {
  const rows = Object.entries(pkg.cost).filter(([, v]) => isCost(v)) as [string, CostItem][];
  return (
    <Section n={7} title="Biaya (aktual / estimasi / UNKNOWN)">
      <Table
        rows={rows}
        rowKey={([k]) => k}
        columns={[
          { header: "Komponen biaya", cell: ([k]) => COST_LABELS[k] ?? k },
          { header: "Nilai", cell: ([, v]) => (v.value === null ? (v.cap_usd !== undefined ? `batas ${usd(v.cap_usd)}` : "—") : usd(v.value)) },
          {
            header: "Dasar",
            cell: ([, v]) => <StatusBadge status={v.basis === "ACTUAL" ? "LIVE" : v.basis} label={v.basis === "NOT_APPLICABLE" ? "Tidak berlaku" : v.basis} />,
          },
          { header: "Catatan", cell: ([, v]) => v.note ?? "" },
        ]}
      />
      <p className="mt-2 text-xs text-ink-3">{String(pkg.cost.rule ?? "")}</p>
    </Section>
  );
}

export function ClaudeSection({ pkg }: { pkg: ReviewPackage }) {
  const c = pkg.claude_review;
  return (
    <Section n={8} title="Rekomendasi Claude — REVIEW / VERIFY / VERDICT">
      <Notice tone="warn">{c.label}</Notice>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge
          status={
            c.status === "COMPLETED" ? "CONFIGURED" : c.status === "NOT_REQUESTED" ? "NOT_CHECKED" : c.status === "QUOTA_WAIT" ? "DEGRADED" : c.status === "STALE" ? "STALE" : "UNKNOWN"
          }
          label={
            {
              COMPLETED: c.reused ? "Selesai (dipakai ulang, hash sama)" : "Selesai (data review)",
              NOT_REQUESTED: "Belum diminta",
              NOT_RUN: "Tidak dijalankan",
              FAILED: "Gagal",
              QUOTA_WAIT: "Menunggu kuota",
              STALE: "Usang — metadata berubah",
            }[c.status] ?? c.status
          }
        />
        {c.verdict && <StatusBadge status={c.verdict} label={`Verdict Claude (data): ${c.verdict}`} />}
        {c.status === "STALE" && c.previous_verdict && <span className="text-xs text-ink-3">verdict lama {c.previous_verdict} tidak berlaku</span>}
        {c.recommendation && <span>Rekomendasi: <Mono>{c.recommendation}</Mono></span>}
        {c.model_resolved !== undefined && <span className="text-xs text-ink-3">model: {c.model_resolved ?? `${c.model_requested} (resolved UNKNOWN)`}</span>}
      </div>
      {c.reason && <p className="mt-1 text-sm text-ink-2">{c.reason}</p>}
      {c.reused && c.reuse_note && <p className="mt-1 text-xs text-ink-3">{c.reuse_note}</p>}
      {c.review && (
        <div className="mt-3 space-y-2 text-sm">
          <div>
            <b>REVIEW:</b> {c.review.summary}
          </div>
          {c.review.risks && c.review.risks.length > 0 && <div>Risiko: {c.review.risks.join("; ")}</div>}
          {c.verify && (
            <div>
              <b>VERIFY:</b>
              <ul className="list-disc pl-5">
                {c.verify.claims.map((cl, i) => (
                  <li key={i}>
                    {cl.claim} — <StatusBadge status={cl.status === "SUPPORTED" ? "CONFIGURED" : "UNKNOWN"} label={cl.status} />
                  </li>
                ))}
              </ul>
            </div>
          )}
          {c.rationale && <div>Alasan: {c.rationale}</div>}
        </div>
      )}
      {c.sent_metadata && <p className="mt-2 text-xs text-ink-3">Metadata publik yang dikirim: {c.sent_metadata.join(", ")}</p>}
    </Section>
  );
}

export function OutcomeSummary({ pkg }: { pkg: ReviewPackage }) {
  const o = pkg.outcome;
  return (
    <KeyValue
      items={[
        ["Status", <StatusBadge key="s" status={o.status === "PENDING" ? "PENDING" : o.status} />],
        ["Diterima?", o.accepted ? "Ya" : "Tidak / belum"],
        ["Bukti", o.evidence_ids?.length ? o.evidence_ids.map((e) => <Mono key={e}>{e.slice(0, 8)}</Mono>) : "—"],
        ["Konfigurasi diterima", o.accepted_configuration ? <Mono>{JSON.stringify(o.accepted_configuration)}</Mono> : "—"],
        ["Kegagalan parsial", o.partial_failures?.length ? o.partial_failures.map(String).join("; ") : "—"],
        ["Rollback", o.rollback ? <Mono>{JSON.stringify(o.rollback)}</Mono> : "—"],
        ["Inventaris/docs", o.inventory_docs_update ?? "PENDING"],
        ["Catatan", o.notes || o.note || "—"],
      ]}
    />
  );
}
