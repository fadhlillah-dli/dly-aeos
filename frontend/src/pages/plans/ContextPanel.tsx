import { ShieldAlert } from "lucide-react";
import type { ChallengeFindings, ContextView, FinalReview, Finding } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, Mono, Table } from "../../components/ui";
import { shortHash } from "../../lib/format";

function Statements({ title, items, empty }: { title: string; items: { statement: string; source?: string; impact?: string }[]; empty: string }) {
  return (
    <div>
      <h3 className="mb-1 text-sm font-semibold">{title}</h3>
      {items.length ? (
        <ul className="grid gap-1 text-sm">
          {items.map((s, i) => (
            <li key={i} className="rounded border border-line px-2 py-1">
              {s.statement}
              {(s.source || s.impact) && <span className="ml-1 text-xs text-ink-3">({s.source ?? s.impact})</span>}
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-sm text-ink-3">{empty}</div>
      )}
    </div>
  );
}

/** §05 step 7: facts, assumptions, limits, material questions and attachments with provenance. */
export function ContextPanel({ context }: { context: ContextView | null }) {
  if (!context) return <EmptyState title="Konteks perencanaan tidak tersedia" />;
  const untrusted = context.constraints.filter((c) => c.kind === "UNTRUSTED_INPUT");
  const fromPrompt = untrusted.filter((c) => c.source === "prompt");
  const fromAttachments = untrusted.filter((c) => c.source !== "prompt");
  return (
    <div className="grid gap-4">
      <div>
        <h3 className="mb-1 text-sm font-semibold">Permintaan manusia (tujuan)</h3>
        <p className="whitespace-pre-wrap rounded bg-surface-2 p-2 text-sm">{context.prompt}</p>
        <div className="mt-1 text-xs text-ink-3">
          Hash konteks <Mono>{shortHash(context.context_hash, 16)}</Mono> · policy snapshot <Mono>{shortHash(context.policy_ref, 16)}</Mono>
        </div>
      </div>
      {fromPrompt.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn-bg p-3 text-sm text-warn" role="status">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div>
            Teks permintaan berisi kalimat mirip instruksi ({fromPrompt.flatMap((u) => u.flags ?? []).join(", ")}). Dicatat sebagai input tak tepercaya: domain
            tetap pilihan manusia dan teks tidak dapat mengubah kebijakan, izin, persetujuan, atau routing.
          </div>
        </div>
      )}
      {fromAttachments.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn-bg p-3 text-sm text-warn" role="status">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div>
            Lampiran berisi teks mirip instruksi ({fromAttachments.flatMap((u) => u.flags ?? []).join(", ")}). Isinya DATA tak tepercaya dan tidak mengubah
            domain, tujuan, izin, memori atau routing.
          </div>
        </div>
      )}
      <div>
        <h3 className="mb-1 text-sm font-semibold">Pertanyaan material</h3>
        {context.material_questions.length ? (
          <ul className="grid gap-1 text-sm">
            {context.material_questions.map((q, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2 rounded border border-line px-2 py-1">
                {q.blocking ? <StatusBadge status="BLOCKED" label="Memblokir" /> : <StatusBadge status="PENDING" label="Tidak memblokir" />}
                <Mono>{q.category}</Mono> {q.question}
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-ink-3">Tidak ada ambiguitas material yang terdeteksi.</div>
        )}
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <Statements title="Fakta" items={context.facts} empty="—" />
        <Statements title="Asumsi" items={context.assumptions} empty="Tidak ada asumsi tercatat" />
        <Statements title="Unknown" items={context.unknowns} empty="Tidak ada unknown tercatat" />
      </div>
      <div>
        <h3 className="mb-1 text-sm font-semibold">Lampiran (DATA tak tepercaya)</h3>
        <Table
          rows={context.attachments}
          rowKey={(a) => a.id}
          empty="Tidak ada lampiran"
          columns={[
            { header: "Berkas", cell: (a) => a.filename },
            { header: "Status", cell: (a) => <StatusBadge status={a.status === "READ" ? "CONFIGURED" : a.status === "UNKNOWN" ? "UNKNOWN" : "BLOCKED"} label={a.status} /> },
            { header: "Klasifikasi", cell: (a) => a.classification },
            { header: "sha256", cell: (a) => <Mono>{shortHash(a.sha256)}</Mono> },
            { header: "Catatan", cell: (a) => a.reason || (a.instruction_flags.length ? `flag: ${a.instruction_flags.join(", ")}` : "—") },
          ]}
        />
      </div>
    </div>
  );
}

function FindingTable({ title, items }: { title: string; items: Finding[] }) {
  if (!items.length) return null;
  return (
    <div>
      <h4 className="mb-1 text-sm font-medium">{title}</h4>
      <Table
        rows={items}
        rowKey={(f) => f.key}
        columns={[
          { header: "Kunci", cell: (f) => <Mono>{f.key}</Mono> },
          { header: "Keparahan", cell: (f) => <StatusBadge status={["HIGH", "CRITICAL"].includes(f.severity) ? "FAILED" : f.severity === "MEDIUM" ? "DEGRADED" : "PENDING"} label={f.severity} /> },
          { header: "Bagian", cell: (f) => f.section ?? "—" },
          { header: "Temuan", cell: (f) => f.description },
          { header: "Rekomendasi", cell: (f) => f.recommendation },
        ]}
      />
    </div>
  );
}

export function ReviewPanel({
  findings,
  resolutions,
  verdict,
  verdictEffective,
}: {
  findings: ChallengeFindings | null;
  resolutions: { finding_key: string; action: string; note: string }[];
  verdict: FinalReview | null;
  verdictEffective: string | null;
}) {
  return (
    <div className="grid gap-4">
      {!findings && !verdict && <EmptyState title="Belum ada hasil review">Jalankan perencanaan untuk menghasilkan temuan penantang dan review akhir.</EmptyState>}
      {findings && (
        <div className="grid gap-3">
          <p className="text-sm">{findings.summary}</p>
          <FindingTable title="Kontradiksi" items={findings.contradictions} />
          <FindingTable title="Dependensi hilang" items={findings.missing_dependencies} />
          <FindingTable title="Risiko" items={findings.risks} />
          <FindingTable title="Masalah scope" items={findings.scope_issues} />
          <FindingTable title="Lainnya" items={findings.other_findings} />
        </div>
      )}
      {resolutions.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-medium">Penyelesaian oleh Synthesizer</h4>
          <ul className="grid gap-1 text-sm">
            {resolutions.map((r) => (
              <li key={r.finding_key}>
                <Mono>{r.finding_key}</Mono> {r.action} — {r.note}
              </li>
            ))}
          </ul>
        </div>
      )}
      {verdict && (
        <div className="grid gap-2 rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">VERDICT</span>
            <StatusBadge status={verdictEffective ?? verdict.verdict} />
            {verdictEffective && verdictEffective !== verdict.verdict && (
              <span className="text-xs text-warn">diturunkan dari {verdict.verdict} karena ada temuan memblokir</span>
            )}
            <span className="text-xs text-ink-3">Review rencana tidak menjalankan tes; maksimal PASS-STATIC. Bukan persetujuan.</span>
          </div>
          <p className="text-sm">{verdict.verdict_rationale}</p>
          <details>
            <summary className="cursor-pointer text-sm">REVIEW</summary>
            <dl className="mt-2 grid gap-1 text-sm">
              {Object.entries(verdict.review).map(([k, v]) => (
                <div key={k}>
                  <dt className="text-ink-3">{k}</dt>
                  <dd>{Array.isArray(v) ? v.join("; ") || "—" : v || "—"}</dd>
                </div>
              ))}
            </dl>
          </details>
          <details>
            <summary className="cursor-pointer text-sm">VERIFY</summary>
            <ul className="mt-2 grid gap-1 text-sm">
              {verdict.verify.claims.map((c, i) => (
                <li key={i}>
                  <StatusBadge status={c.status === "SUPPORTED" ? "CONFIGURED" : c.status === "UNKNOWN" ? "UNKNOWN" : "NOT-PROVEN"} label={c.status} /> {c.claim} — {c.evidence}
                </li>
              ))}
              {verdict.verify.unknowns.map((u, i) => (
                <li key={`u${i}`}>
                  <StatusBadge status="UNKNOWN" /> {u}
                </li>
              ))}
            </ul>
          </details>
          <FindingTable title="Temuan review akhir" items={verdict.findings} />
        </div>
      )}
    </div>
  );
}
